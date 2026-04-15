"""
Purpose: Register the accounting-system deterministic tools exposed through the
generic agent kernel.
Scope: Tool registration, accounting workflow execution, and close-run scoped
state mutations with canonical service calls.
Dependencies: Accounting workflow services, repositories, and durable job dispatch.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from services.accounting.recommendation_apply import ActorContext, RecommendationApplyService
from services.agents.models import AgentExecutionContext, AgentToolDefinition
from services.agents.registry import ToolRegistry
from services.close_runs.service import CloseRunService
from services.close_runs.workflow_guards import require_active_phase
from services.common.enums import (
    DEFAULT_RECONCILIATION_EXECUTION_TYPES,
    ReconciliationType,
    ReviewStatus,
    SupportingScheduleStatus,
    SupportingScheduleType,
    WorkflowPhase,
)
from services.contracts.export_models import (
    EXPORT_DELIVERY_CHANNELS,
    CreateExportRequest,
    DistributeExportRequest,
)
from services.contracts.journal_models import JOURNAL_POSTING_TARGETS
from services.db.models.audit import AuditSourceSurface
from services.db.models.documents import Document
from services.db.models.extractions import DocumentExtraction
from services.db.models.recommendations import Recommendation
from services.db.models.reporting import ReportRunStatus as ReportRunStatusModel
from services.db.repositories.document_repo import DocumentRepository
from services.db.repositories.entity_repo import EntityUserRecord
from services.db.repositories.recommendation_journal_repo import RecommendationJournalRepository
from services.db.repositories.reconciliation_repo import ReconciliationRepository
from services.db.repositories.report_repo import ReportRepository
from services.documents.review_service import DocumentReviewService
from services.exports.service import ExportService
from services.jobs.service import JobService
from services.jobs.task_names import TaskName
from services.reconciliation.service import ReconciliationService
from services.reporting.service import ReportService
from services.supporting_schedules.service import SupportingScheduleService
from sqlalchemy.orm import Session


class AccountingToolset:
    """Provide the typed accounting tool registry used by the generic agent kernel."""

    def __init__(
        self,
        *,
        db_session: Session,
        close_run_service: CloseRunService,
        document_review_service: DocumentReviewService,
        document_repository: DocumentRepository,
        export_service: ExportService,
        job_service: JobService,
        recommendation_service: RecommendationApplyService,
        recommendation_repository: RecommendationJournalRepository,
        reconciliation_service: ReconciliationService,
        reconciliation_repository: ReconciliationRepository,
        report_service: ReportService,
        report_repository: ReportRepository,
        supporting_schedule_service: SupportingScheduleService,
        task_dispatcher: Any,
    ) -> None:
        self._db_session = db_session
        self._close_run_service = close_run_service
        self._document_review_service = document_review_service
        self._document_repo = document_repository
        self._export_service = export_service
        self._job_service = job_service
        self._recommendation_service = recommendation_service
        self._recommendation_repo = recommendation_repository
        self._reconciliation_service = reconciliation_service
        self._reconciliation_repo = reconciliation_repository
        self._report_service = report_service
        self._report_repo = report_repository
        self._supporting_schedule_service = supporting_schedule_service
        self._task_dispatcher = task_dispatcher

    def build_registry(self) -> ToolRegistry:
        """Return the registered accounting tool registry."""

        registry = ToolRegistry()
        self._register(
            registry=registry,
            name="review_document",
            prompt_signature="review_document(document_id, decision: approved|rejected|needs_info, reason?)",
            description="Persist a document review decision and update its workflow state.",
            intent="proposed_edit",
            requires_human_approval=True,
            executor=self._review_document,
            target_type="document",
            target_id_field="document_id",
            input_schema=_schema_object(
                properties={
                    "document_id": _uuid_property("Document UUID to review."),
                    "decision": _enum_string_property(
                        values=("approved", "rejected", "needs_info"),
                        description="Document review decision to persist.",
                    ),
                    "verified_complete": _boolean_property(
                        "Whether the reviewer confirmed the document is complete.",
                    ),
                    "verified_authorized": _boolean_property(
                        "Whether the reviewer confirmed the document is authorized.",
                    ),
                    "verified_period": _boolean_property(
                        "Whether the reviewer confirmed the document belongs to this period.",
                    ),
                    "verified_transaction_match": _boolean_property(
                        "Whether the reviewer confirmed the document matches the transaction.",
                    ),
                    "reason": _optional_string_property("Optional reviewer rationale."),
                },
                required=("document_id", "decision"),
            ),
        )
        self._register(
            registry=registry,
            name="correct_extracted_field",
            prompt_signature="correct_extracted_field(field_id, corrected_value, corrected_type, reason?)",
            description="Correct one extracted field and return the document to review with audit history.",
            intent="proposed_edit",
            requires_human_approval=True,
            executor=self._correct_extracted_field,
            target_type="extracted_field",
            target_id_field="field_id",
            input_schema=_schema_object(
                properties={
                    "field_id": _uuid_property("Extracted field UUID to correct."),
                    "corrected_value": _string_property("Corrected extracted value."),
                    "corrected_type": _string_property("Canonical value type after correction."),
                    "reason": _optional_string_property("Optional reviewer rationale."),
                },
                required=("field_id", "corrected_value", "corrected_type"),
            ),
        )
        self._register(
            registry=registry,
            name="advance_close_run",
            prompt_signature="advance_close_run(target_phase, reason?)",
            description="Advance the close run into the next workflow phase after gate checks pass.",
            intent="workflow_action",
            requires_human_approval=True,
            executor=self._advance_close_run,
            input_schema=_schema_object(
                properties={
                    "target_phase": _enum_string_property(
                        values=tuple(phase.value for phase in WorkflowPhase),
                        description="Workflow phase to transition the close run into.",
                    ),
                    "reason": _optional_string_property("Optional operator reason for the transition."),
                },
                required=("target_phase",),
            ),
        )
        self._register(
            registry=registry,
            name="approve_close_run",
            prompt_signature="approve_close_run(reason?)",
            description="Sign off the close run when the final review gate is ready.",
            intent="approval_request",
            requires_human_approval=True,
            executor=self._approve_close_run,
            input_schema=_schema_object(
                properties={
                    "reason": _optional_string_property("Optional approver rationale."),
                },
            ),
        )
        self._register(
            registry=registry,
            name="generate_recommendations",
            prompt_signature="generate_recommendations(force?, document_ids?)",
            description="Queue accounting recommendation generation jobs for eligible documents.",
            intent="workflow_action",
            requires_human_approval=False,
            executor=self._generate_recommendations,
            input_schema=_schema_object(
                properties={
                    "force": _boolean_property("Recompute recommendations even when one already exists."),
                    "document_ids": _uuid_array_property("Optional subset of document UUIDs to process."),
                },
            ),
        )
        self._register(
            registry=registry,
            name="approve_recommendation",
            prompt_signature="approve_recommendation(recommendation_id, reason?)",
            description="Approve one accounting recommendation and optionally create its journal draft.",
            intent="approval_request",
            requires_human_approval=True,
            executor=self._approve_recommendation,
            target_type="recommendation",
            target_id_field="recommendation_id",
            input_schema=_schema_object(
                properties={
                    "recommendation_id": _uuid_property("Recommendation UUID to approve."),
                    "reason": _optional_string_property("Optional approver rationale."),
                },
                required=("recommendation_id",),
            ),
        )
        self._register(
            registry=registry,
            name="reject_recommendation",
            prompt_signature="reject_recommendation(recommendation_id, reason)",
            description="Reject one accounting recommendation with a reviewer reason.",
            intent="approval_request",
            requires_human_approval=True,
            executor=self._reject_recommendation,
            target_type="recommendation",
            target_id_field="recommendation_id",
            input_schema=_schema_object(
                properties={
                    "recommendation_id": _uuid_property("Recommendation UUID to reject."),
                    "reason": _string_property("Required reviewer rationale for rejection."),
                },
                required=("recommendation_id", "reason"),
            ),
        )
        self._register(
            registry=registry,
            name="approve_journal",
            prompt_signature="approve_journal(journal_id, reason?)",
            description="Approve one journal draft for the current close run.",
            intent="approval_request",
            requires_human_approval=True,
            executor=self._approve_journal,
            target_type="journal",
            target_id_field="journal_id",
            input_schema=_schema_object(
                properties={
                    "journal_id": _uuid_property("Journal draft UUID to approve."),
                    "reason": _optional_string_property("Optional approver rationale."),
                },
                required=("journal_id",),
            ),
        )
        self._register(
            registry=registry,
            name="apply_journal",
            prompt_signature="apply_journal(journal_id, posting_target, reason?)",
            description="Post one approved journal draft either internally or as an external ERP package.",
            intent="approval_request",
            requires_human_approval=True,
            executor=self._apply_journal,
            target_type="journal",
            target_id_field="journal_id",
            input_schema=_schema_object(
                properties={
                    "journal_id": _uuid_property("Journal draft UUID to apply."),
                    "posting_target": {
                        "type": "string",
                        "enum": list(JOURNAL_POSTING_TARGETS),
                        "description": (
                            "Posting target: internal_ledger writes to the platform ledger, "
                            "external_erp_package creates an external ERP import package."
                        ),
                    },
                    "reason": _optional_string_property("Optional approver rationale."),
                },
                required=("journal_id", "posting_target"),
            ),
        )
        self._register(
            registry=registry,
            name="reject_journal",
            prompt_signature="reject_journal(journal_id, reason)",
            description="Reject one journal draft with a reviewer reason.",
            intent="approval_request",
            requires_human_approval=True,
            executor=self._reject_journal,
            target_type="journal",
            target_id_field="journal_id",
            input_schema=_schema_object(
                properties={
                    "journal_id": _uuid_property("Journal draft UUID to reject."),
                    "reason": _string_property("Required reviewer rationale for rejection."),
                },
                required=("journal_id", "reason"),
            ),
        )
        self._register(
            registry=registry,
            name="run_reconciliation",
            prompt_signature="run_reconciliation(reconciliation_types?)",
            description="Queue reconciliation execution for one or more reconciliation types.",
            intent="reconciliation_query",
            requires_human_approval=False,
            executor=self._run_reconciliation,
            input_schema=_schema_object(
                properties={
                    "reconciliation_types": _enum_array_property(
                        values=tuple(reconciliation_type.value for reconciliation_type in ReconciliationType),
                        description="Optional subset of reconciliation types to execute.",
                    ),
                },
            ),
        )
        self._register(
            registry=registry,
            name="upsert_supporting_schedule_row",
            prompt_signature="upsert_supporting_schedule_row(schedule_type, row_payload, row_id?)",
            description="Create or update one Step 6 supporting-schedule workpaper row.",
            intent="proposed_edit",
            requires_human_approval=False,
            executor=self._upsert_supporting_schedule_row,
            input_schema=_schema_object(
                properties={
                    "schedule_type": _enum_string_property(
                        values=tuple(schedule_type.value for schedule_type in SupportingScheduleType),
                        description="Standalone Step 6 schedule type to update.",
                    ),
                    "row_id": _uuid_or_null_property("Existing schedule row UUID for updates."),
                    "row_payload": _supporting_schedule_row_payload_schema(),
                },
                required=("schedule_type", "row_payload"),
            ),
        )
        self._register(
            registry=registry,
            name="delete_supporting_schedule_row",
            prompt_signature="delete_supporting_schedule_row(schedule_type, row_id)",
            description="Delete one Step 6 supporting-schedule workpaper row.",
            intent="proposed_edit",
            requires_human_approval=False,
            executor=self._delete_supporting_schedule_row,
            input_schema=_schema_object(
                properties={
                    "schedule_type": _enum_string_property(
                        values=tuple(schedule_type.value for schedule_type in SupportingScheduleType),
                        description="Standalone Step 6 schedule type to update.",
                    ),
                    "row_id": _uuid_property("Supporting-schedule row UUID to delete."),
                },
                required=("schedule_type", "row_id"),
            ),
        )
        self._register(
            registry=registry,
            name="set_supporting_schedule_status",
            prompt_signature="set_supporting_schedule_status(schedule_type, status, note?)",
            description="Review, approve, or mark a Step 6 supporting schedule not applicable.",
            intent="approval_request",
            requires_human_approval=True,
            executor=self._set_supporting_schedule_status,
            input_schema=_schema_object(
                properties={
                    "schedule_type": _enum_string_property(
                        values=tuple(schedule_type.value for schedule_type in SupportingScheduleType),
                        description="Standalone Step 6 schedule type to review.",
                    ),
                    "status": _enum_string_property(
                        values=(
                            SupportingScheduleStatus.IN_REVIEW.value,
                            SupportingScheduleStatus.APPROVED.value,
                            SupportingScheduleStatus.NOT_APPLICABLE.value,
                        ),
                        description="Requested review status for the schedule.",
                    ),
                    "note": _optional_string_property(
                        "Optional review note. Required when marking the schedule not applicable."
                    ),
                },
                required=("schedule_type", "status"),
            ),
        )
        self._register(
            registry=registry,
            name="approve_reconciliation",
            prompt_signature="approve_reconciliation(reconciliation_id, reason?)",
            description="Approve one reconciliation result after reviewing its disposition.",
            intent="reconciliation_query",
            requires_human_approval=True,
            executor=self._approve_reconciliation,
            target_type="reconciliation",
            target_id_field="reconciliation_id",
            input_schema=_schema_object(
                properties={
                    "reconciliation_id": _uuid_property("Reconciliation UUID to approve."),
                    "reason": _optional_string_property("Optional approver rationale."),
                },
                required=("reconciliation_id",),
            ),
        )
        self._register(
            registry=registry,
            name="generate_reports",
            prompt_signature="generate_reports(template_id?, generate_commentary?, use_llm_commentary?)",
            description="Create a report run and queue report generation for the current close run.",
            intent="report_action",
            requires_human_approval=False,
            executor=self._generate_reports,
            input_schema=_schema_object(
                properties={
                    "template_id": _uuid_or_null_property("Optional report template UUID override."),
                    "generate_commentary": _boolean_property("Whether to draft commentary alongside the report run."),
                    "use_llm_commentary": _boolean_property("Whether to use the model-generated commentary path."),
                },
            ),
        )
        self._register(
            registry=registry,
            name="generate_export",
            prompt_signature="generate_export(include_evidence_pack?, include_audit_trail?, action_qualifier?)",
            description="Generate the export manifest and released artifact bundle for the close run.",
            intent="report_action",
            requires_human_approval=False,
            executor=self._generate_export,
            input_schema=_schema_object(
                properties={
                    "include_evidence_pack": _boolean_property("Whether to include the latest evidence pack."),
                    "include_audit_trail": _boolean_property("Whether to include audit trail records."),
                    "action_qualifier": _optional_string_property("Optional qualifier used for idempotent export naming."),
                },
            ),
        )
        self._register(
            registry=registry,
            name="assemble_evidence_pack",
            prompt_signature="assemble_evidence_pack()",
            description="Assemble or reuse the canonical evidence pack for the close run.",
            intent="report_action",
            requires_human_approval=False,
            executor=self._assemble_evidence_pack,
            input_schema=_schema_object(properties={}),
        )
        self._register(
            registry=registry,
            name="distribute_export",
            prompt_signature=(
                "distribute_export(export_id, recipient_name, recipient_email, "
                "recipient_role?, delivery_channel?, note?)"
            ),
            description="Record distribution of a completed export package to management stakeholders.",
            intent="approval_request",
            requires_human_approval=True,
            executor=self._distribute_export,
            target_type="export",
            target_id_field="export_id",
            input_schema=_schema_object(
                properties={
                    "export_id": _uuid_property("Completed export UUID to distribute."),
                    "recipient_name": _string_property("Stakeholder name receiving the package."),
                    "recipient_email": _string_property("Stakeholder email receiving the package."),
                    "recipient_role": _optional_string_property(
                        "Optional stakeholder role such as CFO or Finance Manager."
                    ),
                    "delivery_channel": _enum_string_property(
                        values=EXPORT_DELIVERY_CHANNELS,
                        description="Controlled delivery channel used for the release.",
                    ),
                    "note": _optional_string_property(
                        "Optional operator note about the release or sign-off context."
                    ),
                },
                required=("export_id", "recipient_name", "recipient_email"),
            ),
        )
        self._register(
            registry=registry,
            name="update_commentary",
            prompt_signature="update_commentary(report_run_id, section_key, body)",
            description="Edit one report commentary section in the current report run.",
            intent="report_action",
            requires_human_approval=False,
            executor=self._update_commentary,
            target_type="report_run",
            target_id_field="report_run_id",
            input_schema=_schema_object(
                properties={
                    "report_run_id": _uuid_property("Report run UUID to update."),
                    "section_key": _string_property("Template section key to update."),
                    "body": _string_property("New commentary body."),
                },
                required=("report_run_id", "section_key", "body"),
            ),
        )
        self._register(
            registry=registry,
            name="approve_commentary",
            prompt_signature="approve_commentary(report_run_id, section_key, body?, reason?)",
            description="Approve one commentary section for release in the report run.",
            intent="report_action",
            requires_human_approval=True,
            executor=self._approve_commentary,
            target_type="report_run",
            target_id_field="report_run_id",
            input_schema=_schema_object(
                properties={
                    "report_run_id": _uuid_property("Report run UUID containing the commentary."),
                    "section_key": _string_property("Template section key to approve."),
                    "body": _optional_string_property("Optional replacement commentary body."),
                    "reason": _optional_string_property("Optional approval rationale."),
                },
                required=("report_run_id", "section_key"),
            ),
        )
        return registry

    def _register(
        self,
        *,
        registry: ToolRegistry,
        name: str,
        prompt_signature: str,
        description: str,
        intent: str,
        requires_human_approval: bool,
        executor: Any,
        target_type: str | None = None,
        target_id_field: str | None = None,
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        """Register one typed accounting tool."""

        registry.register_tool(
            definition=AgentToolDefinition(
                name=name,
                prompt_signature=prompt_signature,
                description=description,
                intent=intent,
                requires_human_approval=requires_human_approval,
                input_schema=input_schema or {"type": "object", "properties": {}},
            ),
            executor=executor,
            target_deriver=(
                _build_target_deriver(target_type=target_type, field_name=target_id_field)
                if target_type is not None and target_id_field is not None
                else None
            ),
        )

    def _review_document(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(context, "Document review requires a close-run-scoped thread.")
        self._require_active_phase_for_mutation(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.COLLECTION,
            action_label="Document review",
        )
        result = self._document_review_service.review_document(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            document_id=UUID(_require_string(arguments, "document_id")),
            decision=_require_string(arguments, "decision"),
            reason=_optional_string(arguments, "reason"),
            verified_complete=_optional_bool(arguments, "verified_complete"),
            verified_authorized=_optional_bool(arguments, "verified_authorized"),
            verified_period=_optional_bool(arguments, "verified_period"),
            verified_transaction_match=_optional_bool(arguments, "verified_transaction_match"),
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        return {
            "tool": "review_document",
            "document_id": result.document.id,
            "status": result.document.status.value,
            "decision": result.decision,
        }

    def _correct_extracted_field(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(context, "Field correction requires a close-run-scoped thread.")
        self._require_active_phase_for_mutation(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.COLLECTION,
            action_label="Extracted-field correction",
        )
        result = self._document_review_service.correct_extracted_field(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            field_id=UUID(_require_string(arguments, "field_id")),
            corrected_value=_require_string(arguments, "corrected_value"),
            corrected_type=_require_string(arguments, "corrected_type"),
            reason=_optional_string(arguments, "reason"),
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        return {
            "tool": "correct_extracted_field",
            "field_id": result.field.id,
            "document_id": result.document.id,
            "document_status": result.document.status.value,
        }

    def _advance_close_run(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(context, "Close-run transition requires a close-run-scoped thread.")
        result = self._close_run_service.transition_close_run(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            target_phase=WorkflowPhase(_require_string(arguments, "target_phase")),
            reason=_optional_string(arguments, "reason"),
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        return {
            "tool": "advance_close_run",
            "close_run_id": result.close_run.id,
            "active_phase": (
                result.close_run.workflow_state.active_phase.value
                if result.close_run.workflow_state.active_phase is not None
                else None
            ),
            "status": result.close_run.status.value,
        }

    def _approve_close_run(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(context, "Close-run approval requires a close-run-scoped thread.")
        result = self._close_run_service.approve_close_run(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            reason=_optional_string(arguments, "reason"),
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        return {
            "tool": "approve_close_run",
            "close_run_id": result.id,
            "status": result.status.value,
        }

    def _generate_recommendations(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context,
            "Recommendation generation requires a close-run-scoped thread.",
        )
        document_ids = [UUID(document_id) for document_id in _optional_string_list(arguments, "document_ids")]
        self._require_active_phase_for_mutation(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.PROCESSING,
            action_label="Recommendation generation",
        )
        queued_jobs = self._queue_recommendation_jobs(
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            actor_user=actor_user,
            document_ids=document_ids or None,
            force=bool(arguments.get("force", False)),
            trace_id=context.trace_id,
        )
        return {
            "tool": "generate_recommendations",
            "queued_jobs": queued_jobs,
            "queued_count": len(queued_jobs),
        }

    def _approve_recommendation(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context,
            "Recommendation approval requires a close-run-scoped thread.",
        )
        self._require_active_phase_for_mutation(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.PROCESSING,
            action_label="Recommendation approval",
        )
        result = self._recommendation_service.approve_recommendation(
            recommendation_id=UUID(_require_string(arguments, "recommendation_id")),
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            actor=ActorContext(
                user_id=actor_user.id,
                full_name=actor_user.full_name,
                email=actor_user.email,
            ),
            reason=_optional_string(arguments, "reason"),
            trace_id=context.trace_id,
            source_surface=cast(AuditSourceSurface, context.source_surface),
        )
        return {
            "tool": "approve_recommendation",
            "recommendation_id": str(result.recommendation_id),
            "final_status": result.final_status.value,
            "journal_id": (
                str(result.journal_draft_result.journal_id)
                if result.journal_draft_result is not None
                else None
            ),
        }

    def _reject_recommendation(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context,
            "Recommendation rejection requires a close-run-scoped thread.",
        )
        self._require_active_phase_for_mutation(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.PROCESSING,
            action_label="Recommendation rejection",
        )
        recommendation_id = UUID(_require_string(arguments, "recommendation_id"))
        self._recommendation_service.reject_recommendation(
            recommendation_id=recommendation_id,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            actor=ActorContext(
                user_id=actor_user.id,
                full_name=actor_user.full_name,
                email=actor_user.email,
            ),
            reason=_require_string(arguments, "reason"),
            trace_id=context.trace_id,
            source_surface=cast(AuditSourceSurface, context.source_surface),
        )
        return {
            "tool": "reject_recommendation",
            "recommendation_id": str(recommendation_id),
            "status": ReviewStatus.REJECTED.value,
        }

    def _approve_journal(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        return self._run_journal_action(arguments=arguments, context=context, action="approve")

    def _apply_journal(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        return self._run_journal_action(arguments=arguments, context=context, action="apply")

    def _reject_journal(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        return self._run_journal_action(arguments=arguments, context=context, action="reject")

    def _run_reconciliation(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context,
            "Reconciliation execution requires a close-run-scoped thread.",
        )
        self._require_active_phase_for_mutation(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.RECONCILIATION,
            action_label="Reconciliation execution",
        )
        reconciliation_types = [
            ReconciliationType(reconciliation_type)
            for reconciliation_type in _optional_string_list(arguments, "reconciliation_types")
        ]
        job = self._job_service.dispatch_job(
            dispatcher=self._task_dispatcher,
            task_name=TaskName.RECONCILIATION_EXECUTE_CLOSE_RUN,
            payload={
                "close_run_id": str(close_run_id),
                "reconciliation_types": [
                    reconciliation_type.value
                    for reconciliation_type in (
                        reconciliation_types or list(DEFAULT_RECONCILIATION_EXECUTION_TYPES)
                    )
                ],
                "actor_user_id": str(actor_user.id),
            },
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            document_id=None,
            actor_user_id=actor_user.id,
            trace_id=context.trace_id,
        )
        return {
            "tool": "run_reconciliation",
            "job_id": str(job.id),
            "status": job.status.value,
        }

    def _upsert_supporting_schedule_row(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context,
            "Supporting schedule maintenance requires a close-run-scoped thread.",
        )
        self._require_active_phase_for_mutation(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.RECONCILIATION,
            action_label="Supporting schedule maintenance",
        )
        schedule_type = SupportingScheduleType(_require_string(arguments, "schedule_type"))
        row_payload = arguments.get("row_payload")
        if not isinstance(row_payload, dict):
            raise ValueError("row_payload must be a structured object.")
        snapshot = self._supporting_schedule_service.save_row(
            close_run_id=close_run_id,
            schedule_type=schedule_type,
            row_id=(
                UUID(_require_string(arguments, "row_id"))
                if isinstance(arguments.get("row_id"), str)
                else None
            ),
            payload=row_payload,
        )
        return {
            "tool": "upsert_supporting_schedule_row",
            "schedule_type": schedule_type.value,
            "schedule_status": snapshot.schedule.status.value,
            "row_count": len(snapshot.rows),
        }

    def _delete_supporting_schedule_row(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context,
            "Supporting schedule maintenance requires a close-run-scoped thread.",
        )
        self._require_active_phase_for_mutation(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.RECONCILIATION,
            action_label="Supporting schedule maintenance",
        )
        schedule_type = SupportingScheduleType(_require_string(arguments, "schedule_type"))
        snapshot = self._supporting_schedule_service.delete_row(
            close_run_id=close_run_id,
            schedule_type=schedule_type,
            row_id=UUID(_require_string(arguments, "row_id")),
        )
        return {
            "tool": "delete_supporting_schedule_row",
            "schedule_type": schedule_type.value,
            "schedule_status": snapshot.schedule.status.value,
            "row_count": len(snapshot.rows),
        }

    def _set_supporting_schedule_status(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context,
            "Supporting schedule review requires a close-run-scoped thread.",
        )
        self._require_active_phase_for_mutation(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.RECONCILIATION,
            action_label="Supporting schedule review",
        )
        schedule_type = SupportingScheduleType(_require_string(arguments, "schedule_type"))
        snapshot = self._supporting_schedule_service.update_status(
            close_run_id=close_run_id,
            schedule_type=schedule_type,
            status=SupportingScheduleStatus(_require_string(arguments, "status")),
            note=_optional_string(arguments, "note"),
            actor_user_id=actor_user.id,
        )
        return {
            "tool": "set_supporting_schedule_status",
            "schedule_type": schedule_type.value,
            "schedule_status": snapshot.schedule.status.value,
            "row_count": len(snapshot.rows),
            "reviewed_at": snapshot.schedule.reviewed_at,
        }

    def _approve_reconciliation(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context,
            "Reconciliation approval requires a close-run-scoped thread.",
        )
        self._require_active_phase_for_mutation(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.RECONCILIATION,
            action_label="Reconciliation approval",
        )
        reconciliation_id = UUID(_require_string(arguments, "reconciliation_id"))
        result = self._reconciliation_service.approve_reconciliation(
            reconciliation_id=reconciliation_id,
            close_run_id=close_run_id,
            reason=_optional_string(arguments, "reason"),
            user_id=actor_user.id,
        )
        if result is None:
            raise ValueError("The reconciliation was not found for this close run.")
        return {
            "tool": "approve_reconciliation",
            "reconciliation_id": str(reconciliation_id),
            "status": result.status.value,
        }

    def _generate_reports(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context,
            "Report generation requires a close-run-scoped thread.",
        )
        self._require_active_phase_for_mutation(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.REPORTING,
            action_label="Report generation",
        )
        run_record = self._queue_report_generation(
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            actor_user=actor_user,
            template_id=_optional_string(arguments, "template_id"),
            generate_commentary=bool(arguments.get("generate_commentary", True)),
            use_llm_commentary=bool(arguments.get("use_llm_commentary", False)),
            trace_id=context.trace_id,
        )
        return {
            "tool": "generate_reports",
            "report_run_id": str(run_record.id),
            "status": run_record.status.value,
            "version_no": run_record.version_no,
        }

    def _generate_export(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context,
            "Export generation requires a close-run-scoped thread.",
        )
        self._require_active_phase_for_mutation(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.REVIEW_SIGNOFF,
            action_label="Export generation",
        )
        export_detail = self._export_service.trigger_export(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            request=CreateExportRequest(
                include_evidence_pack=bool(arguments.get("include_evidence_pack", True)),
                include_audit_trail=bool(arguments.get("include_audit_trail", True)),
                action_qualifier=_optional_string(arguments, "action_qualifier"),
            ),
        )
        return {
            "tool": "generate_export",
            "export_id": export_detail.id,
            "status": export_detail.status,
            "artifact_count": export_detail.artifact_count,
            "has_evidence_pack": export_detail.evidence_pack is not None,
        }

    def _assemble_evidence_pack(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        del arguments
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context,
            "Evidence-pack assembly requires a close-run-scoped thread.",
        )
        self._require_active_phase_for_mutation(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.REVIEW_SIGNOFF,
            action_label="Evidence-pack assembly",
        )
        evidence_pack = self._export_service.assemble_evidence_pack(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
        )
        return {
            "tool": "assemble_evidence_pack",
            "version_no": evidence_pack.version_no,
            "generated_at": evidence_pack.generated_at,
            "storage_key": evidence_pack.storage_key,
            "size_bytes": evidence_pack.size_bytes,
        }

    def _distribute_export(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context,
            "Management distribution requires a close-run-scoped thread.",
        )
        self._require_active_phase_for_mutation(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.REVIEW_SIGNOFF,
            action_label="Management distribution",
        )
        export_detail = self._export_service.distribute_export(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            export_id=UUID(_require_string(arguments, "export_id")),
            request=DistributeExportRequest(
                recipient_name=_require_string(arguments, "recipient_name"),
                recipient_email=_require_string(arguments, "recipient_email"),
                recipient_role=_optional_string(arguments, "recipient_role"),
                delivery_channel=_optional_string(arguments, "delivery_channel") or "secure_email",
                note=_optional_string(arguments, "note"),
            ),
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        latest_record = export_detail.distribution_records[0] if export_detail.distribution_records else None
        return {
            "tool": "distribute_export",
            "export_id": export_detail.id,
            "distribution_count": export_detail.distribution_count,
            "recipient_name": latest_record.recipient_name if latest_record is not None else None,
            "delivery_channel": latest_record.delivery_channel if latest_record is not None else None,
            "distributed_at": latest_record.distributed_at if latest_record is not None else None,
        }

    def _update_commentary(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context,
            "Commentary updates require a close-run-scoped thread.",
        )
        self._require_active_phase_for_mutation(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.REPORTING,
            action_label="Commentary update",
        )
        commentary = self._report_service.update_commentary(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            report_run_id=UUID(_require_string(arguments, "report_run_id")),
            section_key=_require_string(arguments, "section_key"),
            body=_require_string(arguments, "body"),
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        return {
            "tool": "update_commentary",
            "commentary_id": commentary.id,
            "status": commentary.status,
        }

    def _approve_commentary(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context,
            "Commentary approval requires a close-run-scoped thread.",
        )
        self._require_active_phase_for_mutation(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.REPORTING,
            action_label="Commentary approval",
        )
        commentary = self._report_service.approve_commentary(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            report_run_id=UUID(_require_string(arguments, "report_run_id")),
            section_key=_require_string(arguments, "section_key"),
            body=_optional_string(arguments, "body"),
            reason=_optional_string(arguments, "reason"),
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        return {
            "tool": "approve_commentary",
            "commentary_id": commentary.id,
            "status": commentary.status,
        }

    def _run_journal_action(
        self,
        *,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
        action: str,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(context, "Journal actions require a close-run-scoped thread.")
        self._require_active_phase_for_mutation(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.PROCESSING,
            action_label=f"Journal {action}",
        )
        journal_id = UUID(_require_string(arguments, "journal_id"))
        actor = ActorContext(
            user_id=actor_user.id,
            full_name=actor_user.full_name,
            email=actor_user.email,
        )
        if action == "approve":
            result = self._recommendation_service.approve_journal(
                journal_id=journal_id,
                entity_id=context.entity_id,
                close_run_id=close_run_id,
                actor=actor,
                reason=_optional_string(arguments, "reason"),
                trace_id=context.trace_id,
                source_surface=cast(AuditSourceSurface, context.source_surface),
            )
            tool_name = "approve_journal"
        elif action == "apply":
            result = self._recommendation_service.apply_journal(
                journal_id=journal_id,
                entity_id=context.entity_id,
                close_run_id=close_run_id,
                actor=actor,
                posting_target=_require_string(arguments, "posting_target"),
                reason=_optional_string(arguments, "reason"),
                trace_id=context.trace_id,
                source_surface=cast(AuditSourceSurface, context.source_surface),
            )
            tool_name = "apply_journal"
        else:
            result = self._recommendation_service.reject_journal(
                journal_id=journal_id,
                entity_id=context.entity_id,
                close_run_id=close_run_id,
                actor=actor,
                reason=_require_string(arguments, "reason"),
                trace_id=context.trace_id,
                source_surface=cast(AuditSourceSurface, context.source_surface),
            )
            tool_name = "reject_journal"
        return {
            "tool": tool_name,
            "journal_id": str(journal_id),
            "status": result.final_status.value,
        }

    def _queue_recommendation_jobs(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        actor_user: EntityUserRecord,
        document_ids: list[UUID] | None,
        force: bool,
        trace_id: str | None,
    ) -> list[dict[str, Any]]:
        """Queue recommendation generation for collection-approved documents only."""

        document_query = (
            self._db_session.query(Document)
            .join(DocumentExtraction, DocumentExtraction.document_id == Document.id)
            .filter(
                Document.close_run_id == close_run_id,
                Document.status == "approved",
            )
            .order_by(Document.created_at.asc(), Document.id.asc())
        )
        if document_ids:
            document_query = document_query.filter(Document.id.in_(document_ids))

        eligible_documents = document_query.all()
        existing_recommendations: set[UUID] = set()
        if not force:
            existing_recommendations = {
                recommendation.document_id
                for recommendation in self._db_session.query(Recommendation)
                .filter(
                    Recommendation.close_run_id == close_run_id,
                    Recommendation.document_id.isnot(None),
                    Recommendation.superseded_by_id.is_(None),
                )
                .all()
                if recommendation.document_id is not None
            }

        queued_jobs: list[dict[str, Any]] = []
        for document in eligible_documents:
            if not force and document.id in existing_recommendations:
                continue

            job = self._job_service.dispatch_job(
                dispatcher=self._task_dispatcher,
                task_name=TaskName.ACCOUNTING_RECOMMEND_CLOSE_RUN,
                payload={
                    "entity_id": str(entity_id),
                    "close_run_id": str(close_run_id),
                    "document_id": str(document.id),
                    "actor_user_id": str(actor_user.id),
                },
                entity_id=entity_id,
                close_run_id=close_run_id,
                document_id=document.id,
                actor_user_id=actor_user.id,
                trace_id=trace_id,
            )
            queued_jobs.append(
                {
                    "job_id": str(job.id),
                    "document_id": str(document.id),
                    "task_name": job.task_name,
                    "status": job.status.value,
                }
            )
        return queued_jobs

    def _queue_report_generation(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        actor_user: EntityUserRecord,
        template_id: str | None,
        generate_commentary: bool,
        use_llm_commentary: bool,
        trace_id: str | None,
    ) -> Any:
        """Create a report run and dispatch the background generation job."""

        if template_id is not None:
            resolved_template_id = UUID(template_id)
        else:
            resolved_template = (
                self._report_repo.get_active_template_for_entity(entity_id=entity_id)
                or self._report_repo.get_active_global_template()
            )
            if resolved_template is None:
                raise ValueError("No active report template is available for this entity.")
            resolved_template_id = resolved_template.id
        version_no = self._report_repo.next_version_no_for_close_run(close_run_id=close_run_id)
        run_record = self._report_repo.create_report_run(
            close_run_id=close_run_id,
            template_id=resolved_template_id,
            version_no=version_no,
            status=ReportRunStatusModel.PENDING,
            generation_config={
                "generate_commentary": generate_commentary,
                "use_llm_commentary": use_llm_commentary,
            },
            generated_by_user_id=actor_user.id,
        )
        self._job_service.dispatch_job(
            dispatcher=self._task_dispatcher,
            task_name=TaskName.REPORTING_GENERATE_CLOSE_RUN_PACK,
            payload={
                "close_run_id": str(close_run_id),
                "report_run_id": str(run_record.id),
                "actor_user_id": str(actor_user.id),
                "generate_commentary_flag": generate_commentary,
                "use_llm_commentary": use_llm_commentary,
            },
            entity_id=entity_id,
            close_run_id=close_run_id,
            document_id=None,
            actor_user_id=actor_user.id,
            trace_id=trace_id,
        )
        return run_record

    def _require_actor(self, context: AgentExecutionContext) -> EntityUserRecord:
        """Return the typed entity actor required by the accounting services."""

        actor_user = context.actor
        if not isinstance(actor_user, EntityUserRecord):
            raise TypeError("Accounting tools require an EntityUserRecord actor.")
        return actor_user

    def _require_close_run_id(self, context: AgentExecutionContext, message: str) -> UUID:
        """Return the close-run id or fail fast when the thread is not close-run scoped."""

        if context.close_run_id is None:
            raise ValueError(message)
        return context.close_run_id

    def _require_active_phase_for_mutation(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
        required_phase: WorkflowPhase,
        action_label: str,
    ) -> None:
        """Require the expected workflow phase to still be the active mutation phase."""

        close_run = self._close_run_service.get_close_run(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
        require_active_phase(
            close_run,
            required_phase=required_phase,
            action_label=action_label,
        )


def _build_target_deriver(*, target_type: str, field_name: str):
    """Build a target-deriver function for one UUID-bearing tool argument."""

    def derive(arguments: dict[str, Any]) -> tuple[str | None, UUID | None]:
        raw_value = arguments.get(field_name)
        if not isinstance(raw_value, str):
            return target_type, None
        try:
            return target_type, UUID(raw_value)
        except ValueError:
            return target_type, None

    return derive


def _require_string(arguments: dict[str, Any], key: str) -> str:
    """Return one required string argument or fail fast."""

    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Chat action is missing required argument '{key}'.")
    return value.strip()


def _optional_string(arguments: dict[str, Any], key: str) -> str | None:
    """Return one optional string argument when present."""

    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Chat action argument '{key}' must be a string.")
    normalized = value.strip()
    return normalized or None


def _optional_bool(arguments: dict[str, Any], key: str) -> bool | None:
    """Return one optional boolean argument when present."""

    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"Chat action argument '{key}' must be a boolean.")
    return value


def _optional_string_list(arguments: dict[str, Any], key: str) -> list[str]:
    """Return one optional list of strings or an empty list when omitted."""

    value = arguments.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"Chat action argument '{key}' must be a list of strings.")
    return [item.strip() for item in value if item.strip()]


def _schema_object(
    *,
    properties: dict[str, Any],
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build a strict JSON schema object definition for one tool input payload."""

    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        schema["required"] = list(required)
    return schema


