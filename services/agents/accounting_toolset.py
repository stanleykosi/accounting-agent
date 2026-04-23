"""
Purpose: Register the accounting-system deterministic tools exposed through the
generic agent kernel.
Scope: Tool registration, accounting workflow execution, and close-run scoped
state mutations with canonical service calls.
Dependencies: Accounting workflow services, repositories, and durable job dispatch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from services.accounting.recommendation_apply import ActorContext, RecommendationApplyService
from services.agents.models import AgentExecutionContext, AgentToolDefinition
from services.agents.registry import ToolRegistry
from services.chat.continuation_state import (
    ChatOperatorContinuation,
    embed_continuation_in_checkpoint,
    new_chat_operator_continuation,
)
from services.close_runs.delete_service import CloseRunDeleteService
from services.close_runs.service import CloseRunService
from services.close_runs.workflow_guards import require_active_phase
from services.common.enums import (
    CANONICAL_WORKFLOW_PHASES,
    DEFAULT_RECONCILIATION_EXECUTION_TYPES,
    CloseRunStatus,
    DispositionAction,
    ReconciliationType,
    ReviewStatus,
    SupportingScheduleStatus,
    SupportingScheduleType,
    WorkflowPhase,
)
from services.contracts.close_run_models import CreateCloseRunRequest
from services.contracts.entity_models import CreateEntityRequest, UpdateEntityRequest
from services.contracts.export_models import (
    EXPORT_DELIVERY_CHANNELS,
    DistributeExportRequest,
)
from services.contracts.journal_models import JOURNAL_POSTING_TARGETS
from services.db.models.audit import AuditSourceSurface
from services.db.models.documents import Document
from services.db.models.extractions import DocumentExtraction, ExtractedField
from services.db.models.recommendations import Recommendation
from services.db.models.reporting import ReportRunStatus as ReportRunStatusModel
from services.db.repositories.document_repo import DocumentRepository
from services.db.repositories.entity_repo import EntityRepository, EntityUserRecord
from services.db.repositories.recommendation_journal_repo import RecommendationJournalRepository
from services.db.repositories.reconciliation_repo import ReconciliationRepository
from services.db.repositories.report_repo import ReportRepository
from services.documents.imported_ledger_representation import (
    evaluate_documents_imported_gl_representation,
)
from services.documents.recommendation_eligibility import (
    GL_CODING_RECOMMENDATION_ELIGIBLE_TYPE_VALUES,
)
from services.documents.review_service import DocumentReviewService
from services.entity.delete_service import EntityDeleteService
from services.entity.service import EntityService
from services.exports.service import ExportService
from services.jobs.service import JobService
from services.jobs.task_names import TaskName
from services.reconciliation.service import ReconciliationService
from services.reporting.service import ReportService
from services.supporting_schedules.service import SupportingScheduleService
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class PreparedMutationScope:
    """Describe the working close-run scope selected for a mutation."""

    close_run_id: UUID
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ReportGenerationDispatch:
    """Describe one queued report-generation job together with its report run."""

    report_run: Any
    job_id: str
    job_status: str
    continuation_group_id: str | None


@dataclass(frozen=True, slots=True)
class AsyncToolDispatch:
    """Describe one queued async job started from a chat-owned tool invocation."""

    job_id: str
    job_status: str
    continuation_group_id: str | None


@dataclass(frozen=True, slots=True)
class OperatorToolNamespace:
    """Describe one internal operator specialist domain."""

    name: str
    label: str
    specialist_name: str
    specialist_mission: str


_RELEASED_CLOSE_RUN_STATUSES = frozenset(
    {
        CloseRunStatus.APPROVED,
        CloseRunStatus.EXPORTED,
        CloseRunStatus.ARCHIVED,
    }
)

_STAGED_CHAT_CONFIRMATION_TOOLS = frozenset(
    {
        "approve_close_run",
        "archive_close_run",
        "distribute_export",
        "delete_close_run",
        "delete_workspace",
    }
)

_TOOL_NAMESPACES = {
    "workspace_admin": OperatorToolNamespace(
        name="workspace_admin",
        label="Workspace Admin",
        specialist_name="Workspace Steward",
        specialist_mission=(
            "Owns workspace lifecycle, settings, and safe cross-workspace administration."
        ),
    ),
    "close_operator": OperatorToolNamespace(
        name="close_operator",
        label="Close Operations",
        specialist_name="Close Run Operator",
        specialist_mission=(
            "Owns close-run lifecycle, phase movement, sign-off, archive, and reopen control."
        ),
    ),
    "document_control": OperatorToolNamespace(
        name="document_control",
        label="Document Control",
        specialist_name="Document Controller",
        specialist_mission=(
            "Owns source-document intake, review, extraction correction, and collection readiness."
        ),
    ),
    "treatment_and_journals": OperatorToolNamespace(
        name="treatment_and_journals",
        label="Treatment and Journals",
        specialist_name="Journal Specialist",
        specialist_mission=(
            "Owns accounting treatment, recommendation review, and journal approval/application."
        ),
    ),
    "reconciliation_control": OperatorToolNamespace(
        name="reconciliation_control",
        label="Reconciliation Control",
        specialist_name="Reconciliation Analyst",
        specialist_mission=(
            "Owns reconciliation runs, exception disposition, and anomaly resolution."
        ),
    ),
    "reporting_and_release": OperatorToolNamespace(
        name="reporting_and_release",
        label="Reporting and Release",
        specialist_name="Reporting Controller",
        specialist_mission=(
            "Owns supporting schedules, commentary, reporting, export packaging, "
            "evidence packs, and release records."
        ),
    ),
}


class AccountingToolset:
    """Provide the typed accounting tool registry used by the generic agent kernel."""

    def __init__(
        self,
        *,
        db_session: Session,
        close_run_service: CloseRunService,
        close_run_delete_service: CloseRunDeleteService,
        document_review_service: DocumentReviewService,
        document_repository: DocumentRepository,
        entity_repository: EntityRepository,
        entity_service: EntityService,
        entity_delete_service: EntityDeleteService,
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
        self._close_run_delete_service = close_run_delete_service
        self._document_review_service = document_review_service
        self._document_repo = document_repository
        self._entity_repo = entity_repository
        self._entity_service = entity_service
        self._entity_delete_service = entity_delete_service
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

    def requires_human_approval_for_invocation(
        self,
        *,
        tool_name: str,
        tool_arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> bool:
        """Resolve whether one concrete tool invocation must stage for approval."""

        del tool_arguments, context
        return tool_name in _STAGED_CHAT_CONFIRMATION_TOOLS

    def _build_chat_continuation(
        self,
        *,
        context: AgentExecutionContext,
        originating_tool: str,
    ) -> ChatOperatorContinuation | None:
        """Return chat-owned continuation metadata when the execution context is resumable."""

        if context.thread_id is None:
            return None
        if context.operator_objective is None or not context.operator_objective.strip():
            return None

        raw_source_surface = context.source_surface
        source_surface = (
            raw_source_surface.value
            if isinstance(raw_source_surface, AuditSourceSurface)
            else str(raw_source_surface)
        )
        return new_chat_operator_continuation(
            thread_id=context.thread_id,
            entity_id=context.entity_id,
            actor_user_id=self._require_actor(context).id,
            objective=context.operator_objective,
            originating_tool=originating_tool,
            source_surface=source_surface,
        )

    def _build_async_group_result(
        self,
        *,
        continuation: ChatOperatorContinuation | None,
        job_count: int,
    ) -> dict[str, Any] | None:
        """Return compact async-group metadata for chat execution results."""

        if continuation is None or job_count <= 0:
            return None
        return {
            "continuation_group_id": str(continuation.continuation_group_id),
            "job_count": job_count,
        }

    def build_registry(self) -> ToolRegistry:
        """Return the registered accounting tool registry."""

        registry = ToolRegistry()
        self._register(
            registry=registry,
            name="switch_workspace",
            namespace="workspace_admin",
            prompt_signature="switch_workspace(workspace_id)",
            description=(
                "Move the current conversation onto another accessible workspace and clear any "
                "close-run scope."
            ),
            intent="workflow_action",
            requires_human_approval=False,
            executor=self._switch_workspace,
            target_type="workspace",
            target_id_field="workspace_id",
            input_schema=_schema_object(
                properties={
                    "workspace_id": _uuid_property("Accessible workspace UUID to switch into."),
                },
                required=("workspace_id",),
            ),
        )
        self._register(
            registry=registry,
            name="create_workspace",
            namespace="workspace_admin",
            prompt_signature=(
                "create_workspace(name, legal_name?, base_currency?, country_code?, timezone?, "
                "accounting_standard?, autonomy_mode?)"
            ),
            description=(
                "Create another entity workspace for the current operator with canonical "
                "workspace settings."
            ),
            intent="workflow_action",
            requires_human_approval=False,
            executor=self._create_workspace,
            input_schema=_schema_object(
                properties={
                    "name": _string_property("Workspace display name."),
                    "legal_name": _optional_string_property(
                        "Optional legal entity name for reporting."
                    ),
                    "base_currency": _optional_string_property(
                        "Optional three-letter base currency code."
                    ),
                    "country_code": _optional_string_property(
                        "Optional two-letter country code."
                    ),
                    "timezone": _optional_string_property(
                        "Optional IANA timezone identifier."
                    ),
                    "accounting_standard": _optional_string_property(
                        "Optional accounting standard label."
                    ),
                    "autonomy_mode": _enum_string_property(
                        values=("human_review", "reduced_interruption"),
                        description="Optional approval-routing mode for the new workspace.",
                    ),
                },
                required=("name",),
            ),
        )
        self._register(
            registry=registry,
            name="update_workspace",
            namespace="workspace_admin",
            prompt_signature=(
                "update_workspace(workspace_id?, name?, legal_name?, base_currency?, "
                "country_code?, timezone?, accounting_standard?, autonomy_mode?)"
            ),
            description="Update one accessible workspace's settings.",
            intent="proposed_edit",
            requires_human_approval=False,
            executor=self._update_workspace,
            target_type="workspace",
            target_id_field="workspace_id",
            input_schema=_schema_object(
                properties={
                    "workspace_id": _uuid_or_null_property(
                        "Optional workspace UUID. Defaults to the current workspace."
                    ),
                    "name": _optional_string_property("Updated workspace display name."),
                    "legal_name": _optional_string_property(
                        "Updated legal entity name."
                    ),
                    "base_currency": _optional_string_property(
                        "Updated three-letter base currency code."
                    ),
                    "country_code": _optional_string_property(
                        "Updated two-letter country code."
                    ),
                    "timezone": _optional_string_property(
                        "Updated IANA timezone identifier."
                    ),
                    "accounting_standard": _optional_string_property(
                        "Updated accounting standard label."
                    ),
                    "autonomy_mode": _enum_string_property(
                        values=("human_review", "reduced_interruption"),
                        description="Updated approval-routing mode for the workspace.",
                    ),
                },
            ),
        )
        self._register(
            registry=registry,
            name="delete_workspace",
            namespace="workspace_admin",
            prompt_signature="delete_workspace(workspace_id)",
            description=(
                "Delete one accessible workspace that is not the workspace anchoring the current "
                "chat thread."
            ),
            intent="workflow_action",
            requires_human_approval=True,
            executor=self._delete_workspace,
            target_type="workspace",
            target_id_field="workspace_id",
            input_schema=_schema_object(
                properties={
                    "workspace_id": _uuid_property("Workspace UUID to delete."),
                },
                required=("workspace_id",),
            ),
        )
        self._register(
            registry=registry,
            name="review_document",
            namespace="document_control",
            prompt_signature=(
                "review_document("
                "document_id, decision: approved|rejected|needs_info, reason?"
                ")"
            ),
            description="Persist a document review decision and update its workflow state.",
            intent="proposed_edit",
            requires_human_approval=False,
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
            name="ignore_document",
            namespace="document_control",
            prompt_signature="ignore_document(document_id, reason?)",
            description=(
                "Mark a mistaken upload as ignored for this close run. If source-document "
                "intake is already complete, this rewinds the run back to Collection first "
                "so the document can be excluded canonically."
            ),
            intent="proposed_edit",
            requires_human_approval=False,
            executor=self._ignore_document,
            target_type="document",
            target_id_field="document_id",
            input_schema=_schema_object(
                properties={
                    "document_id": _uuid_property("Document UUID to ignore for this close run."),
                    "reason": _optional_string_property(
                        "Optional operator rationale for ignoring the uploaded document."
                    ),
                },
                required=("document_id",),
            ),
        )
        self._register(
            registry=registry,
            name="correct_extracted_field",
            namespace="document_control",
            prompt_signature=(
                "correct_extracted_field("
                "field_id, corrected_value, corrected_type, reason?"
                ")"
            ),
            description=(
                "Correct one extracted field and return the document to review with "
                "audit history."
            ),
            intent="proposed_edit",
            requires_human_approval=False,
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
            name="create_close_run",
            namespace="close_operator",
            prompt_signature=(
                "create_close_run(workspace_id?, period_start, period_end, reporting_currency?, "
                "allow_duplicate_period?, duplicate_period_reason?)"
            ),
            description=(
                "Create a fresh close run for an entity period. Use this when the operator "
                "asks to start a new run, a fresh monthly close, or another run for a period."
            ),
            intent="workflow_action",
            requires_human_approval=False,
            executor=self._create_close_run,
            input_schema=_schema_object(
                properties={
                    "workspace_id": _uuid_or_null_property(
                        "Optional accessible workspace UUID. Defaults to the current workspace."
                    ),
                    "period_start": _date_property(
                        "First day of the close-run period in YYYY-MM-DD format."
                    ),
                    "period_end": _date_property(
                        "Last day of the close-run period in YYYY-MM-DD format."
                    ),
                    "reporting_currency": _optional_string_property(
                        "Optional three-letter reporting currency code."
                    ),
                    "allow_duplicate_period": _boolean_property(
                        "Set true only when the operator explicitly wants another open "
                        "run for the same period."
                    ),
                    "duplicate_period_reason": _optional_string_property(
                        "Required rationale when allow_duplicate_period is true."
                    ),
                },
                required=("period_start", "period_end"),
            ),
        )
        self._register(
            registry=registry,
            name="delete_close_run",
            namespace="close_operator",
            prompt_signature="delete_close_run(close_run_id?)",
            description=(
                "Delete one mutable close run from the current workspace. Defaults to the "
                "current close run when the target is unambiguous."
            ),
            intent="workflow_action",
            requires_human_approval=True,
            executor=self._delete_close_run,
            target_type="close_run",
            target_id_field="close_run_id",
            input_schema=_schema_object(
                properties={
                    "close_run_id": _uuid_or_null_property(
                        "Optional close run UUID to delete."
                    ),
                },
            ),
        )
        self._register(
            registry=registry,
            name="advance_close_run",
            namespace="close_operator",
            prompt_signature="advance_close_run(target_phase, reason?)",
            description=(
                "Advance the close run into the next workflow phase after gate "
                "checks pass."
            ),
            intent="workflow_action",
            requires_human_approval=False,
            executor=self._advance_close_run,
            input_schema=_schema_object(
                properties={
                    "target_phase": _enum_string_property(
                        values=tuple(phase.value for phase in WorkflowPhase),
                        description="Workflow phase to transition the close run into.",
                    ),
                    "reason": _optional_string_property(
                        "Optional operator reason for the transition."
                    ),
                },
                required=("target_phase",),
            ),
        )
        self._register(
            registry=registry,
            name="rewind_close_run",
            namespace="close_operator",
            prompt_signature="rewind_close_run(target_phase, reason?)",
            description=(
                "Move a mutable close run back into an earlier workflow phase so the "
                "operator can resume or correct upstream work."
            ),
            intent="workflow_action",
            requires_human_approval=False,
            executor=self._rewind_close_run,
            input_schema=_schema_object(
                properties={
                    "target_phase": _enum_string_property(
                        values=tuple(phase.value for phase in WorkflowPhase),
                        description="Earlier workflow phase to reopen.",
                    ),
                    "reason": _optional_string_property("Optional operator reason for the rewind."),
                },
                required=("target_phase",),
            ),
        )
        self._register(
            registry=registry,
            name="approve_close_run",
            namespace="close_operator",
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
            name="archive_close_run",
            namespace="close_operator",
            prompt_signature="archive_close_run(reason?)",
            description="Archive an approved or exported close run while preserving its history.",
            intent="approval_request",
            requires_human_approval=True,
            executor=self._archive_close_run,
            input_schema=_schema_object(
                properties={
                    "reason": _optional_string_property(
                        "Optional operator rationale for archiving."
                    ),
                },
            ),
        )
        self._register(
            registry=registry,
            name="reopen_close_run",
            namespace="close_operator",
            prompt_signature="reopen_close_run(reason?)",
            description=(
                "Create a reopened working version of an approved, exported, or archived close run "
                "so the operator can continue making changes."
            ),
            intent="workflow_action",
            requires_human_approval=False,
            executor=self._reopen_close_run,
            input_schema=_schema_object(
                properties={
                    "reason": _optional_string_property(
                        "Optional operator rationale for reopening the close run."
                    ),
                },
            ),
        )
        self._register(
            registry=registry,
            name="generate_recommendations",
            namespace="treatment_and_journals",
            prompt_signature="generate_recommendations(force?, document_ids?)",
            description="Queue accounting recommendation generation jobs for eligible documents.",
            intent="workflow_action",
            requires_human_approval=False,
            executor=self._generate_recommendations,
            input_schema=_schema_object(
                properties={
                    "force": _boolean_property(
                        "Recompute recommendations even when one already exists."
                    ),
                    "document_ids": _uuid_array_property(
                        "Optional subset of document UUIDs to process."
                    ),
                },
            ),
        )
        self._register(
            registry=registry,
            name="approve_recommendation",
            namespace="treatment_and_journals",
            prompt_signature="approve_recommendation(recommendation_id, reason?)",
            description=(
                "Approve one accounting recommendation and optionally create its "
                "journal draft."
            ),
            intent="approval_request",
            requires_human_approval=False,
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
            namespace="treatment_and_journals",
            prompt_signature="reject_recommendation(recommendation_id, reason)",
            description="Reject one accounting recommendation with a reviewer reason.",
            intent="approval_request",
            requires_human_approval=False,
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
            namespace="treatment_and_journals",
            prompt_signature="approve_journal(journal_id, reason?)",
            description="Approve one journal draft for the current close run.",
            intent="approval_request",
            requires_human_approval=False,
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
            namespace="treatment_and_journals",
            prompt_signature="apply_journal(journal_id, posting_target, reason?)",
            description=(
                "Post one approved journal draft either internally or as an "
                "external ERP package."
            ),
            intent="approval_request",
            requires_human_approval=False,
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
            namespace="treatment_and_journals",
            prompt_signature="reject_journal(journal_id, reason)",
            description="Reject one journal draft with a reviewer reason.",
            intent="approval_request",
            requires_human_approval=False,
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
            namespace="reconciliation_control",
            prompt_signature="run_reconciliation(reconciliation_types?)",
            description="Queue reconciliation execution for one or more reconciliation types.",
            intent="reconciliation_query",
            requires_human_approval=False,
            executor=self._run_reconciliation,
            input_schema=_schema_object(
                properties={
                    "reconciliation_types": _enum_array_property(
                        values=tuple(
                            reconciliation_type.value for reconciliation_type in ReconciliationType
                        ),
                        description="Optional subset of reconciliation types to execute.",
                    ),
                },
            ),
        )
        self._register(
            registry=registry,
            name="upsert_supporting_schedule_row",
            namespace="reporting_and_release",
            prompt_signature="upsert_supporting_schedule_row(schedule_type, row_payload, row_id?)",
            description="Create or update one Step 6 supporting-schedule workpaper row.",
            intent="proposed_edit",
            requires_human_approval=False,
            executor=self._upsert_supporting_schedule_row,
            input_schema=_schema_object(
                properties={
                    "schedule_type": _enum_string_property(
                        values=tuple(
                            schedule_type.value for schedule_type in SupportingScheduleType
                        ),
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
            namespace="reporting_and_release",
            prompt_signature="delete_supporting_schedule_row(schedule_type, row_id)",
            description="Delete one Step 6 supporting-schedule workpaper row.",
            intent="proposed_edit",
            requires_human_approval=False,
            executor=self._delete_supporting_schedule_row,
            input_schema=_schema_object(
                properties={
                    "schedule_type": _enum_string_property(
                        values=tuple(
                            schedule_type.value for schedule_type in SupportingScheduleType
                        ),
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
            namespace="reporting_and_release",
            prompt_signature="set_supporting_schedule_status(schedule_type, status, note?)",
            description="Review, approve, or mark a Step 6 supporting schedule not applicable.",
            intent="approval_request",
            requires_human_approval=False,
            executor=self._set_supporting_schedule_status,
            input_schema=_schema_object(
                properties={
                    "schedule_type": _enum_string_property(
                        values=tuple(
                            schedule_type.value for schedule_type in SupportingScheduleType
                        ),
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
            namespace="reconciliation_control",
            prompt_signature="approve_reconciliation(reconciliation_id, reason?)",
            description="Approve one reconciliation result after reviewing its disposition.",
            intent="reconciliation_query",
            requires_human_approval=False,
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
            name="disposition_reconciliation_item",
            namespace="reconciliation_control",
            prompt_signature=(
                "disposition_reconciliation_item("
                "item_id, disposition: resolved|adjusted|accepted_as_is|escalated|pending_info, "
                "reason)"
            ),
            description=(
                "Record the reviewer disposition for one reconciliation exception item."
            ),
            intent="reconciliation_query",
            requires_human_approval=False,
            executor=self._disposition_reconciliation_item,
            target_type="reconciliation_item",
            target_id_field="item_id",
            input_schema=_schema_object(
                properties={
                    "item_id": _uuid_property(
                        "Reconciliation item UUID that needs reviewer disposition."
                    ),
                    "disposition": _enum_string_property(
                        values=tuple(
                            disposition.value for disposition in DispositionAction
                        ),
                        description="Disposition outcome to persist for the selected item.",
                    ),
                    "reason": _string_property(
                        "Reviewer rationale for the recorded disposition."
                    ),
                },
                required=("item_id", "disposition", "reason"),
            ),
        )
        self._register(
            registry=registry,
            name="resolve_reconciliation_anomaly",
            namespace="reconciliation_control",
            prompt_signature="resolve_reconciliation_anomaly(anomaly_id, resolution_note)",
            description=(
                "Resolve one reconciliation anomaly after the reviewer verifies the issue."
            ),
            intent="reconciliation_query",
            requires_human_approval=False,
            executor=self._resolve_reconciliation_anomaly,
            target_type="reconciliation_anomaly",
            target_id_field="anomaly_id",
            input_schema=_schema_object(
                properties={
                    "anomaly_id": _uuid_property("Reconciliation anomaly UUID to resolve."),
                    "resolution_note": _string_property(
                        "Reviewer note explaining why the anomaly is resolved."
                    ),
                },
                required=("anomaly_id", "resolution_note"),
            ),
        )
        self._register(
            registry=registry,
            name="generate_reports",
            namespace="reporting_and_release",
            prompt_signature=(
                "generate_reports("
                "template_id?, generate_commentary?, use_llm_commentary?"
                ")"
            ),
            description=(
                "Create a report run and queue report generation for the current "
                "close run."
            ),
            intent="report_action",
            requires_human_approval=False,
            executor=self._generate_reports,
            input_schema=_schema_object(
                properties={
                    "template_id": _uuid_or_null_property(
                        "Optional report template UUID override."
                    ),
                    "generate_commentary": _boolean_property(
                        "Whether to draft commentary alongside the report run."
                    ),
                    "use_llm_commentary": _boolean_property(
                        "Whether to use the model-generated commentary path."
                    ),
                },
            ),
        )
        self._register(
            registry=registry,
            name="generate_export",
            namespace="reporting_and_release",
            prompt_signature=(
                "generate_export("
                "include_evidence_pack?, include_audit_trail?, action_qualifier?"
                ")"
            ),
            description=(
                "Generate the export manifest and released artifact bundle for the "
                "close run."
            ),
            intent="report_action",
            requires_human_approval=False,
            executor=self._generate_export,
            input_schema=_schema_object(
                properties={
                    "include_evidence_pack": _boolean_property(
                        "Whether to include the latest evidence pack."
                    ),
                    "include_audit_trail": _boolean_property(
                        "Whether to include audit trail records."
                    ),
                    "action_qualifier": _optional_string_property(
                        "Optional qualifier used for idempotent export naming."
                    ),
                },
            ),
        )
        self._register(
            registry=registry,
            name="assemble_evidence_pack",
            namespace="reporting_and_release",
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
            namespace="reporting_and_release",
            prompt_signature=(
                "distribute_export(export_id, recipient_name, recipient_email, "
                "recipient_role?, delivery_channel?, note?)"
            ),
            description=(
                "Record distribution of a completed export package to management "
                "stakeholders."
            ),
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
            namespace="reporting_and_release",
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
            namespace="reporting_and_release",
            prompt_signature="approve_commentary(report_run_id, section_key, body?, reason?)",
            description="Approve one commentary section for release in the report run.",
            intent="report_action",
            requires_human_approval=False,
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
        namespace: str,
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

        namespace_definition = _TOOL_NAMESPACES.get(namespace)
        if namespace_definition is None:
            raise ValueError(f"Unknown operator tool namespace: {namespace}")

        registry.register_tool(
            definition=AgentToolDefinition(
                name=name,
                namespace=namespace_definition.name,
                namespace_label=namespace_definition.label,
                specialist_name=namespace_definition.specialist_name,
                specialist_mission=namespace_definition.specialist_mission,
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
        close_run_id = self._require_close_run_id(
            context,
            "Document review requires a close-run-scoped thread.",
        )
        source_close_run_id = self._source_close_run_id(
            context=context,
            current_close_run_id=close_run_id,
        )
        reason = _optional_string(arguments, "reason")
        prepared = self._prepare_phase_mutation_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.COLLECTION,
            action_label="Document review",
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        document_id = self._resolve_document_id_for_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            source_close_run_id=source_close_run_id,
            target_close_run_id=prepared.close_run_id,
            document_id=UUID(_require_string(arguments, "document_id")),
        )
        result = self._document_review_service.review_document(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=prepared.close_run_id,
            document_id=document_id,
            decision=_require_string(arguments, "decision"),
            reason=reason,
            verified_complete=_optional_bool(arguments, "verified_complete"),
            verified_authorized=_optional_bool(arguments, "verified_authorized"),
            verified_period=_optional_bool(arguments, "verified_period"),
            verified_transaction_match=_optional_bool(arguments, "verified_transaction_match"),
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        return self._with_scope_metadata(
            prepared=prepared,
            result={
                "tool": "review_document",
                "document_id": result.document.id,
                "document_filename": result.document.original_filename,
                "status": result.document.status.value,
                "decision": result.decision,
            },
        )

    def _ignore_document(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context,
            "Ignoring a document requires a close-run-scoped thread.",
        )
        source_close_run_id = self._source_close_run_id(
            context=context,
            current_close_run_id=close_run_id,
        )
        reason = _optional_string(arguments, "reason")
        source_document_id = UUID(_require_string(arguments, "document_id"))
        prepared = self._prepare_phase_mutation_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.COLLECTION,
            action_label="Ignoring a mistaken upload",
            workflow_reason=reason,
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        document_id = self._resolve_document_id_for_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            source_close_run_id=source_close_run_id,
            target_close_run_id=prepared.close_run_id,
            document_id=source_document_id,
        )
        result = self._document_review_service.review_document(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=prepared.close_run_id,
            document_id=document_id,
            decision="rejected",
            reason=reason or "Ignored as a mistaken upload.",
            verified_complete=None,
            verified_authorized=None,
            verified_period=None,
            verified_transaction_match=None,
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        return self._with_scope_metadata(
            prepared=prepared,
            result={
                "tool": "ignore_document",
                "document_id": result.document.id,
                "document_filename": result.document.original_filename,
                "status": result.document.status.value,
                "decision": result.decision,
            },
        )

    def _correct_extracted_field(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context,
            "Field correction requires a close-run-scoped thread.",
        )
        source_close_run_id = self._source_close_run_id(
            context=context,
            current_close_run_id=close_run_id,
        )
        reason = _optional_string(arguments, "reason")
        prepared = self._prepare_phase_mutation_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.COLLECTION,
            action_label="Extracted-field correction",
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        field_id = self._resolve_field_id_for_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            source_close_run_id=source_close_run_id,
            target_close_run_id=prepared.close_run_id,
            field_id=UUID(_require_string(arguments, "field_id")),
        )
        result = self._document_review_service.correct_extracted_field(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=prepared.close_run_id,
            field_id=field_id,
            corrected_value=_require_string(arguments, "corrected_value"),
            corrected_type=_require_string(arguments, "corrected_type"),
            reason=reason,
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        return self._with_scope_metadata(
            prepared=prepared,
            result={
                "tool": "correct_extracted_field",
                "field_id": result.field.id,
                "document_id": result.document.id,
                "document_status": result.document.status.value,
            },
        )

    def _create_workspace(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        payload = CreateEntityRequest.model_validate(arguments)
        result = self._entity_service.create_entity(
            actor_user=actor_user,
            name=payload.name,
            legal_name=payload.legal_name,
            base_currency=payload.base_currency,
            country_code=payload.country_code,
            timezone=payload.timezone,
            accounting_standard=payload.accounting_standard,
            autonomy_mode=payload.autonomy_mode,
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        return {
            "tool": "create_workspace",
            "created_workspace_id": result.id,
            "workspace_id": result.id,
            "workspace_name": result.name,
            "base_currency": result.base_currency,
            "autonomy_mode": result.autonomy_mode.value,
        }

    def _switch_workspace(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        if context.close_run_id is not None:
            raise ValueError(
                "Switch workspaces from the global assistant or a workspace-level assistant, "
                "not from a close-run-scoped thread."
            )

        workspace_id = UUID(_require_string(arguments, "workspace_id"))
        access = self._entity_repo.get_entity_for_user(
            entity_id=workspace_id,
            user_id=actor_user.id,
        )
        if access is None:
            raise ValueError("That workspace is not accessible to the current operator.")

        return {
            "tool": "switch_workspace",
            "switched_workspace_id": access.entity.id,
            "workspace_id": access.entity.id,
            "workspace_name": access.entity.name,
            "base_currency": access.entity.base_currency,
            "autonomy_mode": access.entity.autonomy_mode.value,
        }

    def _update_workspace(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        workspace_id = (
            UUID(_require_string(arguments, "workspace_id"))
            if isinstance(arguments.get("workspace_id"), str)
            else context.entity_id
        )
        raw_payload = {
            key: arguments[key]
            for key in (
                "name",
                "legal_name",
                "base_currency",
                "country_code",
                "timezone",
                "accounting_standard",
                "autonomy_mode",
            )
            if key in arguments
        }
        payload = UpdateEntityRequest.model_validate(raw_payload)
        fields_to_update = frozenset(payload.model_dump(exclude_none=True).keys())
        result = self._entity_service.update_entity(
            actor_user=actor_user,
            entity_id=workspace_id,
            fields_to_update=fields_to_update,
            name=payload.name,
            legal_name=payload.legal_name,
            base_currency=payload.base_currency,
            country_code=payload.country_code,
            timezone=payload.timezone,
            accounting_standard=payload.accounting_standard,
            autonomy_mode=payload.autonomy_mode,
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        return {
            "tool": "update_workspace",
            "workspace_id": result.id,
            "workspace_name": result.name,
            "updated_fields": sorted(fields_to_update),
            "base_currency": result.base_currency,
            "autonomy_mode": result.autonomy_mode.value,
        }

    def _delete_workspace(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        workspace_id = UUID(_require_string(arguments, "workspace_id"))
        if workspace_id == context.entity_id:
            raise ValueError(
                "You cannot delete the workspace anchoring this chat thread from inside the same "
                "workspace chat. Switch to another workspace chat first, then ask me to delete "
                "this one."
            )
        result = self._entity_delete_service.delete_entity(
            actor_user=actor_user,
            entity_id=workspace_id,
        )
        return {
            "tool": "delete_workspace",
            "deleted_workspace_id": result.deleted_entity_id,
            "deleted_workspace_name": result.deleted_entity_name,
            "deleted_close_run_count": result.deleted_close_run_count,
            "deleted_document_count": result.deleted_document_count,
            "deleted_thread_count": result.deleted_thread_count,
            "canceled_job_count": result.canceled_job_count,
        }

    def _create_close_run(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        target_entity_id = (
            UUID(_require_string(arguments, "workspace_id"))
            if isinstance(arguments.get("workspace_id"), str)
            else context.entity_id
        )
        workspace_access = self._entity_repo.get_entity_for_user(
            entity_id=target_entity_id,
            user_id=actor_user.id,
        )
        if workspace_access is None:
            raise ValueError("That workspace is not accessible to the current operator.")
        payload = CreateCloseRunRequest.model_validate(
            {key: value for key, value in arguments.items() if key != "workspace_id"}
        )
        result = self._close_run_service.create_close_run(
            actor_user=actor_user,
            entity_id=target_entity_id,
            period_start=payload.period_start,
            period_end=payload.period_end,
            reporting_currency=payload.reporting_currency,
            allow_duplicate_period=payload.allow_duplicate_period,
            duplicate_period_reason=payload.duplicate_period_reason,
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        return {
            "tool": "create_close_run",
            "created_close_run_id": result.id,
            "created_workspace_id": str(target_entity_id),
            "workspace_id": str(target_entity_id),
            "workspace_name": workspace_access.entity.name,
            "close_run_id": result.id,
            "period_start": result.period_start.isoformat(),
            "period_end": result.period_end.isoformat(),
            "reporting_currency": result.reporting_currency,
            "active_phase": (
                result.workflow_state.active_phase.value
                if result.workflow_state.active_phase is not None
                else None
            ),
            "status": result.status.value,
            "version_no": result.current_version_no,
        }

    def _delete_close_run(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        target_close_run_id = (
            UUID(_require_string(arguments, "close_run_id"))
            if isinstance(arguments.get("close_run_id"), str)
            else self._require_close_run_id(
                context,
                "Close-run deletion requires a close-run target or a close-run-scoped thread.",
            )
        )
        result = self._close_run_delete_service.delete_close_run(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=target_close_run_id,
        )
        return {
            "tool": "delete_close_run",
            "deleted_close_run_id": result.deleted_close_run_id,
            "deleted_document_count": result.deleted_document_count,
            "deleted_recommendation_count": result.deleted_recommendation_count,
            "deleted_journal_count": result.deleted_journal_count,
            "deleted_report_run_count": result.deleted_report_run_count,
            "deleted_thread_count": result.deleted_thread_count,
            "canceled_job_count": result.canceled_job_count,
        }

    def _advance_close_run(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context, "Close-run transition requires a close-run-scoped thread."
        )
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

    def _rewind_close_run(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context,
            "Rewinding a close run requires a close-run-scoped thread.",
        )
        target_phase = WorkflowPhase(_require_string(arguments, "target_phase"))
        reason = _optional_string(arguments, "reason")
        prepared = self._prepare_phase_mutation_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=target_phase,
            action_label="Close-run resume",
            workflow_reason=reason,
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        return self._with_scope_metadata(
            prepared=prepared,
            result={
                "tool": "rewind_close_run",
                "close_run_id": str(prepared.close_run_id),
                "active_phase": target_phase.value,
                "status": CloseRunStatus.REOPENED.value
                if "reopened_close_run_id" in prepared.metadata
                else self._close_run_service.get_close_run(
                    actor_user=actor_user,
                    entity_id=context.entity_id,
                    close_run_id=prepared.close_run_id,
                ).status.value,
            },
        )

    def _approve_close_run(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context, "Close-run approval requires a close-run-scoped thread."
        )
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

    def _archive_close_run(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context,
            "Archiving a close run requires a close-run-scoped thread.",
        )
        result = self._close_run_service.archive_close_run(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            reason=_optional_string(arguments, "reason"),
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        return {
            "tool": "archive_close_run",
            "close_run_id": result.id,
            "status": result.status.value,
        }

    def _reopen_close_run(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context,
            "Reopening a close run requires a close-run-scoped thread.",
        )
        result = self._close_run_service.reopen_close_run(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            reason=_optional_string(arguments, "reason"),
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        return {
            "tool": "reopen_close_run",
            "source_close_run_id": result.source_close_run_id,
            "reopened_close_run_id": result.close_run.id,
            "status": result.close_run.status.value,
            "version_no": result.close_run.current_version_no,
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
        source_close_run_id = self._source_close_run_id(
            context=context,
            current_close_run_id=close_run_id,
        )
        source_document_ids = [
            UUID(document_id) for document_id in _optional_string_list(arguments, "document_ids")
        ]
        prepared = self._prepare_phase_mutation_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.PROCESSING,
            action_label="Recommendation generation",
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        document_ids = [
            self._resolve_document_id_for_scope(
                actor_user=actor_user,
                entity_id=context.entity_id,
                source_close_run_id=source_close_run_id,
                target_close_run_id=prepared.close_run_id,
                document_id=document_id,
            )
            for document_id in source_document_ids
        ]
        queued_jobs = self._queue_recommendation_jobs(
            entity_id=context.entity_id,
            close_run_id=prepared.close_run_id,
            actor_user=actor_user,
            document_ids=document_ids or None,
            force=bool(arguments.get("force", False)),
            context=context,
            trace_id=context.trace_id,
        )
        async_job_group = None
        if queued_jobs:
            first_group_id = queued_jobs[0].get("continuation_group_id")
            if isinstance(first_group_id, str):
                async_job_group = {
                    "continuation_group_id": first_group_id,
                    "job_count": len(queued_jobs),
                }
        return self._with_scope_metadata(
            prepared=prepared,
            result={
                "tool": "generate_recommendations",
                "queued_jobs": queued_jobs,
                "queued_count": len(queued_jobs),
                "async_job_group": async_job_group,
            },
        )

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
        source_close_run_id = self._source_close_run_id(
            context=context,
            current_close_run_id=close_run_id,
        )
        reason = _optional_string(arguments, "reason")
        prepared = self._prepare_phase_mutation_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.PROCESSING,
            action_label="Recommendation approval",
            workflow_reason=reason,
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        recommendation_id = self._resolve_recommendation_id_for_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            source_close_run_id=source_close_run_id,
            target_close_run_id=prepared.close_run_id,
            recommendation_id=UUID(_require_string(arguments, "recommendation_id")),
        )
        result = self._recommendation_service.approve_recommendation(
            recommendation_id=recommendation_id,
            entity_id=context.entity_id,
            close_run_id=prepared.close_run_id,
            actor=ActorContext(
                user_id=actor_user.id,
                full_name=actor_user.full_name,
                email=actor_user.email,
            ),
            reason=reason,
            trace_id=context.trace_id,
            source_surface=cast(AuditSourceSurface, context.source_surface),
        )
        return self._with_scope_metadata(
            prepared=prepared,
            result={
                "tool": "approve_recommendation",
                "recommendation_id": str(result.recommendation_id),
                "final_status": result.final_status.value,
                "journal_id": (
                    str(result.journal_draft_result.journal_id)
                    if result.journal_draft_result is not None
                    else None
                ),
            },
        )

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
        source_close_run_id = self._source_close_run_id(
            context=context,
            current_close_run_id=close_run_id,
        )
        reason = _require_string(arguments, "reason")
        prepared = self._prepare_phase_mutation_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.PROCESSING,
            action_label="Recommendation rejection",
            workflow_reason=reason,
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        recommendation_id = self._resolve_recommendation_id_for_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            source_close_run_id=source_close_run_id,
            target_close_run_id=prepared.close_run_id,
            recommendation_id=UUID(_require_string(arguments, "recommendation_id")),
        )
        self._recommendation_service.reject_recommendation(
            recommendation_id=recommendation_id,
            entity_id=context.entity_id,
            close_run_id=prepared.close_run_id,
            actor=ActorContext(
                user_id=actor_user.id,
                full_name=actor_user.full_name,
                email=actor_user.email,
            ),
            reason=reason,
            trace_id=context.trace_id,
            source_surface=cast(AuditSourceSurface, context.source_surface),
        )
        return self._with_scope_metadata(
            prepared=prepared,
            result={
                "tool": "reject_recommendation",
                "recommendation_id": str(recommendation_id),
                "status": ReviewStatus.REJECTED.value,
            },
        )

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
        prepared = self._prepare_phase_mutation_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.RECONCILIATION,
            action_label="Reconciliation execution",
            workflow_reason=_optional_string(arguments, "reason"),
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        reconciliation_types = [
            ReconciliationType(reconciliation_type)
            for reconciliation_type in _optional_string_list(arguments, "reconciliation_types")
        ]
        continuation = self._build_chat_continuation(
            context=context,
            originating_tool="run_reconciliation",
        )
        job = self._job_service.dispatch_job(
            dispatcher=self._task_dispatcher,
            task_name=TaskName.RECONCILIATION_EXECUTE_CLOSE_RUN,
            payload={
                "close_run_id": str(prepared.close_run_id),
                "reconciliation_types": [
                    reconciliation_type.value
                    for reconciliation_type in (
                        reconciliation_types or list(DEFAULT_RECONCILIATION_EXECUTION_TYPES)
                    )
                ],
                "actor_user_id": str(actor_user.id),
            },
            entity_id=context.entity_id,
            close_run_id=prepared.close_run_id,
            document_id=None,
            actor_user_id=actor_user.id,
            trace_id=context.trace_id,
            checkpoint_payload=(
                embed_continuation_in_checkpoint(
                    checkpoint_payload=None,
                    continuation=continuation,
                )
                if continuation is not None
                else None
            ),
        )
        return self._with_scope_metadata(
            prepared=prepared,
            result={
                "tool": "run_reconciliation",
                "job_id": str(job.id),
                "status": job.status.value,
                "async_job_group": self._build_async_group_result(
                    continuation=continuation,
                    job_count=1,
                ),
            },
        )

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
        source_close_run_id = self._source_close_run_id(
            context=context,
            current_close_run_id=close_run_id,
        )
        schedule_type = SupportingScheduleType(_require_string(arguments, "schedule_type"))
        prepared = self._prepare_phase_mutation_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.RECONCILIATION,
            action_label="Supporting schedule maintenance",
            workflow_reason=_optional_string(arguments, "reason"),
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        row_payload = arguments.get("row_payload")
        if not isinstance(row_payload, dict):
            raise ValueError("row_payload must be a structured object.")
        snapshot = self._supporting_schedule_service.save_row(
            close_run_id=prepared.close_run_id,
            schedule_type=schedule_type,
            row_id=(
                self._resolve_supporting_schedule_row_id_for_scope(
                    source_close_run_id=source_close_run_id,
                    target_close_run_id=prepared.close_run_id,
                    schedule_type=schedule_type,
                    row_id=UUID(_require_string(arguments, "row_id")),
                )
                if isinstance(arguments.get("row_id"), str)
                else None
            ),
            payload=row_payload,
        )
        return self._with_scope_metadata(
            prepared=prepared,
            result={
                "tool": "upsert_supporting_schedule_row",
                "schedule_type": schedule_type.value,
                "schedule_status": snapshot.schedule.status.value,
                "row_count": len(snapshot.rows),
            },
        )

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
        source_close_run_id = self._source_close_run_id(
            context=context,
            current_close_run_id=close_run_id,
        )
        schedule_type = SupportingScheduleType(_require_string(arguments, "schedule_type"))
        prepared = self._prepare_phase_mutation_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.RECONCILIATION,
            action_label="Supporting schedule maintenance",
            workflow_reason=_optional_string(arguments, "reason"),
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        snapshot = self._supporting_schedule_service.delete_row(
            close_run_id=prepared.close_run_id,
            schedule_type=schedule_type,
            row_id=self._resolve_supporting_schedule_row_id_for_scope(
                source_close_run_id=source_close_run_id,
                target_close_run_id=prepared.close_run_id,
                schedule_type=schedule_type,
                row_id=UUID(_require_string(arguments, "row_id")),
            ),
        )
        return self._with_scope_metadata(
            prepared=prepared,
            result={
                "tool": "delete_supporting_schedule_row",
                "schedule_type": schedule_type.value,
                "schedule_status": snapshot.schedule.status.value,
                "row_count": len(snapshot.rows),
            },
        )

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
        schedule_type = SupportingScheduleType(_require_string(arguments, "schedule_type"))
        prepared = self._prepare_phase_mutation_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.RECONCILIATION,
            action_label="Supporting schedule review",
            workflow_reason=_optional_string(arguments, "note"),
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        snapshot = self._supporting_schedule_service.update_status(
            close_run_id=prepared.close_run_id,
            schedule_type=schedule_type,
            status=SupportingScheduleStatus(_require_string(arguments, "status")),
            note=_optional_string(arguments, "note"),
            actor_user_id=actor_user.id,
        )
        return self._with_scope_metadata(
            prepared=prepared,
            result={
                "tool": "set_supporting_schedule_status",
                "schedule_type": schedule_type.value,
                "schedule_status": snapshot.schedule.status.value,
                "row_count": len(snapshot.rows),
                "reviewed_at": snapshot.schedule.reviewed_at,
            },
        )

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
        source_close_run_id = self._source_close_run_id(
            context=context,
            current_close_run_id=close_run_id,
        )
        reason = _optional_string(arguments, "reason")
        prepared = self._prepare_phase_mutation_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.RECONCILIATION,
            action_label="Reconciliation approval",
            workflow_reason=reason,
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        reconciliation_id = self._resolve_reconciliation_id_for_scope(
            source_close_run_id=source_close_run_id,
            target_close_run_id=prepared.close_run_id,
            reconciliation_id=UUID(_require_string(arguments, "reconciliation_id")),
        )
        result = self._reconciliation_service.approve_reconciliation(
            reconciliation_id=reconciliation_id,
            close_run_id=prepared.close_run_id,
            reason=reason,
            user_id=actor_user.id,
        )
        if result is None:
            raise ValueError("The reconciliation was not found for this close run.")
        return self._with_scope_metadata(
            prepared=prepared,
            result={
                "tool": "approve_reconciliation",
                "reconciliation_id": str(reconciliation_id),
                "reconciliation_type": result.reconciliation_type.value,
                "status": result.status.value,
            },
        )

    def _disposition_reconciliation_item(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context,
            "Reconciliation disposition requires a close-run-scoped thread.",
        )
        source_close_run_id = self._source_close_run_id(
            context=context,
            current_close_run_id=close_run_id,
        )
        reason = _require_string(arguments, "reason")
        prepared = self._prepare_phase_mutation_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.RECONCILIATION,
            action_label="Reconciliation item disposition",
            workflow_reason=reason,
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        item_id = self._resolve_reconciliation_item_id_for_scope(
            source_close_run_id=source_close_run_id,
            target_close_run_id=prepared.close_run_id,
            item_id=UUID(_require_string(arguments, "item_id")),
        )
        item = self._reconciliation_service.disposition_item(
            item_id=item_id,
            close_run_id=prepared.close_run_id,
            disposition=DispositionAction(_require_string(arguments, "disposition")),
            reason=reason,
            user_id=actor_user.id,
        )
        if item is None:
            raise ValueError("The reconciliation item was not found for this close run.")
        reconciliation = self._reconciliation_repo.get_reconciliation(item.reconciliation_id)
        return self._with_scope_metadata(
            prepared=prepared,
            result={
                "tool": "disposition_reconciliation_item",
                "item_id": str(item.id),
                "reconciliation_id": str(item.reconciliation_id),
                "reconciliation_type": (
                    reconciliation.reconciliation_type.value
                    if reconciliation is not None
                    else None
                ),
                "source_ref": item.source_ref,
                "match_status": item.match_status.value,
                "disposition": item.disposition.value if item.disposition is not None else None,
            },
        )

    def _resolve_reconciliation_anomaly(
        self,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context,
            "Reconciliation anomaly resolution requires a close-run-scoped thread.",
        )
        source_close_run_id = self._source_close_run_id(
            context=context,
            current_close_run_id=close_run_id,
        )
        resolution_note = _require_string(arguments, "resolution_note")
        prepared = self._prepare_phase_mutation_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.RECONCILIATION,
            action_label="Reconciliation anomaly resolution",
            workflow_reason=resolution_note,
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        anomaly_id = self._resolve_reconciliation_anomaly_id_for_scope(
            source_close_run_id=source_close_run_id,
            target_close_run_id=prepared.close_run_id,
            anomaly_id=UUID(_require_string(arguments, "anomaly_id")),
        )
        anomaly = self._reconciliation_service.resolve_anomaly(
            anomaly_id=anomaly_id,
            close_run_id=prepared.close_run_id,
            resolution_note=resolution_note,
            user_id=actor_user.id,
        )
        if anomaly is None:
            raise ValueError("The reconciliation anomaly was not found for this close run.")
        return self._with_scope_metadata(
            prepared=prepared,
            result={
                "tool": "resolve_reconciliation_anomaly",
                "anomaly_id": str(anomaly.id),
                "severity": anomaly.severity,
                "account_code": anomaly.account_code,
                "description": anomaly.description,
            },
        )

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
        prepared = self._prepare_phase_mutation_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.REPORTING,
            action_label="Report generation",
            workflow_reason=_optional_string(arguments, "reason"),
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        dispatch = self._queue_report_generation(
            entity_id=context.entity_id,
            close_run_id=prepared.close_run_id,
            actor_user=actor_user,
            template_id=_optional_string(arguments, "template_id"),
            generate_commentary=bool(arguments.get("generate_commentary", True)),
            use_llm_commentary=bool(arguments.get("use_llm_commentary", False)),
            context=context,
            trace_id=context.trace_id,
        )
        return self._with_scope_metadata(
            prepared=prepared,
            result={
                "tool": "generate_reports",
                "report_run_id": str(dispatch.report_run.id),
                "job_id": dispatch.job_id,
                "status": dispatch.job_status,
                "version_no": dispatch.report_run.version_no,
                "async_job_group": (
                    {
                        "continuation_group_id": dispatch.continuation_group_id,
                        "job_count": 1,
                    }
                    if dispatch.continuation_group_id is not None
                    else None
                ),
            },
        )

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
        prepared = self._prepare_phase_mutation_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.REVIEW_SIGNOFF,
            action_label="Export generation",
            workflow_reason=_optional_string(arguments, "reason"),
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        dispatch = self._queue_export_generation(
            entity_id=context.entity_id,
            close_run_id=prepared.close_run_id,
            actor_user=actor_user,
            include_evidence_pack=bool(arguments.get("include_evidence_pack", True)),
            include_audit_trail=bool(arguments.get("include_audit_trail", True)),
            action_qualifier=_optional_string(arguments, "action_qualifier"),
            context=context,
            trace_id=context.trace_id,
        )
        return self._with_scope_metadata(
            prepared=prepared,
            result={
                "tool": "generate_export",
                "job_id": dispatch.job_id,
                "status": dispatch.job_status,
                "async_job_group": (
                    {
                        "continuation_group_id": dispatch.continuation_group_id,
                        "job_count": 1,
                    }
                    if dispatch.continuation_group_id is not None
                    else None
                ),
            },
        )

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
        prepared = self._prepare_phase_mutation_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.REVIEW_SIGNOFF,
            action_label="Evidence-pack assembly",
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        dispatch = self._queue_evidence_pack_assembly(
            entity_id=context.entity_id,
            close_run_id=prepared.close_run_id,
            actor_user=actor_user,
            context=context,
            trace_id=context.trace_id,
        )
        return self._with_scope_metadata(
            prepared=prepared,
            result={
                "tool": "assemble_evidence_pack",
                "job_id": dispatch.job_id,
                "status": dispatch.job_status,
                "async_job_group": (
                    {
                        "continuation_group_id": dispatch.continuation_group_id,
                        "job_count": 1,
                    }
                    if dispatch.continuation_group_id is not None
                    else None
                ),
            },
        )

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
        prepared = self._prepare_phase_mutation_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.REVIEW_SIGNOFF,
            action_label="Management distribution",
            workflow_reason=_optional_string(arguments, "note"),
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        if close_run_id != prepared.close_run_id:
            raise ValueError(
                "The close run was reopened into a new working version. Generate a fresh export "
                "package there before recording stakeholder distribution."
            )
        export_detail = self._export_service.distribute_export(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=prepared.close_run_id,
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
        latest_record = (
            export_detail.distribution_records[0] if export_detail.distribution_records else None
        )
        return self._with_scope_metadata(
            prepared=prepared,
            result={
                "tool": "distribute_export",
                "export_id": export_detail.id,
                "distribution_count": export_detail.distribution_count,
                "recipient_name": latest_record.recipient_name
                if latest_record is not None
                else None,
                "delivery_channel": latest_record.delivery_channel
                if latest_record is not None
                else None,
                "distributed_at": latest_record.distributed_at
                if latest_record is not None
                else None,
            },
        )

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
        source_close_run_id = self._source_close_run_id(
            context=context,
            current_close_run_id=close_run_id,
        )
        prepared = self._prepare_phase_mutation_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.REPORTING,
            action_label="Commentary update",
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        commentary = self._report_service.update_commentary(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=prepared.close_run_id,
            report_run_id=self._resolve_report_run_id_for_scope(
                source_close_run_id=source_close_run_id,
                target_close_run_id=prepared.close_run_id,
                report_run_id=UUID(_require_string(arguments, "report_run_id")),
            ),
            section_key=_require_string(arguments, "section_key"),
            body=_require_string(arguments, "body"),
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        return self._with_scope_metadata(
            prepared=prepared,
            result={
                "tool": "update_commentary",
                "report_run_id": str(commentary.report_run_id),
                "section_key": commentary.section_key,
                "commentary_id": commentary.id,
                "status": commentary.status,
            },
        )

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
        source_close_run_id = self._source_close_run_id(
            context=context,
            current_close_run_id=close_run_id,
        )
        prepared = self._prepare_phase_mutation_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.REPORTING,
            action_label="Commentary approval",
            workflow_reason=_optional_string(arguments, "reason"),
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        commentary = self._report_service.approve_commentary(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=prepared.close_run_id,
            report_run_id=self._resolve_report_run_id_for_scope(
                source_close_run_id=source_close_run_id,
                target_close_run_id=prepared.close_run_id,
                report_run_id=UUID(_require_string(arguments, "report_run_id")),
            ),
            section_key=_require_string(arguments, "section_key"),
            body=_optional_string(arguments, "body"),
            reason=_optional_string(arguments, "reason"),
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        return self._with_scope_metadata(
            prepared=prepared,
            result={
                "tool": "approve_commentary",
                "report_run_id": str(commentary.report_run_id),
                "section_key": commentary.section_key,
                "commentary_id": commentary.id,
                "status": commentary.status,
            },
        )

    def _run_journal_action(
        self,
        *,
        arguments: dict[str, Any],
        context: AgentExecutionContext,
        action: str,
    ) -> dict[str, Any]:
        actor_user = self._require_actor(context)
        close_run_id = self._require_close_run_id(
            context, "Journal actions require a close-run-scoped thread."
        )
        source_close_run_id = self._source_close_run_id(
            context=context,
            current_close_run_id=close_run_id,
        )
        reason = (
            _require_string(arguments, "reason")
            if action == "reject"
            else _optional_string(arguments, "reason")
        )
        prepared = self._prepare_phase_mutation_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            close_run_id=close_run_id,
            required_phase=WorkflowPhase.PROCESSING,
            action_label=f"Journal {action}",
            workflow_reason=reason,
            source_surface=cast(AuditSourceSurface, context.source_surface),
            trace_id=context.trace_id,
        )
        journal_id = self._resolve_journal_id_for_scope(
            actor_user=actor_user,
            entity_id=context.entity_id,
            source_close_run_id=source_close_run_id,
            target_close_run_id=prepared.close_run_id,
            journal_id=UUID(_require_string(arguments, "journal_id")),
        )
        journal_record = self._recommendation_repo.get_journal_entry(journal_id=journal_id)
        actor = ActorContext(
            user_id=actor_user.id,
            full_name=actor_user.full_name,
            email=actor_user.email,
        )
        if action == "approve":
            result = self._recommendation_service.approve_journal(
                journal_id=journal_id,
                entity_id=context.entity_id,
                close_run_id=prepared.close_run_id,
                actor=actor,
                reason=reason,
                trace_id=context.trace_id,
                source_surface=cast(AuditSourceSurface, context.source_surface),
            )
            tool_name = "approve_journal"
        elif action == "apply":
            result = self._recommendation_service.apply_journal(
                journal_id=journal_id,
                entity_id=context.entity_id,
                close_run_id=prepared.close_run_id,
                actor=actor,
                posting_target=_require_string(arguments, "posting_target"),
                reason=reason,
                trace_id=context.trace_id,
                source_surface=cast(AuditSourceSurface, context.source_surface),
            )
            tool_name = "apply_journal"
        else:
            result = self._recommendation_service.reject_journal(
                journal_id=journal_id,
                entity_id=context.entity_id,
                close_run_id=prepared.close_run_id,
                actor=actor,
                reason=reason,
                trace_id=context.trace_id,
                source_surface=cast(AuditSourceSurface, context.source_surface),
            )
            tool_name = "reject_journal"
        return self._with_scope_metadata(
            prepared=prepared,
            result={
                "tool": tool_name,
                "journal_id": str(journal_id),
                "journal_number": (
                    journal_record.entry.journal_number
                    if journal_record is not None
                    else None
                ),
                "status": result.final_status.value,
                "posting_target": arguments.get("posting_target") if action == "apply" else None,
            },
        )

    def _queue_recommendation_jobs(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        actor_user: EntityUserRecord,
        document_ids: list[UUID] | None,
        force: bool,
        context: AgentExecutionContext,
        trace_id: str | None,
    ) -> list[dict[str, Any]]:
        """Queue recommendation generation for collection-approved documents only."""

        document_query = (
            self._db_session.query(Document)
            .join(DocumentExtraction, DocumentExtraction.document_id == Document.id)
            .filter(
                Document.close_run_id == close_run_id,
                Document.status == "approved",
                Document.document_type.in_(GL_CODING_RECOMMENDATION_ELIGIBLE_TYPE_VALUES),
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
                    Recommendation.status != ReviewStatus.SUPERSEDED.value,
                )
                .all()
                if recommendation.document_id is not None
            }

        queued_jobs: list[dict[str, Any]] = []
        continuation = self._build_chat_continuation(
            context=context,
            originating_tool="generate_recommendations",
        )
        imported_gl_representation = evaluate_documents_imported_gl_representation(
            session=self._db_session,
            close_run_id=close_run_id,
            document_ids=tuple(document.id for document in eligible_documents),
        )
        for document in eligible_documents:
            if not force and document.id in existing_recommendations:
                continue
            representation_result = imported_gl_representation.get(document.id)
            if (
                representation_result is not None
                and representation_result.represented_in_imported_gl
            ):
                continue

            job = self._job_service.dispatch_job(
                dispatcher=self._task_dispatcher,
                task_name=TaskName.ACCOUNTING_RECOMMEND_CLOSE_RUN,
                payload={
                    "entity_id": str(entity_id),
                    "close_run_id": str(close_run_id),
                    "document_id": str(document.id),
                    "actor_user_id": str(actor_user.id),
                    "force": force,
                },
                entity_id=entity_id,
                close_run_id=close_run_id,
                document_id=document.id,
                actor_user_id=actor_user.id,
                trace_id=trace_id,
                checkpoint_payload=(
                    embed_continuation_in_checkpoint(
                        checkpoint_payload=None,
                        continuation=continuation,
                    )
                    if continuation is not None
                    else None
                ),
            )
            job_payload = {
                "job_id": str(job.id),
                "document_id": str(document.id),
                "task_name": job.task_name,
                "status": job.status.value,
            }
            if continuation is not None:
                job_payload["continuation_group_id"] = str(continuation.continuation_group_id)
            queued_jobs.append(
                job_payload
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
        context: AgentExecutionContext,
        trace_id: str | None,
    ) -> ReportGenerationDispatch:
        """Create a report run and dispatch the background generation job."""

        if template_id is not None:
            resolved_template_id = UUID(template_id)
        else:
            resolved_template = (
                self._report_repo.get_active_template_for_entity(entity_id=entity_id)
                or self._report_repo.ensure_active_global_template()
            )
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
        continuation = self._build_chat_continuation(
            context=context,
            originating_tool="generate_reports",
        )
        job = self._job_service.dispatch_job(
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
            checkpoint_payload=(
                embed_continuation_in_checkpoint(
                    checkpoint_payload=None,
                    continuation=continuation,
                )
                if continuation is not None
                else None
            ),
        )
        return ReportGenerationDispatch(
            report_run=run_record,
            job_id=str(job.id),
            job_status=job.status.value,
            continuation_group_id=(
                str(continuation.continuation_group_id)
                if continuation is not None
                else None
            ),
        )

    def _queue_export_generation(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        actor_user: EntityUserRecord,
        include_evidence_pack: bool,
        include_audit_trail: bool,
        action_qualifier: str | None,
        context: AgentExecutionContext,
        trace_id: str | None,
    ) -> AsyncToolDispatch:
        """Dispatch async export-package generation for the current close run."""

        continuation = self._build_chat_continuation(
            context=context,
            originating_tool="generate_export",
        )
        job = self._job_service.dispatch_job(
            dispatcher=self._task_dispatcher,
            task_name=TaskName.EXPORTS_GENERATE_CLOSE_RUN_PACKAGE,
            payload={
                "entity_id": str(entity_id),
                "close_run_id": str(close_run_id),
                "actor_user_id": str(actor_user.id),
                "include_evidence_pack": include_evidence_pack,
                "include_audit_trail": include_audit_trail,
                "action_qualifier": action_qualifier,
            },
            entity_id=entity_id,
            close_run_id=close_run_id,
            document_id=None,
            actor_user_id=actor_user.id,
            trace_id=trace_id,
            checkpoint_payload=(
                embed_continuation_in_checkpoint(
                    checkpoint_payload=None,
                    continuation=continuation,
                )
                if continuation is not None
                else None
            ),
        )
        return AsyncToolDispatch(
            job_id=str(job.id),
            job_status=job.status.value,
            continuation_group_id=(
                str(continuation.continuation_group_id)
                if continuation is not None
                else None
            ),
        )

    def _queue_evidence_pack_assembly(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        actor_user: EntityUserRecord,
        context: AgentExecutionContext,
        trace_id: str | None,
    ) -> AsyncToolDispatch:
        """Dispatch async evidence-pack assembly for the current close run."""

        continuation = self._build_chat_continuation(
            context=context,
            originating_tool="assemble_evidence_pack",
        )
        job = self._job_service.dispatch_job(
            dispatcher=self._task_dispatcher,
            task_name=TaskName.EXPORTS_ASSEMBLE_EVIDENCE_PACK,
            payload={
                "entity_id": str(entity_id),
                "close_run_id": str(close_run_id),
                "actor_user_id": str(actor_user.id),
            },
            entity_id=entity_id,
            close_run_id=close_run_id,
            document_id=None,
            actor_user_id=actor_user.id,
            trace_id=trace_id,
            checkpoint_payload=(
                embed_continuation_in_checkpoint(
                    checkpoint_payload=None,
                    continuation=continuation,
                )
                if continuation is not None
                else None
            ),
        )
        return AsyncToolDispatch(
            job_id=str(job.id),
            job_status=job.status.value,
            continuation_group_id=(
                str(continuation.continuation_group_id)
                if continuation is not None
                else None
            ),
        )

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

    def _source_close_run_id(
        self,
        *,
        context: AgentExecutionContext,
        current_close_run_id: UUID,
    ) -> UUID:
        """Return the original scope used for ID remapping across reopened versions."""

        return context.source_close_run_id or current_close_run_id

    def _prepare_phase_mutation_scope(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
        required_phase: WorkflowPhase,
        action_label: str,
        workflow_reason: str | None = None,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> PreparedMutationScope:
        """Ensure the requested mutation has a working close run in the correct phase."""

        close_run = self._close_run_service.get_close_run(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
        resolved_close_run_id = close_run_id
        metadata: dict[str, Any] = {}

        if close_run.status in _RELEASED_CLOSE_RUN_STATUSES:
            released_status = close_run.status
            reopened = self._close_run_service.reopen_close_run(
                actor_user=actor_user,
                entity_id=entity_id,
                close_run_id=close_run_id,
                reason=workflow_reason
                or f"Reopen the released close run so {action_label.lower()} can continue.",
                source_surface=source_surface,
                trace_id=trace_id,
            )
            close_run = reopened.close_run
            resolved_close_run_id = UUID(reopened.close_run.id)
            metadata.update(
                {
                    "source_close_run_id": reopened.source_close_run_id,
                    "reopened_close_run_id": reopened.close_run.id,
                    "version_no": reopened.close_run.current_version_no,
                    "reopened_from_status": released_status.value,
                }
            )

        active_phase = close_run.workflow_state.active_phase
        if (
            active_phase is not None
            and active_phase is not required_phase
            and _workflow_phase_index(required_phase) < _workflow_phase_index(active_phase)
        ):
            rewound = self._close_run_service.rewind_close_run(
                actor_user=actor_user,
                entity_id=entity_id,
                close_run_id=resolved_close_run_id,
                target_phase=required_phase,
                reason=workflow_reason
                or (
                    f"Move the close run back to {required_phase.label} so "
                    f"{action_label.lower()} can continue."
                ),
                source_surface=source_surface,
                trace_id=trace_id,
            )
            close_run = rewound.close_run
            resolved_close_run_id = UUID(rewound.close_run.id)
            metadata.update(
                {
                    "rewound_from_phase": rewound.previous_active_phase.value,
                    "active_phase": rewound.active_phase.value,
                }
            )

        require_active_phase(
            close_run,
            required_phase=required_phase,
            action_label=action_label,
        )
        if "active_phase" not in metadata and close_run.workflow_state.active_phase is not None:
            metadata["active_phase"] = close_run.workflow_state.active_phase.value
        return PreparedMutationScope(close_run_id=resolved_close_run_id, metadata=metadata)

    def _resolve_document_id_for_scope(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        source_close_run_id: UUID,
        target_close_run_id: UUID,
        document_id: UUID,
    ) -> UUID:
        """Resolve the document id within the active mutation scope after a reopen."""

        if source_close_run_id == target_close_run_id:
            return document_id

        source_document = self._document_repo.get_document_for_user(
            entity_id=entity_id,
            close_run_id=source_close_run_id,
            document_id=document_id,
            user_id=actor_user.id,
        )
        if source_document is None:
            raise ValueError("That document does not exist in the current close run.")

        source_documents = list(
            self._document_repo.list_documents_for_close_run(close_run_id=source_close_run_id)
        )
        source_fingerprint = _build_document_fingerprint(source_document.document)
        source_peer_ids = [
            document.id
            for document in source_documents
            if _build_document_fingerprint(document) == source_fingerprint
        ]
        if document_id not in source_peer_ids:
            raise ValueError("That document does not map cleanly onto the reopened version.")

        source_index = source_peer_ids.index(document_id)
        target_candidates = [
            document
            for document in self._document_repo.list_documents_for_close_run(
                close_run_id=target_close_run_id
            )
            if _build_document_fingerprint(document) == source_fingerprint
        ]
        if len(target_candidates) > source_index:
            return target_candidates[source_index].id
        if not target_candidates:
            raise ValueError(
                "The reopened working version does not contain a carried-forward "
                "copy of that document."
            )
        raise ValueError(
            "The reopened working version does not contain a usable copy of that document."
        )

    def _resolve_field_id_for_scope(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        source_close_run_id: UUID,
        target_close_run_id: UUID,
        field_id: UUID,
    ) -> UUID:
        """Resolve an extracted-field id within the active mutation scope after a reopen."""

        if source_close_run_id == target_close_run_id:
            return field_id

        source_row = (
            self._db_session.query(ExtractedField, DocumentExtraction, Document)
            .join(
                DocumentExtraction, DocumentExtraction.id == ExtractedField.document_extraction_id
            )
            .join(Document, Document.id == DocumentExtraction.document_id)
            .filter(
                ExtractedField.id == field_id,
                Document.close_run_id == source_close_run_id,
            )
            .one_or_none()
        )
        if source_row is None:
            raise ValueError("That extracted field does not exist in the current close run.")

        source_field, source_extraction, source_document = source_row
        target_document_id = self._resolve_document_id_for_scope(
            actor_user=actor_user,
            entity_id=entity_id,
            source_close_run_id=source_close_run_id,
            target_close_run_id=target_close_run_id,
            document_id=source_document.id,
        )
        candidate_rows = (
            self._db_session.query(ExtractedField)
            .join(
                DocumentExtraction, DocumentExtraction.id == ExtractedField.document_extraction_id
            )
            .filter(
                DocumentExtraction.document_id == target_document_id,
                DocumentExtraction.version_no == source_extraction.version_no,
                ExtractedField.field_name == source_field.field_name,
                ExtractedField.field_type == source_field.field_type,
            )
            .order_by(ExtractedField.created_at.asc(), ExtractedField.id.asc())
            .all()
        )
        for candidate in candidate_rows:
            if (
                candidate.evidence_ref == source_field.evidence_ref
                and candidate.field_value == source_field.field_value
            ):
                return candidate.id
        if len(candidate_rows) == 1:
            return candidate_rows[0].id
        if not candidate_rows:
            raise ValueError(
                "The reopened working version does not contain a carried-forward "
                "copy of that extracted field."
            )
        raise ValueError(
            "The reopened working version contains more than one matching extracted field. "
            "Please restate the request with the document and field name."
        )

    def _resolve_recommendation_id_for_scope(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        source_close_run_id: UUID,
        target_close_run_id: UUID,
        recommendation_id: UUID,
    ) -> UUID:
        """Resolve a recommendation id within the active mutation scope after a reopen."""

        if source_close_run_id == target_close_run_id:
            return recommendation_id

        source_recommendation = self._recommendation_repo.get_recommendation(
            recommendation_id=recommendation_id
        )
        if (
            source_recommendation is None
            or source_recommendation.close_run_id != source_close_run_id
        ):
            raise ValueError("That recommendation does not exist in the current close run.")

        source_recommendations = sorted(
            self._recommendation_repo.list_recommendations_for_close_run(
                close_run_id=source_close_run_id
            ),
            key=_created_at_and_id_sort_key,
        )
        target_recommendations = sorted(
            self._recommendation_repo.list_recommendations_for_close_run(
                close_run_id=target_close_run_id
            ),
            key=_created_at_and_id_sort_key,
        )
        source_fingerprint = self._build_recommendation_fingerprint(
            recommendation=source_recommendation,
            resolved_document_id=(
                self._resolve_document_id_for_scope(
                    actor_user=actor_user,
                    entity_id=entity_id,
                    source_close_run_id=source_close_run_id,
                    target_close_run_id=target_close_run_id,
                    document_id=source_recommendation.document_id,
                )
                if source_recommendation.document_id is not None
                else None
            ),
        )
        source_peer_ids = [
            record.id
            for record in source_recommendations
            if self._build_recommendation_fingerprint(
                recommendation=record,
                resolved_document_id=(
                    self._resolve_document_id_for_scope(
                        actor_user=actor_user,
                        entity_id=entity_id,
                        source_close_run_id=source_close_run_id,
                        target_close_run_id=target_close_run_id,
                        document_id=record.document_id,
                    )
                    if record.document_id is not None
                    else None
                ),
            )
            == source_fingerprint
        ]
        if recommendation_id not in source_peer_ids:
            raise ValueError("That recommendation does not map cleanly onto the reopened version.")
        source_index = source_peer_ids.index(recommendation_id)
        target_candidates = [
            record
            for record in target_recommendations
            if self._build_recommendation_fingerprint(
                recommendation=record,
                resolved_document_id=record.document_id,
            )
            == source_fingerprint
        ]
        if len(target_candidates) > source_index:
            return target_candidates[source_index].id
        if not target_candidates:
            raise ValueError(
                "The reopened working version does not contain a carried-forward "
                "copy of that recommendation."
            )
        raise ValueError(
            "The reopened working version does not contain a usable copy of that recommendation. "
            "Ask the agent to regenerate recommendations first."
        )

    def _resolve_journal_id_for_scope(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        source_close_run_id: UUID,
        target_close_run_id: UUID,
        journal_id: UUID,
    ) -> UUID:
        """Resolve a journal id within the active mutation scope after a reopen."""

        del actor_user, entity_id
        if source_close_run_id == target_close_run_id:
            return journal_id

        source_journal = self._recommendation_repo.get_journal_entry(journal_id=journal_id)
        if source_journal is None or source_journal.entry.close_run_id != source_close_run_id:
            raise ValueError("That journal does not exist in the current close run.")

        source_journals = [
            journal_result
            for journal_result in (
                self._recommendation_repo.get_journal_entry(journal_id=journal.id)
                for journal in sorted(
                    self._recommendation_repo.list_journals_for_close_run(
                        close_run_id=source_close_run_id
                    ),
                    key=_created_at_and_id_sort_key,
                )
            )
            if journal_result is not None
        ]
        target_journals = [
            journal_result
            for journal_result in (
                self._recommendation_repo.get_journal_entry(journal_id=journal.id)
                for journal in sorted(
                    self._recommendation_repo.list_journals_for_close_run(
                        close_run_id=target_close_run_id
                    ),
                    key=_created_at_and_id_sort_key,
                )
            )
            if journal_result is not None
        ]
        source_fingerprint = _build_journal_fingerprint(source_journal)
        source_peer_ids = [
            journal_result.entry.id
            for journal_result in source_journals
            if _build_journal_fingerprint(journal_result) == source_fingerprint
        ]
        if journal_id not in source_peer_ids:
            raise ValueError("That journal does not map cleanly onto the reopened version.")
        source_index = source_peer_ids.index(journal_id)
        target_candidates = [
            journal_result
            for journal_result in target_journals
            if _build_journal_fingerprint(journal_result) == source_fingerprint
        ]
        if len(target_candidates) > source_index:
            return target_candidates[source_index].entry.id
        if not target_candidates:
            raise ValueError(
                "The reopened working version does not contain a carried-forward "
                "copy of that journal."
            )
        raise ValueError(
            "The reopened working version does not contain a usable copy of that journal. "
            "Approve the related recommendation again to regenerate it."
        )

    def _resolve_reconciliation_id_for_scope(
        self,
        *,
        source_close_run_id: UUID,
        target_close_run_id: UUID,
        reconciliation_id: UUID,
    ) -> UUID:
        """Resolve a reconciliation id within the active mutation scope after a reopen."""

        if source_close_run_id == target_close_run_id:
            return reconciliation_id

        source_reconciliation = self._reconciliation_repo.get_reconciliation_for_close_run(
            reconciliation_id=reconciliation_id,
            close_run_id=source_close_run_id,
        )
        if source_reconciliation is None:
            raise ValueError("That reconciliation does not exist in the current close run.")

        source_reconciliations = sorted(
            self._reconciliation_repo.list_reconciliations(source_close_run_id),
            key=_created_at_and_id_sort_key,
        )
        target_reconciliations = sorted(
            self._reconciliation_repo.list_reconciliations(target_close_run_id),
            key=_created_at_and_id_sort_key,
        )
        source_fingerprint = _build_reconciliation_fingerprint(source_reconciliation)
        source_peer_ids = [
            record.id
            for record in source_reconciliations
            if _build_reconciliation_fingerprint(record) == source_fingerprint
        ]
        if reconciliation_id not in source_peer_ids:
            raise ValueError("That reconciliation does not map cleanly onto the reopened version.")
        source_index = source_peer_ids.index(reconciliation_id)
        target_candidates = [
            record
            for record in target_reconciliations
            if _build_reconciliation_fingerprint(record) == source_fingerprint
        ]
        if len(target_candidates) > source_index:
            return target_candidates[source_index].id
        if not target_candidates:
            raise ValueError(
                "The reopened working version does not contain a carried-forward "
                "copy of that reconciliation."
            )
        raise ValueError(
            "The reopened working version does not contain a usable copy of that reconciliation."
        )

    def _resolve_reconciliation_item_id_for_scope(
        self,
        *,
        source_close_run_id: UUID,
        target_close_run_id: UUID,
        item_id: UUID,
    ) -> UUID:
        """Resolve a reconciliation item id within the active mutation scope after a reopen."""

        if source_close_run_id == target_close_run_id:
            return item_id

        source_item = self._reconciliation_repo.get_item_for_close_run(
            item_id=item_id,
            close_run_id=source_close_run_id,
        )
        if source_item is None:
            raise ValueError("That reconciliation item does not exist in the current close run.")

        target_reconciliation_id = self._resolve_reconciliation_id_for_scope(
            source_close_run_id=source_close_run_id,
            target_close_run_id=target_close_run_id,
            reconciliation_id=source_item.reconciliation_id,
        )
        source_items = sorted(
            self._reconciliation_repo.list_items(
                reconciliation_id=source_item.reconciliation_id,
            ),
            key=_created_at_and_id_sort_key,
        )
        target_items = sorted(
            self._reconciliation_repo.list_items(reconciliation_id=target_reconciliation_id),
            key=_created_at_and_id_sort_key,
        )
        source_fingerprint = _build_reconciliation_item_fingerprint(source_item)
        source_peer_ids = [
            record.id
            for record in source_items
            if _build_reconciliation_item_fingerprint(record) == source_fingerprint
        ]
        if item_id not in source_peer_ids:
            raise ValueError(
                "That reconciliation item does not map cleanly onto the reopened version."
            )
        source_index = source_peer_ids.index(item_id)
        target_candidates = [
            record
            for record in target_items
            if _build_reconciliation_item_fingerprint(record) == source_fingerprint
        ]
        if len(target_candidates) > source_index:
            return target_candidates[source_index].id
        if not target_candidates:
            raise ValueError(
                "The reopened working version does not contain a carried-forward copy of that "
                "reconciliation item. Run reconciliation again first."
            )
        raise ValueError(
            "The reopened working version contains more than one matching reconciliation item. "
            "Ask the agent to use the latest unresolved item."
        )

    def _resolve_reconciliation_anomaly_id_for_scope(
        self,
        *,
        source_close_run_id: UUID,
        target_close_run_id: UUID,
        anomaly_id: UUID,
    ) -> UUID:
        """Resolve a reconciliation anomaly id within the active mutation scope after a reopen."""

        if source_close_run_id == target_close_run_id:
            return anomaly_id

        source_anomaly = self._reconciliation_repo.get_anomaly_for_close_run(
            anomaly_id=anomaly_id,
            close_run_id=source_close_run_id,
        )
        if source_anomaly is None:
            raise ValueError("That reconciliation anomaly does not exist in the current close run.")

        source_anomalies = sorted(
            self._reconciliation_repo.list_anomalies(close_run_id=source_close_run_id),
            key=_created_at_and_id_sort_key,
        )
        target_anomalies = sorted(
            self._reconciliation_repo.list_anomalies(close_run_id=target_close_run_id),
            key=_created_at_and_id_sort_key,
        )
        source_fingerprint = _build_reconciliation_anomaly_fingerprint(source_anomaly)
        source_peer_ids = [
            record.id
            for record in source_anomalies
            if _build_reconciliation_anomaly_fingerprint(record) == source_fingerprint
        ]
        if anomaly_id not in source_peer_ids:
            raise ValueError(
                "That reconciliation anomaly does not map cleanly onto the reopened version."
            )
        source_index = source_peer_ids.index(anomaly_id)
        target_candidates = [
            record
            for record in target_anomalies
            if _build_reconciliation_anomaly_fingerprint(record) == source_fingerprint
        ]
        if len(target_candidates) > source_index:
            return target_candidates[source_index].id
        if not target_candidates:
            raise ValueError(
                "The reopened working version does not contain a carried-forward copy of that "
                "reconciliation anomaly. Run reconciliation again first."
            )
        raise ValueError(
            "The reopened working version contains more than one matching reconciliation "
            "anomaly. Ask the agent to use the latest unresolved anomaly."
        )

    def _resolve_report_run_id_for_scope(
        self,
        *,
        source_close_run_id: UUID,
        target_close_run_id: UUID,
        report_run_id: UUID,
    ) -> UUID:
        """Resolve a report-run id within the active mutation scope after a reopen."""

        if source_close_run_id == target_close_run_id:
            return report_run_id

        source_report_run = self._report_repo.get_report_run(
            report_run_id=report_run_id,
            close_run_id=source_close_run_id,
        )
        if source_report_run is None:
            raise ValueError("That report run does not exist in the current close run.")

        target_candidates = [
            run
            for run in self._report_repo.list_report_runs_for_close_run(
                close_run_id=target_close_run_id
            )
            if run.version_no == source_report_run.version_no
        ]
        if len(target_candidates) == 1:
            return target_candidates[0].id
        if not target_candidates:
            raise ValueError(
                "The reopened working version does not contain a carried-forward "
                "copy of that report run."
            )
        raise ValueError(
            "The reopened working version contains more than one matching report run. "
            "Ask the agent to use the latest report run version."
        )

    def _resolve_supporting_schedule_row_id_for_scope(
        self,
        *,
        source_close_run_id: UUID,
        target_close_run_id: UUID,
        schedule_type: SupportingScheduleType,
        row_id: UUID,
    ) -> UUID:
        """Resolve a supporting-schedule row id within the active mutation scope."""

        if source_close_run_id == target_close_run_id:
            return row_id

        source_workspace = self._supporting_schedule_service.list_workspace(
            close_run_id=source_close_run_id
        )
        source_schedule = next(
            (
                snapshot
                for snapshot in source_workspace
                if snapshot.schedule.schedule_type is schedule_type
            ),
            None,
        )
        if source_schedule is None:
            raise ValueError("That supporting schedule does not exist in the current close run.")
        source_row = next((row for row in source_schedule.rows if row.id == row_id), None)
        if source_row is None:
            raise ValueError(
                "That supporting-schedule row does not exist in the current close run."
            )

        target_workspace = self._supporting_schedule_service.list_workspace(
            close_run_id=target_close_run_id
        )
        target_schedule = next(
            (
                snapshot
                for snapshot in target_workspace
                if snapshot.schedule.schedule_type is schedule_type
            ),
            None,
        )
        if target_schedule is None:
            raise ValueError(
                "The reopened working version does not contain a carried-forward "
                "copy of that supporting schedule."
            )
        candidates = [
            row
            for row in target_schedule.rows
            if row.row_ref == source_row.row_ref and row.line_no == source_row.line_no
        ]
        if len(candidates) == 1:
            return candidates[0].id
        if not candidates:
            raise ValueError(
                "The reopened working version does not contain a carried-forward "
                "copy of that supporting-schedule row."
            )
        raise ValueError(
            "The reopened working version contains more than one matching supporting-schedule row. "
            "Please restate the request with the row reference."
        )

    def _build_recommendation_fingerprint(
        self,
        *,
        recommendation: Any,
        resolved_document_id: UUID | None,
    ) -> tuple[Any, ...]:
        """Return a stable fingerprint for matching recommendations across reopened versions."""

        return (
            str(resolved_document_id) if resolved_document_id is not None else None,
            recommendation.recommendation_type,
            recommendation.status,
            recommendation.reasoning_summary,
            _stable_json_string(recommendation.payload),
            str(recommendation.confidence),
            recommendation.created_by_system,
            recommendation.autonomy_mode,
        )

    def _with_scope_metadata(
        self,
        *,
        prepared: PreparedMutationScope,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach any reopen or rewind metadata to a tool result."""

        if not prepared.metadata:
            return result
        return {
            **prepared.metadata,
            **result,
        }

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


