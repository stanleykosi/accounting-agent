"""
Purpose: Implement LangGraph node functions for the accounting recommendation workflow.
Scope: Each node performs one bounded step: prerequisite validation, deterministic rule
evaluation, model-backed reasoning, recommendation assembly, and autonomy-mode routing.
Dependencies: langgraph, pydantic, accounting rules, model gateway, prompt registry,
recommendation contracts, audit service.

Design notes:
- Every node is a pure function that takes and returns a GraphState dict.
- Nodes never mutate external state directly. The worker task persists the final result.
- Deterministic rules run before any LLM call. The model is only invoked when rules
  are ambiguous, incomplete, or need narrative explanation.
- No raw model output can reach the final recommendation without Pydantic validation.
"""

from __future__ import annotations

from typing import Any

from services.accounting.rules import (
    AccountingRuleEvaluation,
    ChartAccount,
    RuleEngineError,
    TransactionContext,
    get_rule_engine,
)
from services.common.enums import AutonomyMode, DocumentType, ReviewStatus, RiskLevel
from services.common.logging import get_logger
from services.contracts.recommendation_models import (
    CoaAccountRef,
    ConfidenceMetrics,
    CreateRecommendationInput,
    EvidenceLink,
    GLCodingExplanationOutput,
)
from services.model_gateway.client import ModelGateway, ModelGatewayError
from services.model_gateway.prompts import (
    GL_CODING_EXPLANATION_PROMPT,
)

logger = get_logger(__name__)


class NodeError(ValueError):
    """Represent a hard failure within a graph node that blocks recommendation generation."""


# ---------------------------------------------------------------------------
# Node: validate_prerequisites
# ---------------------------------------------------------------------------


def validate_prerequisites(state: dict[str, Any]) -> dict[str, Any]:
    """Validate that the recommendation context has all required fields before processing.

    This node is the first gate in the recommendation graph. It ensures:
    - Close run and entity IDs are present
    - Period boundaries are defined
    - At least one COA account is available for mapping

    Args:
        state: LangGraph state dictionary containing the context.

    Returns:
        Updated state with validation result or error appended.
    """
    context_data = state.get("context", {})
    errors: list[str] = list(state.get("errors", []))

    required_fields = ("close_run_id", "entity_id", "period_start", "period_end", "coa_accounts")
    missing = [f for f in required_fields if not context_data.get(f)]
    if missing:
        error_msg = f"Missing required context fields: {', '.join(missing)}."
        logger.error("recommendation_prerequisites_missing", missing=missing)
        errors.append(error_msg)
        return {**state, "errors": errors}

    logger.debug(
        "recommendation_prerequisites_validated",
        close_run_id=str(context_data["close_run_id"]),
        entity_id=str(context_data["entity_id"]),
        coa_account_count=len(context_data["coa_accounts"]),
    )

    return {**state, "errors": errors}


# ---------------------------------------------------------------------------
# Node: evaluate_deterministic_rules
# ---------------------------------------------------------------------------


