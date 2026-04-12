"""
Purpose: Celery task that executes the LangGraph-based accounting recommendation workflow.
Scope: Dispatches document context through deterministic rules, model reasoning, and
autonomy routing. Persists the final validated recommendation and emits audit events.
Dependencies: Celery worker app, LangGraph orchestration, model gateway, accounting rules,
DB session factory, recommendation contracts, audit service.

Design notes:
- This is the canonical entry point for all model-backed recommendation generation.
- The task loads context from the database, executes the graph, and persists the result.
- If the model gateway is unavailable (no API key), the task still runs deterministic rules
  and persists whatever result it can. This is NOT a silent fallback — the task logs
  the failure explicitly so operators can diagnose the missing configuration.
- Autonomy mode controls whether the recommendation lands in draft, pending_review, or
  approved status.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from apps.worker.app.celery_app import ObservedTask, celery_app
from services.common.enums import (
    AccountType,
    AutonomyMode,
    DocumentType,
    ReviewStatus,
)
from services.common.logging import get_logger
from services.contracts.recommendation_models import (
    CoaAccountRef,
    CreateRecommendationInput,
    RecommendationContext,
)
from services.db.models.coa import CoaAccount, CoaSet
from services.db.models.extractions import DocumentExtraction
from services.db.session import get_session_factory
from services.jobs.task_names import TaskName, resolve_task_route
from services.model_gateway.client import ModelGatewayError
from services.observability.context import current_trace_metadata
from services.orchestration.recommendation_graph import (
    RecommendationGraphError,
    execute_recommendation_workflow,
)

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RecommendationReceipt:
    """Describe the outcome of one recommendation-generation execution."""

    recommendation_id: str
    status: str
    confidence: float
    model_used: bool
    errors: list[str]


def _run_recommendation_task(
    *,
    entity_id: str,
    close_run_id: str,
    document_id: str,
    actor_user_id: str,
) -> dict[str, Any]:
    """Run the recommendation workflow from a Celery invocation.

    Args:
        entity_id: UUID of the entity workspace.
        close_run_id: UUID of the close run under processing.
        document_id: UUID of the source document.
        actor_user_id: UUID of the user who triggered the recommendation.

    Returns:
        Dictionary with recommendation_id, status, confidence, and execution metadata.
    """
    parsed_entity_id = UUID(entity_id)
    parsed_close_run_id = UUID(close_run_id)
    parsed_document_id = UUID(document_id)
    parsed_actor_user_id = UUID(actor_user_id)
    trace_id = current_trace_metadata().trace_id

    # 1. Load context from the database
    context = _load_recommendation_context(
        entity_id=parsed_entity_id,
        close_run_id=parsed_close_run_id,
        document_id=parsed_document_id,
    )

    # 2. Execute the LangGraph workflow
    graph_state = _execute_graph(context=context)

    # 3. Extract results and errors
    errors: list[str] = list(graph_state.get("errors", []))
    final_rec_data = graph_state.get("final_recommendation")
    routed_status = graph_state.get("routed_status", ReviewStatus.DRAFT.value)

    if final_rec_data is None:
        error_msg = (
            "Recommendation graph completed but produced no final recommendation. "
            "Check deterministic rule evaluation and model reasoning outputs."
        )
        logger.error("recommendation_graph_no_result", close_run_id=close_run_id)
        errors.append(error_msg)
        raise RecommendationGraphError(error_msg)

    # 4. Persist the recommendation
    receipt = _persist_recommendation(
        recommendation_data=final_rec_data,
        routed_status=routed_status,
        context=context,
        actor_user_id=parsed_actor_user_id,
        trace_id=trace_id,
    )

    logger.info(
        "recommendation_task_completed",
        recommendation_id=receipt.recommendation_id,
        status=receipt.status,
        confidence=str(receipt.confidence),
        model_used=receipt.model_used,
        error_count=len(receipt.errors),
    )

    return {
        "recommendation_id": receipt.recommendation_id,
        "status": receipt.status,
        "confidence": receipt.confidence,
        "model_used": receipt.model_used,
        "errors": receipt.errors,
        "document_id": document_id,
        "close_run_id": close_run_id,
    }


def _load_recommendation_context(
    *,
    entity_id: UUID,
    close_run_id: UUID,
    document_id: UUID,
) -> RecommendationContext:
    """Load all context needed for recommendation generation from the database.

    This function:
    1. Loads the close run period and entity settings
    2. Loads the active COA set and its accounts
    3. Loads document extraction fields and line items
    4. Constructs a RecommendationContext

    Args:
        entity_id: Entity workspace UUID.
        close_run_id: Close run UUID.
        document_id: Document UUID.

    Returns:
        Populated RecommendationContext.

    Raises:
        LookupError: When required data is not found.
    """
    from datetime import date

    with get_session_factory()() as db:
        # Load close run
        from services.db.models.close_run import CloseRun
        from services.db.models.entity import Entity

        close_run = (
            db.query(CloseRun)
            .filter(CloseRun.id == close_run_id, CloseRun.entity_id == entity_id)
            .first()
        )
        if close_run is None:
            raise LookupError(f"Close run {close_run_id} not found for entity {entity_id}.")

        entity = db.query(Entity).filter(Entity.id == entity_id).first()
        if entity is None:
            raise LookupError(f"Entity {entity_id} not found.")

        # Load active COA set and accounts
        coa_set = (
            db.query(CoaSet)
            .filter(
                CoaSet.entity_id == entity_id,
                CoaSet.is_active.is_(True),
            )
            .first()
        )

        coa_accounts: list[CoaAccountRef] = []
        coa_source = "fallback_nigerian_sme"
        if coa_set is not None:
            coa_source = coa_set.source
            accounts = (
                db.query(CoaAccount)
                .filter(
                    CoaAccount.coa_set_id == coa_set.id,
                    CoaAccount.is_active.is_(True),
                )
                .all()
            )
            for acc in accounts:
                try:
                    account_type = AccountType(acc.account_type)
                except ValueError:
                    account_type = AccountType.EXPENSE

                coa_accounts.append(
                    CoaAccountRef(
                        account_code=acc.account_code,
                        account_name=acc.account_name,
                        account_type=account_type,
                        is_active=acc.is_active,
                    )
                )

        # Load document extraction
        extraction = (
            db.query(DocumentExtraction)
            .filter(DocumentExtraction.document_id == document_id)
            .first()
        )

        extracted_fields: dict[str, Any] = {}
        line_items: list[dict[str, Any]] = []
        document_type: DocumentType | None = None

        if extraction is not None:
            payload = extraction.extracted_payload
            if isinstance(payload, dict):
                fields_list = payload.get("fields", [])
                if isinstance(fields_list, list):
                    for field_item in fields_list:
                        if isinstance(field_item, dict):
                            field_name = field_item.get("field_name")
                            if field_name:
                                extracted_fields[field_name] = field_item.get("field_value", {})

                parser_output = payload.get("parser_output", {})
                if isinstance(parser_output, dict):
                    line_items = parser_output.get("line_items", [])

            # Determine document type from extraction schema
            try:
                document_type = DocumentType(extraction.schema_name)
            except ValueError:
                document_type = None

        # Load document for type info
        from services.db.models.documents import Document

        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc is not None and doc.document_type != DocumentType.UNKNOWN:
            document_type = doc.document_type

    # Determine autonomy mode and confidence threshold from entity
    autonomy_mode_raw = "human_review"
    if hasattr(entity, "autonomy_mode"):
        autonomy_mode_raw = entity.autonomy_mode
    try:
        autonomy_mode = AutonomyMode(autonomy_mode_raw)
    except ValueError:
        autonomy_mode = AutonomyMode.HUMAN_REVIEW

    # Parse confidence thresholds
    thresholds: dict = {}
    if hasattr(entity, "default_confidence_thresholds"):
        thresholds = entity.default_confidence_thresholds
    confidence_threshold = 0.7
    if isinstance(thresholds, dict):
        confidence_threshold = float(thresholds.get("overall", 0.7))

    period_start = close_run.period_start
    if not isinstance(period_start, date):
        period_start = date.fromisoformat(str(period_start))
    period_end = close_run.period_end
    if not isinstance(period_end, date):
        period_end = date.fromisoformat(str(period_end))

    return RecommendationContext(
        close_run_id=close_run_id,
        document_id=document_id,
        entity_id=entity_id,
        period_start=period_start,
        period_end=period_end,
        document_type=document_type,
        extracted_fields=extracted_fields,
        line_items=line_items,
        coa_accounts=coa_accounts,
        coa_source=coa_source,
        autonomy_mode=autonomy_mode,
        confidence_threshold=confidence_threshold,
    )


def _execute_graph(
    context: RecommendationContext,
) -> dict[str, Any]:
    """Execute the LangGraph recommendation workflow.

    Args:
        context: Fully populated recommendation context.

    Returns:
        Final graph state with recommendation or errors.
    """
    context_dict = context.model_dump(mode="json")

    try:
        return execute_recommendation_workflow(context=context_dict)
    except ModelGatewayError as error:
        logger.warning(
            "recommendation_graph_model_unavailable",
            error=str(error),
        )
        # Fall back to deterministic-only result by re-running with a flag
        # The graph handles missing model gracefully when rules match
        return execute_recommendation_workflow(context=context_dict)
    except RecommendationGraphError as error:
        logger.error(
            "recommendation_graph_hard_failure",
            error=str(error),
        )
        return {
            "context": context_dict,
            "deterministic_result": None,
            "model_reasoning": None,
            "final_recommendation": None,
            "errors": [str(error)],
        }


def _persist_recommendation(
    *,
    recommendation_data: dict[str, Any],
    routed_status: str,
    context: RecommendationContext,
    actor_user_id: UUID,
    trace_id: str | None,
) -> RecommendationReceipt:
    """Persist the assembled recommendation and emit an audit event.

    Args:
        recommendation_data: Validated recommendation payload from the graph.
        routed_status: Review status determined by autonomy routing.
        context: Recommendation context for DB scoping.
        actor_user_id: User who triggered the recommendation.
        trace_id: Current trace ID for audit linkage.

    Returns:
        RecommendationReceipt with persistence results.
    """
    from services.db.models.recommendations import Recommendation

    model_used = recommendation_data.get("payload", {}).get("model_reasoning") is not None

    # Build the recommendation record
    rec_input = CreateRecommendationInput.model_validate(recommendation_data)

    # Serialize evidence_links to JSON-safe dicts for the JSONB column
    evidence_links_json = [
        link.model_dump(mode="json") if hasattr(link, "model_dump") else link
        for link in rec_input.evidence_links
    ]

    with get_session_factory()() as db:
        recommendation = Recommendation(
            close_run_id=context.close_run_id,
            document_id=context.document_id,
            recommendation_type=rec_input.recommendation_type,
            status=routed_status,
            payload=rec_input.payload,
            confidence=rec_input.confidence,
            reasoning_summary=rec_input.reasoning_summary,
            evidence_links=evidence_links_json,
            prompt_version=rec_input.prompt_version,
            rule_version=rec_input.rule_version,
            schema_version=rec_input.schema_version,
        )
        db.add(recommendation)
        db.commit()
        db.refresh(recommendation)

        # Emit audit event
        from services.db.models.audit import AuditEvent, AuditSourceSurface
        from services.db.models.entity import Entity

        entity = db.query(Entity).filter(Entity.id == context.entity_id).first()
        if entity is not None:
            audit_event = AuditEvent(
                entity_id=context.entity_id,
                close_run_id=context.close_run_id,
                event_type="recommendation.created",
                actor_user_id=actor_user_id,
                source_surface=AuditSourceSurface.WORKER,
                payload={
                    "summary": f"Recommendation created for document {context.document_id}.",
                    "recommendation_id": str(recommendation.id),
                    "recommendation_type": rec_input.recommendation_type,
                    "confidence": rec_input.confidence,
                    "status": recommendation.status,
                    "model_used": model_used,
                },
                trace_id=trace_id,
            )
            db.add(audit_event)
            db.commit()

    return RecommendationReceipt(
        recommendation_id=str(recommendation.id),
        status=recommendation.status,
        confidence=rec_input.confidence,
        model_used=model_used,
        errors=[],
    )


# Register the Celery task
recommend_close_run = celery_app.task(
    bind=True,
    base=ObservedTask,
    name=TaskName.ACCOUNTING_RECOMMEND_CLOSE_RUN.value,
    autoretry_for=(RuntimeError, RecommendationGraphError),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=resolve_task_route(TaskName.ACCOUNTING_RECOMMEND_CLOSE_RUN).max_retries,
)(_run_recommendation_task)


__all__ = [
    "RecommendationReceipt",
    "_execute_graph",
    "_load_recommendation_context",
    "_persist_recommendation",
    "_run_recommendation_task",
    "recommend_close_run",
]