def _created_at_and_id_sort_key(record: Any) -> tuple[Any, str]:
    """Return a stable ordering key for repository records with created_at/id fields."""

    return record.created_at, str(record.id)


def _stable_json_string(value: object) -> str:
    """Return a deterministic JSON string for fingerprint comparisons."""

    return json.dumps(value, sort_keys=True, default=str)


def _build_document_fingerprint(document: Any) -> tuple[str, str, str, int]:
    """Return a stable fingerprint for matching copied documents across reopen."""

    return (
        document.original_filename,
        document.storage_key,
        document.sha256_hash,
        document.file_size_bytes,
    )


def _build_journal_fingerprint(journal_result: Any) -> tuple[Any, ...]:
    """Return a stable fingerprint for matching journals across reopened versions."""

    entry = journal_result.entry
    return (
        entry.posting_date.isoformat(),
        entry.status,
        entry.description,
        str(entry.total_debits),
        str(entry.total_credits),
        entry.line_count,
        entry.source_surface,
        entry.autonomy_mode,
        entry.reasoning_summary,
        tuple(
            (
                line.line_no,
                line.account_code,
                line.line_type,
                str(line.amount),
                line.description,
                _stable_json_string(line.dimensions),
                line.reference,
            )
            for line in journal_result.lines
        ),
    )