def evaluate_deterministic_rules(state: dict[str, Any]) -> dict[str, Any]:
    """Run deterministic accounting rules against the extraction context.

    This node:
    1. Builds a TransactionContext from extracted fields
    2. Constructs a rule engine from the active COA accounts
    3. Evaluates rules and captures the result
    4. Falls through to model reasoning if no rule matches

    Args:
        state: LangGraph state with context and extraction data.

    Returns:
        Updated state with deterministic_result populated, or errors if rule
        evaluation fails.
    """
    context_data = state.get("context", {})
    errors: list[str] = list(state.get("errors", []))
    deterministic_result: dict[str, Any] | None = None

    try:
        coa_accounts = _build_coa_accounts(context_data.get("coa_accounts", []))
        rule_engine = get_rule_engine(accounts=coa_accounts)

        transaction_context = _build_transaction_context(context_data)

        evaluation = rule_engine.evaluate(context=transaction_context)
        deterministic_result = _serialize_rule_evaluation(evaluation)

        logger.debug(
            "deterministic_rule_evaluated",
            rule_type=evaluation.rule_type,
            account_code=evaluation.account.account_code,
            confidence=str(evaluation.confidence),
            treatment=evaluation.treatment.value,
        )

    except RuleEngineError as error:
        logger.debug(
            "deterministic_rule_no_match",
            reason=str(error),
        )
        # No rule matched: model reasoning will fill the gap
        deterministic_result = {"matched": False, "reason": str(error)}
    except Exception as error:
        logger.error("deterministic_rule_evaluation_error", error=str(error))
        errors.append(f"Deterministic rule evaluation failed: {error}")

    return {
        **state,
        "deterministic_result": deterministic_result,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Node: invoke_model_reasoning
# ---------------------------------------------------------------------------


def invoke_model_reasoning(state: dict[str, Any]) -> dict[str, Any]:
    """Invoke the LLM for bounded reasoning when deterministic rules are incomplete.

    This node:
    1. Checks if deterministic rules already produced a high-confidence result
    2. If not, renders the GL coding explanation prompt
    3. Calls the model gateway with strict JSON schema validation
    4. Stores the validated output in the graph state

    Args:
        state: LangGraph state with deterministic result already populated.

    Returns:
        Updated state with model_reasoning populated, or errors if the model call fails.
    """
    context_data = state.get("context", {})
    deterministic_result = state.get("deterministic_result")
    errors: list[str] = list(state.get("errors", []))
    model_reasoning: dict[str, Any] | None = None

    # If deterministic rules produced a high-confidence result, skip the model
    if deterministic_result and deterministic_result.get("matched"):
        confidence = deterministic_result.get("confidence", 0.0)
        if confidence >= 0.85:
            logger.debug(
                "model_reasoning_skipped_high_confidence",
                confidence=str(confidence),
            )
            return {**state, "model_reasoning": model_reasoning, "errors": errors}

    try:
        system_prompt, user_prompt = _render_gl_coding_prompt(context_data, deterministic_result)
        gateway = ModelGateway()
        reasoning = gateway.complete_structured(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_model=GLCodingExplanationOutput,
        )
        model_reasoning = reasoning.model_dump(mode="json")

        logger.debug(
            "model_reasoning_completed",
            confidence=str(reasoning.confidence),
            risk_factor_count=len(reasoning.risk_factors),
        )

    except ModelGatewayError as error:
        logger.error("model_reasoning_failed", error=str(error))
        errors.append(f"Model reasoning failed: {error}")
    except Exception as error:
        logger.error("model_reasoning_unexpected_error", error=str(error))
        errors.append(f"Unexpected error in model reasoning: {error}")

    return {
        **state,
        "model_reasoning": model_reasoning,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Node: assemble_recommendation
# ---------------------------------------------------------------------------


def assemble_recommendation(state: dict[str, Any]) -> dict[str, Any]:
    """Assemble the final validated recommendation from deterministic and model outputs.

    This node:
    1. Merges deterministic rule results with model reasoning
    2. Computes aggregate confidence metrics
    3. Builds evidence links
    4. Constructs a CreateRecommendationInput ready for persistence

    Args:
        state: LangGraph state with all processing nodes completed.

    Returns:
        Updated state with final_recommendation populated, or errors if assembly fails.
    """
    context_data = state.get("context", {})
    deterministic_result = state.get("deterministic_result", {})
    model_reasoning = state.get("model_reasoning")
    errors: list[str] = list(state.get("errors", []))

    try:
        payload = _build_recommendation_payload(
            context=context_data,
            deterministic_result=deterministic_result,
            model_reasoning=model_reasoning,
        )
        confidence_metrics = _compute_aggregate_confidence(
            deterministic_result=deterministic_result,
            model_reasoning=model_reasoning,
        )
        evidence_links = _build_evidence_links(
            context=context_data,
            deterministic_result=deterministic_result,
        )

        recommendation = CreateRecommendationInput(
            close_run_id=context_data["close_run_id"],
            document_id=context_data.get("document_id"),
            recommendation_type="gl_coding",
            payload=payload,
            confidence=confidence_metrics.overall_confidence,
            reasoning_summary=payload.get("reasoning_summary", "No reasoning available."),
            evidence_links=evidence_links,
            prompt_version=GL_CODING_EXPLANATION_PROMPT.version,
            rule_version="1.0.0",
            schema_version="1.0.0",
        )

        logger.debug(
            "recommendation_assembled",
            confidence=str(recommendation.confidence),
            evidence_link_count=len(recommendation.evidence_links),
        )

        return {
            **state,
            "final_recommendation": recommendation.model_dump(mode="json"),
            "errors": errors,
        }

    except Exception as error:
        logger.error("recommendation_assembly_failed", error=str(error))
        errors.append(f"Recommendation assembly failed: {error}")
        return {**state, "errors": errors}


# ---------------------------------------------------------------------------
# Node: apply_autonomy_routing
# ---------------------------------------------------------------------------


def apply_autonomy_routing(state: dict[str, Any]) -> dict[str, Any]:
    """Determine the initial review status based on entity autonomy mode and confidence.

    Routing rules:
    - human_review: always route to pending_review
    - reduced_interruption: if confidence >= threshold and risk is LOW, mark as approved;
      otherwise route to pending_review

    Args:
        state: LangGraph state with the assembled recommendation.

    Returns:
        Updated state with the final recommendation including the routed status.
    """
    context_data = state.get("context", {})
    final_rec = state.get("final_recommendation")
    errors: list[str] = list(state.get("errors", []))

    if final_rec is None:
        errors.append("Cannot apply autonomy routing: no recommendation assembled.")
        return {**state, "errors": errors}

    autonomy_mode_raw = context_data.get("autonomy_mode", "human_review")
    autonomy_mode = AutonomyMode(autonomy_mode_raw)
    confidence_threshold = context_data.get("confidence_threshold", 0.7)
    recommendation_confidence = final_rec.get("confidence", 0.0)

    if autonomy_mode == AutonomyMode.REDUCED_INTERRUPTION:
        risk_level = final_rec.get("payload", {}).get("risk_level", "medium")
        if (
            recommendation_confidence >= confidence_threshold
            and risk_level == RiskLevel.LOW.value
        ):
            status = ReviewStatus.APPROVED
            logger.debug(
                "autonomy_routing_auto_approved",
                confidence=str(recommendation_confidence),
                risk_level=risk_level,
            )
        else:
            status = ReviewStatus.PENDING_REVIEW
            logger.debug(
                "autonomy_routing_pending_review",
                reason="confidence_below_threshold_or_risk_elevated",
                confidence=str(recommendation_confidence),
            )
    else:
        status = ReviewStatus.PENDING_REVIEW
        logger.debug(
            "autonomy_routing_human_review",
            confidence=str(recommendation_confidence),
        )

    # Store routed status in a separate state key to avoid polluting the
    # CreateRecommendationInput contract (which forbids extra fields).
    return {
        **state,
        "final_recommendation": final_rec,
        "routed_status": status.value,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Conditional edge: should_invoke_model
# ---------------------------------------------------------------------------


def should_invoke_model(state: dict[str, Any]) -> str:
    """Conditional edge: decide whether to invoke model reasoning or skip to assembly.

    Args:
        state: LangGraph state after deterministic rule evaluation.

    Returns:
        Node name to route to next: 'invoke_model_reasoning' or 'assemble_recommendation'.
    """
    deterministic_result = state.get("deterministic_result")
    if deterministic_result is None or not deterministic_result.get("matched"):
        return "invoke_model_reasoning"

    confidence = deterministic_result.get("confidence", 0.0)
    if confidence < 0.85:
        return "invoke_model_reasoning"

    return "assemble_recommendation"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_coa_accounts(raw_accounts: list[dict[str, Any]]) -> tuple[ChartAccount, ...]:
    """Convert raw COA account dicts into ChartAccount dataclass instances."""
    accounts: list[ChartAccount] = []
    for raw in raw_accounts:
        try:
            ref = CoaAccountRef.model_validate(raw)
            accounts.append(
                ChartAccount(
                    account_code=ref.account_code,
                    account_name=ref.account_name,
                    account_type=ref.account_type,
                    is_active=ref.is_active,
                )
            )
        except Exception:
            continue
    return tuple(accounts)


def _build_transaction_context(context_data: dict[str, Any]) -> TransactionContext:
    """Build a deterministic TransactionContext from the recommendation context."""
    from datetime import date

    extracted = context_data.get("extracted_fields", {})

    # Parse amount from extracted fields
    amount_value = extracted.get("total", {}).get("value", "0")
    try:
        from decimal import Decimal

        amount = Decimal(str(amount_value))
    except Exception:
        amount = Decimal("0")

    # Determine transaction date
    date_str = extracted.get("date", {}).get("value", "")
    try:
        transaction_date = date.fromisoformat(str(date_str))
    except (ValueError, TypeError):
        transaction_date = date.today()

    # Determine document type
    doc_type = context_data.get("document_type", DocumentType.UNKNOWN)
    if isinstance(doc_type, str):
        try:
            doc_type = DocumentType(doc_type)
        except ValueError:
            doc_type = DocumentType.UNKNOWN

    # Determine vendor name
    vendor_name = extracted.get("vendor", {}).get("value")

    return TransactionContext(
        amount=amount,
        transaction_date=transaction_date,
        period=_build_period_boundary(context_data),
        document_type=doc_type,
        vendor_name=str(vendor_name) if vendor_name else None,
    )


def _build_period_boundary(context_data: dict[str, Any]) -> Any:
    """Build a PeriodBoundary from context period dates."""
    from datetime import date as date_type

    from services.accounting.preprocess import PeriodBoundary

    period_start = context_data.get("period_start")
    period_end = context_data.get("period_end")

    # Handle both date objects and ISO strings
    if isinstance(period_start, date_type):
        start_date = period_start
    else:
        start_date = date_type.fromisoformat(str(period_start))

    if isinstance(period_end, date_type):
        end_date = period_end
    else:
        end_date = date_type.fromisoformat(str(period_end))

    return PeriodBoundary(period_start=start_date, period_end=end_date)


def _serialize_rule_evaluation(evaluation: AccountingRuleEvaluation) -> dict[str, Any]:
    """Serialize a deterministic rule evaluation into a JSON-safe dict."""
    return {
        "matched": True,
        "account_code": evaluation.account.account_code,
        "account_name": evaluation.account.account_name,
        "account_type": evaluation.account.account_type.value,
        "confidence": float(evaluation.confidence),
        "dimensions": evaluation.dimensions,
        "treatment": evaluation.treatment.value,
        "rule_type": evaluation.rule_type,
        "reasons": list(evaluation.reasons),
        "risk_level": evaluation.policy_decision.risk_level.value,
        "approval_level": evaluation.policy_decision.approval_level.value,
    }


def _render_gl_coding_prompt(
    context_data: dict[str, Any],
    deterministic_result: dict[str, Any] | None,
) -> tuple[str, str]:
    """Render the GL coding explanation prompt with context variables."""
    extracted = context_data.get("extracted_fields", {})
    coa_source = context_data.get("coa_source", "fallback_nigerian_sme")

    amount = extracted.get("total", {}).get("value", "0")
    currency = extracted.get("currency", {}).get("value", "NGN")
    vendor = extracted.get("vendor", {}).get("value", "Unknown")
    doc_type = context_data.get("document_type", DocumentType.UNKNOWN)
    if isinstance(doc_type, DocumentType):
        doc_type = doc_type.value

    # Use deterministic result if available, otherwise fall back to "no rule matched"
    if deterministic_result and deterministic_result.get("matched"):
        rule_info = (
            f"{deterministic_result.get('rule_type', 'unknown')} rule matched "
            f"account {deterministic_result.get('account_code', 'unknown')}."
        )
        account_code = deterministic_result.get("account_code", "")
        account_name = deterministic_result.get("account_name", "")
        account_type = deterministic_result.get("account_type", "unknown")
    else:
        rule_info = "No deterministic rule matched. Model must suggest an account."
        account_code = "N/A"
        account_name = "N/A"
        account_type = "N/A"

    line_items = context_data.get("line_items", [])
    line_items_summary = _summarize_line_items(line_items)

    return GL_CODING_EXPLANATION_PROMPT.render(
        document_type=doc_type,
        vendor_name=str(vendor),
        amount=str(amount),
        currency=str(currency),
        deterministic_rule=rule_info,
        account_code=account_code,
        account_name=account_name,
        account_type=account_type,
        coa_source=coa_source,
        line_items=line_items_summary,
    )


def _summarize_line_items(line_items: list[dict[str, Any]], max_items: int = 5) -> str:
    """Summarize line items for prompt inclusion."""
    if not line_items:
        return "No line items extracted."
    summary_parts = []
    for item in line_items[:max_items]:
        desc = item.get("description", "No description")
        amount = item.get("amount", "N/A")
        summary_parts.append(f"- {desc}: {amount}")
    if len(line_items) > max_items:
        summary_parts.append(f"... and {len(line_items) - max_items} more items")
    return "\n".join(summary_parts)


def _build_recommendation_payload(
    context: dict[str, Any],
    deterministic_result: dict[str, Any],
    model_reasoning: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge deterministic and model outputs into the recommendation payload."""
    payload: dict[str, Any] = {
        "deterministic_result": deterministic_result,
    }

    if model_reasoning:
        payload["model_reasoning"] = model_reasoning
        # Merge model risk factors into payload
        payload["risk_factors"] = model_reasoning.get("risk_factors", [])
        payload["alternative_accounts"] = model_reasoning.get("alternative_accounts", [])
    else:
        payload["risk_factors"] = []
        payload["alternative_accounts"] = []

    # Include account info from deterministic result
    if deterministic_result.get("matched"):
        payload["suggested_account_code"] = deterministic_result["account_code"]
        payload["suggested_account_name"] = deterministic_result["account_name"]
        payload["account_type"] = deterministic_result["account_type"]
        payload["treatment"] = deterministic_result["treatment"]
        payload["dimensions"] = deterministic_result.get("dimensions", {})
        payload["risk_level"] = deterministic_result.get("risk_level", "medium")
    elif model_reasoning:
        # Model must suggest an account when no rule matched
        payload["risk_level"] = "high"
        payload["reasoning_summary"] = model_reasoning.get("reasoning_summary", "")

    payload["reasoning_summary"] = ""
    if model_reasoning is not None:
        payload["reasoning_summary"] = model_reasoning.get("reasoning_summary", "")
    if not payload["reasoning_summary"]:
        reasons = deterministic_result.get("reasons")
        if reasons:
            payload["reasoning_summary"] = reasons[0]
        else:
            payload["reasoning_summary"] = "No reasoning available."

    return payload


def _compute_aggregate_confidence(
    deterministic_result: dict[str, Any],
    model_reasoning: dict[str, Any] | None,
) -> ConfidenceMetrics:
    """Compute aggregate confidence from deterministic and model sources."""
    det_confidence = None
    if deterministic_result.get("matched"):
        det_confidence = deterministic_result.get("confidence")

    model_confidence = None
    if model_reasoning:
        model_confidence = model_reasoning.get("confidence")

    # Aggregate: prefer deterministic when available, blend when both exist
    if det_confidence is not None and model_confidence is not None:
        overall = round(0.6 * det_confidence + 0.4 * model_confidence, 4)
    elif det_confidence is not None:
        overall = det_confidence
    elif model_confidence is not None:
        overall = model_confidence
    else:
        overall = 0.1

    low_confidence_fields: list[str] = []
    if overall < 0.7:
        low_confidence_fields.append("overall_confidence")

    return ConfidenceMetrics(
        overall_confidence=overall,
        deterministic_confidence=det_confidence,
        model_confidence=model_confidence,
        low_confidence_fields=low_confidence_fields,
    )


def _build_evidence_links(
    context: dict[str, Any],
    deterministic_result: dict[str, Any],
) -> list[EvidenceLink]:
    """Build evidence links from the processing context."""
    evidence: list[EvidenceLink] = []

    # Link to source document extraction
    doc_id = context.get("document_id")
    if doc_id:
        evidence.append(
            EvidenceLink(
                source_type="extraction",
                source_id=str(doc_id),
                description="Source document extraction fields and line items.",
            )
        )

    # Link to deterministic rule result
    if deterministic_result.get("matched"):
        evidence.append(
            EvidenceLink(
                source_type="rule",
                source_id=deterministic_result.get("rule_type", "unknown"),
                description=(
                    f"Deterministic {deterministic_result.get('rule_type', 'unknown')} "
                    f"rule evaluated to account "
                    f"{deterministic_result.get('account_code', 'unknown')}."
                ),
            )
        )

    return evidence


__all__ = [
    "NodeError",
    "apply_autonomy_routing",
    "assemble_recommendation",
    "evaluate_deterministic_rules",
    "invoke_model_reasoning",
    "should_invoke_model",
    "validate_prerequisites",
]