def _string_property(description: str) -> dict[str, Any]:
    """Build a required string property schema."""

    return {
        "type": "string",
        "minLength": 1,
        "description": description,
    }


def _optional_string_property(description: str) -> dict[str, Any]:
    """Build an optional string-or-null property schema."""

    return {
        "type": ["string", "null"],
        "minLength": 1,
        "description": description,
    }


def _boolean_property(description: str) -> dict[str, Any]:
    """Build a boolean property schema."""

    return {
        "type": "boolean",
        "description": description,
    }


def _uuid_property(description: str) -> dict[str, Any]:
    """Build a UUID string property schema."""

    return {
        "type": "string",
        "format": "uuid",
        "description": description,
    }


def _uuid_or_null_property(description: str) -> dict[str, Any]:
    """Build an optional UUID string property schema."""

    return {
        "type": ["string", "null"],
        "format": "uuid",
        "description": description,
    }


def _uuid_array_property(description: str) -> dict[str, Any]:
    """Build a UUID string array property schema."""

    return {
        "type": "array",
        "description": description,
        "items": {
            "type": "string",
            "format": "uuid",
        },
    }


def _supporting_schedule_row_payload_schema() -> dict[str, Any]:
    """Return a typed JSON schema for Step 6 supporting-schedule rows."""

    return {
        "oneOf": [
            _schema_object(
                properties={
                    "asset_id": _string_property("Stable asset reference."),
                    "asset_name": _string_property("Display asset name."),
                    "acquisition_date": _string_property("Acquisition date in YYYY-MM-DD format."),
                    "asset_account_code": _string_property("Fixed-asset ledger account code."),
                    "accumulated_depreciation_account_code": _string_property(
                        "Accumulated depreciation ledger account code."
                    ),
                    "cost": _string_property("Asset cost as a decimal string."),
                    "accumulated_depreciation": _string_property(
                        "Accumulated depreciation as a decimal string."
                    ),
                    "net_book_value": _optional_string_property(
                        "Optional net book value as a decimal string."
                    ),
                    "depreciation_expense": _optional_string_property(
                        "Optional current-period depreciation expense."
                    ),
                    "disposal_date": _optional_string_property(
                        "Optional disposal date in YYYY-MM-DD format."
                    ),
                    "notes": _optional_string_property("Optional operator note."),
                },
                required=(
                    "asset_id",
                    "asset_name",
                    "acquisition_date",
                    "asset_account_code",
                    "accumulated_depreciation_account_code",
                    "cost",
                    "accumulated_depreciation",
                ),
            ),
            _schema_object(
                properties={
                    "loan_id": _string_property("Stable loan reference."),
                    "lender_name": _string_property("Lender display name."),
                    "payment_no": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Sequential payment number.",
                    },
                    "due_date": _string_property("Payment due date in YYYY-MM-DD format."),
                    "loan_account_code": _string_property("Loan-balance ledger account code."),
                    "interest_account_code": _string_property("Interest ledger account code."),
                    "principal": _string_property("Scheduled principal amount."),
                    "interest": _string_property("Scheduled interest amount."),
                    "balance": _string_property("Outstanding balance after this payment."),
                    "notes": _optional_string_property("Optional operator note."),
                },
                required=(
                    "loan_id",
                    "lender_name",
                    "payment_no",
                    "due_date",
                    "loan_account_code",
                    "interest_account_code",
                    "principal",
                    "interest",
                    "balance",
                ),
            ),
            _schema_object(
                properties={
                    "ref": _string_property("Stable accrual reference."),
                    "description": _string_property("Accrual description."),
                    "account_code": _string_property("Accrual ledger account code."),
                    "amount": _string_property("Expected accrual amount."),
                    "period": _string_property("Accounting period in YYYY-MM format."),
                    "reversal_date": _optional_string_property(
                        "Optional reversal date in YYYY-MM-DD format."
                    ),
                    "counterparty": _optional_string_property(
                        "Optional counterparty or contract reference."
                    ),
                    "notes": _optional_string_property("Optional operator note."),
                },
                required=("ref", "description", "account_code", "amount", "period"),
            ),
            _schema_object(
                properties={
                    "account_code": _string_property("Budget account code."),
                    "period": _string_property("Budget period in YYYY-MM format."),
                    "budget_amount": _string_property("Budget amount as a decimal string."),
                    "department": _optional_string_property("Optional department dimension."),
                    "cost_centre": _optional_string_property("Optional cost-centre dimension."),
                    "project": _optional_string_property("Optional project dimension."),
                    "notes": _optional_string_property("Optional operator note."),
                },
                required=("account_code", "period", "budget_amount"),
            ),
        ],
        "description": "Typed Step 6 schedule-row payload.",
    }


def _enum_string_property(*, values: tuple[str, ...], description: str) -> dict[str, Any]:
    """Build a constrained string property schema."""

    return {
        "type": "string",
        "enum": list(values),
        "description": description,
    }


def _enum_array_property(*, values: tuple[str, ...], description: str) -> dict[str, Any]:
    """Build an enum-array property schema."""

    return {
        "type": "array",
        "description": description,
        "items": {
            "type": "string",
            "enum": list(values),
        },
    }