def _build_reconciliation_fingerprint(reconciliation: Any) -> tuple[str, str, str]:
    """Return a stable fingerprint for matching reconciliations across reopened versions."""

    return (
        reconciliation.reconciliation_type.value,
        reconciliation.status.value,
        _stable_json_string(reconciliation.summary),
    )


def _build_reconciliation_item_fingerprint(item: Any) -> tuple[Any, ...]:
    """Return a stable fingerprint for matching reconciliation items across reopen."""

    return (
        item.source_type,
        item.source_ref,
        item.match_status.value,
        str(item.amount),
        _stable_json_string(item.matched_to),
        str(item.difference_amount),
        item.explanation,
        item.requires_disposition,
        _stable_json_string(item.dimensions),
        item.period_date,
    )


def _build_reconciliation_anomaly_fingerprint(anomaly: Any) -> tuple[Any, ...]:
    """Return a stable fingerprint for matching reconciliation anomalies across reopen."""

    return (
        anomaly.anomaly_type.value,
        anomaly.severity,
        anomaly.account_code,
        anomaly.description,
        _stable_json_string(anomaly.details),
        anomaly.resolved,
    )


def _workflow_phase_index(phase: WorkflowPhase) -> int:
    """Return the canonical order index for one workflow phase."""

    return CANONICAL_WORKFLOW_PHASES.index(phase)


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


def _date_property(description: str) -> dict[str, Any]:
    """Build an ISO date string property schema."""

    return {
        "type": "string",
        "format": "date",
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
