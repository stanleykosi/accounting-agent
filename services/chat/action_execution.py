"""
Purpose: Adapt chat threads and approval workflows onto the reusable agent
runtime and accounting tool registry.
Scope: Thread loading, message persistence, planner invocation, staged action
plans, approval execution, and assistant message creation.
Dependencies: Chat repositories, grounding, agent kernel, and accounting
workflow services.
"""

from __future__ import annotations

import json
import re
from calendar import monthrange
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from services.accounting.recommendation_apply import (
    RecommendationApplyError,
    RecommendationApplyService,
)
from services.agents.accounting_context import AccountingWorkspaceContextBuilder
from services.agents.accounting_toolset import AccountingToolset
from services.agents.kernel import AgentKernel, AgentKernelError
from services.agents.models import (
    AgentExecutionContext,
    AgentPlannedAction,
    AgentPlanningResult,
)
from services.agents.policy import ExecutionPolicy
from services.auth.service import serialize_uuid
from services.chat.continuation_state import (
    ChatOperatorContinuation,
    build_pending_async_turn_payload,
    get_active_async_turn,
)
from services.chat.grounding import ChatGroundingService, GroundingContextRecord
from services.chat.operator_memory import (
    DEFAULT_PREFERRED_CONFIRMATION_STYLE,
    DEFAULT_PREFERRED_EXPLANATION_DEPTH,
    build_recovery_guidance,
    compact_recent_values,
    merge_context_payload_with_cross_thread_memory,
    optional_memory_int,
    optional_memory_text,
)
from services.close_runs.delete_service import CloseRunDeleteService
from services.close_runs.service import CloseRunService, CloseRunServiceError
from services.coa.service import CoaRepository, CoaService
from services.common.enums import CloseRunStatus, ReportSectionKey, WorkflowPhase
from services.common.types import utc_now
from services.contracts.chat_models import (
    AgentCoaSummary,
    AgentMemorySummary,
    AgentOperatorControl,
    AgentRunReadiness,
    AgentToolManifestItem,
    AgentTraceRecord,
    ChatThreadWorkspaceResponse,
    GroundingContext,
)
from services.db.models.audit import AuditSourceSurface
from services.db.repositories.chat_action_repo import (
    ChatActionPlanRecord,
    ChatActionRepository,
)
from services.db.repositories.chat_repo import ChatRepository
from services.db.repositories.close_run_repo import CloseRunRepository
from services.db.repositories.document_repo import DocumentRepository
from services.db.repositories.entity_repo import EntityRepository, EntityUserRecord
from services.db.repositories.recommendation_journal_repo import (
    RecommendationJournalRepository,
)
from services.db.repositories.reconciliation_repo import ReconciliationRepository
from services.db.repositories.report_repo import ReportRepository
from services.db.repositories.supporting_schedule_repo import SupportingScheduleRepository
from services.documents.review_service import (
    DocumentReviewService,
    DocumentReviewServiceError,
)
from services.entity.delete_service import EntityDeleteService, EntityDeleteServiceError
from services.entity.service import EntityService, EntityServiceError
from services.exports.service import ExportService, ExportServiceError
from services.jobs.service import JobRecord, JobService, JobServiceError
from services.model_gateway.client import ModelGateway
from services.reconciliation.service import ReconciliationService
from services.reporting.service import ReportService, ReportServiceError
from services.supporting_schedules.service import (
    SupportingScheduleService,
    SupportingScheduleServiceError,
)
from sqlalchemy.orm import Session


class ChatActionExecutionErrorCode(StrEnum):
    """Enumerate stable error codes surfaced by the chat action executor."""

    THREAD_NOT_FOUND = "thread_not_found"
    ACCESS_DENIED = "access_denied"
    PLANNING_FAILED = "planning_failed"
    THREAD_TURN_IN_PROGRESS = "thread_turn_in_progress"
    ACTION_PLAN_NOT_FOUND = "action_plan_not_found"
    INVALID_ACTION_PLAN = "invalid_action_plan"
    EXECUTION_FAILED = "execution_failed"


class ChatActionExecutionError(Exception):
    """Represent an expected chat-execution-domain failure for API translation."""

    def __init__(
        self,
        *,
        status_code: int,
        code: ChatActionExecutionErrorCode,
        message: str,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ChatExecutionOutcome:
    """Describe the result of planning and optionally executing one chat action."""

    assistant_message_id: str
    assistant_content: str
    action_plan: ChatActionPlanRecord | None
    is_read_only: bool
    thread_entity_id: str
    thread_close_run_id: str | None


@dataclass(frozen=True, slots=True)
class McpToolCallOutcome:
    """Describe one deterministic MCP tool call applied through the agent runtime."""

    message_id: str
    tool_name: str
    status: str
    requires_human_approval: bool
    action_plan_id: str | None
    summary: str
    result: dict[str, Any] | None


_MAX_OPERATOR_LOOP_STEPS = 8
_OPERATOR_PLANNER_POLICY_VERSION = "2026-04-21.operator-planner.v1"
_OPERATOR_CONFIRMATION_POLICY_VERSION = "2026-04-21.operator-confirmation.v1"
_OPERATOR_EVAL_SCHEMA_VERSION = "2026-04-21.operator-eval.v1"
_MCP_MANIFEST_VERSION = "2025-11-25"
_TERMINAL_TURN_STATUSES = frozenset(
    {"completed", "pending", "waiting_async", "partial", "failed"}
)
_AUTO_APPROVABLE_RELEASE_TOOLS = frozenset(
    {"approve_close_run", "archive_close_run", "distribute_export"}
)
_NEVER_AUTO_APPROVE_TOOLS = frozenset({"delete_close_run", "delete_workspace"})
_THREAD_APPROVAL_POLICY_KEY = "agent_approval_policy"


@dataclass(frozen=True, slots=True)
class _OperatorLoopContext:
    """Describe the current bounded multi-step operator turn."""

    objective: str
    iteration: int
    max_iterations: int
    completed_summaries: tuple[str, ...]


class ChatActionExecutor:
    """Plan and execute tool-aware chat actions through the reusable agent runtime."""

    def __init__(
        self,
        *,
        db_session: Session,
        chat_repository: ChatRepository,
        action_repository: ChatActionRepository,
        grounding_service: ChatGroundingService,
        entity_repository: EntityRepository,
        close_run_service: CloseRunService,
        close_run_delete_service: CloseRunDeleteService,
        close_run_repository: CloseRunRepository,
        document_review_service: DocumentReviewService,
        document_repository: DocumentRepository,
        entity_service: EntityService,
        entity_delete_service: EntityDeleteService,
        recommendation_service: RecommendationApplyService,
        recommendation_repository: RecommendationJournalRepository,
        reconciliation_service: ReconciliationService,
        reconciliation_repository: ReconciliationRepository,
        report_service: ReportService,
        report_repository: ReportRepository,
        export_service: ExportService,
        model_gateway: ModelGateway,
        job_service: JobService,
        task_dispatcher: Any,
    ) -> None:
        self._db_session = db_session
        self._chat_repo = chat_repository
        self._action_repo = action_repository
        self._grounding = grounding_service
        self._entity_repo = entity_repository
        self._entity_service = entity_service
        self._entity_delete_service = entity_delete_service
        self._close_run_service = close_run_service
        self._coa_service = CoaService(repository=CoaRepository(db_session=db_session))
        self._document_repository = document_repository

        self._workspace_builder = AccountingWorkspaceContextBuilder(
            action_repository=action_repository,
            close_run_service=close_run_service,
            coa_repository=CoaRepository(db_session=db_session),
            document_repository=document_repository,
            entity_repository=entity_repository,
            export_service=export_service,
            job_service=job_service,
            reconciliation_repository=reconciliation_repository,
            recommendation_repository=recommendation_repository,
            report_repository=report_repository,
            supporting_schedule_service=SupportingScheduleService(
                repository=SupportingScheduleRepository(session=db_session),
            ),
        )
        self._toolset = AccountingToolset(
            db_session=db_session,
            close_run_service=close_run_service,
            close_run_delete_service=close_run_delete_service,
            document_review_service=document_review_service,
            document_repository=document_repository,
            entity_repository=entity_repository,
            entity_service=entity_service,
            entity_delete_service=entity_delete_service,
            export_service=export_service,
            job_service=job_service,
            recommendation_service=recommendation_service,
            recommendation_repository=recommendation_repository,
            reconciliation_service=reconciliation_service,
            reconciliation_repository=reconciliation_repository,
            report_service=report_service,
            report_repository=report_repository,
            supporting_schedule_service=SupportingScheduleService(
                repository=SupportingScheduleRepository(session=db_session),
            ),
            task_dispatcher=task_dispatcher,
        )
        tool_registry = self._toolset.build_registry()
        self._tool_registry = tool_registry
        self._kernel = AgentKernel(
            model_gateway=model_gateway,
            tool_registry=tool_registry,
            execution_policy=ExecutionPolicy(tool_registry=tool_registry),
        )

    def _build_execution_outcome(
        self,
        *,
        assistant_message_id: str,
        assistant_content: str,
        action_plan: ChatActionPlanRecord | None,
        is_read_only: bool,
        thread: Any,
    ) -> ChatExecutionOutcome:
        """Build one canonical chat execution outcome from the current persisted thread scope."""

        return ChatExecutionOutcome(
            assistant_message_id=assistant_message_id,
            assistant_content=assistant_content,
            action_plan=action_plan,
            is_read_only=is_read_only,
            thread_entity_id=serialize_uuid(thread.entity_id),
            thread_close_run_id=serialize_uuid(thread.close_run_id)
            if thread.close_run_id is not None
            else None,
        )

    def _lock_thread_for_turn(self, *, thread_id: UUID) -> None:
        """Serialize operator turns for one thread before any side effect is planned."""

        thread = self._chat_repo.lock_thread_for_turn(thread_id=thread_id)
        if thread is None:
            raise ChatActionExecutionError(
                status_code=404,
                code=ChatActionExecutionErrorCode.THREAD_NOT_FOUND,
                message="That chat thread does not exist.",
            )

    def _release_thread_turn_lock(self, *, thread_id: UUID) -> None:
        """Release the thread turn serialization lock without masking turn outcomes."""

        release_lock = getattr(self._chat_repo, "release_thread_turn_lock", None)
        if not callable(release_lock):
            return
        try:
            release_lock(thread_id=thread_id)
        except Exception:
            self._db_session.rollback()

    def _replay_completed_turn_if_present(
        self,
        *,
        thread_id: UUID,
        actor_user: EntityUserRecord,
        client_turn_id: str | None,
        allow_received_turn: bool = False,
    ) -> ChatExecutionOutcome | None:
        """Return the already-persisted answer for an idempotent turn retry."""

        if client_turn_id is None:
            return None

        thread = self._chat_repo.get_thread_by_id(thread_id=thread_id)
        if thread is None:
            raise ChatActionExecutionError(
                status_code=404,
                code=ChatActionExecutionErrorCode.THREAD_NOT_FOUND,
                message="That chat thread does not exist.",
            )
        access = self._entity_repo.get_entity_for_user(
            entity_id=thread.entity_id,
            user_id=actor_user.id,
        )
        if access is None:
            raise ChatActionExecutionError(
                status_code=403,
                code=ChatActionExecutionErrorCode.ACCESS_DENIED,
                message="You are not a member of this workspace.",
            )

        messages = self._chat_repo.list_messages_for_thread(thread_id=thread_id)
        for message in reversed(messages):
            metadata = message.model_metadata if isinstance(message.model_metadata, dict) else {}
            if (
                message.role == "user"
                and metadata.get("chat_turn_id") == client_turn_id
                and metadata.get("turn_status") == "received"
            ):
                if allow_received_turn:
                    return None
                raise ChatActionExecutionError(
                    status_code=409,
                    code=ChatActionExecutionErrorCode.THREAD_TURN_IN_PROGRESS,
                    message=(
                        "That chat turn is still running. I saved the request and will attach "
                        "the assistant reply to this thread when it finishes."
                    ),
                )
            if (
                message.role == "assistant"
                and metadata.get("chat_turn_id") == client_turn_id
                and metadata.get("turn_status") in _TERMINAL_TURN_STATUSES
            ):
                action_plan = (
                    self._action_repo.get_action_plan_by_id(
                        action_plan_id=message.linked_action_id,
                    )
                    if message.linked_action_id is not None
                    else None
                )
                return self._build_execution_outcome(
                    assistant_message_id=serialize_uuid(message.id),
                    assistant_content=message.content,
                    action_plan=action_plan,
                    is_read_only=message.linked_action_id is None,
                    thread=thread,
                )

        staged_actions = self._action_repo.list_actions_for_thread_turn(
            thread_id=thread_id,
            entity_id=thread.entity_id,
            client_turn_id=client_turn_id,
            limit=5,
        )
        for action in staged_actions:
            if action.status not in {"pending", "applied"}:
                continue
            grounding = self._grounding.resolve_context(
                entity_id=thread.entity_id,
                close_run_id=thread.close_run_id,
                user_id=actor_user.id,
            )
            summary = _build_recovered_turn_summary(action=action)
            assistant_message = self._chat_repo.create_message(
                thread_id=thread_id,
                role="assistant",
                content=summary,
                message_type="action",
                linked_action_id=action.id,
                grounding_payload=self._build_grounding_payload(grounding),
                model_metadata=_build_turn_metadata(
                    metadata=self._build_trace_metadata(
                        trace_id=None,
                        mode="planner",
                        tool_name=(
                            str(action.payload.get("tool_name"))
                            if isinstance(action.payload.get("tool_name"), str)
                            else None
                        ),
                        action_status=action.status,
                        summary=summary,
                    ),
                    client_turn_id=client_turn_id,
                    turn_status="pending" if action.status == "pending" else "completed",
                ),
            )
            self._db_session.commit()
            return self._build_execution_outcome(
                assistant_message_id=serialize_uuid(assistant_message.id),
                assistant_content=assistant_message.content,
                action_plan=action,
                is_read_only=False,
                thread=thread,
            )

        return None

    def send_action_message(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        actor_user: EntityUserRecord,
        content: str,
        client_turn_id: str | None = None,
        message_grounding_payload: dict[str, Any] | None = None,
        operator_message_for_memory: str | None = None,
        user_message_content: str | None = None,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
        persist_user_message: bool = True,
        process_existing_user_turn: bool = False,
    ) -> ChatExecutionOutcome:
        """Plan a chat response and optionally execute the selected deterministic tool."""

        visible_user_content = user_message_content if user_message_content is not None else content
        return self._run_operator_turn(
            thread_id=thread_id,
            entity_id=entity_id,
            actor_user=actor_user,
            content=content,
            operator_message_for_memory=(
                operator_message_for_memory
                if operator_message_for_memory is not None
                else visible_user_content
            ),
            client_turn_id=client_turn_id,
            message_grounding_payload=message_grounding_payload,
            persist_user_message=persist_user_message,
            process_existing_user_turn=process_existing_user_turn,
            user_message_content=visible_user_content,
            source_surface=source_surface,
            trace_id=trace_id,
        )

    def send_direct_status_message_if_supported(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        actor_user: EntityUserRecord,
        content: str,
        client_turn_id: str | None = None,
        message_grounding_payload: dict[str, Any] | None = None,
        user_message_content: str | None = None,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> ChatExecutionOutcome | None:
        """Return an immediate deterministic read-only reply when one is available."""

        del message_grounding_payload, source_surface, trace_id, user_message_content
        _, thread = self._load_thread_context(
            thread_id=thread_id,
            entity_id=entity_id,
            user_id=actor_user.id,
        )
        active_entity_id = thread.entity_id

        try:
            replayed_outcome = self._replay_completed_turn_if_present(
                thread_id=thread_id,
                actor_user=actor_user,
                client_turn_id=client_turn_id,
            )
            if replayed_outcome is not None:
                return replayed_outcome

            _, thread = self._load_thread_context(
                thread_id=thread_id,
                entity_id=active_entity_id,
                user_id=actor_user.id,
            )
            active_entity_id = thread.entity_id
            operator_memory = self._memory_for_thread(
                thread_id=thread_id,
                entity_id=active_entity_id,
                actor_user_id=actor_user.id,
                context_payload=thread.context_payload,
            )
            snapshot: dict[str, Any] | None = None
            assistant_content = None
            document_repository = getattr(self, "_document_repository", None)
            if (
                thread.close_run_id is not None
                and document_repository is not None
                and _is_document_skip_follow_up_request(
                    operator_content=content,
                    operator_memory=operator_memory,
                )
            ):
                documents = document_repository.list_documents_for_close_run(
                    close_run_id=thread.close_run_id,
                )
                snapshot = {
                    "documents": [
                        {
                            "filename": document.original_filename,
                            "status": (
                                document.status.value
                                if hasattr(document.status, "value")
                                else str(document.status)
                            ),
                        }
                        for document in documents
                    ]
                }
                assistant_content = _build_document_skip_follow_up_response(
                    snapshot=snapshot,
                    operator_content=content,
                    operator_memory=operator_memory,
                )

            if assistant_content is None:
                snapshot = self._snapshot_for_thread(
                    actor_user=actor_user,
                    entity_id=active_entity_id,
                    close_run_id=thread.close_run_id,
                    thread_id=thread_id,
                )
                assistant_content = _build_direct_operator_status_response(
                    snapshot=snapshot,
                    operator_content=content,
                    operator_memory=operator_memory,
                )
            if assistant_content is None:
                return None

            return self._build_execution_outcome(
                assistant_message_id=f"direct:{uuid4()}",
                assistant_content=assistant_content,
                action_plan=None,
                is_read_only=True,
                thread=thread,
            )
        except ChatActionExecutionError:
            self._db_session.rollback()
            raise
        except Exception as error:
            self._db_session.rollback()
            raise ChatActionExecutionError(
                status_code=500,
                code=ChatActionExecutionErrorCode.EXECUTION_FAILED,
                message=_build_unexpected_operator_failure_message(error=error),
            ) from error

    def resume_operator_turn(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        actor_user: EntityUserRecord,
        objective: str,
        completed_jobs: tuple[JobRecord, ...],
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> ChatExecutionOutcome:
        """Resume one chat-owned operator objective after background jobs settle."""

        return self._run_operator_turn(
            thread_id=thread_id,
            entity_id=entity_id,
            actor_user=actor_user,
            content=_build_resume_operator_prompt(
                objective=objective,
                completed_jobs=completed_jobs,
            ),
            operator_message_for_memory=None,
            client_turn_id=None,
            message_grounding_payload=None,
            persist_user_message=False,
            source_surface=source_surface,
            trace_id=trace_id,
        )

    def _run_operator_turn(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        actor_user: EntityUserRecord,
        content: str,
        operator_message_for_memory: str | None,
        client_turn_id: str | None,
        message_grounding_payload: dict[str, Any] | None,
        persist_user_message: bool,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
        process_existing_user_turn: bool = False,
        user_message_content: str | None = None,
    ) -> ChatExecutionOutcome:
        """Execute one bounded operator turn with optional user-message persistence."""

        active_entity_id = entity_id
        grounding, thread = self._load_thread_context(
            thread_id=thread_id,
            entity_id=active_entity_id,
            user_id=actor_user.id,
        )
        active_entity_id = thread.entity_id
        self._ensure_entity_coa_available(actor_user=actor_user, entity_id=active_entity_id)
        user_message = None
        final_record: ChatActionPlanRecord | None = None
        last_tool_name: str | None = None
        last_tool_arguments: dict[str, Any] | None = None
        last_action_status = "read_only"
        last_snapshot: dict[str, Any] | None = None
        applied_results: list[dict[str, Any]] = []
        completed_summaries: list[str] = []
        seen_action_signatures: set[str] = set()
        user_message_persisted = False
        turn_lock_acquired = False

        try:
            self._lock_thread_for_turn(thread_id=thread_id)
            turn_lock_acquired = True
            replayed_outcome = self._replay_completed_turn_if_present(
                thread_id=thread_id,
                actor_user=actor_user,
                client_turn_id=client_turn_id,
                allow_received_turn=process_existing_user_turn,
            )
            if replayed_outcome is not None:
                return replayed_outcome

            if persist_user_message:
                user_message = self._chat_repo.create_message(
                    thread_id=thread_id,
                    role="user",
                    content=user_message_content if user_message_content is not None else content,
                    message_type="action",
                    linked_action_id=None,
                    grounding_payload=dict(message_grounding_payload or {}),
                    model_metadata=_build_turn_metadata(
                        metadata=None,
                        client_turn_id=client_turn_id,
                        turn_status="received",
                    ),
                )
                user_message_persisted = True
                self._db_session.commit()
                grounding, thread = self._load_thread_context(
                    thread_id=thread_id,
                    entity_id=active_entity_id,
                    user_id=actor_user.id,
                )
                active_entity_id = thread.entity_id
                pending_confirmation_outcome = self._handle_pending_plan_reply(
                    thread_id=thread_id,
                    entity_id=active_entity_id,
                    actor_user=actor_user,
                    content=content,
                    source_surface=source_surface,
                    trace_id=trace_id,
                )
                if pending_confirmation_outcome is not None:
                    return pending_confirmation_outcome
            action_message_id = user_message.id if user_message is not None else None

            for iteration in range(1, _MAX_OPERATOR_LOOP_STEPS + 1):
                loop_context = _OperatorLoopContext(
                    objective=content,
                    iteration=iteration,
                    max_iterations=_MAX_OPERATOR_LOOP_STEPS,
                    completed_summaries=tuple(completed_summaries[-4:]),
                )
                operator_memory = self._memory_for_thread(
                    thread_id=thread_id,
                    entity_id=active_entity_id,
                    actor_user_id=actor_user.id,
                    context_payload=thread.context_payload,
                )
                last_snapshot = self._snapshot_for_thread(
                    actor_user=actor_user,
                    entity_id=active_entity_id,
                    close_run_id=thread.close_run_id,
                    thread_id=thread_id,
                )
                planning = self._plan_action(
                    thread_id=thread_id,
                    entity_id=active_entity_id,
                    actor_user=actor_user,
                    content=content,
                    grounding=grounding,
                    operator_memory=operator_memory,
                    loop_context=loop_context,
                )
                planning = self._hydrate_planning_result(
                    planning=planning,
                    snapshot=last_snapshot,
                    operator_content=content,
                    operator_memory=operator_memory,
                )
                clarification = self._build_runtime_clarification(
                    planning=planning,
                    snapshot=last_snapshot,
                )
                if clarification is not None:
                    assistant_content = _compose_assistant_content(
                        assistant_response=clarification,
                        handoff_message=None,
                        result_summary=_format_operator_loop_result_summary(applied_results),
                        next_step=None,
                    )
                    assistant_message = self._chat_repo.create_message(
                        thread_id=thread_id,
                        role="assistant",
                        content=assistant_content,
                        message_type="analysis" if not applied_results else "action",
                        linked_action_id=final_record.id if final_record is not None else None,
                        grounding_payload=self._build_grounding_payload(grounding),
                        model_metadata=self._build_trace_metadata(
                            trace_id=trace_id,
                            mode="planner",
                            tool_name=planning.tool_name,
                            action_status=(
                                "read_only"
                                if not applied_results
                                else last_action_status
                            ),
                            summary=_format_operator_loop_result_summary(applied_results),
                        ),
                    )
                    self._update_thread_memory(
                        thread_id=thread_id,
                        existing_payload=thread.context_payload,
                        operator_message=operator_message_for_memory,
                        assistant_response=assistant_content,
                        tool_name=planning.tool_name,
                        tool_arguments=planning.tool_arguments,
                        action_status="read_only" if not applied_results else last_action_status,
                        trace_id=trace_id,
                        snapshot=last_snapshot,
                    )
                    self._db_session.commit()
                    return self._build_execution_outcome(
                        assistant_message_id=serialize_uuid(assistant_message.id),
                        assistant_content=assistant_message.content,
                        action_plan=final_record,
                        is_read_only=not applied_results,
                        thread=thread,
                    )
                action = self._resolve_action(planning=planning)

                if action is None:
                    assistant_content = _compose_assistant_content(
                        assistant_response=planning.assistant_response,
                        handoff_message=None,
                        result_summary=_format_operator_loop_result_summary(applied_results),
                        next_step=(
                            None
                            if _should_suppress_generic_next_step(
                                operator_content=content,
                                last_tool_name=last_tool_name,
                            )
                            else _format_next_step(last_snapshot)
                        ),
                    )
                    assistant_message = self._chat_repo.create_message(
                        thread_id=thread_id,
                        role="assistant",
                        content=assistant_content,
                        message_type="analysis" if not applied_results else "action",
                        linked_action_id=final_record.id if final_record is not None else None,
                        grounding_payload=self._build_grounding_payload(grounding),
                        model_metadata=self._build_trace_metadata(
                            trace_id=trace_id,
                            mode="planner",
                            tool_name=last_tool_name,
                            action_status="read_only" if not applied_results else "applied",
                            summary=(
                                last_snapshot.get("progress_summary")
                                if not applied_results
                                else _format_operator_loop_result_summary(applied_results)
                            ),
                        ),
                    )
                    self._update_thread_memory(
                        thread_id=thread_id,
                        existing_payload=thread.context_payload,
                        operator_message=operator_message_for_memory,
                        assistant_response=assistant_content,
                        tool_name=last_tool_name,
                        tool_arguments=last_tool_arguments,
                        action_status="read_only" if not applied_results else last_action_status,
                        trace_id=trace_id,
                        snapshot=last_snapshot,
                    )
                    self._db_session.commit()
                    return self._build_execution_outcome(
                        assistant_message_id=serialize_uuid(assistant_message.id),
                        assistant_content=assistant_message.content,
                        action_plan=final_record,
                        is_read_only=not applied_results,
                        thread=thread,
                    )

                action_signature = _build_operator_loop_action_signature(action)
                if action_signature in seen_action_signatures:
                    assistant_content = _compose_assistant_content(
                        assistant_response=(
                            "I handled the part I could complete and stopped because the next "
                            "step was resolving to the same action again."
                            if applied_results
                            else (
                                "I stopped because the next step would only repeat the "
                                "same action."
                            )
                        ),
                        handoff_message=None,
                        result_summary=_format_operator_loop_result_summary(applied_results),
                        next_step=_format_next_step(last_snapshot),
                    )
                    assistant_message = self._chat_repo.create_message(
                        thread_id=thread_id,
                        role="assistant",
                        content=assistant_content,
                        message_type="analysis" if not applied_results else "action",
                        linked_action_id=final_record.id if final_record is not None else None,
                        grounding_payload=self._build_grounding_payload(grounding),
                        model_metadata=self._build_trace_metadata(
                            trace_id=trace_id,
                            mode="planner",
                            tool_name=last_tool_name,
                            action_status="read_only" if not applied_results else "applied",
                            summary=_format_operator_loop_result_summary(applied_results),
                        ),
                    )
                    self._update_thread_memory(
                        thread_id=thread_id,
                        existing_payload=thread.context_payload,
                        operator_message=operator_message_for_memory,
                        assistant_response=assistant_content,
                        tool_name=last_tool_name,
                        action_status="read_only" if not applied_results else last_action_status,
                        trace_id=trace_id,
                        snapshot=last_snapshot,
                    )
                    self._db_session.commit()
                    return self._build_execution_outcome(
                        assistant_message_id=serialize_uuid(assistant_message.id),
                        assistant_content=assistant_message.content,
                        action_plan=final_record,
                        is_read_only=not applied_results,
                        thread=thread,
                    )
                seen_action_signatures.add(action_signature)

                action_entity_id, action_close_run_id = _resolve_action_thread_scope(
                    action=action,
                    default_entity_id=active_entity_id,
                    default_close_run_id=thread.close_run_id,
                )
                execution_context = self._build_execution_context(
                    actor_user=actor_user,
                    entity_id=action_entity_id,
                    close_run_id=action_close_run_id,
                    source_close_run_id=thread.close_run_id,
                    thread_id=thread_id,
                    operator_objective=content,
                    trace_id=trace_id,
                    source_surface=source_surface,
                )
                requires_human_approval = self._requires_human_approval(
                    action=action,
                    execution_context=execution_context,
                )
                safe_tool_arguments = _json_safe_payload(action.planning.tool_arguments)
                record = self._action_repo.create_action_plan(
                    thread_id=thread_id,
                    message_id=action_message_id,
                    entity_id=action_entity_id,
                    close_run_id=action_close_run_id,
                    actor_user_id=actor_user.id,
                    intent=action.tool.intent,
                    target_type=action.target_type,
                    target_id=action.target_id,
                    payload={
                        "tool_name": action.tool.name,
                        "tool_arguments": safe_tool_arguments,
                        "assistant_response": action.planning.assistant_response,
                        "reasoning": action.planning.reasoning,
                        "requires_human_approval": requires_human_approval,
                        "loop_iteration": iteration,
                        "turn_objective": _truncate_text(content, limit=300),
                        "chat_turn_id": client_turn_id,
                        "operator_control": self._build_operator_control_payload(
                            tool=action.tool,
                            target_type=action.target_type,
                            target_id=action.target_id,
                            requires_human_approval=requires_human_approval,
                            turn_objective=content,
                            loop_iteration=iteration,
                        ),
                    },
                    confidence=1.0,
                    autonomy_mode=grounding.context.autonomy_mode,
                    requires_human_approval=requires_human_approval,
                    reasoning=action.planning.reasoning,
                )
                final_record = record
                last_tool_name = action.tool.name
                last_tool_arguments = dict(safe_tool_arguments)

                if requires_human_approval:
                    assistant_content = _compose_assistant_content(
                        assistant_response=_build_pending_confirmation_message(
                            tool_name=action.tool.name,
                            tool_arguments=safe_tool_arguments,
                            snapshot=last_snapshot,
                        ),
                        handoff_message=None,
                        result_summary=_compose_assistant_content(
                            assistant_response=_format_operator_loop_result_summary(
                                applied_results
                            ),
                            handoff_message=None,
                            result_summary=(
                                "I have the next change ready and I'm holding it for "
                                "confirmation before I apply it."
                            ),
                            next_step=None,
                        ),
                        next_step=None,
                    )
                    assistant_message = self._chat_repo.create_message(
                        thread_id=thread_id,
                        role="assistant",
                        content=assistant_content,
                        message_type="action",
                        linked_action_id=record.id,
                        grounding_payload=self._build_grounding_payload(grounding),
                        model_metadata={
                            **self._build_trace_metadata(
                                trace_id=trace_id,
                                mode="planner",
                                tool_name=action.tool.name,
                                action_status="pending",
                                summary=_format_operator_loop_result_summary(applied_results),
                            ),
                            "requires_human_approval": True,
                        },
                    )
                    self._update_thread_memory(
                        thread_id=thread_id,
                        existing_payload=thread.context_payload,
                        operator_message=operator_message_for_memory,
                        assistant_response=assistant_content,
                        tool_name=action.tool.name,
                        tool_arguments=safe_tool_arguments,
                        action_status="pending",
                        trace_id=trace_id,
                        snapshot=last_snapshot,
                    )
                    self._db_session.commit()
                    return self._build_execution_outcome(
                        assistant_message_id=serialize_uuid(assistant_message.id),
                        assistant_content=assistant_message.content,
                        action_plan=record,
                        is_read_only=False,
                        thread=thread,
                    )

                applied_result = _json_safe_payload(
                    self._execute_action(
                        action=action,
                        execution_context=execution_context,
                    )
                )
                grounding, thread, _ = self._handoff_thread_scope_if_needed(
                    actor_user=actor_user,
                    entity_id=active_entity_id,
                    thread_id=thread_id,
                    thread=thread,
                    grounding=grounding,
                    applied_result=applied_result,
                )
                active_entity_id = thread.entity_id
                final_record = self._action_repo.update_action_plan_status(
                    action_plan_id=record.id,
                    status="applied",
                    applied_result=applied_result,
                ) or record
                applied_results.append(applied_result)
                completed_summary = _humanize_applied_result(applied_result)
                if completed_summary not in completed_summaries:
                    completed_summaries.append(completed_summary)
                last_action_status = "applied"

                self._db_session.commit()
                grounding, thread = self._load_thread_context(
                    thread_id=thread_id,
                    entity_id=active_entity_id,
                    user_id=actor_user.id,
                )
                active_entity_id = thread.entity_id

                async_job_group = _extract_async_job_group(applied_result=applied_result)
                if async_job_group is not None:
                    last_snapshot = self._snapshot_for_thread(
                        actor_user=actor_user,
                        entity_id=active_entity_id,
                        close_run_id=thread.close_run_id,
                        thread_id=thread_id,
                    )
                    continuation = ChatOperatorContinuation(
                        continuation_group_id=UUID(async_job_group["continuation_group_id"]),
                        thread_id=thread_id,
                        entity_id=active_entity_id,
                        actor_user_id=actor_user.id,
                        objective=content,
                        originating_tool=last_tool_name or "async_workflow",
                        source_surface=(
                            source_surface.value
                            if isinstance(source_surface, AuditSourceSurface)
                            else str(source_surface)
                        ),
                    )
                    assistant_content = _compose_assistant_content(
                        assistant_response=_build_async_wait_message(applied_result=applied_result),
                        handoff_message=None,
                        result_summary=_format_operator_loop_result_summary(applied_results[:-1]),
                        next_step=None,
                    )
                    assistant_message = self._chat_repo.create_message(
                        thread_id=thread_id,
                        role="assistant",
                        content=assistant_content,
                        message_type="action",
                        linked_action_id=final_record.id if final_record is not None else None,
                        grounding_payload=self._build_grounding_payload(grounding),
                        model_metadata=self._build_trace_metadata(
                            trace_id=trace_id,
                            mode="planner",
                            tool_name=last_tool_name,
                            action_status="waiting_async",
                            summary=_summarize_applied_result(applied_result),
                        ),
                    )
                    self._update_thread_memory(
                        thread_id=thread_id,
                        existing_payload=build_pending_async_turn_payload(
                            existing_payload=thread.context_payload,
                            continuation=continuation,
                            job_count=int(async_job_group["job_count"]),
                            trace_id=trace_id,
                        ),
                        operator_message=operator_message_for_memory,
                        assistant_response=assistant_content,
                        tool_name=last_tool_name,
                        tool_arguments=last_tool_arguments,
                        action_status="waiting_async",
                        trace_id=trace_id,
                        snapshot=last_snapshot,
                    )
                    self._db_session.commit()
                    return self._build_execution_outcome(
                        assistant_message_id=serialize_uuid(assistant_message.id),
                        assistant_content=assistant_message.content,
                        action_plan=final_record,
                        is_read_only=False,
                        thread=thread,
                    )

                if _is_terminal_workspace_admin_tool(
                    action.tool.name
                ) and not _should_continue_after_workspace_admin_tool(
                    tool_name=action.tool.name,
                    operator_content=content,
                ):
                    last_snapshot = self._snapshot_for_thread(
                        actor_user=actor_user,
                        entity_id=active_entity_id,
                        close_run_id=thread.close_run_id,
                        thread_id=thread_id,
                    )
                    assistant_content = _compose_assistant_content(
                        assistant_response=_format_operator_loop_result_summary(applied_results),
                        handoff_message=None,
                        result_summary=None,
                        next_step=None,
                    )
                    assistant_message = self._chat_repo.create_message(
                        thread_id=thread_id,
                        role="assistant",
                        content=assistant_content,
                        message_type="action",
                        linked_action_id=final_record.id if final_record is not None else None,
                        grounding_payload=self._build_grounding_payload(grounding),
                        model_metadata=self._build_trace_metadata(
                            trace_id=trace_id,
                            mode="planner",
                            tool_name=last_tool_name,
                            action_status="applied",
                            summary=_format_operator_loop_result_summary(applied_results),
                        ),
                    )
                    self._update_thread_memory(
                        thread_id=thread_id,
                        existing_payload=thread.context_payload,
                        operator_message=operator_message_for_memory,
                        assistant_response=assistant_content,
                        tool_name=last_tool_name,
                        tool_arguments=last_tool_arguments,
                        action_status="applied",
                        trace_id=trace_id,
                        snapshot=last_snapshot,
                    )
                    self._db_session.commit()
                    return self._build_execution_outcome(
                        assistant_message_id=serialize_uuid(assistant_message.id),
                        assistant_content=assistant_message.content,
                        action_plan=final_record,
                        is_read_only=False,
                        thread=thread,
                    )

                if iteration == _MAX_OPERATOR_LOOP_STEPS:
                    last_snapshot = self._snapshot_for_thread(
                        actor_user=actor_user,
                        entity_id=active_entity_id,
                        close_run_id=thread.close_run_id,
                        thread_id=thread_id,
                    )
                    assistant_content = _compose_assistant_content(
                        assistant_response=(
                            "I completed the main steps I could safely take in this turn."
                        ),
                        handoff_message=None,
                        result_summary=_format_operator_loop_result_summary(applied_results),
                        next_step=_format_next_step(last_snapshot),
                    )
                    assistant_message = self._chat_repo.create_message(
                        thread_id=thread_id,
                        role="assistant",
                        content=assistant_content,
                        message_type="action",
                        linked_action_id=final_record.id if final_record is not None else None,
                        grounding_payload=self._build_grounding_payload(grounding),
                        model_metadata=self._build_trace_metadata(
                            trace_id=trace_id,
                            mode="planner",
                            tool_name=last_tool_name,
                            action_status="applied",
                            summary=_format_operator_loop_result_summary(applied_results),
                        ),
                    )
                    self._update_thread_memory(
                        thread_id=thread_id,
                        existing_payload=thread.context_payload,
                        operator_message=operator_message_for_memory,
                        assistant_response=assistant_content,
                        tool_name=last_tool_name,
                        tool_arguments=last_tool_arguments,
                        action_status="applied",
                        trace_id=trace_id,
                        snapshot=last_snapshot,
                    )
                    self._db_session.commit()
                    return self._build_execution_outcome(
                        assistant_message_id=serialize_uuid(assistant_message.id),
                        assistant_content=assistant_message.content,
                        action_plan=final_record,
                        is_read_only=False,
                        thread=thread,
                    )
        except ChatActionExecutionError as error:
            if error.code is ChatActionExecutionErrorCode.THREAD_TURN_IN_PROGRESS:
                self._db_session.rollback()
                raise
            if applied_results:
                self._db_session.rollback()
                grounding, thread = self._load_thread_context(
                    thread_id=thread_id,
                    entity_id=active_entity_id,
                    user_id=actor_user.id,
                )
                last_snapshot = self._snapshot_for_thread(
                    actor_user=actor_user,
                    entity_id=active_entity_id,
                    close_run_id=thread.close_run_id,
                    thread_id=thread_id,
                )
                assistant_content = _compose_assistant_content(
                    assistant_response=(
                        "I completed part of that request and then hit a blocker on the next step."
                    ),
                    handoff_message=error.message,
                    result_summary=_format_operator_loop_result_summary(applied_results),
                    next_step=_format_next_step(last_snapshot),
                )
                assistant_message = self._chat_repo.create_message(
                    thread_id=thread_id,
                    role="assistant",
                    content=assistant_content,
                    message_type="action",
                    linked_action_id=final_record.id if final_record is not None else None,
                    grounding_payload=self._build_grounding_payload(grounding),
                    model_metadata=self._build_trace_metadata(
                        trace_id=trace_id,
                        mode="planner",
                        tool_name=last_tool_name,
                        action_status="partial",
                        summary=_format_operator_loop_result_summary(applied_results),
                    ),
                )
                self._update_thread_memory(
                    thread_id=thread_id,
                    existing_payload=thread.context_payload,
                    operator_message=operator_message_for_memory,
                    assistant_response=assistant_content,
                    tool_name=last_tool_name,
                    tool_arguments=last_tool_arguments,
                    action_status="partial",
                    trace_id=trace_id,
                    snapshot=last_snapshot,
                )
                self._db_session.commit()
                return self._build_execution_outcome(
                    assistant_message_id=serialize_uuid(assistant_message.id),
                    assistant_content=assistant_message.content,
                    action_plan=final_record,
                    is_read_only=False,
                    thread=thread,
                )
            self._db_session.rollback()
            surfaced_outcome = self._surface_operator_error_in_thread(
                thread_id=thread_id,
                entity_id=active_entity_id,
                actor_user=actor_user,
                content=content,
                operator_message_for_memory=operator_message_for_memory,
                client_turn_id=client_turn_id,
                message_grounding_payload=message_grounding_payload,
                persist_user_message=persist_user_message and not user_message_persisted,
                user_message_content=user_message_content,
                trace_id=trace_id,
                error=error,
                tool_name=last_tool_name,
            )
            if surfaced_outcome is not None:
                return surfaced_outcome
            raise
        except Exception as error:
            self._db_session.rollback()
            surfaced_error = ChatActionExecutionError(
                status_code=500,
                code=ChatActionExecutionErrorCode.EXECUTION_FAILED,
                message=_build_unexpected_operator_failure_message(error=error),
            )
            surfaced_outcome = self._surface_operator_error_in_thread(
                thread_id=thread_id,
                entity_id=active_entity_id,
                actor_user=actor_user,
                content=content,
                operator_message_for_memory=operator_message_for_memory,
                client_turn_id=client_turn_id,
                message_grounding_payload=message_grounding_payload,
                persist_user_message=persist_user_message and not user_message_persisted,
                user_message_content=user_message_content,
                trace_id=trace_id,
                error=surfaced_error,
                tool_name=last_tool_name,
            )
            if surfaced_outcome is not None:
                return surfaced_outcome
            raise surfaced_error from error
        finally:
            if turn_lock_acquired:
                self._release_thread_turn_lock(thread_id=thread_id)

    def _surface_operator_error_in_thread(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        actor_user: EntityUserRecord,
        content: str,
        operator_message_for_memory: str | None,
        client_turn_id: str | None,
        message_grounding_payload: dict[str, Any] | None,
        persist_user_message: bool,
        user_message_content: str | None,
        trace_id: str | None,
        error: ChatActionExecutionError,
        tool_name: str | None,
    ) -> ChatExecutionOutcome | None:
        """Persist a natural assistant failure reply instead of leaking a raw action error."""

        try:
            loaded_context = self._load_thread_context_for_error_surface(
                thread_id=thread_id,
                entity_id=entity_id,
                actor_user=actor_user,
            )
            if loaded_context is None:
                return None
            grounding, thread = loaded_context
            if persist_user_message:
                self._chat_repo.create_message(
                    thread_id=thread_id,
                    role="user",
                    content=user_message_content if user_message_content is not None else content,
                    message_type="action",
                    linked_action_id=None,
                    grounding_payload=dict(message_grounding_payload or {}),
                    model_metadata=_build_turn_metadata(
                        metadata=None,
                        client_turn_id=client_turn_id,
                        turn_status="received",
                    ),
                )
            snapshot = self._snapshot_for_thread(
                actor_user=actor_user,
                entity_id=thread.entity_id,
                close_run_id=thread.close_run_id,
                thread_id=thread_id,
            )
            assistant_content = _compose_assistant_content(
                assistant_response=_build_operator_failure_message(
                    error=error,
                    tool_name=tool_name,
                ),
                handoff_message=None,
                result_summary=None,
                next_step=_build_failure_next_step(snapshot=snapshot),
            )
            assistant_message = self._chat_repo.create_message(
                thread_id=thread_id,
                role="assistant",
                content=assistant_content,
                message_type="warning",
                linked_action_id=None,
                grounding_payload=self._build_grounding_payload(grounding),
                model_metadata=self._build_trace_metadata(
                    trace_id=trace_id,
                    mode="planner",
                    tool_name=tool_name,
                    action_status="failed",
                    summary=error.message,
                ),
            )
            self._update_thread_memory(
                thread_id=thread_id,
                existing_payload=thread.context_payload,
                operator_message=operator_message_for_memory,
                assistant_response=assistant_content,
                tool_name=tool_name,
                tool_arguments=None,
                action_status="failed",
                trace_id=trace_id,
                snapshot=snapshot,
            )
            self._db_session.commit()
            return self._build_execution_outcome(
                assistant_message_id=serialize_uuid(assistant_message.id),
                assistant_content=assistant_content,
                action_plan=None,
                is_read_only=True,
                thread=thread,
            )
        except Exception:
            self._db_session.rollback()
            return None

    def _load_thread_context_for_error_surface(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        actor_user: EntityUserRecord,
    ) -> tuple[GroundingContextRecord, Any] | None:
        """Load a thread for a failure reply, recovering from stale workspace scope."""

        try:
            return self._load_thread_context(
                thread_id=thread_id,
                entity_id=entity_id,
                user_id=actor_user.id,
            )
        except ChatActionExecutionError as error:
            if error.code not in {
                ChatActionExecutionErrorCode.ACCESS_DENIED,
                ChatActionExecutionErrorCode.THREAD_NOT_FOUND,
            }:
                raise

        get_thread_by_id = getattr(self._chat_repo, "get_thread_by_id", None)
        if not callable(get_thread_by_id):
            return None
        thread = get_thread_by_id(thread_id=thread_id)
        if thread is None:
            return None

        access = self._entity_repo.get_entity_for_user(
            entity_id=thread.entity_id,
            user_id=actor_user.id,
        )
        if access is None:
            return None

        grounding = self._grounding.resolve_context(
            entity_id=thread.entity_id,
            close_run_id=thread.close_run_id,
            user_id=actor_user.id,
        )
        return grounding, thread

    def activate_async_job_group(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        actor_user: EntityUserRecord,
        continuation_group_id: UUID,
        objective: str,
        originating_tool: str,
        job_count: int,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> None:
        """Persist one chat-owned async continuation group onto the thread context."""

        _, thread = self._load_thread_context(
            thread_id=thread_id,
            entity_id=entity_id,
            user_id=actor_user.id,
        )
        snapshot = self._snapshot_for_thread(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=thread.close_run_id,
            thread_id=thread_id,
        )
        self._update_thread_memory(
            thread_id=thread_id,
            existing_payload=build_pending_async_turn_payload(
                existing_payload=thread.context_payload,
                continuation=ChatOperatorContinuation(
                    continuation_group_id=continuation_group_id,
                    thread_id=thread_id,
                    entity_id=entity_id,
                    actor_user_id=actor_user.id,
                    objective=objective,
                    originating_tool=originating_tool,
                    source_surface=(
                        source_surface.value
                        if isinstance(source_surface, AuditSourceSurface)
                        else str(source_surface)
                    ),
                ),
                job_count=job_count,
                trace_id=trace_id,
            ),
            operator_message=None,
            assistant_response=None,
            tool_name=originating_tool,
            action_status="waiting_async",
            trace_id=trace_id,
            snapshot=snapshot,
        )
        self._db_session.commit()

    def approve_action_plan(
        self,
        *,
        action_plan_id: UUID,
        thread_id: UUID,
        entity_id: UUID,
        actor_user: EntityUserRecord,
        reason: str | None,
        approval_policy: str | None = None,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> ChatActionPlanRecord:
        """Apply one pending action plan after explicit human approval."""

        plan = self._action_repo.get_action_plan_for_thread(
            action_plan_id=action_plan_id,
            thread_id=thread_id,
        )
        if plan is None or plan.entity_id != entity_id:
            raise ChatActionExecutionError(
                status_code=404,
                code=ChatActionExecutionErrorCode.ACTION_PLAN_NOT_FOUND,
                message="That action plan does not exist in this workspace.",
            )
        if plan.status != "pending":
            raise ChatActionExecutionError(
                status_code=409,
                code=ChatActionExecutionErrorCode.INVALID_ACTION_PLAN,
                message="Only pending chat actions can be approved.",
            )

        thread = self._chat_repo.get_thread_for_entity(thread_id=thread_id, entity_id=entity_id)
        thread_context_payload = (
            dict(getattr(thread, "context_payload", {})) if thread is not None else {}
        )
        if approval_policy == "auto_release_for_thread":
            thread_context_payload = _with_thread_approval_policy(
                context_payload=thread_context_payload,
                mode="auto_release_for_thread",
                actor_user_id=actor_user.id,
                reason=reason,
            )
            updated_thread = self._chat_repo.update_thread_context(
                thread_id=thread_id,
                context_payload=thread_context_payload,
            )
            if updated_thread is not None:
                thread = updated_thread
        payload = dict(plan.payload)
        self._require_plan_scope_match(
            thread=thread,
            plan=plan,
            payload=payload,
        )
        planning = self._planning_from_payload(payload)
        action = self._resolve_action(planning=planning)
        if action is None:
            raise ChatActionExecutionError(
                status_code=422,
                code=ChatActionExecutionErrorCode.INVALID_ACTION_PLAN,
                message="The stored chat action payload is incomplete.",
            )

        execution_close_run_id, source_close_run_id = self._resolve_action_execution_scopes(
            thread=thread,
            plan=plan,
            payload=payload,
        )
        applied_result = _json_safe_payload(
            self._execute_action(
                action=action,
                execution_context=self._build_execution_context(
                    actor_user=actor_user,
                    entity_id=entity_id,
                    close_run_id=execution_close_run_id,
                    source_close_run_id=source_close_run_id,
                    thread_id=thread_id,
                    operator_objective=(
                        str(payload.get("turn_objective"))
                        if payload.get("turn_objective") is not None
                        else None
                    ),
                    trace_id=trace_id,
                    source_surface=source_surface,
                ),
            ),
        )
        grounding = None
        handoff_message = None
        if thread is not None:
            grounding = self._grounding.resolve_context(
                entity_id=entity_id,
                close_run_id=thread.close_run_id,
                user_id=actor_user.id,
            )
            grounding, thread, handoff_message = self._handoff_thread_scope_if_needed(
                actor_user=actor_user,
                entity_id=entity_id,
                thread_id=thread_id,
                thread=thread,
                grounding=grounding,
                applied_result=applied_result,
            )
        updated = self._action_repo.update_action_plan_status(
            action_plan_id=action_plan_id,
            status="applied",
            applied_result={
                **applied_result,
                "approved_by": str(actor_user.id),
                "approval_reason": reason,
            },
        )
        if updated is None:
            raise ChatActionExecutionError(
                status_code=404,
                code=ChatActionExecutionErrorCode.ACTION_PLAN_NOT_FOUND,
                message="That action plan could not be updated.",
            )

        applied_results = [applied_result]
        if thread is not None and action.tool.name == "delete_close_run":
            follow_up_result = self._apply_post_approval_close_run_follow_up(
                thread_id=thread_id,
                entity_id=entity_id,
                actor_user=actor_user,
                thread=thread,
                grounding=grounding,
                payload=payload,
                source_surface=source_surface,
                trace_id=trace_id,
            )
            if follow_up_result is not None:
                grounding, thread, follow_up_applied_result = follow_up_result
                applied_results.append(follow_up_applied_result)

        self._chat_repo.create_message(
            thread_id=thread_id,
            role="assistant",
            content=_compose_assistant_content(
                assistant_response=(
                    payload.get("assistant_response")
                    if isinstance(payload.get("assistant_response"), str)
                    else "Approved action executed."
                ),
                handoff_message=handoff_message,
                result_summary=_format_operator_loop_result_summary(applied_results),
                next_step=(
                    _format_next_step(
                        self._snapshot_for_thread(
                            actor_user=actor_user,
                            entity_id=entity_id,
                            close_run_id=thread.close_run_id,
                            thread_id=thread_id,
                        )
                    )
                    if thread is not None
                    else None
                ),
            ),
            message_type="action",
            linked_action_id=updated.id,
            grounding_payload=(
                self._build_grounding_payload(grounding)
                if grounding is not None
                else {}
            ),
            model_metadata=self._build_trace_metadata(
                trace_id=trace_id,
                mode="approval",
                tool_name=action.tool.name,
                action_status="applied",
                summary=_summarize_applied_result(applied_result),
            ),
        )
        if thread is not None:
            snapshot = self._snapshot_for_thread(
                actor_user=actor_user,
                entity_id=entity_id,
                close_run_id=thread.close_run_id,
                thread_id=thread_id,
            )
            self._update_thread_memory(
                thread_id=thread_id,
                existing_payload=thread_context_payload,
                operator_message=None,
                assistant_response=(
                    payload.get("assistant_response")
                    if isinstance(payload.get("assistant_response"), str)
                    else None
                ),
                tool_name=action.tool.name,
                tool_arguments=planning.tool_arguments,
                action_status="applied",
                trace_id=trace_id,
                snapshot=snapshot,
            )
        self._db_session.commit()
        return updated

    def _apply_post_approval_close_run_follow_up(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        actor_user: EntityUserRecord,
        thread: Any,
        grounding: GroundingContextRecord | None,
        payload: dict[str, Any],
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> tuple[GroundingContextRecord | None, Any, dict[str, Any]] | None:
        """Continue a governed close-run correction after the destructive step is approved."""

        objective = payload.get("turn_objective")
        if not isinstance(objective, str) or not objective.strip():
            return None

        snapshot = self._snapshot_for_thread(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=thread.close_run_id,
            thread_id=thread_id,
        )
        planning = _build_open_close_run_intent_planning(
            snapshot=snapshot,
            operator_content=objective,
        )
        if (
            planning is None
            or planning.mode != "tool"
            or planning.tool_name != "open_close_run"
        ):
            return None

        action = self._resolve_action(planning=planning)
        if action is None:
            return None
        action_entity_id, action_close_run_id = _resolve_action_thread_scope(
            action=action,
            default_entity_id=entity_id,
            default_close_run_id=thread.close_run_id,
        )
        execution_context = self._build_execution_context(
            actor_user=actor_user,
            entity_id=action_entity_id,
            close_run_id=action_close_run_id,
            source_close_run_id=thread.close_run_id,
            thread_id=thread_id,
            operator_objective=objective,
            trace_id=trace_id,
            source_surface=source_surface,
        )
        if self._requires_human_approval(action=action, execution_context=execution_context):
            return None

        safe_tool_arguments = _json_safe_payload(action.planning.tool_arguments)
        follow_up_record = self._action_repo.create_action_plan(
            thread_id=thread_id,
            message_id=None,
            entity_id=action_entity_id,
            close_run_id=action_close_run_id,
            actor_user_id=actor_user.id,
            intent=action.tool.intent,
            target_type=action.target_type,
            target_id=action.target_id,
            payload={
                "tool_name": action.tool.name,
                "tool_arguments": safe_tool_arguments,
                "assistant_response": action.planning.assistant_response,
                "reasoning": action.planning.reasoning,
                "requires_human_approval": False,
                "turn_objective": _truncate_text(objective, limit=300),
            },
            confidence=1.0,
            autonomy_mode=(
                grounding.context.autonomy_mode if grounding is not None else "human_review"
            ),
            requires_human_approval=False,
            reasoning=action.planning.reasoning,
        )
        applied_result = _json_safe_payload(
            self._execute_action(
                action=action,
                execution_context=execution_context,
            )
        )
        grounding, thread, _ = self._handoff_thread_scope_if_needed(
            actor_user=actor_user,
            entity_id=entity_id,
            thread_id=thread_id,
            thread=thread,
            grounding=grounding,
            applied_result=applied_result,
        )
        self._action_repo.update_action_plan_status(
            action_plan_id=follow_up_record.id,
            status="applied",
            applied_result=applied_result,
        )
        return grounding, thread, applied_result

    def reject_action_plan(
        self,
        *,
        action_plan_id: UUID,
        thread_id: UUID,
        entity_id: UUID,
        actor_user: EntityUserRecord,
        reason: str,
    ) -> ChatActionPlanRecord:
        """Reject one pending action plan and persist the decision in chat history."""

        plan = self._action_repo.get_action_plan_for_thread(
            action_plan_id=action_plan_id,
            thread_id=thread_id,
        )
        if plan is None or plan.entity_id != entity_id:
            raise ChatActionExecutionError(
                status_code=404,
                code=ChatActionExecutionErrorCode.ACTION_PLAN_NOT_FOUND,
                message="That action plan does not exist in this workspace.",
            )
        if plan.status != "pending":
            raise ChatActionExecutionError(
                status_code=409,
                code=ChatActionExecutionErrorCode.INVALID_ACTION_PLAN,
                message="Only pending chat actions can be rejected.",
            )

        updated = self._action_repo.update_action_plan_status(
            action_plan_id=action_plan_id,
            status="rejected",
            rejected_reason=reason,
        )
        if updated is None:
            raise ChatActionExecutionError(
                status_code=404,
                code=ChatActionExecutionErrorCode.ACTION_PLAN_NOT_FOUND,
                message="That action plan could not be updated.",
            )

        self._chat_repo.create_message(
            thread_id=thread_id,
            role="assistant",
            content=f"Action rejected. Reason recorded: {reason}",
            message_type="warning",
            linked_action_id=updated.id,
            grounding_payload={},
            model_metadata=self._build_trace_metadata(
                trace_id=None,
                mode="rejection",
                tool_name=None,
                action_status="rejected",
                summary=reason,
            ),
        )
        thread = self._chat_repo.get_thread_for_entity(thread_id=thread_id, entity_id=entity_id)
        if thread is not None:
            snapshot = self._snapshot_for_thread(
                actor_user=actor_user,
                entity_id=entity_id,
                close_run_id=plan.close_run_id,
                thread_id=thread_id,
            )
            self._update_thread_memory(
                thread_id=thread_id,
                existing_payload=thread.context_payload,
                operator_message=None,
                assistant_response=reason,
                tool_name=(
                    str(plan.payload.get("tool_name"))
                    if isinstance(plan.payload.get("tool_name"), str)
                    else None
                ),
                tool_arguments=(
                    dict(plan.payload.get("tool_arguments"))
                    if isinstance(plan.payload.get("tool_arguments"), dict)
                    else None
                ),
                action_status="rejected",
                trace_id=None,
                snapshot=snapshot,
            )
        self._db_session.commit()
        return updated

    def get_thread_workspace(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        actor_user: EntityUserRecord,
    ) -> ChatThreadWorkspaceResponse:
        """Return the agent workspace context exposed for one chat thread."""

        self._ensure_entity_coa_available(actor_user=actor_user, entity_id=entity_id)
        _, thread = self._load_thread_context(
            thread_id=thread_id,
            entity_id=entity_id,
            user_id=actor_user.id,
        )
        snapshot = self._snapshot_for_thread(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=thread.close_run_id,
            thread_id=thread_id,
        )
        messages = self._chat_repo.list_messages_for_thread(thread_id=thread_id, limit=100)
        operator_memory = self._memory_for_thread(
            thread_id=thread.id,
            entity_id=entity_id,
            actor_user_id=actor_user.id,
            context_payload=thread.context_payload,
        )
        pending_actions = self._action_repo.list_pending_actions_for_thread(
            thread_id=thread_id,
            entity_id=entity_id,
            limit=3,
        )
        grounding_payload = {
            key: thread.context_payload.get(key)
            for key in (
                "entity_id",
                "entity_name",
                "close_run_id",
                "period_label",
                "autonomy_mode",
                "base_currency",
            )
        }
        return ChatThreadWorkspaceResponse(
            thread_id=str(thread.id),
            grounding=GroundingContext(**grounding_payload),
            progress_summary=snapshot.get("progress_summary"),
            coa=AgentCoaSummary(**snapshot.get("coa", {})),
            readiness=AgentRunReadiness(**snapshot.get("readiness", {})),
            memory=operator_memory,
            tools=self._build_tool_manifest_items(),
            recent_traces=self._build_recent_traces(messages),
            operator_controls=self._build_operator_controls(
                thread=thread,
                snapshot=snapshot,
                operator_memory=operator_memory,
                pending_actions=pending_actions,
            ),
            mcp_manifest=self._build_mcp_manifest(),
        )

    def list_registered_tools(self) -> tuple[AgentToolManifestItem, ...]:
        """Return the portable tool manifest exposed by the accounting agent."""

        return self._build_tool_manifest_items()

    def read_mcp_manifest(self) -> dict[str, Any]:
        """Return the canonical MCP-style manifest for this operator runtime."""

        return self._build_mcp_manifest()

    def _build_tool_manifest_items(self) -> tuple[AgentToolManifestItem, ...]:
        """Return the namespace-aware manifest items for registered operator tools."""

        return tuple(
            AgentToolManifestItem(
                name=tool.name,
                namespace=tool.namespace,
                namespace_label=tool.namespace_label,
                specialist_name=tool.specialist_name,
                specialist_mission=tool.specialist_mission,
                prompt_signature=tool.prompt_signature,
                description=tool.description,
                intent=tool.intent,
                requires_human_approval=tool.requires_human_approval,
                input_schema=tool.input_schema,
            )
            for tool in self._tool_registry.list_tools()
        )

    def _build_tool_namespace_manifest(self) -> tuple[dict[str, Any], ...]:
        """Return the grouped operator-domain manifest for external runtimes."""

        return tuple(
            {
                "name": namespace.name,
                "label": namespace.label,
                "specialist_name": namespace.specialist_name,
                "specialist_mission": namespace.specialist_mission,
                "tool_names": list(namespace.tool_names),
            }
            for namespace in self._tool_registry.list_namespaces()
        )

    def execute_registered_tool(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        actor_user: EntityUserRecord,
        tool_name: str,
        tool_arguments: dict[str, Any],
        trace_id: str | None,
        source_surface: AuditSourceSurface,
    ) -> McpToolCallOutcome:
        """Execute or stage one deterministic tool call through the shared agent runtime."""

        self._ensure_entity_coa_available(actor_user=actor_user, entity_id=entity_id)
        grounding, thread = self._load_thread_context(
            thread_id=thread_id,
            entity_id=entity_id,
            user_id=actor_user.id,
        )
        planning = AgentPlanningResult(
            mode="tool",
            assistant_response=(
                f"Deterministic tool '{tool_name}' requested through the MCP runtime."
            ),
            reasoning="Explicit MCP tool call requested by the operator.",
            tool_name=tool_name,
            tool_arguments=tool_arguments,
        )

        try:
            action = self._resolve_action(planning=planning)
            if action is None:
                raise ChatActionExecutionError(
                    status_code=422,
                    code=ChatActionExecutionErrorCode.INVALID_ACTION_PLAN,
                    message="The requested tool call did not resolve to an executable action.",
                )

            operator_message = self._chat_repo.create_message(
                thread_id=thread_id,
                role="user",
                content=f"MCP tool call: {tool_name}",
                message_type="action",
                linked_action_id=None,
                grounding_payload={"tool_arguments": _json_safe_payload(tool_arguments)},
                model_metadata=self._build_trace_metadata(
                    trace_id=trace_id,
                    mode="mcp_request",
                    tool_name=tool_name,
                    action_status="requested",
                    summary=None,
                ),
            )
            execution_context = self._build_execution_context(
                actor_user=actor_user,
                entity_id=entity_id,
                close_run_id=thread.close_run_id,
                source_close_run_id=thread.close_run_id,
                thread_id=thread_id,
                operator_objective=f"MCP tool call: {tool_name}",
                trace_id=trace_id,
                source_surface=source_surface,
            )
            requires_human_approval = self._requires_human_approval(
                action=action,
                execution_context=execution_context,
            )
            record = self._action_repo.create_action_plan(
                thread_id=thread_id,
                message_id=operator_message.id,
                entity_id=entity_id,
                close_run_id=thread.close_run_id,
                actor_user_id=actor_user.id,
                intent=action.tool.intent,
                target_type=action.target_type,
                target_id=action.target_id,
                payload={
                    "tool_name": action.tool.name,
                    "tool_arguments": _json_safe_payload(action.planning.tool_arguments),
                    "assistant_response": planning.assistant_response,
                    "reasoning": planning.reasoning,
                    "requires_human_approval": requires_human_approval,
                    "requested_via": "mcp",
                    "operator_control": self._build_operator_control_payload(
                        tool=action.tool,
                        target_type=action.target_type,
                        target_id=action.target_id,
                        requires_human_approval=requires_human_approval,
                        turn_objective=f"MCP tool call: {tool_name}",
                        loop_iteration=None,
                    ),
                },
                confidence=1.0,
                autonomy_mode=grounding.context.autonomy_mode,
                requires_human_approval=requires_human_approval,
                reasoning=planning.reasoning,
            )

            applied_result: dict[str, Any] | None = None
            final_record = record
            action_status = "pending" if requires_human_approval else "applied"
            summary = (
                f"Tool '{tool_name}' is staged for approval before the system applies it."
                if requires_human_approval
                else f"Tool '{tool_name}' executed successfully."
            )
            if not requires_human_approval:
                applied_result = _json_safe_payload(
                    self._execute_action(
                        action=action,
                        execution_context=execution_context,
                    )
                )
                grounding, thread, handoff_message = self._handoff_thread_scope_if_needed(
                    actor_user=actor_user,
                    entity_id=entity_id,
                    thread_id=thread_id,
                    thread=thread,
                    grounding=grounding,
                    applied_result=applied_result,
                )
                final_record = self._action_repo.update_action_plan_status(
                    action_plan_id=record.id,
                    status="applied",
                    applied_result=applied_result,
                ) or record
                summary = (
                    f"{_summarize_applied_result(applied_result) or summary}"
                    f"{_format_scope_handoff_summary(handoff_message)}"
                )

            snapshot = self._snapshot_for_thread(
                actor_user=actor_user,
                entity_id=entity_id,
                close_run_id=thread.close_run_id,
                thread_id=thread_id,
            )
            assistant_message = self._chat_repo.create_message(
                thread_id=thread_id,
                role="assistant",
                content=summary,
                message_type="action",
                linked_action_id=final_record.id,
                grounding_payload=self._build_grounding_payload(grounding),
                model_metadata={
                    **self._build_trace_metadata(
                        trace_id=trace_id,
                        mode="mcp",
                        tool_name=tool_name,
                        action_status=action_status,
                        summary=summary,
                    ),
                    "requires_human_approval": requires_human_approval,
                },
            )
            self._update_thread_memory(
                thread_id=thread_id,
                existing_payload=thread.context_payload,
                operator_message=f"MCP tool call: {tool_name}",
                assistant_response=summary,
                tool_name=tool_name,
                action_status=action_status,
                trace_id=trace_id,
                snapshot=snapshot,
            )
            self._db_session.commit()
            return McpToolCallOutcome(
                message_id=serialize_uuid(assistant_message.id),
                tool_name=tool_name,
                status=action_status,
                requires_human_approval=requires_human_approval,
                action_plan_id=serialize_uuid(final_record.id),
                summary=summary,
                result=applied_result,
            )
        except ChatActionExecutionError:
            self._db_session.rollback()
            raise
        except Exception as error:
            self._db_session.rollback()
            raise ChatActionExecutionError(
                status_code=500,
                code=ChatActionExecutionErrorCode.EXECUTION_FAILED,
                message="The deterministic tool call stopped before it finished.",
            ) from error

    def _snapshot_for_thread(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID | None,
        thread_id: UUID,
    ) -> dict[str, Any]:
        """Build the live workspace snapshot for one thread."""

        return self._workspace_builder.build_snapshot(
            actor=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
            thread_id=thread_id,
        )

    def _ensure_entity_coa_available(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
    ) -> None:
        """Ensure chat/workbench reads observe the canonical active-or-fallback COA state."""

        self._coa_service.read_workspace(
            actor_user=actor_user,
            entity_id=entity_id,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=None,
        )

    def _build_execution_context(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID | None,
        source_close_run_id: UUID | None,
        thread_id: UUID,
        operator_objective: str | None,
        trace_id: str | None,
        source_surface: AuditSourceSurface,
    ) -> AgentExecutionContext:
        """Build one deterministic execution context for a tool invocation."""

        return AgentExecutionContext(
            actor=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
            source_close_run_id=source_close_run_id,
            thread_id=thread_id,
            operator_objective=operator_objective,
            trace_id=trace_id,
            source_surface=source_surface,
        )

    def _requires_human_approval(
        self,
        *,
        action: AgentPlannedAction,
        execution_context: AgentExecutionContext,
    ) -> bool:
        """Resolve the runtime approval requirement for one planned action."""

        requires_approval = self._toolset.requires_human_approval_for_invocation(
            tool_name=action.tool.name,
            tool_arguments=action.planning.tool_arguments,
            context=execution_context,
        )
        if not requires_approval:
            return False
        if action.tool.name in _NEVER_AUTO_APPROVE_TOOLS:
            return True
        if action.tool.name not in _AUTO_APPROVABLE_RELEASE_TOOLS:
            return True
        return not self._thread_allows_auto_release_approval(
            thread_id=execution_context.thread_id,
        )

    def _thread_allows_auto_release_approval(self, *, thread_id: UUID | None) -> bool:
        """Return whether this thread has an explicit release-control approval policy."""

        if thread_id is None:
            return False
        thread = self._chat_repo.get_thread_by_id(thread_id=thread_id)
        if thread is None:
            return False
        policy = thread.context_payload.get(_THREAD_APPROVAL_POLICY_KEY)
        if not isinstance(policy, dict):
            return False
        return policy.get("mode") == "auto_release_for_thread"

    def _handle_pending_plan_reply(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        actor_user: EntityUserRecord,
        content: str,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> ChatExecutionOutcome | None:
        """Apply or reject a pending governed action when the operator confirms it in chat."""

        decision = _parse_pending_plan_decision(content)
        if decision is None:
            return None

        pending_actions = self._action_repo.list_pending_actions_for_thread(
            thread_id=thread_id,
            entity_id=entity_id,
            limit=5,
        )
        if not pending_actions:
            return None

        if len(pending_actions) != 1:
            thread = self._chat_repo.get_thread_for_entity(thread_id=thread_id, entity_id=entity_id)
            snapshot = (
                self._snapshot_for_thread(
                    actor_user=actor_user,
                    entity_id=entity_id,
                    close_run_id=thread.close_run_id if thread is not None else None,
                    thread_id=thread_id,
                )
                if thread is not None
                else {}
            )
            assistant_content = _build_pending_action_selection_message(
                decision=decision,
                pending_actions=pending_actions,
                snapshot=snapshot,
            )
            assistant_message = self._chat_repo.create_message(
                thread_id=thread_id,
                role="assistant",
                content=assistant_content,
                message_type="analysis",
                linked_action_id=None,
                grounding_payload=self._build_grounding_payload(
                    self._grounding.resolve_context(
                        entity_id=entity_id,
                        close_run_id=thread.close_run_id if thread is not None else None,
                        user_id=actor_user.id,
                    )
                )
                if thread is not None
                else {},
                model_metadata=self._build_trace_metadata(
                    trace_id=trace_id,
                    mode="planner",
                    tool_name=None,
                    action_status="read_only",
                    summary="Pending governed actions require a more specific confirmation.",
                ),
            )
            if thread is not None:
                self._update_thread_memory(
                    thread_id=thread_id,
                    existing_payload=thread.context_payload,
                    operator_message=content,
                    assistant_response=assistant_content,
                    tool_name=None,
                    action_status="read_only",
                    trace_id=trace_id,
                    snapshot=snapshot,
                )
            self._db_session.commit()
            return self._build_execution_outcome(
                assistant_message_id=serialize_uuid(assistant_message.id),
                assistant_content=assistant_message.content,
                action_plan=None,
                is_read_only=True,
                thread=thread,
            )

        plan = pending_actions[0]
        if decision == "approve":
            updated_plan = self.approve_action_plan(
                action_plan_id=plan.id,
                thread_id=thread_id,
                entity_id=entity_id,
                actor_user=actor_user,
                reason="Confirmed by operator in chat.",
                source_surface=source_surface,
                trace_id=trace_id,
            )
        else:
            updated_plan = self.reject_action_plan(
                action_plan_id=plan.id,
                thread_id=thread_id,
                entity_id=entity_id,
                actor_user=actor_user,
                reason="Rejected by operator in chat.",
            )

        messages = self._chat_repo.list_messages_for_thread(thread_id=thread_id)
        if not messages:
            raise ChatActionExecutionError(
                status_code=500,
                code=ChatActionExecutionErrorCode.EXECUTION_FAILED,
                message=(
                    "The governed action updated, but the assistant response "
                    "could not be read."
                ),
            )
        latest_message = messages[-1]
        latest_thread = self._chat_repo.get_thread_by_id(thread_id=thread_id)
        if latest_thread is None:
            raise ChatActionExecutionError(
                status_code=500,
                code=ChatActionExecutionErrorCode.EXECUTION_FAILED,
                message="The updated thread scope could not be read after the governed action.",
            )
        return self._build_execution_outcome(
            assistant_message_id=serialize_uuid(latest_message.id),
            assistant_content=latest_message.content,
            action_plan=updated_plan,
            is_read_only=False,
            thread=latest_thread,
        )

    def _build_runtime_clarification(
        self,
        *,
        planning: AgentPlanningResult,
        snapshot: dict[str, Any],
    ) -> str | None:
        """Return a concise clarification prompt when a planned tool call lacks safe inputs."""

        if planning.mode != "tool" or planning.tool_name is None:
            return None

        tool_name = planning.tool_name
        tool_arguments = planning.tool_arguments

        target_clarification = _build_target_clarification(
            tool_name=tool_name,
            tool_arguments=tool_arguments,
            snapshot=snapshot,
        )
        if target_clarification is not None:
            return target_clarification

        try:
            tool_definition = self._tool_registry.get_tool(tool_name=tool_name)
        except Exception:
            return None

        missing_fields = _missing_required_fields(
            tool_arguments=tool_arguments,
            required_fields=tool_definition.input_schema.get("required"),
        )
        if not missing_fields:
            return None
        return _build_missing_field_clarification(
            tool_name=tool_name,
            missing_fields=missing_fields,
        )

    def _resolve_action_execution_scopes(
        self,
        *,
        thread: Any | None,
        plan: ChatActionPlanRecord,
        payload: dict[str, Any],
    ) -> tuple[UUID | None, UUID | None]:
        """Return the current thread scope and original source scope for approval execution."""

        execution_close_run_id = thread.close_run_id if thread is not None else plan.close_run_id
        source_close_run_id = _optional_uuid_from_payload(
            payload=payload,
            key="source_close_run_id",
        )
        if source_close_run_id is None:
            source_close_run_id = plan.close_run_id
        return execution_close_run_id, source_close_run_id

    def _require_plan_scope_match(
        self,
        *,
        thread: Any | None,
        plan: ChatActionPlanRecord,
        payload: dict[str, Any],
    ) -> None:
        """Reject stale approvals that still belong to an earlier thread scope."""

        if thread is None or thread.close_run_id == plan.close_run_id:
            return
        if _optional_uuid_from_payload(payload=payload, key="source_close_run_id") is not None:
            return
        raise ChatActionExecutionError(
            status_code=409,
            code=ChatActionExecutionErrorCode.INVALID_ACTION_PLAN,
            message=(
                "This pending action belongs to a previous close-run scope and can no longer "
                "be approved from this thread."
            ),
        )

    def _handoff_thread_scope_if_needed(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        thread_id: UUID,
        thread: Any,
        grounding: GroundingContextRecord,
        applied_result: dict[str, Any] | None,
    ) -> tuple[GroundingContextRecord, Any, str | None]:
        """Move the current thread onto a newly created or reopened close run when needed."""

        if applied_result is None:
            return grounding, thread, None

        switched_workspace_id = _optional_uuid_from_result(
            applied_result=applied_result,
            key="switched_workspace_id",
        )
        reopened_close_run_id = _optional_uuid_from_result(
            applied_result=applied_result,
            key="reopened_close_run_id",
        )
        opened_close_run_id = _optional_uuid_from_result(
            applied_result=applied_result,
            key="opened_close_run_id",
        )
        created_close_run_id = _optional_uuid_from_result(
            applied_result=applied_result,
            key="created_close_run_id",
        )
        created_workspace_id = _optional_uuid_from_result(
            applied_result=applied_result,
            key="created_workspace_id",
        )
        target_entity_id = (
            created_workspace_id
            or _optional_uuid_from_result(
                applied_result=applied_result,
                key="target_entity_id",
            )
            or entity_id
        )
        deleted_close_run_id = _optional_uuid_from_result(
            applied_result=applied_result,
            key="deleted_close_run_id",
        )
        target_close_run_id = reopened_close_run_id or opened_close_run_id or created_close_run_id
        if (
            switched_workspace_id is None
            and created_workspace_id is None
            and target_close_run_id is None
            and deleted_close_run_id is None
        ):
            return grounding, thread, None

        previous_close_run_id = thread.close_run_id
        workspace_scope_entity_id = switched_workspace_id or created_workspace_id
        if workspace_scope_entity_id is not None and target_close_run_id is None:
            workspace_grounding = self._grounding.resolve_context(
                entity_id=workspace_scope_entity_id,
                close_run_id=None,
                user_id=actor_user.id,
            )
            updated_payload = {
                **thread.context_payload,
                **self._grounding.build_context_payload(context=workspace_grounding.context),
            }
            updated_thread = self._chat_repo.update_thread_scope(
                thread_id=thread_id,
                entity_id=workspace_scope_entity_id,
                close_run_id=None,
                context_payload=updated_payload,
            )
            if previous_close_run_id is not None:
                self._action_repo.supersede_pending_actions_for_close_run_scope(
                    thread_id=thread_id,
                    close_run_id=previous_close_run_id,
                )
            return workspace_grounding, updated_thread or thread, None

        if deleted_close_run_id is not None:
            if previous_close_run_id != deleted_close_run_id:
                return grounding, thread, None
            entity_grounding = self._grounding.resolve_context(
                entity_id=entity_id,
                close_run_id=None,
                user_id=actor_user.id,
            )
            updated_payload = {
                **thread.context_payload,
                **self._grounding.build_context_payload(context=entity_grounding.context),
            }
            updated_thread = self._chat_repo.update_thread_scope(
                thread_id=thread_id,
                entity_id=entity_id,
                close_run_id=None,
                context_payload=updated_payload,
            )
            self._action_repo.supersede_pending_actions_for_close_run_scope(
                thread_id=thread_id,
                close_run_id=deleted_close_run_id,
            )
            handoff_message = _build_scope_handoff_message(applied_result=applied_result)
            return entity_grounding, updated_thread or thread, handoff_message

        reopened_grounding = self._grounding.resolve_context(
            entity_id=target_entity_id,
            close_run_id=target_close_run_id,
            user_id=actor_user.id,
        )
        updated_payload = {
            **thread.context_payload,
            **self._grounding.build_context_payload(context=reopened_grounding.context),
        }
        updated_thread = self._chat_repo.update_thread_scope(
            thread_id=thread_id,
            entity_id=target_entity_id,
            close_run_id=target_close_run_id,
            context_payload=updated_payload,
        )
        if (
            reopened_close_run_id is not None
            and previous_close_run_id is not None
            and previous_close_run_id != target_close_run_id
        ):
            self._action_repo.rebind_pending_actions_to_close_run(
                thread_id=thread_id,
                from_close_run_id=previous_close_run_id,
                to_close_run_id=target_close_run_id,
            )
        elif (
            opened_close_run_id is not None
            and previous_close_run_id is not None
            and previous_close_run_id != target_close_run_id
        ):
            self._action_repo.supersede_pending_actions_for_close_run_scope(
                thread_id=thread_id,
                close_run_id=previous_close_run_id,
            )
        elif (
            created_close_run_id is not None
            and previous_close_run_id is not None
            and previous_close_run_id != target_close_run_id
        ):
            self._action_repo.supersede_pending_actions_for_close_run_scope(
                thread_id=thread_id,
                close_run_id=previous_close_run_id,
            )
        handoff_message = _build_scope_handoff_message(applied_result=applied_result)
        return reopened_grounding, updated_thread or thread, handoff_message

    def _update_thread_memory(
        self,
        *,
        thread_id: UUID,
        existing_payload: dict[str, Any],
        operator_message: str | None,
        assistant_response: str | None,
        tool_name: str | None,
        tool_arguments: dict[str, Any] | None = None,
        action_status: str,
        trace_id: str | None,
        snapshot: dict[str, Any],
    ) -> None:
        """Persist compact working memory for the thread in its context payload."""

        existing_memory = (
            existing_payload.get("agent_memory")
            if isinstance(existing_payload.get("agent_memory"), dict)
            else {}
        )
        recent_tool_names = list(existing_payload.get("agent_recent_tool_names", []))
        recent_tool_namespaces = list(existing_payload.get("agent_recent_tool_namespaces", []))
        recent_objectives = list(existing_payload.get("agent_recent_objectives", []))
        recent_entity_names = list(existing_payload.get("agent_recent_entity_names", []))
        recent_period_labels = list(existing_payload.get("agent_recent_period_labels", []))
        recent_target_labels = list(existing_payload.get("agent_recent_target_labels", []))
        tool_definition = self._resolve_tool_definition(tool_name=tool_name)
        resolved_target = _resolve_memory_target_snapshot(
            tool_name=tool_name,
            tool_arguments=tool_arguments or {},
            snapshot=snapshot,
        )
        if tool_name is not None:
            recent_tool_names.append(tool_name)
        if tool_definition is not None:
            recent_tool_namespaces.append(tool_definition.namespace)
        current_entity_name = existing_payload.get("entity_name")
        if isinstance(current_entity_name, str) and current_entity_name.strip():
            recent_entity_names.append(current_entity_name.strip())
        current_period_label = existing_payload.get("period_label")
        if isinstance(current_period_label, str) and current_period_label.strip():
            recent_period_labels.append(current_period_label.strip())
        if resolved_target is not None and resolved_target["label"].strip():
            recent_target_labels.append(resolved_target["label"].strip())
        if operator_message is not None and operator_message.strip():
            recent_objectives.append(_truncate_text(operator_message.strip(), limit=160))
        compact_recent_tools = tuple(recent_tool_names[-5:])
        compact_recent_namespaces = tuple(recent_tool_namespaces[-5:])
        compact_recent_objectives = compact_recent_values(recent_objectives, limit=4)
        compact_recent_entities = compact_recent_values(recent_entity_names, limit=4)
        compact_recent_periods = compact_recent_values(recent_period_labels, limit=4)
        compact_recent_targets = compact_recent_values(recent_target_labels, limit=5)
        active_async_turn = get_active_async_turn(context_payload=existing_payload)
        last_async_turn = (
            dict(existing_payload.get("agent_last_async_turn"))
            if isinstance(existing_payload.get("agent_last_async_turn"), dict)
            else None
        )
        approved_objective = _resolve_approved_objective(
            existing_memory=existing_memory,
            operator_message=operator_message,
            action_status=action_status,
        )
        working_subtask = _resolve_working_subtask(
            existing_memory=existing_memory,
            operator_message=operator_message,
            tool_name=tool_name,
            resolved_target=resolved_target,
            action_status=action_status,
            snapshot=snapshot,
            active_async_turn=active_async_turn,
        )
        pending_branch = _resolve_pending_branch(
            existing_memory=existing_memory,
            tool_name=tool_name,
            action_status=action_status,
            snapshot=snapshot,
            active_async_turn=active_async_turn,
        )
        updated_payload = {
            **existing_payload,
            "agent_memory": {
                "last_operator_message": operator_message,
                "last_assistant_response": _truncate_text(assistant_response),
                "last_tool_name": tool_name,
                "last_tool_namespace": (
                    tool_definition.namespace if tool_definition is not None else None
                ),
                "last_action_status": action_status,
                "last_trace_id": trace_id,
                "preferred_explanation_depth": _resolve_preferred_explanation_depth(
                    existing_value=existing_memory.get("preferred_explanation_depth"),
                    operator_message=operator_message,
                ),
                "preferred_confirmation_style": _resolve_preferred_confirmation_style(
                    existing_value=existing_memory.get("preferred_confirmation_style"),
                    operator_message=operator_message,
                ),
                "pending_action_count": int(snapshot.get("pending_action_count", 0)),
                "progress_summary": snapshot.get("progress_summary"),
                "recent_tool_names": compact_recent_tools,
                "recent_tool_namespaces": compact_recent_namespaces,
                "recent_objectives": compact_recent_objectives,
                "recent_entity_names": compact_recent_entities,
                "recent_period_labels": compact_recent_periods,
                "recent_target_labels": compact_recent_targets,
                "last_target_type": (
                    resolved_target["target_type"] if resolved_target is not None else None
                ),
                "last_target_id": (
                    resolved_target["target_id"] if resolved_target is not None else None
                ),
                "last_target_label": (
                    resolved_target["label"] if resolved_target is not None else None
                ),
                "working_subtask": working_subtask,
                "approved_objective": approved_objective,
                "pending_branch": pending_branch,
                "active_async_status": optional_memory_text(active_async_turn, "status"),
                "active_async_objective": optional_memory_text(active_async_turn, "objective"),
                "active_async_originating_tool": optional_memory_text(
                    active_async_turn,
                    "originating_tool",
                ),
                "active_async_retry_count": optional_memory_int(
                    active_async_turn,
                    "resume_attempt_count",
                ),
                "active_async_last_failure": optional_memory_text(
                    active_async_turn,
                    "last_resume_failure",
                ),
                "last_async_status": optional_memory_text(last_async_turn, "status"),
                "last_async_objective": optional_memory_text(last_async_turn, "objective"),
                "last_async_note": optional_memory_text(last_async_turn, "final_note"),
                **build_recovery_guidance(
                    active_async_turn=active_async_turn,
                    last_async_turn=last_async_turn,
                ),
                "updated_at": utc_now().isoformat(),
            },
            "agent_recent_tool_names": compact_recent_tools,
            "agent_recent_tool_namespaces": compact_recent_namespaces,
            "agent_recent_objectives": compact_recent_objectives,
            "agent_recent_entity_names": compact_recent_entities,
            "agent_recent_period_labels": compact_recent_periods,
            "agent_recent_target_labels": compact_recent_targets,
            "agent_progress_summary": snapshot.get("progress_summary"),
            "agent_last_trace_id": trace_id,
        }
        self._chat_repo.update_thread_context(
            thread_id=thread_id,
            context_payload=updated_payload,
        )

    def _memory_from_context_payload(
        self,
        context_payload: dict[str, Any],
    ) -> AgentMemorySummary:
        """Build the API memory contract from one thread context payload."""

        memory = context_payload.get("agent_memory")
        payload = dict(memory) if isinstance(memory, dict) else {}
        payload.setdefault(
            "recent_tool_names",
            tuple(context_payload.get("agent_recent_tool_names", ())),
        )
        payload.setdefault(
            "recent_tool_namespaces",
            tuple(context_payload.get("agent_recent_tool_namespaces", ())),
        )
        payload.setdefault(
            "recent_objectives",
            tuple(context_payload.get("agent_recent_objectives", ())),
        )
        payload.setdefault(
            "recent_entity_names",
            tuple(context_payload.get("agent_recent_entity_names", ())),
        )
        payload.setdefault(
            "recent_period_labels",
            tuple(context_payload.get("agent_recent_period_labels", ())),
        )
        payload.setdefault(
            "recent_target_labels",
            tuple(context_payload.get("agent_recent_target_labels", ())),
        )

        active_async_turn = get_active_async_turn(context_payload=context_payload)
        last_async_turn = (
            dict(context_payload.get("agent_last_async_turn"))
            if isinstance(context_payload.get("agent_last_async_turn"), dict)
            else None
        )
        payload["active_async_status"] = optional_memory_text(active_async_turn, "status")
        payload["active_async_objective"] = optional_memory_text(active_async_turn, "objective")
        payload["active_async_originating_tool"] = optional_memory_text(
            active_async_turn,
            "originating_tool",
        )
        payload["active_async_retry_count"] = optional_memory_int(
            active_async_turn,
            "resume_attempt_count",
        )
        payload["active_async_last_failure"] = optional_memory_text(
            active_async_turn,
            "last_resume_failure",
        )
        payload["last_async_status"] = optional_memory_text(last_async_turn, "status")
        payload["last_async_objective"] = optional_memory_text(last_async_turn, "objective")
        payload["last_async_note"] = optional_memory_text(last_async_turn, "final_note")
        payload.update(
            build_recovery_guidance(
                active_async_turn=active_async_turn,
                last_async_turn=last_async_turn,
            )
        )
        if "preferred_explanation_depth" not in payload:
            payload["preferred_explanation_depth"] = DEFAULT_PREFERRED_EXPLANATION_DEPTH
        if "preferred_confirmation_style" not in payload:
            payload["preferred_confirmation_style"] = DEFAULT_PREFERRED_CONFIRMATION_STYLE
        return AgentMemorySummary(**payload)

    def _memory_for_thread(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        actor_user_id: UUID,
        context_payload: dict[str, Any],
    ) -> AgentMemorySummary:
        """Return current-thread memory enriched with recent thread carry-forward."""

        merged_payload = merge_context_payload_with_cross_thread_memory(
            context_payload=context_payload,
            recent_context_payloads=self._recent_thread_context_payloads(
                entity_id=entity_id,
                current_thread_id=thread_id,
            ),
            cross_workspace_recent_context_payloads=self._recent_user_context_payloads(
                actor_user_id=actor_user_id,
                current_thread_id=thread_id,
            ),
        )
        return self._memory_from_context_payload(merged_payload)

    def _recent_thread_context_payloads(
        self,
        *,
        entity_id: UUID,
        current_thread_id: UUID,
    ) -> tuple[dict[str, Any], ...]:
        """Return recent thread context payloads used for cross-thread memory carry-forward."""

        recent_threads = self._chat_repo.list_recent_threads_for_entity_any_scope(
            entity_id=entity_id,
            limit=8,
            exclude_thread_id=current_thread_id,
        )
        return tuple(thread.context_payload for thread in recent_threads)

    def _recent_user_context_payloads(
        self,
        *,
        actor_user_id: UUID,
        current_thread_id: UUID,
    ) -> tuple[dict[str, Any], ...]:
        """Return recent thread payloads across the operator's accessible workspaces."""

        recent_threads = self._chat_repo.list_recent_threads_for_user_any_scope(
            user_id=actor_user_id,
            limit=12,
            exclude_thread_id=current_thread_id,
        )
        return tuple(thread.context_payload for thread in recent_threads)

    def _resolve_tool_definition(self, *, tool_name: str | None) -> Any | None:
        """Return one registered tool definition when the name exists in the registry."""

        if tool_name is None:
            return None
        try:
            return self._tool_registry.get_tool(tool_name=tool_name)
        except Exception:
            return None

    def _build_operator_control_payload(
        self,
        *,
        tool: Any,
        target_type: str | None,
        target_id: UUID | None,
        requires_human_approval: bool,
        turn_objective: str | None,
        loop_iteration: int | None,
    ) -> dict[str, Any]:
        """Return audit and eval metadata persisted with one action plan."""

        action_mode = "governed" if requires_human_approval else "direct"
        payload: dict[str, Any] = {
            "planner_policy_version": _OPERATOR_PLANNER_POLICY_VERSION,
            "confirmation_policy_version": _OPERATOR_CONFIRMATION_POLICY_VERSION,
            "eval_schema_version": _OPERATOR_EVAL_SCHEMA_VERSION,
            "tool_name": tool.name,
            "tool_namespace": tool.namespace,
            "namespace_label": tool.namespace_label,
            "specialist_name": tool.specialist_name,
            "specialist_mission": tool.specialist_mission,
            "tool_intent": tool.intent,
            "action_mode": action_mode,
            "requires_human_approval": requires_human_approval,
            "target_type": target_type,
            "target_id": serialize_uuid(target_id) if target_id is not None else None,
            "eval_tags": self._build_eval_tags(
                mode="tool",
                action_status="pending" if requires_human_approval else "applied",
                tool_definition=tool,
            ),
        }
        if turn_objective is not None:
            payload["turn_objective"] = _truncate_text(turn_objective, limit=300)
        if loop_iteration is not None:
            payload["loop_iteration"] = loop_iteration
        return payload

    def _build_eval_tags(
        self,
        *,
        mode: str,
        action_status: str,
        tool_definition: Any | None,
    ) -> tuple[str, ...]:
        """Return compact deterministic eval tags for traces and staged actions."""

        tags = [f"mode:{mode}", f"status:{action_status}"]
        if tool_definition is None:
            tags.append("surface:analysis")
        else:
            tags.append(f"namespace:{tool_definition.namespace}")
            tags.append(f"intent:{tool_definition.intent}")
            tags.append(
                "approval:required"
                if tool_definition.requires_human_approval
                else "approval:direct"
            )
        return tuple(tags)

    def _build_trace_metadata(
        self,
        *,
        trace_id: str | None,
        mode: str,
        tool_name: str | None,
        action_status: str,
        summary: str | None,
    ) -> dict[str, Any]:
        """Build standardized trace metadata attached to assistant and system messages."""

        tool_definition = self._resolve_tool_definition(tool_name=tool_name)
        metadata: dict[str, Any] = {
            "provider": "system" if mode in {"approval", "rejection"} else "openrouter",
            "mode": mode,
            "action_status": action_status,
            "planner_policy_version": _OPERATOR_PLANNER_POLICY_VERSION,
            "confirmation_policy_version": _OPERATOR_CONFIRMATION_POLICY_VERSION,
            "eval_schema_version": _OPERATOR_EVAL_SCHEMA_VERSION,
            "eval_tags": list(
                self._build_eval_tags(
                    mode=mode,
                    action_status=action_status,
                    tool_definition=tool_definition,
                )
            ),
        }
        if tool_name is not None:
            metadata["tool"] = tool_name
        if tool_definition is not None:
            metadata["tool_namespace"] = tool_definition.namespace
            metadata["specialist_name"] = tool_definition.specialist_name
            metadata["tool_intent"] = tool_definition.intent
        if trace_id is not None:
            metadata["trace_id"] = trace_id
        if summary is not None:
            metadata["summary"] = summary
        return metadata

    def _build_recent_traces(
        self,
        messages: tuple[Any, ...],
    ) -> tuple[AgentTraceRecord, ...]:
        """Return recent trace events derived from assistant and system message metadata."""

        traces: list[AgentTraceRecord] = []
        for message in reversed(messages):
            metadata = message.model_metadata
            if not isinstance(metadata, dict):
                continue
            if (
                "trace_id" not in metadata
                and "tool" not in metadata
                and "mode" not in metadata
                and "summary" not in metadata
            ):
                continue
            traces.append(
                AgentTraceRecord(
                    message_id=str(message.id),
                    created_at=message.created_at,
                    mode=str(metadata.get("mode")) if metadata.get("mode") is not None else None,
                    tool_name=(
                        str(metadata.get("tool"))
                        if metadata.get("tool") is not None
                        else None
                    ),
                    tool_namespace=(
                        str(metadata.get("tool_namespace"))
                        if metadata.get("tool_namespace") is not None
                        else None
                    ),
                    specialist_name=(
                        str(metadata.get("specialist_name"))
                        if metadata.get("specialist_name") is not None
                        else None
                    ),
                    tool_intent=(
                        str(metadata.get("tool_intent"))
                        if metadata.get("tool_intent") is not None
                        else None
                    ),
                    trace_id=(
                        str(metadata.get("trace_id"))
                        if metadata.get("trace_id") is not None
                        else None
                    ),
                    planner_policy_version=(
                        str(metadata.get("planner_policy_version"))
                        if metadata.get("planner_policy_version") is not None
                        else None
                    ),
                    confirmation_policy_version=(
                        str(metadata.get("confirmation_policy_version"))
                        if metadata.get("confirmation_policy_version") is not None
                        else None
                    ),
                    action_status=(
                        str(metadata.get("action_status"))
                        if metadata.get("action_status") is not None
                        else None
                    ),
                    summary=(
                        str(metadata.get("summary"))
                        if metadata.get("summary") is not None
                        else None
                    ),
                    eval_tags=tuple(
                        str(tag)
                        for tag in metadata.get("eval_tags", [])
                        if isinstance(tag, str)
                    ),
                )
            )
            if len(traces) >= 20:
                break
        return tuple(traces)

    def _build_mcp_manifest(self) -> dict[str, Any]:
        """Build a portable MCP-style manifest for the registered accounting tools."""

        return {
            "protocol": "model-context-protocol",
            "version": _MCP_MANIFEST_VERSION,
            "namespaces": list(self._build_tool_namespace_manifest()),
            "operator_policy": {
                "planner_policy_version": _OPERATOR_PLANNER_POLICY_VERSION,
                "confirmation_policy_version": _OPERATOR_CONFIRMATION_POLICY_VERSION,
                "eval_schema_version": _OPERATOR_EVAL_SCHEMA_VERSION,
            },
            "operator_controls": {
                "delivery": "natural_language_command",
                "kinds": [
                    "governed_action",
                    "next_step",
                    "recovery",
                    "status_check",
                ],
            },
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.input_schema,
                    "annotations": {
                        "namespace": tool.namespace,
                        "namespaceLabel": tool.namespace_label,
                        "specialistName": tool.specialist_name,
                        "requiresHumanApproval": tool.requires_human_approval,
                        "intent": tool.intent,
                    },
                }
                for tool in self.list_registered_tools()
            ],
        }

    def _build_operator_controls(
        self,
        *,
        thread: Any,
        snapshot: dict[str, Any],
        operator_memory: AgentMemorySummary,
        pending_actions: tuple[ChatActionPlanRecord, ...],
    ) -> tuple[AgentOperatorControl, ...]:
        """Return portable operator controls derived from pending work and readiness."""

        scope = "close_run" if thread.close_run_id is not None else "entity"
        controls: list[AgentOperatorControl] = []
        seen_commands: set[str] = set()

        def add_control(
            *,
            control_id: str,
            label: str,
            command: str,
            kind: str,
            description: str | None = None,
            requires_confirmation: bool = False,
            enabled: bool = True,
            disabled_reason: str | None = None,
        ) -> None:
            normalized_command = command.strip()
            if not normalized_command or normalized_command in seen_commands:
                return
            seen_commands.add(normalized_command)
            controls.append(
                AgentOperatorControl(
                    id=control_id,
                    label=label,
                    command=normalized_command,
                    kind=kind,
                    scope=scope,
                    description=description,
                    requires_confirmation=requires_confirmation,
                    enabled=enabled,
                    disabled_reason=disabled_reason,
                )
            )

        if len(pending_actions) == 1:
            pending_action = pending_actions[0]
            payload = pending_action.payload if isinstance(pending_action.payload, dict) else {}
            tool_name = str(payload.get("tool_name") or "")
            tool_arguments = (
                payload.get("tool_arguments")
                if isinstance(payload.get("tool_arguments"), dict)
                else {}
            )
            description = _build_pending_confirmation_message(
                tool_name=tool_name,
                tool_arguments=tool_arguments,
                snapshot=snapshot,
            )
            add_control(
                control_id="confirm_pending_action",
                label="Confirm pending action",
                command="confirm",
                kind="governed_action",
                description=description,
                requires_confirmation=True,
            )
            add_control(
                control_id="cancel_pending_action",
                label="Cancel pending action",
                command="cancel",
                kind="governed_action",
                description="Drop the governed action currently waiting in this thread.",
                requires_confirmation=False,
            )
        elif len(pending_actions) > 1:
            add_control(
                control_id="review_pending_actions",
                label="Review pending actions",
                command="Show the pending governed actions waiting in this thread.",
                kind="governed_action",
                description="More than one governed action is waiting for a specific confirmation.",
                requires_confirmation=False,
            )

        if operator_memory.recovery_summary is not None:
            add_control(
                control_id="check_recovery_status",
                label="Check recovery status",
                command="What is the recovery status for the current background work?",
                kind="status_check",
                description=operator_memory.recovery_summary,
            )

        for index, action in enumerate(operator_memory.recovery_actions[:2], start=1):
            cleaned_action = _normalize_operator_control_command(action)
            add_control(
                control_id=f"recovery_action_{index}",
                label=_build_operator_control_label(cleaned_action),
                command=cleaned_action,
                kind="recovery",
                description="Recovery guidance derived from the latest async workflow state.",
            )

        readiness = snapshot.get("readiness")
        next_actions = (
            readiness.get("next_actions")
            if isinstance(readiness, dict) and isinstance(readiness.get("next_actions"), list)
            else []
        )
        for index, action in enumerate(next_actions[:3], start=1):
            if not isinstance(action, str):
                continue
            cleaned_action = _normalize_operator_control_command(action)
            add_control(
                control_id=f"next_action_{index}",
                label=_build_operator_control_label(cleaned_action),
                command=cleaned_action,
                kind="next_step",
                description=(
                    "Suggested next operator action derived from current "
                    "workspace readiness."
                ),
            )

        if not controls and thread.close_run_id is not None:
            add_control(
                control_id="default_close_status",
                label="Check close blockers",
                command="What is blocking this close right now?",
                kind="status_check",
                description=(
                    "Use the current close-run context to summarize blockers "
                    "and next steps."
                ),
            )
        elif not controls:
            add_control(
                control_id="default_workspace_status",
                label="Show active close runs",
                command="Show me the active close runs in this workspace.",
                kind="status_check",
                description="Use the current entity context to summarize available close runs.",
            )

        return tuple(controls)

    def _load_thread_context(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        user_id: UUID,
    ) -> tuple[GroundingContextRecord, Any]:
        """Load thread and grounding context for planning and execution."""

        access = self._entity_repo.get_entity_for_user(entity_id=entity_id, user_id=user_id)
        if access is None:
            relocated_thread = self._chat_repo.get_thread_by_id(thread_id=thread_id)
            if relocated_thread is None:
                raise ChatActionExecutionError(
                    status_code=404,
                    code=ChatActionExecutionErrorCode.THREAD_NOT_FOUND,
                    message="That chat thread does not exist.",
                )
            relocated_access = self._entity_repo.get_entity_for_user(
                entity_id=relocated_thread.entity_id,
                user_id=user_id,
            )
            if relocated_access is None:
                raise ChatActionExecutionError(
                    status_code=403,
                    code=ChatActionExecutionErrorCode.ACCESS_DENIED,
                    message="You are not a member of this workspace.",
                )
            entity_id = relocated_thread.entity_id
            thread = relocated_thread
        else:
            thread = self._chat_repo.get_thread_for_entity(
                thread_id=thread_id,
                entity_id=entity_id,
            )
            if thread is None:
                relocated_thread = self._chat_repo.get_thread_by_id(thread_id=thread_id)
                if relocated_thread is None:
                    raise ChatActionExecutionError(
                        status_code=404,
                        code=ChatActionExecutionErrorCode.THREAD_NOT_FOUND,
                        message="That chat thread does not exist.",
                    )
                relocated_access = self._entity_repo.get_entity_for_user(
                    entity_id=relocated_thread.entity_id,
                    user_id=user_id,
                )
                if relocated_access is None:
                    raise ChatActionExecutionError(
                        status_code=403,
                        code=ChatActionExecutionErrorCode.ACCESS_DENIED,
                        message="You are not a member of this workspace.",
                    )
                thread = relocated_thread
                entity_id = relocated_thread.entity_id

        grounding = self._grounding.resolve_context(
            entity_id=entity_id,
            close_run_id=thread.close_run_id,
            user_id=user_id,
        )
        return grounding, thread

    def _plan_action(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        actor_user: EntityUserRecord,
        content: str,
        grounding: GroundingContextRecord,
        operator_memory: AgentMemorySummary,
        loop_context: _OperatorLoopContext | None = None,
    ) -> AgentPlanningResult:
        """Use the generic agent kernel to choose between analysis and a tool call."""

        thread_messages = self._chat_repo.list_messages_for_thread(thread_id=thread_id, limit=20)
        snapshot = self._workspace_builder.build_snapshot(
            actor=actor_user,
            entity_id=entity_id,
            close_run_id=grounding.close_run.id if grounding.close_run is not None else None,
            thread_id=thread_id,
        )
        conversation = [
            {"role": message.role, "content": message.content}
            for message in thread_messages
        ]
        conversation.append({"role": "user", "content": content})

        direct_status_response = _build_direct_operator_status_response(
            snapshot=snapshot,
            operator_content=content,
            operator_memory=operator_memory,
        )
        if direct_status_response is not None:
            return AgentPlanningResult(
                mode="read_only",
                assistant_response=direct_status_response,
                reasoning=(
                    "Handled through the deterministic grounded status-response layer "
                    "before planner invocation."
                ),
                tool_name=None,
                tool_arguments={},
            )

        correction_delete_planning = _build_close_run_correction_delete_planning(
            snapshot=snapshot,
            operator_content=content,
        )
        if correction_delete_planning is not None:
            return correction_delete_planning

        open_close_run_planning = _build_open_close_run_intent_planning(
            snapshot=snapshot,
            operator_content=content,
        )
        if open_close_run_planning is not None:
            return open_close_run_planning

        create_close_run_planning = _build_create_close_run_intent_planning(
            snapshot=snapshot,
            operator_content=content,
            operator_memory=operator_memory,
        )
        if create_close_run_planning is not None:
            return create_close_run_planning

        create_workspace_planning = _build_create_workspace_intent_planning(
            snapshot=snapshot,
            operator_content=content,
            operator_memory=operator_memory,
        )
        if create_workspace_planning is not None:
            return create_workspace_planning

        try:
            return self._kernel.plan(
                instructions=self._build_planner_instructions(
                    grounding=grounding,
                    snapshot=snapshot,
                    operator_memory=operator_memory,
                    loop_context=loop_context,
                ),
                conversation=conversation,
                snapshot=snapshot,
            )
        except AgentKernelError as error:
            raise ChatActionExecutionError(
                status_code=503,
                code=ChatActionExecutionErrorCode.PLANNING_FAILED,
                message=f"Chat planning is unavailable: {error}",
            ) from error

    def _build_planner_instructions(
        self,
        *,
        grounding: GroundingContextRecord,
        snapshot: dict[str, Any],
        operator_memory: AgentMemorySummary,
        loop_context: _OperatorLoopContext | None = None,
    ) -> str:
        """Build the system instructions consumed by the generic agent kernel."""

        specialist_lines = [
            (
                f"- {namespace.label} via {namespace.specialist_name}: "
                f"{namespace.specialist_mission}"
            )
            for namespace in self._tool_registry.list_namespaces()
        ]
        memory_lines = [
            (
                "Operator explanation preference: "
                f"{operator_memory.preferred_explanation_depth}."
            ),
            (
                "Operator confirmation preference: "
                f"{operator_memory.preferred_confirmation_style}."
            ),
        ]
        if operator_memory.recent_objectives:
            memory_lines.append(
                "Recent operator objectives: " + " | ".join(operator_memory.recent_objectives[-3:])
            )
        if operator_memory.last_target_label is not None:
            memory_lines.append(
                f"Current conversational focus target: {operator_memory.last_target_label}."
            )
        if operator_memory.working_subtask is not None:
            memory_lines.append(f"Compressed working subtask: {operator_memory.working_subtask}")
        if operator_memory.approved_objective is not None:
            memory_lines.append(
                f"Last committed operator objective: {operator_memory.approved_objective}"
            )
        if operator_memory.pending_branch is not None:
            memory_lines.append(f"Pending branch: {operator_memory.pending_branch}")
        if operator_memory.active_async_objective is not None:
            memory_lines.append(
                "Active interrupted workflow: "
                f"{operator_memory.active_async_status or 'pending'} / "
                f"{operator_memory.active_async_objective}"
            )
        if operator_memory.last_async_objective is not None:
            memory_lines.append(
                "Most recent async workflow outcome: "
                f"{operator_memory.last_async_status or 'completed'} / "
                f"{operator_memory.last_async_objective}"
            )
        if operator_memory.recovery_summary is not None:
            memory_lines.append(f"Recovery state: {operator_memory.recovery_summary}")
        if operator_memory.recovery_actions:
            memory_lines.append(
                "Recovery actions: " + " | ".join(operator_memory.recovery_actions[:2])
            )
        focus_lines = _build_planner_focus_lines(snapshot=snapshot)
        instructions = [
                f"You are the accounting operations agent for workspace '{grounding.entity.name}'.",
                f"Base currency: {grounding.context.base_currency}.",
                f"Autonomy mode: {grounding.context.autonomy_mode}.",
                f"Current UTC date: {utc_now().date().isoformat()}.",
                "You are fully aware of the current system state described below.",
                (
                    "This chat is the operator's primary control surface for the accounting "
                    "workflow. Act on the operator's behalf whenever their intent is clear "
                    "and the requested operation is available."
                ),
                (
                    "Under the hood you have internal specialist domains. Route each request "
                    "through the best-fitting specialist first and stay inside that domain "
                    "unless the next canonical step clearly belongs to another one."
                ),
                "Internal specialist domains:",
                *specialist_lines,
                "Operator memory:",
                *memory_lines,
                "Current likely focus:",
                *(
                    focus_lines
                    or [
                        "- No unusually strong focus signal is present in the "
                        "current snapshot."
                    ]
                ),
                (
                    "Never tell the operator to use an internal tool name. Never say things "
                    "like 'use the review_document tool' or 'call create_close_run'. If you "
                    "can do the work here, do it here."
                ),
                (
                    "When the operator asks for a change, prefer taking the action over "
                    "describing the action. If identifiers are implicit but there is one "
                    "clear candidate in the current workspace state or recent conversation, "
                    "resolve it yourself."
                ),
                (
                    "Only ask a clarifying question when more than one plausible target fits "
                    "the request or when a required value truly cannot be inferred."
                ),
                (
                    "Treat natural outcome-oriented phrasing as actionable when it clearly "
                    "implies a supported workflow step. Examples: 'I need the reports', "
                    "'can you get the exports ready', 'we need to finish reconciliation', "
                    "'take this back to reconciliation', 'take this back to collection so I "
                    "can upload more files', 'start over from document intake', 'archive this "
                    "run', 'start a new April close run', 'open a fresh run for this month', "
                    "'create another run for this period', or 'ignore the PDF I uploaded by "
                    "mistake'."
                ),
                (
                    "If the operator wants to revisit a released close run to make more "
                    "changes, you may reopen it and continue inside the same thread. From a "
                    "workspace-level thread, resolve the target from the workspace close-run "
                    "list when exactly one approved, exported, or archived run is clearly "
                    "named or implied."
                ),
                (
                    "If the operator asks to work on, enter, open, pin, select, or use an "
                    "existing close run, use open_close_run instead of creating another run. "
                    "Create a run only when the operator asks for a new, fresh, or duplicate "
                    "run, or when no existing run matches the requested period."
                ),
                (
                    "If the operator says the current close run was a mistake and asks to "
                    "delete or remove it, target the current close run first. If they also "
                    "name the intended period, handle the deletion first and then continue "
                    "toward the named existing run after the governed delete is confirmed."
                ),
                (
                    "When a requested change belongs in an earlier workflow phase, you may "
                    "move the close run back into that phase so the operator can keep working "
                    "in one thread without navigating manually."
                ),
                (
                    "If the operator wants to move backward inside an active mutable close run, "
                    "use the rewind tool with the correct earlier phase instead of asking the "
                    "operator to navigate elsewhere."
                ),
                (
                    "If the operator says an uploaded document was a mistake, use the ignore "
                    "document action when the snapshot shows one clear matching document. If "
                    "more than one document could match, ask one short clarifying question."
                ),
                (
                    "You can directly review documents, correct extracted values, approve or "
                    "reject recommendations, approve or reject journals, apply journals, "
                    "disposition reconciliation exceptions, resolve reconciliation anomalies, "
                    "update commentary, and approve commentary when the request is clear."
                ),
                (
                    "If there is exactly one clear pending recommendation, journal, "
                    "reconciliation item, anomaly, or commentary section in scope, treat "
                    "phrases like 'approve it', 'reject it', 'apply it', 'resolve it', or "
                    "'update that commentary' as permission to act without making the operator "
                    "repeat identifiers."
                ),
                (
                    "When a rejection or reconciliation disposition requires a reason and the "
                    "operator did not supply one, use a concise audit-safe reason based on the "
                    "operator instruction instead of blocking on formality."
                ),
                (
                    "When the operator asks to start a new or fresh close run, use the "
                    "create_close_run action with explicit ISO period_start and period_end "
                    "values. Resolve relative phrases like 'this month', 'next month', or "
                    "named months against the current UTC date above. If the intended period "
                    "is still ambiguous, ask one short clarifying question."
                ),
                (
                    "You can also create workspaces, update workspace settings, delete mutable "
                    "close runs, and delete other accessible workspaces through chat when the "
                    "request is explicit and the target is clear."
                ),
                (
                    "When the operator asks to switch this conversation to another accessible "
                    "workspace, use the switch_workspace action instead of asking them to use "
                    "a visible selector."
                ),
                (
                    "Do not switch workspaces from a close-run-scoped thread. In that case ask "
                    "the operator to open the global assistant or a workspace-level assistant "
                    "first."
                ),
                (
                    "If the operator asks to delete the workspace anchoring this exact chat "
                    "thread, tell them to switch to another workspace chat first so you can "
                    "delete it safely without dropping the current conversation scope."
                ),
                (
                    "If the operator explicitly wants another open run for the same period, set "
                    "allow_duplicate_period=true and include a concise duplicate_period_reason. "
                    "Otherwise do not create a duplicate run."
                ),
                (
                    "Call a concrete platform tool when the operator is asking you to make "
                    "a change, trigger a workflow step, approve or reject work, generate an "
                    "artifact, or otherwise do something the registered actions can accomplish."
                ),
                (
                    "Call answer_operator for analysis, explanation, status narration, "
                    "missing identifiers, or ambiguous requests."
                ),
                (
                    "When the operator asks for business recommendations, management advice, "
                    "growth assessment, or what the financial report implies, call "
                    "answer_operator. Do not route that to generate_recommendations unless the "
                    "operator explicitly asks to generate accounting recommendations."
                ),
                (
                    "Greetings, status checks, and help requests should still return a "
                    "useful read_only response grounded in the current workspace."
                ),
                (
                    "When you call a platform tool, use only the registered deterministic "
                    "actions and include JSON-safe arguments. The tool call must use the exact "
                    "concrete action name such as switch_workspace or create_close_run, never "
                    "a namespace or specialist label."
                ),
                (
                    "If a required identifier is missing and there is no single clear target in "
                    "the snapshot, do not invent it. Call answer_operator and ask one "
                    "short clarifying question."
                ),
                (
                    "If the operator says 'approve it', 'reject it', 'ignore it', 'run it', or "
                    "'do that', resolve the pronoun against the latest clear item from the "
                    "workspace snapshot or recent turns instead of pushing the operator to name "
                    "the identifier again."
                ),
                (
                    "For document approvals, when the operator clearly instructs you to approve "
                    "the document, treat that instruction as confirmation to complete the review "
                    "unless the workspace state makes the target ambiguous."
                ),
                (
                    "For broad document instructions such as 'approve all documents', use the "
                    "batch document-review action. Explain any documents skipped because parsing "
                    "is still running, open issues remain, or the status is not reviewable."
                ),
                (
                    "When discussing uploaded documents, use parsed fields and open issues to "
                    "summarize what the documents say, point out missing or unresolved evidence, "
                    "and let the operator either upload more documents or explicitly continue "
                    "with the available evidence."
                ),
                (
                    "Keep the tone natural and teammate-like. Default to short conversational "
                    "paragraphs. Match the operator's preferred explanation depth when it is "
                    "known. Avoid markdown bullets, bold markers, tables, or rigid templates "
                    "unless the operator explicitly asks for structure."
                ),
                (
                    "When you call a platform tool, the assistant_response must be brief and "
                    "operator-facing. Say what you are doing for the operator in one or two "
                    "plain sentences. Do not mention internal tool names, JSON fields, or "
                    "implementation details."
                ),
                (
                    "You only have the capabilities listed in Available tools and the "
                    "workspace snapshot. Do not claim hidden abilities, external access, "
                    "or background jobs that are not represented there."
                ),
                (
                    "Use progress_summary, document details, open issues, coa summary, "
                    "readiness blockers, workflow phase states, recent jobs, exports, and "
                    "recent actions to explain current state and suggest the next move."
                ),
                (
                    "If the active chart of accounts is missing, do not choose an action for "
                    "recommendation generation, journals, reconciliation, reporting, or exports. "
                    "Call answer_operator and ask the operator to upload a production COA "
                    "from the entity workspace or Chart of Accounts page first. Do not say you "
                    "can source or upload a production COA through chat; the operator must provide "
                    "the file or use an installed accounting-system integration such as QuickBooks."
                ),
                (
                    "If the workspace has an active fallback chart of accounts, treat it as "
                    "usable for the close workflow. Do not imply that a production COA is "
                    "required. If the operator asks about a production COA, explain that it "
                    "is optional and must come from the operator's accounting system, "
                    "accountant, or an installed integration."
                ),
                (
                    "If source documents are missing, tell the operator they can upload them "
                    "through chat or from the document workspace and that parsing starts "
                    "automatically after upload."
                ),
                (
                    "If the operator asks you to run, finish, process, or report the close "
                    "end-to-end, treat that as permission to drive the workflow autonomously "
                    "with the available non-governed tools. Continue through document review, "
                    "phase advancement, recommendation generation, recommendation and journal "
                    "review/application, applicable reconciliation, reporting, export, and "
                    "evidence-pack assembly until the objective is complete or a concrete "
                    "blocker appears."
                ),
                (
                    "For autonomous close work, make discretionary decisions only from the "
                    "workspace evidence: approve clean parsed documents, recommendations, "
                    "journals, and reconciliations when there are no open issues; skip or mark "
                    "work not applicable only when the snapshot or tool result supports that "
                    "decision. Stop and explain when evidence is missing, ambiguous, failed, "
                    "blocked, or a governed release/destructive action requires confirmation."
                ),
                (
                    "When an autonomous close objective stops or completes, summarize what "
                    "went through, what did not run, what failed or was skipped, and the "
                    "decision basis for each material step."
                ),
                (
                    "High-stakes sign-off and release actions may still require confirmation. "
                    "Most operational requests should be handled directly."
                ),
                (
                    "Respect the operator's confirmation preference when it is known, but "
                    "never bypass governed confirmation for destructive or release-critical "
                    "actions."
                ),
                (
                    "When there is one pending governed action waiting for confirmation and the "
                    "operator replies with 'confirm', 'go ahead', 'proceed', or 'cancel', "
                    "treat that as approval or rejection of the pending action."
                ),
            ]
        if loop_context is not None:
            instructions.extend(
                [
                    (
                        "You are continuing the same operator request inside a bounded "
                        "multi-step execution loop."
                    ),
                    f"Overall operator objective for this turn: {loop_context.objective}",
                    (
                        f"This is loop step {loop_context.iteration} of "
                        f"{loop_context.max_iterations}."
                    ),
                    (
                        "If there is another clear, safe, and useful deterministic action "
                        "that materially advances the same objective, call the platform tool for "
                        "the single best next action."
                    ),
                    (
                        "For explicit autonomous/end-to-end close objectives, keep choosing "
                        "the next workflow tool across phases until final reporting/export is "
                        "done or the snapshot shows a concrete blocker."
                    ),
                    (
                        "If the main objective is now waiting on human approval, asynchronous "
                        "processing, missing inputs, ambiguity, or a blocker, call "
                        "answer_operator and explain the current state briefly."
                    ),
                    (
                        "Do not repeat an action already completed in this turn unless the "
                        "latest workspace snapshot shows a materially different target or "
                        "state transition."
                    ),
                ]
            )
            if loop_context.completed_summaries:
                instructions.append(
                    "Steps already completed in this turn: "
                    + " ".join(loop_context.completed_summaries)
                )
        return "\n".join(instructions)

    def _resolve_action(
        self,
        *,
        planning: AgentPlanningResult,
    ) -> AgentPlannedAction | None:
        """Validate and resolve one tool invocation selected by the planner."""

        try:
            return self._kernel.resolve_action(planning=planning)
        except AgentKernelError as error:
            raise ChatActionExecutionError(
                status_code=422,
                code=ChatActionExecutionErrorCode.INVALID_ACTION_PLAN,
                message=str(error),
            ) from error

    def _execute_action(
        self,
        *,
        action: AgentPlannedAction,
        execution_context: AgentExecutionContext,
    ) -> dict[str, Any]:
        """Execute one resolved action through the generic agent kernel."""

        try:
            return self._kernel.execute(
                action=action,
                execution_context=execution_context,
            )
        except (
            DocumentReviewServiceError,
            RecommendationApplyError,
            CloseRunServiceError,
            EntityServiceError,
            EntityDeleteServiceError,
            ExportServiceError,
            ReportServiceError,
            JobServiceError,
            SupportingScheduleServiceError,
        ) as error:
            raise _coerce_execution_error(error) from error
        except AgentKernelError as error:
            raise ChatActionExecutionError(
                status_code=422,
                code=ChatActionExecutionErrorCode.INVALID_ACTION_PLAN,
                message=str(error),
            ) from error
        except ValueError as error:
            raise ChatActionExecutionError(
                status_code=422,
                code=ChatActionExecutionErrorCode.INVALID_ACTION_PLAN,
                message=str(error),
            ) from error

    def _hydrate_planning_result(
        self,
        *,
        planning: AgentPlanningResult,
        snapshot: dict[str, Any],
        operator_content: str,
        operator_memory: AgentMemorySummary,
    ) -> AgentPlanningResult:
        """Normalize operator-facing text and fill missing low-ambiguity tool arguments."""

        normalized_response = _normalize_operator_facing_text(planning.assistant_response)
        direct_status_response = _build_direct_operator_status_response(
            snapshot=snapshot,
            operator_content=operator_content,
            operator_memory=operator_memory,
        )
        if direct_status_response is not None:
            return planning.model_copy(
                update={
                    "mode": "read_only",
                    "assistant_response": direct_status_response,
                    "tool_name": None,
                    "tool_arguments": {},
                }
            )

        correction_delete_planning = _build_close_run_correction_delete_planning(
            snapshot=snapshot,
            operator_content=operator_content,
        )
        if correction_delete_planning is not None:
            return correction_delete_planning

        open_close_run_planning = _build_open_close_run_intent_planning(
            snapshot=snapshot,
            operator_content=operator_content,
        )
        if open_close_run_planning is not None and (
            planning.mode != "tool"
            or _normalize_planned_tool_name(planning.tool_name) != "open_close_run"
        ):
            return open_close_run_planning

        create_close_run_planning = _build_create_close_run_intent_planning(
            snapshot=snapshot,
            operator_content=operator_content,
            operator_memory=operator_memory,
        )
        if create_close_run_planning is not None and (
            planning.mode != "tool"
            or _normalize_planned_tool_name(planning.tool_name) != "create_close_run"
        ):
            return create_close_run_planning

        create_workspace_planning = _build_create_workspace_intent_planning(
            snapshot=snapshot,
            operator_content=operator_content,
            operator_memory=operator_memory,
        )
        if create_workspace_planning is not None:
            return create_workspace_planning

        cross_domain_clarification = _build_cross_domain_ambiguity_clarification(
            snapshot=snapshot,
            operator_content=operator_content,
            operator_memory=operator_memory,
        )
        if cross_domain_clarification is not None:
            return planning.model_copy(
                update={
                    "mode": "read_only",
                    "assistant_response": cross_domain_clarification,
                    "tool_name": None,
                    "tool_arguments": {},
                }
            )

        create_close_run_follow_up = _build_create_close_run_follow_up_arguments(
            snapshot=snapshot,
            operator_content=operator_content,
            operator_memory=operator_memory,
        )
        if create_close_run_follow_up is not None and (
            planning.mode != "tool"
            or _normalize_planned_tool_name(planning.tool_name) != "create_close_run"
        ):
            return planning.model_copy(
                update={
                    "mode": "tool",
                    "assistant_response": (
                        "I'll open that close run now and move this thread onto it."
                    ),
                    "tool_name": "create_close_run",
                    "tool_arguments": create_close_run_follow_up,
                }
            )

        repaired_tool_name = self._repair_planned_tool_name(
            tool_name=planning.tool_name,
            tool_arguments=planning.tool_arguments,
            operator_content=operator_content,
        )
        if planning.mode != "tool" or repaired_tool_name is None:
            return planning.model_copy(
                update={
                    "assistant_response": normalized_response,
                    "tool_name": repaired_tool_name,
                }
            )

        if hasattr(self, "_tool_registry") and self._resolve_tool_definition(
            tool_name=repaired_tool_name
        ) is None:
            return planning.model_copy(
                update={
                    "mode": "read_only",
                    "assistant_response": _build_unresolved_tool_selection_message(
                        operator_content=operator_content,
                        snapshot=snapshot,
                    ),
                    "tool_name": None,
                    "tool_arguments": {},
                }
            )

        tool_arguments = dict(planning.tool_arguments)
        if repaired_tool_name == "review_document":
            tool_arguments = _hydrate_review_document_arguments(
                tool_arguments=tool_arguments,
                snapshot=snapshot,
                operator_content=operator_content,
                operator_memory=operator_memory,
            )
        elif repaired_tool_name == "review_documents":
            tool_arguments = _hydrate_review_documents_arguments(
                tool_arguments=tool_arguments,
                operator_content=operator_content,
            )
        elif repaired_tool_name == "ignore_document":
            document_id = _resolve_document_id_from_snapshot(
                snapshot=snapshot,
                operator_content=operator_content,
                preferred_statuses=("needs_review", "uploaded", "processing", "parsed"),
                operator_memory=operator_memory,
            )
            if document_id is not None and not isinstance(tool_arguments.get("document_id"), str):
                tool_arguments["document_id"] = document_id
        elif repaired_tool_name in {"approve_recommendation", "reject_recommendation"}:
            tool_arguments = _hydrate_recommendation_arguments(
                tool_name=repaired_tool_name,
                tool_arguments=tool_arguments,
                snapshot=snapshot,
                operator_content=operator_content,
                operator_memory=operator_memory,
            )
        elif repaired_tool_name in {"approve_journal", "apply_journal", "reject_journal"}:
            tool_arguments = _hydrate_journal_arguments(
                tool_name=repaired_tool_name,
                tool_arguments=tool_arguments,
                snapshot=snapshot,
                operator_content=operator_content,
                operator_memory=operator_memory,
            )
        elif repaired_tool_name in {
            "approve_reconciliation",
            "disposition_reconciliation_item",
            "resolve_reconciliation_anomaly",
        }:
            tool_arguments = _hydrate_reconciliation_arguments(
                tool_name=repaired_tool_name,
                tool_arguments=tool_arguments,
                snapshot=snapshot,
                operator_content=operator_content,
                operator_memory=operator_memory,
            )
        elif repaired_tool_name in {"update_commentary", "approve_commentary"}:
            tool_arguments = _hydrate_commentary_arguments(
                tool_name=repaired_tool_name,
                tool_arguments=tool_arguments,
                snapshot=snapshot,
                operator_content=operator_content,
                operator_memory=operator_memory,
            )
        elif repaired_tool_name == "distribute_export":
            tool_arguments = _hydrate_export_arguments(
                tool_arguments=tool_arguments,
                snapshot=snapshot,
            )
        elif repaired_tool_name == "create_workspace":
            tool_arguments = _hydrate_create_workspace_arguments(
                tool_arguments=tool_arguments,
                snapshot=snapshot,
            )
        elif repaired_tool_name == "create_close_run":
            tool_arguments = _hydrate_create_close_run_arguments(
                tool_arguments=tool_arguments,
                snapshot=snapshot,
                operator_content=operator_content,
                operator_memory=operator_memory,
            )
        elif repaired_tool_name == "open_close_run":
            tool_arguments = _hydrate_open_close_run_arguments(
                tool_arguments=tool_arguments,
                snapshot=snapshot,
                operator_content=operator_content,
                operator_memory=operator_memory,
            )
        elif repaired_tool_name in {"switch_workspace", "update_workspace", "delete_workspace"}:
            tool_arguments = _hydrate_workspace_arguments(
                tool_name=repaired_tool_name,
                tool_arguments=tool_arguments,
                snapshot=snapshot,
                operator_content=operator_content,
                operator_memory=operator_memory,
            )
        elif repaired_tool_name == "delete_close_run":
            tool_arguments = _hydrate_delete_close_run_arguments(
                tool_arguments=tool_arguments,
                snapshot=snapshot,
                operator_content=operator_content,
                operator_memory=operator_memory,
            )
        elif repaired_tool_name == "reopen_close_run":
            tool_arguments = _hydrate_reopen_close_run_arguments(
                tool_arguments=tool_arguments,
                snapshot=snapshot,
                operator_content=operator_content,
                operator_memory=operator_memory,
            )

        tool_definition = self._resolve_tool_definition(tool_name=repaired_tool_name)
        if tool_definition is not None:
            tool_arguments = _normalize_tool_arguments_against_schema(
                tool_arguments=tool_arguments,
                schema=getattr(tool_definition, "input_schema", None),
            )

        return planning.model_copy(
            update={
                "assistant_response": normalized_response,
                "tool_name": repaired_tool_name,
                "tool_arguments": tool_arguments,
            }
        )

    def _repair_planned_tool_name(
        self,
        *,
        tool_name: str | None,
        tool_arguments: dict[str, Any],
        operator_content: str,
    ) -> str | None:
        """Normalize planner tool selections onto one concrete registered tool name."""

        normalized_tool_name = _normalize_planned_tool_name(tool_name)
        if normalized_tool_name is None:
            return None
        if not hasattr(self, "_tool_registry"):
            return normalized_tool_name
        if self._resolve_tool_definition(tool_name=normalized_tool_name) is not None:
            return normalized_tool_name

        namespace_name = self._resolve_tool_namespace_name(
            tool_name=normalized_tool_name,
        )
        if namespace_name is None:
            return normalized_tool_name

        repaired_tool_name = _infer_tool_name_from_namespace(
            namespace_name=namespace_name,
            operator_content=operator_content,
            tool_arguments=tool_arguments,
        )
        return repaired_tool_name or normalized_tool_name

    def _resolve_tool_namespace_name(
        self,
        *,
        tool_name: str,
    ) -> str | None:
        """Resolve a planner-emitted namespace or specialist label back to its namespace name."""

        normalized_tool_name = _searchable_text(tool_name)
        if not normalized_tool_name:
            return None
        if not hasattr(self, "_tool_registry"):
            return None

        for namespace in self._tool_registry.list_namespaces():
            candidates = {
                namespace.name,
                namespace.name.replace("_", " "),
                namespace.label,
                namespace.specialist_name,
            }
            if any(_searchable_text(candidate) == normalized_tool_name for candidate in candidates):
                return namespace.name
        return None

    def _planning_from_payload(
        self,
        payload: dict[str, Any],
    ) -> AgentPlanningResult:
        """Reconstruct a planning result from one persisted action payload."""

        tool_name_raw = payload.get("tool_name")
        tool_arguments = payload.get("tool_arguments")
        if not isinstance(tool_name_raw, str) or not isinstance(tool_arguments, dict):
            raise ChatActionExecutionError(
                status_code=422,
                code=ChatActionExecutionErrorCode.INVALID_ACTION_PLAN,
                message="The stored chat action payload is incomplete.",
            )
        assistant_response = payload.get("assistant_response")
        reasoning = payload.get("reasoning")
        if not isinstance(assistant_response, str) or not isinstance(reasoning, str):
            raise ChatActionExecutionError(
                status_code=422,
                code=ChatActionExecutionErrorCode.INVALID_ACTION_PLAN,
                message="The stored chat action payload is incomplete.",
            )
        return AgentPlanningResult(
            mode="tool",
            assistant_response=assistant_response,
            reasoning=reasoning,
            tool_name=tool_name_raw,
            tool_arguments=tool_arguments,
        )

    def _build_grounding_payload(self, grounding: GroundingContextRecord) -> dict[str, Any]:
        """Build the evidence snapshot attached to action-mode assistant messages."""

        payload = {
            "entity_id": grounding.context.entity_id,
            "entity_name": grounding.context.entity_name,
            "autonomy_mode": grounding.context.autonomy_mode,
            "base_currency": grounding.context.base_currency,
        }
        if grounding.context.close_run_id is not None:
            payload["close_run_id"] = grounding.context.close_run_id
            payload["period_label"] = grounding.context.period_label
        return payload


def _format_execution_result(applied_result: dict[str, Any]) -> str:
    """Render a compact assistant-visible execution summary from tool output."""

    return _humanize_applied_result(applied_result)


def _parse_pending_plan_decision(content: str) -> str | None:
    """Return whether the operator is confirming or canceling a pending governed action."""

    normalized = _searchable_text(content)
    if any(
        phrase in normalized
        for phrase in (
            "confirm",
            "go ahead",
            "proceed",
            "yes do it",
            "yes apply it",
            "approve the pending action",
            "approve that action",
        )
    ):
        return "approve"
    if any(
        phrase in normalized
        for phrase in (
            "cancel that action",
            "reject that action",
            "reject the pending action",
            "do not do that",
            "don't do that",
            "never mind",
            "stop that",
            "cancel it",
        )
    ):
        return "reject"
    return None


def _build_pending_confirmation_message(
    *,
    tool_name: str,
    tool_arguments: dict[str, Any],
    snapshot: dict[str, Any],
) -> str:
    """Return the governed-action confirmation prompt shown before application."""

    action_summary = _describe_pending_action(
        tool_name=tool_name,
        tool_arguments=tool_arguments,
        snapshot=snapshot,
    )
    consequence = _describe_governed_action_consequence(tool_name=tool_name)
    if consequence is not None:
        return (
            f"{action_summary} {consequence} Reply 'confirm' to apply it or "
            "'cancel' to drop it."
        )
    return f"{action_summary} Reply 'confirm' to apply it or 'cancel' to drop it."


def _build_pending_action_selection_message(
    *,
    decision: str,
    pending_actions: tuple[ChatActionPlanRecord, ...],
    snapshot: dict[str, Any],
) -> str:
    """Ask the operator to pick one pending governed action when several are waiting."""

    action_word = "confirm" if decision == "approve" else "cancel"
    described_actions = [
        _describe_pending_action(
            tool_name=str(record.payload.get("tool_name") or ""),
            tool_arguments=(
                record.payload.get("tool_arguments")
                if isinstance(record.payload.get("tool_arguments"), dict)
                else {}
            ),
            snapshot=snapshot,
        )
        for record in pending_actions[:3]
    ]
    joined_actions = _join_choice_labels(described_actions)
    return (
        f"I have more than one governed action waiting. Tell me which one to {action_word}: "
        f"{joined_actions}."
    )


def _describe_pending_action(
    *,
    tool_name: str,
    tool_arguments: dict[str, Any],
    snapshot: dict[str, Any],
) -> str:
    """Return a natural preview of one governed action waiting for confirmation."""

    if tool_name == "approve_close_run":
        period_label = _optional_snapshot_text(snapshot, "period_label")
        if period_label is not None:
            return f"I have close-run approval ready for {period_label}."
        return "I have close-run approval ready."

    if tool_name == "archive_close_run":
        period_label = _optional_snapshot_text(snapshot, "period_label")
        if period_label is not None:
            return f"I have archive ready for the {period_label} close run."
        return "I have archive ready for this close run."

    if tool_name == "delete_close_run":
        close_run_label = _resolve_close_run_label(
            snapshot=snapshot,
            close_run_id=_optional_argument_text(tool_arguments, "close_run_id"),
        )
        if close_run_label is not None:
            return f"I can permanently delete the {close_run_label} close run."
        return "I can permanently delete this close run."

    if tool_name == "open_close_run":
        close_run_label = _resolve_close_run_label(
            snapshot=snapshot,
            close_run_id=_optional_argument_text(tool_arguments, "close_run_id"),
        )
        if close_run_label is not None:
            return f"I can pin this thread to the {close_run_label} close run."
        return "I can pin this thread to that close run."

    if tool_name == "delete_workspace":
        workspace_label = _resolve_workspace_label(
            snapshot=snapshot,
            workspace_id=_optional_argument_text(tool_arguments, "workspace_id"),
        )
        if workspace_label is not None:
            return f"I can permanently delete the {workspace_label} workspace."
        return "I can permanently delete that workspace."

    if tool_name == "distribute_export":
        recipient_name = _optional_argument_text(tool_arguments, "recipient_name")
        if recipient_name is not None:
            return f"I can record the export distribution for {recipient_name}."
        return "I can record the export distribution."

    return "I have that governed action ready."


def _describe_governed_action_consequence(*, tool_name: str) -> str | None:
    """Return one compact consequence note for a governed action preview."""

    if tool_name == "delete_workspace":
        return "This is permanent and cannot be undone."
    if tool_name == "delete_close_run":
        return "This permanently removes the selected close run."
    if tool_name == "archive_close_run":
        return "This moves the close run into an archived state."
    if tool_name == "approve_close_run":
        return "This marks the current close version as approved."
    if tool_name == "distribute_export":
        return "This records the governed release distribution."
    return None


def _build_target_clarification(
    *,
    tool_name: str,
    tool_arguments: dict[str, Any],
    snapshot: dict[str, Any],
) -> str | None:
    """Return a concise clarification when a tool target is still unresolved."""

    if tool_name in {"review_document", "ignore_document"} and not isinstance(
        tool_arguments.get("document_id"),
        str,
    ):
        choices = _document_choice_labels(snapshot=snapshot)
        if choices:
            return (
                "Which document should I work on? "
                f"I can use { _join_choice_labels(choices) }."
            )
        return "Which document should I work on?"

    if tool_name in {"approve_recommendation", "reject_recommendation"} and not isinstance(
        tool_arguments.get("recommendation_id"),
        str,
    ):
        choices = _recommendation_choice_labels(snapshot=snapshot)
        if choices:
            return (
                "Which recommendation should I handle? "
                f"I can use { _join_choice_labels(choices) }."
            )
        return "Which recommendation should I handle?"

    if tool_name in {"approve_journal", "apply_journal", "reject_journal"} and not isinstance(
        tool_arguments.get("journal_id"),
        str,
    ):
        if tool_name == "apply_journal":
            approved_choices = _journal_choice_labels_with_status(
                snapshot=snapshot,
                statuses={"approved"},
            )
            if approved_choices:
                return (
                    "Which approved journal should I apply? "
                    f"I can use { _join_choice_labels(approved_choices) }."
                )
            pending_choices = _journal_choice_labels_with_status(
                snapshot=snapshot,
                statuses={"pending_review", "pending", "draft"},
            )
            if pending_choices:
                return (
                    "There isn't an approved journal ready to post yet. "
                    "I can approve "
                    f"{_join_choice_labels(pending_choices)} first if you want."
                )
        choices = _journal_choice_labels(snapshot=snapshot)
        if choices:
            return f"Which journal should I use? I can use { _join_choice_labels(choices) }."
        return "Which journal should I use?"

    if tool_name == "approve_reconciliation" and not isinstance(
        tool_arguments.get("reconciliation_id"),
        str,
    ):
        choices = _reconciliation_choice_labels(snapshot=snapshot)
        if choices:
            return (
                "Which reconciliation should I approve? "
                f"I can use { _join_choice_labels(choices) }."
            )
        return "Which reconciliation should I approve?"

    if tool_name == "disposition_reconciliation_item" and not isinstance(
        tool_arguments.get("item_id"),
        str,
    ):
        choices = _reconciliation_item_choice_labels(snapshot=snapshot)
        if choices:
            return (
                "Which reconciliation exception should I resolve? "
                f"I can use { _join_choice_labels(choices) }."
            )
        return "Which reconciliation exception should I resolve?"

    if tool_name == "resolve_reconciliation_anomaly" and not isinstance(
        tool_arguments.get("anomaly_id"),
        str,
    ):
        choices = _reconciliation_anomaly_choice_labels(snapshot=snapshot)
        if choices:
            return (
                "Which reconciliation anomaly should I resolve? "
                f"I can use { _join_choice_labels(choices) }."
            )
        return "Which reconciliation anomaly should I resolve?"

    if tool_name in {"update_commentary", "approve_commentary"} and (
        not isinstance(tool_arguments.get("report_run_id"), str)
        or not isinstance(tool_arguments.get("section_key"), str)
    ):
        choices = _commentary_choice_labels(snapshot=snapshot)
        if choices:
            return (
                "Which commentary section should I use? "
                f"I can use { _join_choice_labels(choices) }."
            )
        return "Which commentary section should I use?"

    if tool_name == "distribute_export" and not isinstance(tool_arguments.get("export_id"), str):
        choices = _export_choice_labels(snapshot=snapshot)
        if choices:
            return (
                "Which export should I distribute? "
                f"I can use {_join_choice_labels(choices)}."
            )
        return (
            "There isn't a completed export ready to distribute yet. "
            "I can package one first if you want."
        )

    if tool_name in {"switch_workspace", "update_workspace", "delete_workspace"} and not isinstance(
        tool_arguments.get("workspace_id"),
        str,
    ):
        choices = _workspace_choice_labels(snapshot=snapshot)
        if choices:
            return (
                "Which workspace should I use? "
                f"I can use { _join_choice_labels(choices) }."
            )
        return "Which workspace should I use?"

    if tool_name == "delete_close_run" and not isinstance(tool_arguments.get("close_run_id"), str):
        choices = _close_run_choice_labels(snapshot=snapshot)
        if choices:
            return (
                "Which close run should I delete? "
                f"I can use { _join_choice_labels(choices) }."
            )
        return "Which close run should I delete?"

    if tool_name == "open_close_run" and not isinstance(tool_arguments.get("close_run_id"), str):
        choices = _close_run_choice_labels(snapshot=snapshot)
        if choices:
            return (
                "Which close run should I use? "
                f"I can use { _join_choice_labels(choices) }."
            )
        return "Which close run should I use?"

    return None


def _missing_required_fields(
    *,
    tool_arguments: dict[str, Any],
    required_fields: object,
) -> tuple[str, ...]:
    """Return required tool fields that are still missing after hydration."""

    if not isinstance(required_fields, list):
        return ()
    missing: list[str] = []
    for field_name in required_fields:
        if not isinstance(field_name, str):
            continue
        value = tool_arguments.get(field_name)
        if value is None:
            missing.append(field_name)
            continue
        if isinstance(value, str) and not value.strip():
            missing.append(field_name)
    return tuple(missing)


def _build_missing_field_clarification(
    *,
    tool_name: str,
    missing_fields: tuple[str, ...],
) -> str:
    """Return a concise clarification for remaining required non-target fields."""

    if tool_name == "create_close_run" and {
        "period_start",
        "period_end",
    }.intersection(missing_fields):
        return "Which period should I open? Give me the month or the exact start and end date."
    if tool_name == "create_workspace" and "name" in missing_fields:
        return "What should I call the new workspace?"
    if tool_name == "update_commentary" and "body" in missing_fields:
        return "What commentary should I write into that section?"

    field_labels = [_humanize_field_name(field_name) for field_name in missing_fields[:2]]
    if len(field_labels) == 1:
        return f"I can do that, but I still need {field_labels[0]}."
    return f"I can do that, but I still need {field_labels[0]} and {field_labels[1]}."


def _normalize_tool_arguments_against_schema(
    *,
    tool_arguments: dict[str, Any],
    schema: object,
) -> dict[str, Any]:
    """Repair harmless model formatting drift before strict registry validation."""

    if not isinstance(schema, dict):
        return tool_arguments
    normalized = _normalize_schema_object_value(value=tool_arguments, schema=schema)
    return normalized if isinstance(normalized, dict) else tool_arguments


def _normalize_schema_object_value(*, value: Any, schema: dict[str, Any]) -> Any:
    schema_types = _schema_type_names(schema.get("type"))
    if "object" in schema_types or isinstance(schema.get("properties"), dict):
        if not isinstance(value, dict):
            return value
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return value
        allow_extra = schema.get("additionalProperties") is not False
        normalized_object: dict[str, Any] = {}
        for key, nested_value in value.items():
            if not isinstance(key, str):
                continue
            nested_schema = properties.get(key)
            if not isinstance(nested_schema, dict):
                if allow_extra:
                    normalized_object[key] = nested_value
                continue
            normalized_object[key] = _normalize_schema_object_value(
                value=nested_value,
                schema=nested_schema,
            )
        return normalized_object

    if isinstance(value, str):
        enum_values = schema.get("enum")
        if isinstance(enum_values, list):
            enum_match = _match_schema_enum_value(value=value, enum_values=enum_values)
            if enum_match is not None:
                return enum_match
        if "boolean" in schema_types:
            boolean_value = _coerce_string_boolean(value)
            if boolean_value is not None:
                return boolean_value
        if "integer" in schema_types:
            integer_value = _coerce_string_integer(value)
            if integer_value is not None:
                return integer_value
        if "number" in schema_types:
            number_value = _coerce_string_number(value)
            if number_value is not None:
                return number_value

    if isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            return [
                _normalize_schema_object_value(value=item, schema=item_schema)
                for item in value
            ]
        return value

    return value


def _schema_type_names(raw_type: object) -> set[str]:
    """Return JSON-schema type names from a scalar or union type declaration."""

    if isinstance(raw_type, str):
        return {raw_type}
    if isinstance(raw_type, list):
        return {item for item in raw_type if isinstance(item, str)}
    return set()


def _match_schema_enum_value(*, value: str, enum_values: list[object]) -> Any | None:
    """Return an enum member matching relaxed user/model spelling."""

    normalized_value = _enum_match_key(value)
    for enum_value in enum_values:
        if not isinstance(enum_value, str):
            continue
        if _enum_match_key(enum_value) == normalized_value:
            return enum_value
    return None


def _enum_match_key(value: str) -> str:
    """Normalize a string for enum repair without changing canonical output."""

    return " ".join(
        value.strip().lower().replace("-", " ").replace("_", " ").split()
    )


def _coerce_string_boolean(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized in {"true", "yes", "y", "1"}:
        return True
    if normalized in {"false", "no", "n", "0"}:
        return False
    return None


def _coerce_string_integer(value: str) -> int | None:
    stripped = value.strip()
    if not stripped:
        return None
    try:
        parsed = int(stripped)
    except ValueError:
        return None
    return parsed


def _coerce_string_number(value: str) -> int | float | None:
    stripped = value.strip()
    if not stripped:
        return None
    try:
        parsed = float(stripped)
    except ValueError:
        return None
    return int(parsed) if parsed.is_integer() else parsed


def _humanize_field_name(field_name: str) -> str:
    """Return a compact operator-facing label for one tool argument name."""

    return field_name.replace("_", " ")


def _join_choice_labels(labels: list[str]) -> str:
    """Join clarification options into one short conversational phrase."""

    cleaned = [label.strip() for label in labels if label.strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} or {cleaned[1]}"
    return f"{cleaned[0]}, {cleaned[1]}, or {cleaned[2]}"


def _normalize_operator_control_command(value: str) -> str:
    """Return one channel-portable command string derived from guidance text."""

    cleaned = value.strip()
    if not cleaned:
        return cleaned
    lowered = cleaned.lower()
    prefixes = (
        "ask the agent to ",
        "next, i can ",
        "next i can ",
    )
    for prefix in prefixes:
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    return cleaned.rstrip()


def _build_operator_control_label(command: str) -> str:
    """Return a short label for one portable operator command."""

    cleaned = command.strip().rstrip(".")
    if not cleaned:
        return "Suggested action"
    truncated = cleaned if len(cleaned) <= 42 else f"{cleaned[:39].rstrip()}…"
    return truncated[:1].upper() + truncated[1:]


def _optional_argument_text(arguments: dict[str, Any], key: str) -> str | None:
    """Return one normalized string argument when present."""

    value = arguments.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _optional_snapshot_text(snapshot: dict[str, Any], key: str) -> str | None:
    """Return one normalized string snapshot field when present."""

    value = snapshot.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _document_choice_labels(*, snapshot: dict[str, Any]) -> list[str]:
    """Return compact document labels for clarification prompts."""

    documents = snapshot.get("documents")
    if not isinstance(documents, list):
        return []
    labels = [
        str(record.get("filename"))
        for record in documents
        if isinstance(record, dict) and isinstance(record.get("filename"), str)
    ]
    return labels[:3]


def _recommendation_choice_labels(*, snapshot: dict[str, Any]) -> list[str]:
    """Return compact recommendation labels for clarification prompts."""

    recommendations = snapshot.get("recommendations")
    if not isinstance(recommendations, list):
        return []
    labels: list[str] = []
    for record in recommendations:
        if not isinstance(record, dict):
            continue
        if isinstance(record.get("document_filename"), str):
            labels.append(str(record["document_filename"]))
            continue
        if isinstance(record.get("recommendation_type"), str):
            labels.append(str(record["recommendation_type"]).replace("_", " "))
    return labels[:3]


def _journal_choice_labels(*, snapshot: dict[str, Any]) -> list[str]:
    """Return compact journal labels for clarification prompts."""

    return _journal_choice_labels_with_status(snapshot=snapshot, statuses=None)


def _journal_choice_labels_with_status(
    *,
    snapshot: dict[str, Any],
    statuses: set[str] | None,
) -> list[str]:
    """Return compact journal labels filtered to the requested statuses when provided."""

    journals = snapshot.get("journals")
    if not isinstance(journals, list):
        return []
    labels: list[str] = []
    for record in journals:
        if not isinstance(record, dict):
            continue
        if statuses is not None and str(record.get("status") or "") not in statuses:
            continue
        if isinstance(record.get("journal_number"), str):
            labels.append(str(record["journal_number"]))
            continue
        if isinstance(record.get("description"), str):
            labels.append(str(record["description"]))
    return labels[:3]


def _reconciliation_choice_labels(*, snapshot: dict[str, Any]) -> list[str]:
    """Return reconciliation labels for clarification prompts."""

    reconciliations = snapshot.get("reconciliations")
    if not isinstance(reconciliations, list):
        return []
    labels = [
        str(record.get("type")).replace("_", " ")
        for record in reconciliations
        if isinstance(record, dict) and isinstance(record.get("type"), str)
    ]
    return labels[:3]


def _reconciliation_item_choice_labels(*, snapshot: dict[str, Any]) -> list[str]:
    """Return reconciliation-item labels for clarification prompts."""

    items = snapshot.get("reconciliation_items")
    if not isinstance(items, list):
        return []
    labels: list[str] = []
    for record in items:
        if not isinstance(record, dict):
            continue
        if isinstance(record.get("source_ref"), str):
            labels.append(str(record["source_ref"]))
            continue
        if isinstance(record.get("explanation"), str):
            labels.append(str(record["explanation"]))
    return labels[:3]


def _reconciliation_anomaly_choice_labels(*, snapshot: dict[str, Any]) -> list[str]:
    """Return reconciliation-anomaly labels for clarification prompts."""

    anomalies = snapshot.get("reconciliation_anomalies")
    if not isinstance(anomalies, list):
        return []
    labels: list[str] = []
    for record in anomalies:
        if not isinstance(record, dict):
            continue
        if isinstance(record.get("account_code"), str):
            labels.append(str(record["account_code"]))
            continue
        if isinstance(record.get("description"), str):
            labels.append(str(record["description"]))
    return labels[:3]


def _commentary_choice_labels(*, snapshot: dict[str, Any]) -> list[str]:
    """Return commentary-section labels for clarification prompts."""

    commentary = snapshot.get("commentary")
    if not isinstance(commentary, list):
        return []
    labels = [
        _format_report_section_label(str(record.get("section_key")))
        for record in commentary
        if isinstance(record, dict) and isinstance(record.get("section_key"), str)
    ]
    return labels[:3]


def _export_choice_labels(*, snapshot: dict[str, Any]) -> list[str]:
    """Return export labels for clarification prompts."""

    exports = snapshot.get("exports")
    if not isinstance(exports, list):
        return []
    labels: list[str] = []
    for record in exports:
        if not isinstance(record, dict):
            continue
        version_no = record.get("version_no")
        status = record.get("status")
        if isinstance(version_no, int) and isinstance(status, str):
            labels.append(f"export v{version_no} ({status})")
            continue
        if isinstance(status, str):
            labels.append(f"export ({status})")
    return labels[:3]


def _workspace_choice_labels(*, snapshot: dict[str, Any]) -> list[str]:
    """Return workspace labels for clarification prompts."""

    workspaces = snapshot.get("accessible_workspaces")
    if not isinstance(workspaces, list):
        current = snapshot.get("workspace")
        if isinstance(current, dict) and isinstance(current.get("name"), str):
            return [str(current["name"])]
        return []
    labels = [
        str(record.get("name"))
        for record in workspaces
        if isinstance(record, dict) and isinstance(record.get("name"), str)
    ]
    current = snapshot.get("workspace")
    if isinstance(current, dict) and isinstance(current.get("name"), str):
        current_name = str(current["name"])
        if current_name not in labels:
            labels.insert(0, current_name)
    return labels[:3]


def _close_run_choice_labels(*, snapshot: dict[str, Any]) -> list[str]:
    """Return close-run labels for clarification prompts."""

    close_runs = snapshot.get("entity_close_runs")
    if not isinstance(close_runs, list):
        return []
    labels = [
        str(record.get("period_label"))
        for record in close_runs
        if isinstance(record, dict) and isinstance(record.get("period_label"), str)
    ]
    return labels[:3]


def _workspace_name_from_snapshot(*, snapshot: dict[str, Any]) -> str | None:
    """Return the current workspace display name when present."""

    workspace = snapshot.get("workspace")
    if not isinstance(workspace, dict):
        return None
    name = workspace.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _first_readiness_action(*, snapshot: dict[str, Any]) -> str | None:
    """Return the first grounded next action from the readiness snapshot."""

    readiness = snapshot.get("readiness")
    if not isinstance(readiness, dict):
        return None
    next_actions = readiness.get("next_actions")
    if not isinstance(next_actions, list):
        return None
    for action in next_actions:
        if isinstance(action, str) and action.strip():
            return action.strip().rstrip(".")
    return None


def _readiness_items(*, snapshot: dict[str, Any], key: str) -> list[str]:
    """Return one normalized readiness list field."""

    readiness = snapshot.get("readiness")
    if not isinstance(readiness, dict):
        return []
    values = readiness.get(key)
    if not isinstance(values, list):
        return []
    return [
        value.strip()
        for value in values
        if isinstance(value, str) and value.strip()
    ]


def _summarize_status_items(
    *,
    items: list[str],
    single_prefix: str,
    multi_prefix: str,
) -> str:
    """Return one short natural-language summary of blocker or warning items."""

    cleaned = [item.strip().rstrip(".") for item in items if item.strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return f"{single_prefix}{cleaned[0]}."
    return f"{multi_prefix}{cleaned[0]} and {cleaned[1]}."


def _describe_close_run_summary(*, record: dict[str, Any]) -> str | None:
    """Return one short close-run summary label for list-style answers."""

    period_label = record.get("period_label")
    if not isinstance(period_label, str) or not period_label.strip():
        return None
    status = record.get("status")
    active_phase = record.get("active_phase")
    if isinstance(active_phase, str) and active_phase.strip():
        return (
            f"{period_label.strip()} "
            f"({str(status or 'active').replace('_', ' ')}, "
            f"{_format_phase_label(active_phase)})"
        )
    if isinstance(status, str) and status.strip():
        return f"{period_label.strip()} ({status.replace('_', ' ')})"
    return period_label.strip()


def _pending_document_records(*, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return document records that are actionable for chat-driven review."""

    documents = snapshot.get("documents")
    if not isinstance(documents, list):
        return []
    return [
        record
        for record in documents
        if isinstance(record, dict)
        and str(record.get("status") or "") in {"needs_review", "parsed"}
    ]


def _single_pending_document_record(*, snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Return the single actionable document when exactly one is pending."""

    records = _pending_document_records(snapshot=snapshot)
    return records[0] if len(records) == 1 else None


def _count_pending_documents(*, snapshot: dict[str, Any]) -> int:
    """Return the count of actionable document review targets."""

    return len(_pending_document_records(snapshot=snapshot))


def _document_specific_label(*, record: dict[str, Any]) -> str | None:
    """Return one specific document label for clarifications and focus cues."""

    filename = record.get("filename")
    if isinstance(filename, str) and filename.strip():
        return f"the document {filename.strip()}"
    return None


def _document_domain_candidate_label(
    *,
    snapshot: dict[str, Any],
    intent: str,
) -> str | None:
    """Return a document-domain label for a generic cross-domain clarification."""

    records = _pending_document_records(snapshot=snapshot)
    if not records:
        return None
    if len(records) == 1:
        return _document_specific_label(record=records[0])
    if intent == "ignore":
        return "a source document"
    return "a document awaiting review"


def _pending_recommendation_records(*, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return recommendation records that are still waiting on operator action."""

    recommendations = snapshot.get("recommendations")
    if not isinstance(recommendations, list):
        return []
    return [
        record
        for record in recommendations
        if isinstance(record, dict)
        and str(record.get("status") or "") in {"pending_review", "pending", "draft"}
    ]


def _single_pending_recommendation_record(*, snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Return the single pending recommendation when one clear target exists."""

    records = _pending_recommendation_records(snapshot=snapshot)
    return records[0] if len(records) == 1 else None


def _recommendation_specific_label(*, record: dict[str, Any]) -> str | None:
    """Return one recommendation label grounded in document or type context."""

    document_filename = record.get("document_filename")
    if isinstance(document_filename, str) and document_filename.strip():
        return f"the recommendation for {document_filename.strip()}"
    recommendation_type = record.get("recommendation_type")
    if isinstance(recommendation_type, str) and recommendation_type.strip():
        return f"the {recommendation_type.replace('_', ' ')} recommendation"
    return "the recommendation"


def _recommendation_domain_candidate_label(
    *,
    snapshot: dict[str, Any],
    intent: str,
) -> str | None:
    """Return a recommendation-domain label for a generic cross-domain clarification."""

    records = _pending_recommendation_records(snapshot=snapshot)
    if not records:
        return None
    if len(records) == 1:
        return _recommendation_specific_label(record=records[0])
    if intent == "reject":
        return "a pending recommendation"
    return "a recommendation awaiting review"


def _pending_journal_records(
    *,
    snapshot: dict[str, Any],
    intent: str,
) -> list[dict[str, Any]]:
    """Return journal records relevant to one generic operator verb."""

    journals = snapshot.get("journals")
    if not isinstance(journals, list):
        return []
    if intent == "apply":
        target_statuses = {"approved"}
    else:
        target_statuses = {"pending_review", "pending", "draft"}
    return [
        record
        for record in journals
        if isinstance(record, dict)
        and str(record.get("status") or "") in target_statuses
    ]


def _single_pending_journal_record(
    *,
    snapshot: dict[str, Any],
    intent: str,
) -> dict[str, Any] | None:
    """Return the single actionable journal when exactly one exists."""

    records = _pending_journal_records(snapshot=snapshot, intent=intent)
    return records[0] if len(records) == 1 else None


def _journal_specific_label(*, record: dict[str, Any]) -> str | None:
    """Return one specific journal label for clarifications and focus cues."""

    journal_number = record.get("journal_number")
    if isinstance(journal_number, str) and journal_number.strip():
        return f"journal {journal_number.strip()}"
    description = record.get("description")
    if isinstance(description, str) and description.strip():
        return f"the journal {description.strip()}"
    return "the journal"


def _journal_domain_candidate_label(
    *,
    snapshot: dict[str, Any],
    intent: str,
) -> str | None:
    """Return a journal-domain label for a generic cross-domain clarification."""

    records = _pending_journal_records(snapshot=snapshot, intent=intent)
    if not records:
        return None
    if len(records) == 1:
        return _journal_specific_label(record=records[0])
    if intent == "apply":
        return "an approved journal"
    return "a journal awaiting review"


def _pending_reconciliation_records(*, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return reconciliation headers still waiting on approval."""

    reconciliations = snapshot.get("reconciliations")
    if not isinstance(reconciliations, list):
        return []
    return [
        record
        for record in reconciliations
        if isinstance(record, dict)
        and str(record.get("status") or "") in {"in_review", "blocked", "draft"}
    ]


def _reconciliation_domain_candidate_label(*, snapshot: dict[str, Any]) -> str | None:
    """Return a reconciliation approval label for a generic clarification."""

    records = _pending_reconciliation_records(snapshot=snapshot)
    if not records:
        return None
    if len(records) == 1:
        reconciliation_type = records[0].get("type")
        if isinstance(reconciliation_type, str) and reconciliation_type.strip():
            return f"the {reconciliation_type.replace('_', ' ')} reconciliation"
        return "the reconciliation"
    return "a reconciliation awaiting approval"


def _pending_reconciliation_item_records(*, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return reconciliation items that still require disposition."""

    items = snapshot.get("reconciliation_items")
    if not isinstance(items, list):
        return []
    return [
        record
        for record in items
        if isinstance(record, dict)
        and bool(record.get("requires_disposition"))
        and record.get("disposition") is None
    ]


def _single_pending_reconciliation_item_record(
    *,
    snapshot: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the single unresolved reconciliation item when exactly one exists."""

    records = _pending_reconciliation_item_records(snapshot=snapshot)
    return records[0] if len(records) == 1 else None


def _reconciliation_item_specific_label(*, record: dict[str, Any]) -> str | None:
    """Return one specific reconciliation-item label."""

    source_ref = record.get("source_ref")
    if isinstance(source_ref, str) and source_ref.strip():
        return f"the reconciliation exception {source_ref.strip()}"
    explanation = record.get("explanation")
    if isinstance(explanation, str) and explanation.strip():
        return f"the reconciliation exception {explanation.strip()}"
    return "the reconciliation exception"


def _reconciliation_item_domain_candidate_label(*, snapshot: dict[str, Any]) -> str | None:
    """Return a reconciliation-item label for generic resolve requests."""

    records = _pending_reconciliation_item_records(snapshot=snapshot)
    if not records:
        return None
    if len(records) == 1:
        return _reconciliation_item_specific_label(record=records[0])
    return "a reconciliation exception"


def _pending_reconciliation_anomaly_records(*, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return unresolved reconciliation anomalies from the snapshot."""

    anomalies = snapshot.get("reconciliation_anomalies")
    if not isinstance(anomalies, list):
        return []
    return [
        record
        for record in anomalies
        if isinstance(record, dict) and bool(record.get("resolved")) is False
    ]


def _single_pending_reconciliation_anomaly_record(
    *,
    snapshot: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the single unresolved reconciliation anomaly when exactly one exists."""

    records = _pending_reconciliation_anomaly_records(snapshot=snapshot)
    return records[0] if len(records) == 1 else None


def _reconciliation_anomaly_specific_label(*, record: dict[str, Any]) -> str | None:
    """Return one specific reconciliation-anomaly label."""

    account_code = record.get("account_code")
    if isinstance(account_code, str) and account_code.strip():
        return f"the anomaly on account {account_code.strip()}"
    description = record.get("description")
    if isinstance(description, str) and description.strip():
        return f"the anomaly {description.strip()}"
    return "the reconciliation anomaly"


def _reconciliation_anomaly_domain_candidate_label(*, snapshot: dict[str, Any]) -> str | None:
    """Return a reconciliation-anomaly label for generic resolve requests."""

    records = _pending_reconciliation_anomaly_records(snapshot=snapshot)
    if not records:
        return None
    if len(records) == 1:
        return _reconciliation_anomaly_specific_label(record=records[0])
    return "a reconciliation anomaly"


def _pending_commentary_records(*, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return commentary sections that are still in draft or review."""

    commentary = snapshot.get("commentary")
    if not isinstance(commentary, list):
        return []
    return [
        record
        for record in commentary
        if isinstance(record, dict)
        and str(record.get("status") or "") in {"draft", "in_review", "pending_review"}
    ]


def _single_pending_commentary_record(*, snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Return the single pending commentary section when exactly one exists."""

    records = _pending_commentary_records(snapshot=snapshot)
    return records[0] if len(records) == 1 else None


def _commentary_specific_label(*, record: dict[str, Any]) -> str | None:
    """Return one specific commentary-section label."""

    section_key = record.get("section_key")
    if isinstance(section_key, str) and section_key.strip():
        return f"the {_format_report_section_label(section_key)} commentary"
    return "the commentary section"


def _commentary_domain_candidate_label(*, snapshot: dict[str, Any]) -> str | None:
    """Return a commentary label for generic approval requests."""

    records = _pending_commentary_records(snapshot=snapshot)
    if not records:
        return None
    if len(records) == 1:
        return _commentary_specific_label(record=records[0])
    return "a commentary section awaiting approval"


def _resolve_workspace_label(*, snapshot: dict[str, Any], workspace_id: str | None) -> str | None:
    """Resolve one workspace display label from the snapshot when possible."""

    current = snapshot.get("workspace")
    if isinstance(current, dict) and str(current.get("id") or "") == str(workspace_id or ""):
        name = current.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()

    workspaces = snapshot.get("accessible_workspaces")
    if not isinstance(workspaces, list):
        return None
    for record in workspaces:
        if not isinstance(record, dict):
            continue
        if str(record.get("id") or "") != str(workspace_id or ""):
            continue
        name = record.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _resolve_close_run_label(*, snapshot: dict[str, Any], close_run_id: str | None) -> str | None:
    """Resolve one close-run period label from the snapshot when possible."""

    close_runs = snapshot.get("entity_close_runs")
    if not isinstance(close_runs, list):
        period_label = _optional_snapshot_text(snapshot, "period_label")
        return period_label
    for record in close_runs:
        if not isinstance(record, dict):
            continue
        if str(record.get("id") or "") != str(close_run_id or ""):
            continue
        period_label = record.get("period_label")
        if isinstance(period_label, str) and period_label.strip():
            return period_label.strip()
    return _optional_snapshot_text(snapshot, "period_label")


def _build_operator_loop_action_signature(action: AgentPlannedAction) -> str:
    """Return one stable signature for repeat-guard checks inside a looped turn."""

    return json.dumps(
        {
            "tool": action.tool.name,
            "arguments": action.planning.tool_arguments,
        },
        default=str,
        sort_keys=True,
    )


def _resolve_action_thread_scope(
    *,
    action: AgentPlannedAction,
    default_entity_id: UUID,
    default_close_run_id: UUID | None,
) -> tuple[UUID, UUID | None]:
    """Return the entity/close-run scope the action should be recorded against."""

    if action.tool.name == "create_close_run":
        workspace_id = _optional_uuid_from_arguments(
            arguments=action.planning.tool_arguments,
            key="workspace_id",
        )
        if workspace_id is not None:
            return workspace_id, None
    if action.tool.name == "reopen_close_run":
        close_run_id = _optional_uuid_from_arguments(
            arguments=action.planning.tool_arguments,
            key="close_run_id",
        )
        if close_run_id is not None:
            return default_entity_id, close_run_id
    if action.tool.name == "open_close_run":
        close_run_id = _optional_uuid_from_arguments(
            arguments=action.planning.tool_arguments,
            key="close_run_id",
        )
        if close_run_id is not None:
            return default_entity_id, close_run_id
    return default_entity_id, default_close_run_id


def _format_operator_loop_result_summary(applied_results: list[dict[str, Any]]) -> str | None:
    """Return a compact summary of the work already completed in this turn."""

    if not applied_results:
        return None
    unique_summaries: list[str] = []
    for applied_result in applied_results[-4:]:
        summary = _humanize_applied_result(applied_result)
        if summary in unique_summaries:
            continue
        unique_summaries.append(summary)
    if not unique_summaries:
        return None
    if len(unique_summaries) == 1:
        return unique_summaries[0]
    return " ".join(unique_summaries)


def _format_approval_message(
    assistant_response: object,
    applied_result: dict[str, Any],
    *,
    handoff_message: str | None = None,
    snapshot: dict[str, Any] | None = None,
) -> str:
    """Render the assistant follow-up after a human-approved action executes."""

    base = (
        _normalize_operator_facing_text(assistant_response)
        if isinstance(assistant_response, str)
        else "Approved action executed."
    )
    return _compose_assistant_content(
        assistant_response=base,
        handoff_message=handoff_message,
        result_summary=_format_execution_result(applied_result),
        next_step=_format_next_step(snapshot),
    )


def _compose_assistant_content(
    *,
    assistant_response: str | None,
    handoff_message: str | None,
    result_summary: str | None,
    next_step: str | None,
) -> str:
    """Join the user-facing response, scope notes, result summary, and suggested next step."""

    parts = [
        _normalize_operator_facing_text(assistant_response),
        _normalize_operator_facing_text(handoff_message) if handoff_message else None,
        _normalize_operator_facing_text(result_summary) if result_summary else None,
        _normalize_operator_facing_text(next_step) if next_step else None,
    ]
    unique_parts: list[str] = []
    for part in parts:
        if not part:
            continue
        if part in unique_parts:
            continue
        unique_parts.append(part)
    return "\n\n".join(unique_parts)


def _normalize_operator_facing_text(value: object) -> str:
    """Strip rigid markdown formatting so chat replies read like normal assistant text."""

    if not isinstance(value, str):
        return ""
    normalized = value.replace("\r\n", "\n").strip()
    if not normalized:
        return ""

    normalized = normalized.replace("**", "").replace("__", "").replace("`", "")
    cleaned_lines: list[str] = []
    previous_blank = False
    for raw_line in normalized.split("\n"):
        line = raw_line.strip()
        if not line:
            if not previous_blank:
                cleaned_lines.append("")
            previous_blank = True
            continue
        previous_blank = False
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^[-*•]\s+", "", line)
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


def _build_direct_operator_status_response(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary | None = None,
) -> str | None:
    """Return a deterministic grounded answer for common read-only operator questions."""

    if _is_close_run_creation_request(operator_content):
        return None

    for builder in (
        _build_workspace_scope_status_response,
        _build_close_run_scope_status_response,
        _build_document_skip_follow_up_response,
        _build_document_upload_status_response,
        _build_close_blocker_status_response,
        _build_next_step_status_response,
        _build_financial_report_analysis_response,
        _build_close_run_detail_status_response,
        _build_all_workspace_close_run_directory_response,
        _build_close_run_directory_response,
    ):
        response = builder(
            snapshot=snapshot,
            operator_content=operator_content,
            operator_memory=operator_memory,
        )
        if response is not None:
            return response
    return None


def _build_close_run_scope_status_response(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary | None = None,
) -> str | None:
    """Answer direct current-close-scope questions without invoking the planner."""

    del operator_memory
    normalized_content = _searchable_text(operator_content)
    if not any(
        phrase in normalized_content
        for phrase in (
            "which close run are you currently on",
            "what close run are you currently on",
            "which close run is this",
            "what close run is this",
            "which close run is this chat on",
            "what close run is this chat on",
            "which period are you on",
            "what period are you on",
            "what period is this close",
            "what close are we on",
            "which close are we on",
        )
    ):
        return None

    workspace_name = _workspace_name_from_snapshot(snapshot=snapshot)
    close_run_id = snapshot.get("close_run_id")
    if not isinstance(close_run_id, str) or not close_run_id.strip():
        if workspace_name is not None:
            return (
                f"This chat is not pinned to a close run right now. "
                f"It is currently at {workspace_name} workspace scope."
            )
        return "This chat is not pinned to a close run right now."

    close_run_label = _resolve_close_run_label(
        snapshot=snapshot,
        close_run_id=close_run_id,
    ) or "the active close run"
    if workspace_name is not None:
        return (
            f"This chat is currently pinned to {close_run_label} in "
            f"{workspace_name}."
        )
    return f"This chat is currently pinned to {close_run_label}."


def _build_close_blocker_status_response(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary | None = None,
) -> str | None:
    """Answer common blocker questions directly from the readiness snapshot."""

    del operator_memory
    normalized_content = _searchable_text(operator_content)
    if not any(
        phrase in normalized_content
        for phrase in (
            "what is blocking this close",
            "what s blocking this close",
            "what is blocking the close",
            "what s blocking the close",
            "why is this close blocked",
            "why is the close blocked",
            "what is stopping this close",
            "what is holding up this close",
        )
    ):
        return None

    readiness = snapshot.get("readiness")
    if not isinstance(readiness, dict):
        return None

    if snapshot.get("close_run_id") is None:
        next_action = _first_readiness_action(snapshot=snapshot)
        if next_action is not None:
            return (
                "This chat is not pinned to a close run yet. "
                f"The next step is to { _lowercase_leading_character(next_action) }"
            )
        return "This chat is not pinned to a close run yet."

    blockers = [
        blocker.strip()
        for blocker in readiness.get("blockers", [])
        if isinstance(blocker, str) and blocker.strip()
    ]
    warnings = [
        warning.strip()
        for warning in readiness.get("warnings", [])
        if isinstance(warning, str) and warning.strip()
    ]
    next_action = _first_readiness_action(snapshot=snapshot)
    if blockers:
        blocker_text = _summarize_status_items(
            items=blockers,
            single_prefix="Right now this close is blocked by ",
            multi_prefix="Right now the main blockers are ",
        )
        if next_action is not None:
            return (
                f"{blocker_text} "
                f"The next best move is to { _lowercase_leading_character(next_action) }"
            )
        return blocker_text
    if warnings:
        warning_text = _summarize_status_items(
            items=warnings,
            single_prefix=(
                "Nothing is hard-blocking this close right now. "
                "The main thing needing attention is "
            ),
            multi_prefix=(
                "Nothing is hard-blocking this close right now. "
                "The main things needing attention are "
            ),
        )
        if next_action is not None:
            return (
                f"{warning_text} "
                f"The next best move is to { _lowercase_leading_character(next_action) }"
            )
        return warning_text
    if next_action is not None:
        return (
            "Nothing is hard-blocking this close right now. "
            f"The next best move is to { _lowercase_leading_character(next_action) }"
        )
    return "Nothing is hard-blocking this close right now."


def _build_next_step_status_response(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary | None = None,
) -> str | None:
    """Answer direct next-step questions from readiness without a planner turn."""

    del operator_memory
    normalized_content = _searchable_text(operator_content)
    if not any(
        phrase in normalized_content
        for phrase in (
            "what should we do next",
            "what should i do next",
            "what do we do next",
            "what do i do next",
            "what s the next step",
            "what is the next step",
            "what next",
            "what should happen next",
            "where should we go next",
        )
    ):
        return None

    next_action = _first_readiness_action(snapshot=snapshot)
    if next_action is None:
        return None
    blockers = _readiness_items(snapshot=snapshot, key="blockers")
    if blockers:
        return (
            f"The next best move is to { _lowercase_leading_character(next_action) } "
            f"First, we still need to clear {blockers[0]}."
        )
    return f"The next best move is to { _lowercase_leading_character(next_action) }"


def _build_document_skip_follow_up_response(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary | None = None,
) -> str | None:
    """Answer follow-ups asking which source documents were skipped or held."""

    if not _is_document_skip_follow_up_request(
        operator_content=operator_content,
        operator_memory=operator_memory,
    ):
        return None

    pending_documents = _pending_document_records(snapshot=snapshot)
    if not pending_documents:
        return "I do not see any source documents still awaiting review in this close run."

    labels = [
        label
        for record in pending_documents
        if (label := _format_skipped_document_label(record=record)) is not None
    ]
    if not labels:
        return (
            "I can see skipped source documents, but they do not have filenames in "
            "the current snapshot."
        )
    if len(labels) == 1:
        return f"The skipped document is {labels[0]}."
    if len(labels) <= 3:
        return f"The skipped documents are {_join_choice_labels(labels)}."
    return (
        "The skipped documents are "
        f"{', '.join(labels[:3])}, and {len(labels) - 3} more."
    )


def _is_document_skip_follow_up_request(
    *,
    operator_content: str,
    operator_memory: AgentMemorySummary | None,
) -> bool:
    """Return whether a short follow-up is asking about skipped document review targets."""

    normalized = _searchable_text(operator_content)
    if not normalized:
        return False
    asks_about_skips = any(token in normalized for token in ("skip", "skipped", "hold", "held"))
    asks_for_identity = any(
        phrase in normalized
        for phrase in (
            "which",
            "what",
            "show",
            "tell me",
            "list",
            "name",
        )
    )
    if not asks_about_skips or not asks_for_identity:
        return False

    if any(token in normalized for token in ("document", "documents", "invoice", "file", "files")):
        return True

    if operator_memory is None:
        return False
    memory_text = _searchable_text(
        " ".join(
            value
            for value in (
                operator_memory.last_operator_message,
                operator_memory.last_assistant_response,
                operator_memory.working_subtask,
                operator_memory.approved_objective,
            )
            if isinstance(value, str)
        )
    )
    return (
        operator_memory.last_tool_name in {"review_document", "review_documents"}
        or "document" in memory_text
        or "documents" in memory_text
        or "invoice" in memory_text
    )


def _format_skipped_document_label(*, record: dict[str, Any]) -> str | None:
    """Return a compact label for one document left out of a review batch."""

    filename = record.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        return None
    status = _format_document_status_label(str(record.get("status") or "unknown"))
    open_issues = record.get("open_issues")
    issue_count = len(open_issues) if isinstance(open_issues, list) else 0
    if issue_count > 0:
        return (
            f"{filename.strip()} ({status}, {issue_count} open issue"
            f"{'' if issue_count == 1 else 's'})"
        )
    return f"{filename.strip()} ({status})"


def _build_document_upload_status_response(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary | None = None,
) -> str | None:
    """Answer document/upload follow-ups from the live close-run snapshot."""

    del operator_memory
    if not _is_document_upload_status_request(operator_content):
        return None
    if snapshot.get("close_run_id") is None:
        return (
            "This chat is not pinned to a close run, so I cannot see close-run source "
            "documents here yet. Open or create the close run first, then upload the "
            "document into that run."
        )

    documents = snapshot.get("documents")
    if not isinstance(documents, list):
        return None
    records = [record for record in documents if isinstance(record, dict)]
    if not records:
        return (
            "I do not see any source documents attached to this close run yet. If you just "
            "uploaded one, it has not reached the close-run snapshot I can see from this "
            "thread."
        )

    counts = _document_status_counts(records=records)
    count_text = _format_document_count_summary(counts=counts)
    details = [
        detail
        for record in records[:8]
        if (detail := _format_uploaded_document_detail(record=record)) is not None
    ]
    detail_text = " ".join(details)
    next_text = _document_upload_next_step(counts=counts)
    if detail_text and next_text:
        return f"I can see {count_text}. {detail_text} {next_text}"
    if detail_text:
        return f"I can see {count_text}. {detail_text}"
    if next_text:
        return f"I can see {count_text}. {next_text}"
    return f"I can see {count_text}."


def _is_document_upload_status_request(value: str) -> bool:
    """Return whether the operator is asking about uploaded close-run documents."""

    normalized = _searchable_text(value)
    if not normalized:
        return False
    if any(token in normalized for token in ("coa", "chart of accounts")):
        return False
    if normalized in {"here", "attached here", "uploaded here", "i uploaded it here"}:
        return True
    if any(
        phrase in normalized
        for phrase in (
            "already made an upload",
            "already uploaded",
            "i made an upload",
            "i uploaded",
            "the upload",
            "tell me about the upload",
            "this me about the upload",
            "what about the upload",
            "did the upload",
            "do you see the upload",
            "source document",
            "source documents",
            "uploaded document",
            "uploaded documents",
            "attached document",
            "attached documents",
            "uploaded file",
            "uploaded files",
            "attached file",
            "attached files",
            "all them parsed",
            "all parsed",
            "are they parsed",
            "are all parsed",
            "are all of them parsed",
            "is parsing done",
            "parsing done",
            "parsing finished",
            "extraction done",
            "extraction finished",
            "tell me about the contents",
            "tell me about contents",
            "content of this document",
            "content of these documents",
            "contents of this document",
            "contents of these documents",
            "what is inside the document",
            "what is inside these documents",
            "what's inside the document",
            "what's inside these documents",
        )
    ):
        return True
    return "upload" in normalized and any(
        token in normalized for token in ("document", "file", "source", "already", "made")
    )


def _document_status_counts(*, records: list[dict[str, Any]]) -> dict[str, int]:
    """Count document statuses from a snapshot document list."""

    counts: dict[str, int] = {}
    for record in records:
        status = str(record.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _format_document_count_summary(*, counts: dict[str, int]) -> str:
    """Render a compact document-count summary."""

    total = sum(counts.values())
    parts = [
        f"{count} {_format_document_status_label(status)}"
        for status, count in sorted(counts.items())
        if count > 0
    ]
    if not parts:
        return f"{total} source document{'' if total == 1 else 's'}"
    return (
        f"{total} source document{'' if total == 1 else 's'} "
        f"({_join_choice_labels(parts)})"
    )


def _format_document_status_label(status: str) -> str:
    """Return a plain label for one document status."""

    labels = {
        "uploaded": "uploaded",
        "processing": "processing",
        "parsed": "parsed",
        "needs_review": "awaiting review",
        "approved": "approved",
        "rejected": "rejected",
        "ignored": "ignored",
    }
    return labels.get(status, status.replace("_", " "))


def _format_uploaded_document_detail(*, record: dict[str, Any]) -> str | None:
    """Summarize one uploaded document with parsed fields and issues when available."""

    filename = record.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        return None
    status = _format_document_status_label(str(record.get("status") or "unknown"))
    document_type = str(record.get("document_type") or "").replace("_", " ").strip()
    prefix = f"{filename.strip()} is {status}"
    if document_type:
        prefix = f"{prefix} as {document_type}"
    open_issues = record.get("open_issues")
    issue_count = len(open_issues) if isinstance(open_issues, list) else 0
    if issue_count:
        prefix = f"{prefix} with {issue_count} open issue{'' if issue_count == 1 else 's'}"
    fields = record.get("fields")
    field_parts: list[str] = []
    if isinstance(fields, list):
        for field in fields[:3]:
            if not isinstance(field, dict):
                continue
            name = str(field.get("field_name") or "").replace("_", " ").strip()
            value = str(field.get("value") or "").strip()
            if name and value:
                field_parts.append(f"{name}: {value}")
    if field_parts:
        return f"{prefix}; parsed fields include {', '.join(field_parts)}."
    return f"{prefix}."


def _document_upload_next_step(*, counts: dict[str, int]) -> str | None:
    """Return the next document-specific action from upload state."""

    if counts.get("uploaded", 0) > 0 or counts.get("processing", 0) > 0:
        return "Parsing is still in progress, so the next step is to wait for extraction to finish."
    if counts.get("needs_review", 0) > 0 or counts.get("parsed", 0) > 0:
        return "The next step is document review; I can review or approve the clean ones from chat."
    if counts.get("approved", 0) > 0:
        return "Approved documents are ready for recommendation generation or the next close step."
    return None


def _safe_text(value: object) -> str | None:
    """Return a stripped string when the value is meaningful text."""

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _build_financial_report_analysis_response(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary | None = None,
) -> str | None:
    """Answer management-analysis questions from the latest grounded report state."""

    del operator_memory
    normalized_content = _searchable_text(operator_content)
    if not _is_financial_report_analysis_request(normalized_content):
        return None

    target = _resolve_close_run_detail_target(
        snapshot=snapshot,
        normalized_content=normalized_content,
    )
    if target is None:
        return None

    workspace_name, record = target
    commentary = [item for item in record.get("commentary", []) if isinstance(item, dict)]
    report_runs = [item for item in record.get("report_runs", []) if isinstance(item, dict)]
    period_label = _safe_text(record.get("period_label")) or "the selected period"
    if not commentary and not report_runs:
        return (
            f"I do not see generated report commentary for {workspace_name} {period_label} yet, "
            "so I cannot give a grounded view on growth or management recommendations "
            "from the report."
        )

    growth_assessment = _build_growth_assessment(commentary=commentary)
    report_signals = _build_report_signal_summary(commentary=commentary)
    recommendations = _build_management_recommendations(commentary=commentary)
    response = (
        f"For {workspace_name} {period_label}, I would read the report this way: "
        f"{growth_assessment}"
    )
    if report_signals is not None:
        response += f" The main report signals are {report_signals}."
    if recommendations:
        response += f" My recommendations: {'; '.join(recommendations)}."
    return response


def _is_financial_report_analysis_request(normalized_content: str) -> bool:
    """Return whether the operator wants business interpretation, not workflow generation."""

    if any(
        phrase in normalized_content
        for phrase in (
            "generate recommendations",
            "generate accounting recommendations",
            "queue recommendations",
            "queue recommendation",
            "start recommendations",
            "start recommendation",
            "approve recommendation",
            "reject recommendation",
        )
    ):
        return False

    asks_for_business_recommendations = any(
        token in normalized_content
        for token in ("recommendation", "recommendations", "recomendation", "recomendations")
    ) and any(
        token in normalized_content
        for token in (
            "company",
            "business",
            "financial report",
            "finanical report",
            "after seeing",
            "after reading",
            "growth",
            "growing",
        )
    )
    asks_for_growth = any(
        token in normalized_content
        for token in ("are they growing", "is it growing", "growth", "growing", "grow or not")
    ) and any(
        token in normalized_content
        for token in ("company", "business", "report", "financial", "finanical")
    )
    return asks_for_business_recommendations or asks_for_growth


def _build_growth_assessment(*, commentary: list[dict[str, Any]]) -> str:
    """Return a grounded growth assessment from available report commentary."""

    combined = _searchable_text(" ".join(_commentary_body_previews(commentary)))
    if any(
        phrase in combined
        for phrase in (
            "no significant period over period",
            "no significant period period",
            "stable",
            "no significant changes",
        )
    ):
        return (
            "the company looks profitable and operationally stable, but I would not call it "
            "clearly growing from this report alone because the available commentary does not "
            "show a strong period-over-period increase."
        )
    if any(
        token in combined
        for token in ("growth", "increased", "increase", "higher", "expanded")
    ):
        return (
            "there are growth signals in the commentary, but I would still validate them against "
            "prior-period revenue, margin, and cash-flow trends before calling the growth durable."
        )
    return (
        "the report supports a profitability/cash-generation view, but it does not by itself "
        "prove the company is growing because I do not see enough prior-period comparison data."
    )


def _build_report_signal_summary(*, commentary: list[dict[str, Any]]) -> str | None:
    """Return short section-grounded signals from report commentary previews."""

    signals = []
    for record in commentary[:4]:
        section_key = record.get("section_key")
        body_preview = _safe_text(record.get("body_preview"))
        if section_key is None or body_preview is None:
            continue
        signals.append(
            f"{_format_report_section_label(str(section_key))}: "
            f"{_truncate_operator_text(body_preview, limit=220)}"
        )
    return " | ".join(signals) if signals else None


def _build_management_recommendations(*, commentary: list[dict[str, Any]]) -> list[str]:
    """Return practical management recommendations from report commentary."""

    combined = _searchable_text(" ".join(_commentary_body_previews(commentary)))
    recommendations = []
    if any(token in combined for token in ("unexplained difference", "difference")):
        recommendations.append(
            "investigate and clear the balance-sheet difference before relying on the "
            "pack for board decisions"
        )
    if any(token in combined for token in ("net profit", "net margin", "gross margin")):
        recommendations.append(
            "validate revenue quality and margin drivers so the strong profit result is repeatable"
        )
    if any(token in combined for token in ("cash flow", "operating activities", "cash")):
        recommendations.append(
            "protect operating cash flow and review large investing outflows against "
            "the growth plan"
        )
    recommendations.append(
        "add prior-month comparatives and revenue-by-stream trend analysis to confirm "
        "whether the company is actually growing"
    )
    return recommendations


def _commentary_body_previews(commentary: list[dict[str, Any]]) -> list[str]:
    """Return commentary preview text from report snapshot records."""

    previews = []
    for record in commentary:
        body_preview = _safe_text(record.get("body_preview"))
        if body_preview is not None:
            previews.append(body_preview)
    return previews


def _truncate_operator_text(value: str, *, limit: int) -> str:
    """Return compact text for one conversational report signal."""

    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "..."


def _build_close_run_detail_status_response(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary | None = None,
) -> str | None:
    """Answer close-run detail and report questions from the grounded snapshot."""

    del operator_memory
    normalized_content = _searchable_text(operator_content)
    if not _is_close_run_detail_request(normalized_content):
        return None

    target = _resolve_close_run_detail_target(
        snapshot=snapshot,
        normalized_content=normalized_content,
    )
    if target is None:
        return None

    workspace_name, record = target
    period_label = _safe_text(record.get("period_label")) or "the selected period"
    status = (_safe_text(record.get("status")) or "unknown").replace("_", " ")
    active_phase = _safe_text(record.get("active_phase"))
    phase_text = (
        f" Its active phase is {_format_phase_label(active_phase)}."
        if active_phase is not None
        else " It has no active phase, which usually means the run is complete."
    )
    details = (
        f"{workspace_name}: {period_label} is {status}. "
        f"Close Run ID: {record.get('id')}. "
        f"Period: {_safe_text(record.get('period_start')) or 'unknown'} to "
        f"{_safe_text(record.get('period_end')) or 'unknown'}. "
        f"Reporting currency: {_safe_text(record.get('reporting_currency')) or 'unknown'}. "
        f"Version: {record.get('version_no') or 'unknown'}."
        f"{phase_text}"
    )
    if "report" not in normalized_content and "reports" not in normalized_content:
        return details
    return f"{details} {_describe_close_run_report_state(record=record)}"


def _is_close_run_detail_request(normalized_content: str) -> bool:
    """Return whether the operator is asking to inspect a close run, not mutate it."""

    if not any(
        token in normalized_content
        for token in ("close run", "closed run", "close", "run", "report", "reports")
    ):
        return False
    return any(
        phrase in normalized_content
        for phrase in (
            "tell me more",
            "more detail",
            "more details",
            "get me more",
            "details of",
            "detail of",
            "tell me the report",
            "tell me the reports",
            "what report",
            "what reports",
            "which report",
            "which reports",
        )
    )


def _resolve_close_run_detail_target(
    *,
    snapshot: dict[str, Any],
    normalized_content: str,
) -> tuple[str, dict[str, Any]] | None:
    """Resolve one close-run detail target from current and accessible workspace rows."""

    candidates = _close_run_detail_candidates(snapshot=snapshot)
    if not candidates:
        return None

    matched_candidates = [
        candidate
        for candidate in candidates
        if _close_run_detail_candidate_matches(
            candidate=candidate,
            normalized_content=normalized_content,
        )
    ]
    if len(matched_candidates) == 1:
        return matched_candidates[0]

    if "approved" in normalized_content:
        approved_candidates = [
            candidate
            for candidate in candidates
            if str(candidate[1].get("status") or "") == "approved"
        ]
        if len(approved_candidates) == 1:
            return approved_candidates[0]

    return candidates[0] if len(candidates) == 1 else None


def _close_run_detail_candidates(
    *,
    snapshot: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """Return close-run records paired with their workspace name."""

    candidates: list[tuple[str, dict[str, Any]]] = []
    workspace_rows = snapshot.get("accessible_workspace_close_runs")
    if isinstance(workspace_rows, list):
        for row in workspace_rows:
            if not isinstance(row, dict):
                continue
            workspace = row.get("workspace")
            if not isinstance(workspace, dict):
                continue
            workspace_name = _safe_text(workspace.get("name")) or "Workspace"
            close_runs = row.get("close_runs")
            if not isinstance(close_runs, list):
                continue
            for record in close_runs:
                if isinstance(record, dict):
                    candidates.append((workspace_name, record))

    if candidates:
        return candidates

    workspace = snapshot.get("workspace")
    workspace_name = (
        _safe_text(workspace.get("name"))
        if isinstance(workspace, dict)
        else None
    ) or "This workspace"
    close_runs = snapshot.get("entity_close_runs")
    if isinstance(close_runs, list):
        for record in close_runs:
            if isinstance(record, dict):
                candidates.append((workspace_name, record))
    return candidates


def _close_run_detail_candidate_matches(
    *,
    candidate: tuple[str, dict[str, Any]],
    normalized_content: str,
) -> bool:
    """Return whether one close-run candidate is explicitly referenced."""

    workspace_name, record = candidate
    record_id = record.get("id")
    if record_id is not None and str(record_id).lower() in normalized_content:
        return True
    period_label = _safe_text(record.get("period_label"))
    if period_label is not None and _text_value_matches_text(period_label, normalized_content):
        return True
    status = _safe_text(record.get("status"))
    if status is not None and status in normalized_content:
        return True
    if _workspace_name_matches_text(workspace_name, normalized_content):
        return True
    return _workspace_name_keyword_matches_text(workspace_name, normalized_content)


def _describe_close_run_report_state(*, record: dict[str, Any]) -> str:
    """Return a compact description of report, commentary, and export state."""

    report_runs = [item for item in record.get("report_runs", []) if isinstance(item, dict)]
    commentary = [item for item in record.get("commentary", []) if isinstance(item, dict)]
    exports = [item for item in record.get("exports", []) if isinstance(item, dict)]

    if not report_runs:
        return "I do not see a generated report run for this close run yet."

    report_labels = []
    for run in report_runs[:3]:
        version_no = run.get("version_no")
        status = str(run.get("status") or "unknown").replace("_", " ")
        artifact_count = run.get("artifact_count")
        artifact_text = (
            f", {artifact_count} artifact{'s' if artifact_count != 1 else ''}"
            if isinstance(artifact_count, int)
            else ""
        )
        report_labels.append(f"v{version_no or '?'} ({status}{artifact_text})")

    response = f"Reports: {_join_choice_labels(report_labels)}."
    if commentary:
        commentary_statuses = _count_record_values(commentary, key="status")
        section_labels = [
            _format_report_section_label(str(item.get("section_key")))
            for item in commentary[:5]
            if item.get("section_key") is not None
        ]
        response += (
            f" Commentary: {json.dumps(commentary_statuses, sort_keys=True)}"
            f" across {_join_choice_labels(section_labels)}."
        )
    if exports:
        export_labels = [
            f"v{item.get('version_no') or '?'} ({item.get('status') or 'unknown'})"
            for item in exports[:3]
        ]
        response += f" Exports: {_join_choice_labels(export_labels)}."
    return response


def _count_record_values(records: list[dict[str, Any]], *, key: str) -> dict[str, int]:
    """Count one record key into a deterministic summary map."""

    counts: dict[str, int] = {}
    for record in records:
        value = record.get(key)
        if value is None:
            continue
        text = str(value)
        counts[text] = counts.get(text, 0) + 1
    return {text: counts[text] for text in sorted(counts)}


def _build_all_workspace_close_run_directory_response(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary | None = None,
) -> str | None:
    """Answer cross-workspace close-run listing questions from the grounded snapshot."""

    del operator_memory
    normalized_content = _searchable_text(operator_content)
    if "close run" not in normalized_content and "close runs" not in normalized_content:
        return None
    workspace_rows = snapshot.get("accessible_workspace_close_runs")
    if not isinstance(workspace_rows, list):
        return None

    named_workspace_rows = [
        row
        for row in workspace_rows
        if isinstance(row, dict)
        and _workspace_close_run_row_matches_text(row=row, normalized_text=normalized_content)
    ]
    asks_across_workspaces = any(
        phrase in normalized_content
        for phrase in (
            "across my workspaces",
            "across all workspaces",
            "all workspaces",
            "my workspaces",
            "each workspace",
            "both workspaces",
        )
    )
    if not asks_across_workspaces and len(named_workspace_rows) != 1:
        return None

    rows_to_render = workspace_rows if asks_across_workspaces else named_workspace_rows

    summaries: list[str] = []
    empty_workspaces: list[str] = []
    for row in rows_to_render[:8]:
        if not isinstance(row, dict):
            continue
        workspace = row.get("workspace")
        if not isinstance(workspace, dict):
            continue
        workspace_name = workspace.get("name")
        if not isinstance(workspace_name, str) or not workspace_name.strip():
            continue
        close_runs = row.get("close_runs")
        records = (
            [record for record in close_runs if isinstance(record, dict)]
            if isinstance(close_runs, list)
            else []
        )
        if not records:
            empty_workspaces.append(workspace_name.strip())
            continue
        labels = []
        for record in records[:3]:
            label = _describe_close_run_summary(record=record)
            if label is not None:
                labels.append(label)
        if labels:
            summaries.append(f"{workspace_name.strip()}: {_join_choice_labels(labels)}")

    if not summaries and not empty_workspaces:
        return None
    if not summaries:
        if len(empty_workspaces) == 1:
            return f"There are no close runs recorded for {empty_workspaces[0]}."
        return "There are no close runs in your accessible workspaces yet."

    response = (
        "Across your workspaces I can see " + "; ".join(summaries) + "."
        if asks_across_workspaces
        else "I can see " + "; ".join(summaries) + "."
    )
    if empty_workspaces:
        response += f" No close runs are recorded for {_join_choice_labels(empty_workspaces[:3])}."
    return response


def _workspace_close_run_row_matches_text(*, row: dict[str, Any], normalized_text: str) -> bool:
    """Return whether a cross-workspace close-run row is explicitly named by the operator."""

    workspace = row.get("workspace")
    if not isinstance(workspace, dict):
        return False
    workspace_name = workspace.get("name")
    if isinstance(workspace_name, str) and _workspace_name_matches_text(
        workspace_name,
        normalized_text,
    ):
        return True
    legal_name = workspace.get("legal_name")
    return isinstance(legal_name, str) and _workspace_name_matches_text(
        legal_name,
        normalized_text,
    )


def _build_close_run_directory_response(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary | None = None,
) -> str | None:
    """Answer direct close-run listing questions from the entity snapshot."""

    del operator_memory
    normalized_content = _searchable_text(operator_content)
    if not any(
        phrase in normalized_content
        for phrase in (
            "show me the active close runs",
            "show active close runs",
            "what close runs are open",
            "which close runs are open",
            "what close runs exist",
            "which close runs exist",
            "list the close runs",
            "show the close runs",
        )
    ):
        return None

    close_runs = snapshot.get("entity_close_runs")
    if not isinstance(close_runs, list):
        return None

    records = [record for record in close_runs if isinstance(record, dict)]
    if not records:
        return "There are no close runs in this workspace yet."

    active_like = [
        record
        for record in records
        if str(record.get("status") or "") not in {"archived", "deleted"}
    ]
    display_records = active_like or records
    labels = []
    for record in display_records[:3]:
        label = _describe_close_run_summary(record=record)
        if label is not None:
            labels.append(label)
    if not labels:
        return "I can see close runs in this workspace, but I need one more specific question."

    return (
        "In this workspace I can see "
        f"{_join_choice_labels(labels)}."
    )


def _build_workspace_scope_status_response(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary | None = None,
) -> str | None:
    """Answer direct current-workspace questions without routing through a mutation tool."""

    del operator_memory
    normalized_content = _searchable_text(operator_content)
    if not any(
        phrase in normalized_content
        for phrase in (
            "which workspace are you currently on",
            "what workspace are you currently on",
            "which workspace is this",
            "what workspace is this",
            "which workspace am i in",
            "what workspace am i in",
            "which workspace is this chat on",
            "what workspace is this chat on",
            "tell me the current workspace",
            "what is the current workspace",
        )
    ):
        return None

    workspace = snapshot.get("workspace")
    if not isinstance(workspace, dict):
        return "This chat is currently anchored to the active workspace."

    workspace_name = workspace.get("name")
    if not isinstance(workspace_name, str) or not workspace_name.strip():
        return "This chat is currently anchored to the active workspace."

    if snapshot.get("close_run_id") is not None:
        return (
            f"This chat is currently anchored to {workspace_name}. "
            "It is also pinned to a close run in that workspace."
        )
    return f"This chat is currently anchored to {workspace_name}."


def _build_cross_domain_ambiguity_clarification(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary,
) -> str | None:
    """Ask one short clarification when a generic request matches multiple tool domains."""

    intent = _classify_cross_domain_operator_intent(operator_content=operator_content)
    if intent is None:
        return None
    if _operator_memory_can_disambiguate_intent(
        operator_memory=operator_memory,
        snapshot=snapshot,
        intent=intent,
    ):
        return None

    candidate_labels = _build_cross_domain_candidate_labels(
        snapshot=snapshot,
        intent=intent,
    )
    if len(candidate_labels) <= 1:
        return None

    verb = {
        "approve": "approve",
        "reject": "reject",
        "apply": "apply",
        "resolve": "resolve",
        "ignore": "ignore",
    }.get(intent, "handle")
    return f"I can {verb} {_join_choice_labels(candidate_labels)}. Which one do you want?"


def _classify_cross_domain_operator_intent(*, operator_content: str) -> str | None:
    """Return the ambiguous pronoun-heavy intent that still needs domain selection."""

    normalized_content = _searchable_text(operator_content)
    explicit_nouns = (
        "document",
        "recommendation",
        "journal",
        "commentary",
        "reconciliation",
        "anomaly",
        "exception",
        "workspace",
        "close run",
        "close-run",
        "export",
        "report",
    )
    if any(noun in normalized_content for noun in explicit_nouns):
        return None
    if any(
        phrase in normalized_content
        for phrase in ("approve it", "approve that", "approve this")
    ):
        return "approve"
    if any(
        phrase in normalized_content
        for phrase in ("reject it", "reject that", "reject this")
    ):
        return "reject"
    if any(
        phrase in normalized_content
        for phrase in ("apply it", "apply that", "post it", "post that")
    ):
        return "apply"
    if any(
        phrase in normalized_content
        for phrase in ("resolve it", "resolve that", "fix it", "clear it")
    ):
        return "resolve"
    if any(
        phrase in normalized_content
        for phrase in ("ignore it", "ignore that", "remove it")
    ):
        return "ignore"
    return None


def _build_cross_domain_candidate_labels(
    *,
    snapshot: dict[str, Any],
    intent: str,
) -> list[str]:
    """Return domain-level candidate labels for an ambiguous operator verb."""

    labels: list[str] = []
    if intent in {"approve", "reject", "ignore"}:
        document_label = _document_domain_candidate_label(
            snapshot=snapshot,
            intent=intent,
        )
        if document_label is not None:
            labels.append(document_label)
    if intent in {"approve", "reject"}:
        recommendation_label = _recommendation_domain_candidate_label(
            snapshot=snapshot,
            intent=intent,
        )
        if recommendation_label is not None:
            labels.append(recommendation_label)
    if intent in {"approve", "reject", "apply"}:
        journal_label = _journal_domain_candidate_label(
            snapshot=snapshot,
            intent=intent,
        )
        if journal_label is not None:
            labels.append(journal_label)
    if intent == "approve":
        reconciliation_label = _reconciliation_domain_candidate_label(snapshot=snapshot)
        if reconciliation_label is not None:
            labels.append(reconciliation_label)
        commentary_label = _commentary_domain_candidate_label(snapshot=snapshot)
        if commentary_label is not None:
            labels.append(commentary_label)
    if intent == "resolve":
        item_label = _reconciliation_item_domain_candidate_label(snapshot=snapshot)
        if item_label is not None:
            labels.append(item_label)
        anomaly_label = _reconciliation_anomaly_domain_candidate_label(snapshot=snapshot)
        if anomaly_label is not None:
            labels.append(anomaly_label)
    return labels


def _build_create_workspace_intent_planning(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary,
) -> AgentPlanningResult | None:
    """Plan workspace creation deterministically when the operator intent is explicit."""

    is_create_request = _is_workspace_creation_request(operator_content)
    is_name_follow_up = _memory_indicates_pending_workspace_creation(
        operator_memory=operator_memory,
    ) and _is_workspace_creation_follow_up_candidate(operator_content)
    if not is_create_request and not is_name_follow_up:
        return None

    pending_workspace_name = _resolve_pending_workspace_name_from_memory(
        operator_memory=operator_memory,
    )
    workspace_name = (
        _extract_workspace_name_from_creation_text(operator_content)
        or pending_workspace_name
    )
    if workspace_name is None:
        return AgentPlanningResult(
            mode="read_only",
            assistant_response="What would you like to name the new workspace?",
            reasoning=(
                "The operator clearly asked to create a workspace, but the required "
                "workspace name is not present."
            ),
            tool_name=None,
            tool_arguments={},
        )

    if _workspace_name_exists_in_snapshot(snapshot=snapshot, workspace_name=workspace_name):
        return AgentPlanningResult(
            mode="read_only",
            assistant_response=(
                f"A workspace named {workspace_name} already exists. Give me a distinct "
                "workspace name for the new entity."
            ),
            reasoning=(
                "The requested workspace display name already exists in the operator's "
                "accessible workspace list."
            ),
            tool_name=None,
            tool_arguments={},
        )

    legal_name = _extract_workspace_legal_name_from_creation_text(operator_content)
    if legal_name is None and pending_workspace_name is not None and not is_create_request:
        legal_name = _clean_extracted_workspace_name(operator_content)
    if legal_name is None:
        return AgentPlanningResult(
            mode="read_only",
            assistant_response=(
                f"What is the legal entity name for {workspace_name}? I'll use the current "
                "workspace defaults for currency, country, timezone, and approval routing unless "
                "you specify different values."
            ),
            reasoning=(
                "The operator supplied the display name, but chat-created workspaces require "
                "a legal entity name before mutation."
            ),
            tool_name=None,
            tool_arguments={},
        )

    tool_arguments = _hydrate_create_workspace_arguments(
        tool_arguments={"name": workspace_name, "legal_name": legal_name},
        snapshot=snapshot,
    )
    return AgentPlanningResult(
        mode="tool",
        assistant_response=(
            f"I'll create the {workspace_name} workspace now using the current workspace defaults."
        ),
        reasoning=(
            "The operator supplied the required workspace name; optional settings can use "
            "the canonical current-scope defaults."
        ),
        tool_name="create_workspace",
        tool_arguments=tool_arguments,
    )


def _is_workspace_creation_request(value: str) -> bool:
    """Return whether the operator is asking to create an entity workspace."""

    normalized = _searchable_text(value)
    if not normalized:
        return False
    if "close run" in normalized or "close-run" in normalized:
        return False
    if "workspace" not in normalized and "entity" not in normalized:
        return False
    creation_tokens = ("create", "add", "new", "another", "fresh")
    return any(token in normalized for token in creation_tokens)


def _memory_indicates_pending_workspace_creation(
    *,
    operator_memory: AgentMemorySummary,
) -> bool:
    """Return whether recent turns show an unfinished create-workspace request."""

    candidates = [
        operator_memory.last_operator_message,
        operator_memory.last_assistant_response,
        operator_memory.working_subtask,
        operator_memory.approved_objective,
        operator_memory.pending_branch,
        *operator_memory.recent_objectives,
    ]
    for value in candidates:
        normalized = _searchable_text(value)
        if not normalized:
            continue
        if "workspace" not in normalized and "entity" not in normalized:
            continue
        if any(token in normalized for token in ("create", "add", "new")):
            return True
        if "what would you like to name" in normalized or "workspace name" in normalized:
            return True
        if "legal entity name" in normalized:
            return True
    return False


def _is_workspace_creation_follow_up_candidate(value: str) -> bool:
    """Return whether a message plausibly supplies a missing workspace name/legal name."""

    normalized = _searchable_text(value)
    if not normalized:
        return False
    if _is_document_upload_status_request(value):
        return False
    if normalized in {"here", "done", "ok", "okay", "yes", "no", "uploaded"}:
        return False
    if any(
        phrase in normalized
        for phrase in (
            "already uploaded",
            "already made",
            "upload",
            "uploaded",
            "document",
            "source file",
            "close run",
            "close-run",
            "recommendation",
            "journal",
            "reconciliation",
            "report",
            "export",
            "next step",
            "what next",
        )
    ):
        return False
    has_question = "?" in value
    if has_question and not any(token in normalized for token in ("name", "legal")):
        return False
    token_count = len(normalized.split())
    if token_count > 12 and not any(token in normalized for token in ("name", "legal")):
        return False
    return True


def _resolve_pending_workspace_name_from_memory(
    *,
    operator_memory: AgentMemorySummary,
) -> str | None:
    """Return the workspace display name from a recent unfinished creation turn."""

    candidates = [
        operator_memory.last_operator_message,
        *reversed(operator_memory.recent_objectives),
    ]
    for value in candidates:
        if not isinstance(value, str):
            continue
        workspace_name = _extract_workspace_name_from_creation_text(value)
        if workspace_name is not None:
            return workspace_name
    return None


def _extract_workspace_name_from_creation_text(value: str) -> str | None:
    """Extract a workspace display name from common create-workspace phrasing."""

    raw_value = value.strip()
    if not raw_value:
        return None

    patterns = (
        r"\b(?:called|named)\s+(?P<name>.+)$",
        r"\b(?:the\s+)?(?:workspace|entity)?\s*name\s+(?:is|as|to|would\s+be|will\s+be|should\s+be)\s+(?P<name>.+)$",
        r"^\s*(?P<name>.+?)\s+(?:would|will|should)\s+be\s+the\s+name\b",
        r"\b(?:create|add|open|start)\s+(?:a\s+|an\s+|another\s+|new\s+|fresh\s+|the\s+)*workspace\s+(?P<name>.+)$",
        r"\bnew\s+workspace\s+(?P<name>.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, raw_value, flags=re.IGNORECASE)
        if match is None:
            continue
        cleaned = _clean_extracted_workspace_name(match.group("name"))
        if cleaned is not None:
            return cleaned
    return None


def _extract_workspace_legal_name_from_creation_text(value: str) -> str | None:
    """Extract an optional legal entity name from create-workspace phrasing."""

    patterns = (
        r"\blegal\s+(?:entity\s+)?name\s+(?:is|as|to|would\s+be|will\s+be|should\s+be)\s+(?P<name>.+)$",
        r"\blegal\s+(?:entity\s+)?name[:\s]+(?P<name>.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, value.strip(), flags=re.IGNORECASE)
        if match is None:
            continue
        cleaned = _clean_extracted_workspace_name(match.group("name"))
        if cleaned is not None:
            return cleaned
    return None


def _workspace_name_exists_in_snapshot(
    *,
    snapshot: dict[str, Any],
    workspace_name: str,
) -> bool:
    """Return whether the operator already has an accessible workspace with this name."""

    normalized_name = _searchable_text(workspace_name)
    if not normalized_name:
        return False
    workspaces = snapshot.get("accessible_workspaces")
    records = (
        [record for record in workspaces if isinstance(record, dict)]
        if isinstance(workspaces, list)
        else []
    )
    current_workspace = snapshot.get("workspace")
    if isinstance(current_workspace, dict):
        records.append(current_workspace)
    for record in records:
        name = record.get("name")
        if isinstance(name, str) and _searchable_text(name) == normalized_name:
            return True
    return False


def _clean_extracted_workspace_name(value: str) -> str | None:
    """Return one safe workspace display name extracted from operator text."""

    cleaned = value.strip()
    cleaned = re.split(
        (
            r"\b(?:with\s+legal\s+(?:entity\s+)?name|legal\s+(?:entity\s+)?name|"
            r"any\s+other\s+details|anything\s+else|"
            r"do\s+you\s+need|should\s+i\s+provide)\b"
        ),
        cleaned,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    cleaned = re.sub(r"\b(?:please|thanks|thank\s+you)\b\.?$", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" \t\r\n\"'`.,;:!?")
    if not cleaned:
        return None
    normalized = _searchable_text(cleaned)
    if normalized in {
        "a workspace",
        "new workspace",
        "a new workspace",
        "the workspace",
        "workspace",
        "entity",
    }:
        return None
    if len(cleaned) > 200:
        return None
    return cleaned


def _build_create_close_run_intent_planning(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary,
) -> AgentPlanningResult | None:
    """Plan close-run creation deterministically when the operator intent is explicit."""

    if not _is_close_run_creation_request(operator_content):
        return None

    workspace_id = _resolve_workspace_id_for_create_close_run(
        snapshot=snapshot,
        operator_content=operator_content,
        operator_memory=operator_memory,
    )
    workspace_label = _resolve_workspace_label(snapshot=snapshot, workspace_id=workspace_id)
    period = _infer_close_run_period_from_text(operator_content)
    if period is None:
        assistant_response = (
            f"Which period should I open for {workspace_label}? "
            "Give me the month or the exact start and end date."
            if workspace_label is not None
            else (
                "Which period should I open? Give me the month or the exact start "
                "and end date."
            )
        )
        return AgentPlanningResult(
            mode="read_only",
            assistant_response=assistant_response,
            reasoning=(
                "The operator clearly asked to create a close run, but the required "
                "period is not present."
            ),
            tool_name=None,
            tool_arguments={},
        )

    tool_arguments: dict[str, Any] = {
        "period_start": period[0],
        "period_end": period[1],
    }
    if workspace_id is not None:
        tool_arguments["workspace_id"] = workspace_id
    return AgentPlanningResult(
        mode="tool",
        assistant_response=(
            f"I'll open that close run for {workspace_label} now."
            if workspace_label is not None
            else "I'll open that close run now."
        ),
        reasoning=(
            "The operator explicitly asked to create a close run and provided a "
            "resolvable period."
        ),
        tool_name="create_close_run",
        tool_arguments=tool_arguments,
    )


def _build_close_run_correction_delete_planning(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
) -> AgentPlanningResult | None:
    """Delete the current mistaken run before resolving a corrected target period."""

    normalized = _searchable_text(operator_content)
    if not any(token in normalized for token in ("delete", "remove", "cancel")):
        return None
    if not any(
        phrase in normalized
        for phrase in (
            "mistake",
            "wrong",
            "typo",
            "made a mistake",
            "i meant",
            "should be",
            "its ",
            "it s ",
        )
    ):
        return None
    current_close_run_id = snapshot.get("close_run_id")
    if not isinstance(current_close_run_id, str) or not current_close_run_id.strip():
        return None
    return AgentPlanningResult(
        mode="tool",
        assistant_response=(
            "I'll remove the mistaken close run first, then continue with the corrected "
            "period once that governed deletion is confirmed."
        ),
        reasoning=(
            "The operator asked to delete the current mistaken close run before moving to "
            "the corrected period."
        ),
        tool_name="delete_close_run",
        tool_arguments={"close_run_id": current_close_run_id},
    )


def _build_open_close_run_intent_planning(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
) -> AgentPlanningResult | None:
    """Plan an existing close-run scope switch when the operator asks to work there."""

    if _is_close_run_creation_request(operator_content):
        return None
    normalized = _searchable_text(operator_content)
    if any(
        phrase in normalized
        for phrase in (
            "reopen",
            "alter it after approval",
            "alter after approval",
            "change it after approval",
            "approved working version",
        )
    ):
        return None
    is_correction_target = (
        _infer_close_run_period_from_text(operator_content) is not None
        and any(
            phrase in normalized
            for phrase in (
                "mistake",
                "wrong",
                "typo",
                "i meant",
                "should be",
                "its ",
                "it s ",
            )
        )
    )
    if not is_correction_target and not any(
        phrase in normalized
        for phrase in (
            "work on ",
            "lets work on",
            "let s work on",
            "open ",
            "enter ",
            "pin ",
            "select ",
            "use ",
            "switch to ",
        )
    ):
        return None

    close_run_id = _resolve_close_run_id_from_snapshot(
        snapshot=snapshot,
        operator_content=operator_content,
        operator_memory=AgentMemorySummary(),
    )
    if close_run_id is None:
        return None
    if snapshot.get("close_run_id") == close_run_id:
        label = _resolve_close_run_label(snapshot=snapshot, close_run_id=close_run_id)
        return AgentPlanningResult(
            mode="read_only",
            assistant_response=(
                f"We're already working in the {label} close run."
                if label is not None
                else "We're already working in that close run."
            ),
            reasoning="The requested close run is already the active thread scope.",
            tool_name=None,
            tool_arguments={},
        )

    label = _resolve_close_run_label(snapshot=snapshot, close_run_id=close_run_id)
    return AgentPlanningResult(
        mode="tool",
        assistant_response=(
            f"I'll pin this thread to the {label} close run now."
            if label is not None
            else "I'll pin this thread to that close run now."
        ),
        reasoning=(
            "The operator asked to work on an existing close run, so the existing run "
            "should be opened instead of creating a duplicate."
        ),
        tool_name="open_close_run",
        tool_arguments={"close_run_id": close_run_id},
    )


def _is_close_run_creation_request(value: str) -> bool:
    """Return whether the operator is asking to create, not list, a close run."""

    normalized = _searchable_text(value)
    if "close run" not in normalized and "close runs" not in normalized:
        return False
    creation_phrases = (
        "create a close run",
        "create close run",
        "create new close run",
        "create a new close run",
        "start a close run",
        "start close run",
        "start new close run",
        "start a new close run",
        "open a new close run",
        "open new close run",
        "fresh close run",
        "new close run",
        "another close run",
    )
    return any(phrase in normalized for phrase in creation_phrases)


_MONTH_NUMBER_BY_NAME = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def _build_create_close_run_follow_up_arguments(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary,
) -> dict[str, Any] | None:
    """Return create-close-run arguments when a clarification reply supplies the period."""

    period = _infer_close_run_period_from_text(operator_content)
    if period is None:
        return None
    if not _memory_indicates_pending_close_run_creation(operator_memory=operator_memory):
        return None

    arguments = {
        "period_start": period[0],
        "period_end": period[1],
    }
    workspace_id = _resolve_workspace_id_for_create_close_run(
        snapshot=snapshot,
        operator_content=operator_content,
        operator_memory=operator_memory,
    )
    if workspace_id is not None:
        arguments["workspace_id"] = workspace_id
    return arguments


def _hydrate_open_close_run_arguments(
    *,
    tool_arguments: dict[str, Any],
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary,
) -> dict[str, Any]:
    """Fill the close-run identifier for existing-run open requests."""

    hydrated = dict(tool_arguments)
    if isinstance(hydrated.get("close_run_id"), str):
        return hydrated

    resolved_close_run_id = _resolve_close_run_id_from_snapshot(
        snapshot=snapshot,
        operator_content=operator_content,
        operator_memory=operator_memory,
    )
    if resolved_close_run_id is not None:
        hydrated["close_run_id"] = resolved_close_run_id
    return hydrated


def _hydrate_create_close_run_arguments(
    *,
    tool_arguments: dict[str, Any],
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary,
) -> dict[str, Any]:
    """Fill period and workspace targeting for close-run creation when unambiguous."""

    hydrated = dict(tool_arguments)
    if not isinstance(hydrated.get("workspace_id"), str):
        workspace_id = _resolve_workspace_id_for_create_close_run(
            snapshot=snapshot,
            operator_content=operator_content,
            operator_memory=operator_memory,
        )
        if workspace_id is not None:
            hydrated["workspace_id"] = workspace_id

    if not isinstance(hydrated.get("period_start"), str) or not isinstance(
        hydrated.get("period_end"),
        str,
    ):
        period = _infer_close_run_period_from_text(operator_content)
        if period is not None:
            hydrated["period_start"] = period[0]
            hydrated["period_end"] = period[1]

    return hydrated


def _memory_indicates_pending_close_run_creation(
    *,
    operator_memory: AgentMemorySummary,
) -> bool:
    """Return whether recent turns show an unfinished create-close-run request."""

    candidates = [
        operator_memory.last_operator_message,
        operator_memory.last_assistant_response,
        operator_memory.working_subtask,
        operator_memory.approved_objective,
        operator_memory.pending_branch,
        *operator_memory.recent_objectives,
    ]
    for value in candidates:
        normalized = _searchable_text(value)
        if not normalized:
            continue
        if "close run" not in normalized and "close" not in normalized:
            continue
        if any(token in normalized for token in ("create", "start", "open", "new", "fresh")):
            return True
        if "which period" in normalized or "for which period" in normalized:
            return True
    return False


def _resolve_workspace_id_for_create_close_run(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary,
) -> str | None:
    """Resolve the workspace target for close-run creation from text or compact memory."""

    explicit_workspace_id = _resolve_workspace_id_from_snapshot(
        snapshot=snapshot,
        operator_content=operator_content,
        operator_memory=operator_memory,
    )
    if explicit_workspace_id is not None:
        return explicit_workspace_id

    if operator_memory.last_target_type == "workspace":
        target_id = operator_memory.last_target_id
        if isinstance(target_id, str) and _snapshot_contains_target(
            snapshot=snapshot,
            target_type="workspace",
            target_id=target_id,
        ):
            return target_id

    for objective in reversed(operator_memory.recent_objectives):
        workspace_id = _resolve_workspace_id_from_snapshot(
            snapshot=snapshot,
            operator_content=objective,
            operator_memory=operator_memory,
        )
        if workspace_id is not None:
            return workspace_id

    workspace = snapshot.get("workspace")
    if isinstance(workspace, dict) and isinstance(workspace.get("id"), str):
        return str(workspace["id"])
    return None


def _infer_close_run_period_from_text(value: str) -> tuple[str, str] | None:
    """Infer one monthly close-run period from common operator phrasing."""

    normalized = _searchable_text(value)
    if not normalized:
        return None

    month_names = "|".join(sorted(_MONTH_NUMBER_BY_NAME, key=len, reverse=True))
    match = re.search(rf"\b({month_names})\s+(20\d{{2}})\b", normalized)
    if match is None:
        match = re.search(rf"\b(20\d{{2}})\s+({month_names})\b", normalized)
        if match is not None:
            year = int(match.group(1))
            month = _MONTH_NUMBER_BY_NAME[match.group(2)]
            return _month_period_iso(year=year, month=month)
    else:
        month = _MONTH_NUMBER_BY_NAME[match.group(1)]
        year = int(match.group(2))
        return _month_period_iso(year=year, month=month)

    today = utc_now().date()
    if any(phrase in normalized for phrase in ("this month", "current month")):
        return _month_period_iso(year=today.year, month=today.month)
    if "next month" in normalized:
        next_year = today.year + (1 if today.month == 12 else 0)
        next_month = 1 if today.month == 12 else today.month + 1
        return _month_period_iso(year=next_year, month=next_month)
    if "last month" in normalized or "previous month" in normalized:
        previous_year = today.year - (1 if today.month == 1 else 0)
        previous_month = 12 if today.month == 1 else today.month - 1
        return _month_period_iso(year=previous_year, month=previous_month)
    return None


def _month_period_iso(*, year: int, month: int) -> tuple[str, str]:
    """Return ISO start/end strings for one calendar month."""

    last_day = monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last_day:02d}"


def _operator_memory_can_disambiguate_intent(
    *,
    operator_memory: AgentMemorySummary,
    snapshot: dict[str, Any],
    intent: str,
) -> bool:
    """Return whether thread-local target memory can safely break a cross-domain tie."""

    target_type = operator_memory.last_target_type
    target_id = operator_memory.last_target_id
    if not isinstance(target_type, str) or not isinstance(target_id, str):
        return False
    allowed_target_types = {
        "approve": {"document", "recommendation", "journal", "reconciliation", "commentary"},
        "reject": {"document", "recommendation", "journal"},
        "apply": {"journal"},
        "resolve": {"reconciliation_item", "reconciliation_anomaly"},
        "ignore": {"document"},
    }.get(intent, set())
    if target_type not in allowed_target_types:
        return False
    return _snapshot_contains_target(
        snapshot=snapshot,
        target_type=target_type,
        target_id=target_id,
    )


def _resolve_recent_target_id(
    *,
    operator_memory: AgentMemorySummary,
    snapshot: dict[str, Any],
    target_type: str,
    referential_only: bool,
    operator_content: str,
) -> str | None:
    """Return the last thread-local target id when it is safe to reuse."""

    if referential_only and not _is_referential_follow_up(operator_content=operator_content):
        return None
    if operator_memory.last_target_type != target_type:
        return None
    last_target_id = operator_memory.last_target_id
    if not isinstance(last_target_id, str) or not last_target_id.strip():
        return None
    if not _snapshot_contains_target(
        snapshot=snapshot,
        target_type=target_type,
        target_id=last_target_id,
    ):
        return None
    return last_target_id


def _is_referential_follow_up(*, operator_content: str) -> bool:
    """Return whether the operator is referring back to the last concrete target."""

    normalized_content = _searchable_text(operator_content)
    referential_phrases = (
        "it",
        "that",
        "this",
        "continue",
        "go ahead",
        "do that",
        "do it",
        "use that",
        "use it",
        "the same one",
        "that one",
        "same one",
    )
    return any(phrase in normalized_content for phrase in referential_phrases)


def _snapshot_contains_target(
    *,
    snapshot: dict[str, Any],
    target_type: str,
    target_id: str,
) -> bool:
    """Return whether the current snapshot still contains the remembered target."""

    normalized_target_id = target_id.strip()
    if not normalized_target_id:
        return False
    collection, matcher = _snapshot_collection_for_target_type(target_type=target_type)
    if collection is None:
        return False
    records = snapshot.get(collection)
    if not isinstance(records, list):
        if target_type == "workspace":
            workspace = snapshot.get("workspace")
            if (
                isinstance(workspace, dict)
                and str(workspace.get("id") or "") == normalized_target_id
            ):
                return True
            accessible_workspaces = snapshot.get("accessible_workspaces")
            if isinstance(accessible_workspaces, list):
                return any(
                    isinstance(record, dict)
                    and str(record.get("id") or "") == normalized_target_id
                    for record in accessible_workspaces
                )
        return False
    for record in records:
        if isinstance(record, dict) and matcher(record, normalized_target_id):
            return True
    return False


def _resolve_memory_target_snapshot(
    *,
    tool_name: str | None,
    tool_arguments: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, str] | None:
    """Return one concrete target snapshot to persist into thread memory."""

    if tool_name in {"update_commentary", "approve_commentary"}:
        commentary = snapshot.get("commentary")
        if not isinstance(commentary, list):
            return None
        report_run_id = tool_arguments.get("report_run_id")
        section_key = tool_arguments.get("section_key")
        if not isinstance(report_run_id, str) or not isinstance(section_key, str):
            return None
        for record in commentary:
            if not isinstance(record, dict):
                continue
            if str(record.get("report_run_id") or "") != report_run_id:
                continue
            if str(record.get("section_key") or "") != section_key:
                continue
            target_id = record.get("id")
            if not isinstance(target_id, str) or not target_id.strip():
                return None
            label = _commentary_specific_label(record=record)
            if label is None:
                return None
            return {
                "target_type": "commentary",
                "target_id": target_id.strip(),
                "label": label,
            }
        return None

    target_spec = _tool_memory_target_spec(tool_name=tool_name)
    if target_spec is None:
        return None
    target_type, target_argument = target_spec
    target_id = tool_arguments.get(target_argument)
    if not isinstance(target_id, str) or not target_id.strip():
        return None
    label = _resolve_target_label_from_snapshot(
        snapshot=snapshot,
        target_type=target_type,
        target_id=target_id,
    )
    if label is None:
        return None
    return {
        "target_type": target_type,
        "target_id": target_id.strip(),
        "label": label,
    }


def _resolve_approved_objective(
    *,
    existing_memory: dict[str, Any],
    operator_message: str | None,
    action_status: str,
) -> str | None:
    """Return the last operator objective that the agent actively committed to carry out."""

    if (
        operator_message is not None
        and operator_message.strip()
        and action_status in {"pending", "applied", "waiting_async", "partial"}
    ):
        return _truncate_text(operator_message.strip(), limit=180)
    existing_value = existing_memory.get("approved_objective")
    return existing_value if isinstance(existing_value, str) and existing_value.strip() else None


def _resolve_working_subtask(
    *,
    existing_memory: dict[str, Any],
    operator_message: str | None,
    tool_name: str | None,
    resolved_target: dict[str, str] | None,
    action_status: str,
    snapshot: dict[str, Any],
    active_async_turn: dict[str, Any] | None,
) -> str | None:
    """Return one compact current-subtask summary for long-turn continuity."""

    active_async_objective = optional_memory_text(active_async_turn, "objective")
    if action_status == "waiting_async" and active_async_objective is not None:
        return f"Waiting for background work to finish so I can continue {active_async_objective}"
    if resolved_target is not None and tool_name is not None:
        verb = _tool_memory_subtask_verb(tool_name=tool_name)
        if verb is not None:
            return f"{verb} {resolved_target['label']}"
    if tool_name is not None:
        generic_subtask = _tool_memory_generic_subtask(tool_name=tool_name)
        if generic_subtask is not None:
            return generic_subtask
    if operator_message is not None and operator_message.strip() and action_status != "read_only":
        return _truncate_text(operator_message.strip(), limit=180)
    next_action = _first_readiness_action(snapshot=snapshot)
    if next_action is not None:
        return _truncate_text(next_action, limit=180)
    existing_value = existing_memory.get("working_subtask")
    return existing_value if isinstance(existing_value, str) and existing_value.strip() else None


def _resolve_pending_branch(
    *,
    existing_memory: dict[str, Any],
    tool_name: str | None,
    action_status: str,
    snapshot: dict[str, Any],
    active_async_turn: dict[str, Any] | None,
) -> str | None:
    """Return the next pending branch or hold state kept in compact memory."""

    if action_status == "pending":
        if isinstance(tool_name, str) and tool_name.strip():
            return f"Awaiting operator confirmation for {tool_name.replace('_', ' ')}"
        return "Awaiting operator confirmation for the next governed change"
    if action_status == "waiting_async":
        active_async_objective = optional_memory_text(active_async_turn, "objective")
        if active_async_objective is not None:
            return (
                "Resume automatically when background work completes for "
                f"{active_async_objective}"
            )
        return "Resume automatically when the current background work completes"
    next_action = _first_readiness_action(snapshot=snapshot)
    if next_action is not None:
        return _truncate_text(
            f"Next branch: {_lowercase_leading_character(next_action)}",
            limit=180,
        )
    existing_value = existing_memory.get("pending_branch")
    return existing_value if isinstance(existing_value, str) and existing_value.strip() else None


def _tool_memory_subtask_verb(*, tool_name: str) -> str | None:
    """Return the compact working-set verb phrase for one deterministic tool."""

    mapping = {
        "review_document": "Review",
        "ignore_document": "Ignore",
        "approve_recommendation": "Approve",
        "reject_recommendation": "Reject",
        "approve_journal": "Approve",
        "apply_journal": "Apply",
        "reject_journal": "Reject",
        "approve_reconciliation": "Approve",
        "disposition_reconciliation_item": "Resolve",
        "resolve_reconciliation_anomaly": "Resolve",
        "update_commentary": "Update",
        "approve_commentary": "Approve",
        "switch_workspace": "Work in",
        "update_workspace": "Update",
        "delete_workspace": "Delete",
        "create_close_run": "Create a close run in",
        "open_close_run": "Work in",
        "delete_close_run": "Delete",
    }
    return mapping.get(tool_name)


def _tool_memory_generic_subtask(*, tool_name: str) -> str | None:
    """Return a generic current-subtask label when no concrete target is pinned."""

    mapping = {
        "generate_recommendations": "Generate recommendations for the current close run",
        "run_reconciliation": "Run reconciliation for the current close run",
        "generate_reports": "Generate reports for the current close run",
        "generate_export": "Generate the export package for the current close run",
        "assemble_evidence_pack": "Assemble the evidence pack for the current close run",
        "create_workspace": "Create the new workspace",
        "create_close_run": "Create the next close run",
        "open_close_run": "Open the close run",
        "advance_close_run": "Advance the close run",
        "rewind_close_run": "Move the close run back to the requested phase",
        "reopen_close_run": "Reopen the close run as a working version",
        "approve_close_run": "Approve the close run",
        "archive_close_run": "Archive the close run",
    }
    return mapping.get(tool_name)


def _resolve_target_label_from_snapshot(
    *,
    snapshot: dict[str, Any],
    target_type: str,
    target_id: str,
) -> str | None:
    """Return one operator-facing target label from the current workspace snapshot."""

    collection, matcher = _snapshot_collection_for_target_type(target_type=target_type)
    if collection == "workspace":
        return _resolve_workspace_label(snapshot=snapshot, workspace_id=target_id)
    if collection == "entity_close_runs":
        return _resolve_close_run_label(snapshot=snapshot, close_run_id=target_id)
    if collection is None:
        return None
    records = snapshot.get(collection)
    if not isinstance(records, list):
        return None
    for record in records:
        if not isinstance(record, dict) or not matcher(record, target_id):
            continue
        return _target_label_from_record(target_type=target_type, record=record)
    return None


def _snapshot_collection_for_target_type(
    *,
    target_type: str,
) -> tuple[str | None, Any]:
    """Map one canonical target type onto its snapshot collection and id matcher."""

    def matcher(record: dict[str, Any], value: str) -> bool:
        return str(record.get("id") or "") == value

    if target_type == "document":
        return "documents", matcher
    if target_type == "recommendation":
        return "recommendations", matcher
    if target_type == "journal":
        return "journals", matcher
    if target_type == "reconciliation":
        return "reconciliations", matcher
    if target_type == "reconciliation_item":
        return "reconciliation_items", matcher
    if target_type == "reconciliation_anomaly":
        return "reconciliation_anomalies", matcher
    if target_type == "commentary":
        return "commentary", matcher
    if target_type == "workspace":
        return "workspace", matcher
    if target_type == "close_run":
        return "entity_close_runs", matcher
    return None, matcher


def _tool_memory_target_spec(*, tool_name: str | None) -> tuple[str, str] | None:
    """Return the target type and argument key remembered for one tool."""

    mapping = {
        "review_document": ("document", "document_id"),
        "ignore_document": ("document", "document_id"),
        "approve_recommendation": ("recommendation", "recommendation_id"),
        "reject_recommendation": ("recommendation", "recommendation_id"),
        "approve_journal": ("journal", "journal_id"),
        "apply_journal": ("journal", "journal_id"),
        "reject_journal": ("journal", "journal_id"),
        "approve_reconciliation": ("reconciliation", "reconciliation_id"),
        "disposition_reconciliation_item": ("reconciliation_item", "item_id"),
        "resolve_reconciliation_anomaly": ("reconciliation_anomaly", "anomaly_id"),
        "update_commentary": ("commentary", "commentary_id"),
        "approve_commentary": ("commentary", "commentary_id"),
        "switch_workspace": ("workspace", "workspace_id"),
        "update_workspace": ("workspace", "workspace_id"),
        "delete_workspace": ("workspace", "workspace_id"),
        "create_close_run": ("workspace", "workspace_id"),
        "open_close_run": ("close_run", "close_run_id"),
        "delete_close_run": ("close_run", "close_run_id"),
        "reopen_close_run": ("close_run", "close_run_id"),
    }
    return mapping.get(tool_name or "")


def _target_label_from_record(*, target_type: str, record: dict[str, Any]) -> str | None:
    """Return one concise label for a remembered target record."""

    if target_type == "document":
        return _document_specific_label(record=record)
    if target_type == "recommendation":
        return _recommendation_specific_label(record=record)
    if target_type == "journal":
        return _journal_specific_label(record=record)
    if target_type == "reconciliation":
        reconciliation_type = record.get("type")
        if isinstance(reconciliation_type, str) and reconciliation_type.strip():
            return f"the {reconciliation_type.replace('_', ' ')} reconciliation"
        return "the reconciliation"
    if target_type == "reconciliation_item":
        return _reconciliation_item_specific_label(record=record)
    if target_type == "reconciliation_anomaly":
        return _reconciliation_anomaly_specific_label(record=record)
    if target_type == "commentary":
        return _commentary_specific_label(record=record)
    return None


def _build_planner_focus_lines(*, snapshot: dict[str, Any]) -> list[str]:
    """Return compact focus cues that help the planner choose the next action naturally."""

    lines: list[str] = []
    workspace_name = _workspace_name_from_snapshot(snapshot=snapshot)
    if workspace_name is not None:
        lines.append(f"- Active workspace: {workspace_name}.")

    close_run_id = snapshot.get("close_run_id")
    if isinstance(close_run_id, str) and close_run_id.strip():
        close_run_label = _resolve_close_run_label(
            snapshot=snapshot,
            close_run_id=close_run_id,
        )
        if close_run_label is not None:
            lines.append(f"- Active close run: {close_run_label}.")
    else:
        lines.append("- Thread scope: workspace-level with no close run pinned.")

    blockers = _readiness_items(snapshot=snapshot, key="blockers")
    if blockers:
        lines.append(f"- Top blocker: {blockers[0]}.")
    next_action = _first_readiness_action(snapshot=snapshot)
    if next_action is not None:
        lines.append(f"- Suggested next action: {next_action}.")

    pending_document = _single_pending_document_record(snapshot=snapshot)
    if pending_document is not None:
        filename = pending_document.get("filename")
        if isinstance(filename, str) and filename.strip():
            lines.append(f"- Clear document review target: {filename.strip()}.")
    else:
        pending_document_count = _count_pending_documents(snapshot=snapshot)
        if pending_document_count > 1:
            lines.append(f"- Documents awaiting review: {pending_document_count}.")

    pending_recommendation = _single_pending_recommendation_record(snapshot=snapshot)
    if pending_recommendation is not None:
        recommendation_label = _recommendation_specific_label(record=pending_recommendation)
        if recommendation_label is not None:
            lines.append(f"- Clear recommendation target: {recommendation_label}.")

    pending_journal = _single_pending_journal_record(snapshot=snapshot, intent="approve")
    if pending_journal is not None:
        journal_label = _journal_specific_label(record=pending_journal)
        if journal_label is not None:
            lines.append(f"- Clear journal target: {journal_label}.")

    pending_item = _single_pending_reconciliation_item_record(snapshot=snapshot)
    if pending_item is not None:
        item_label = _reconciliation_item_specific_label(record=pending_item)
        if item_label is not None:
            lines.append(f"- Clear reconciliation item: {item_label}.")

    pending_anomaly = _single_pending_reconciliation_anomaly_record(snapshot=snapshot)
    if pending_anomaly is not None:
        anomaly_label = _reconciliation_anomaly_specific_label(record=pending_anomaly)
        if anomaly_label is not None:
            lines.append(f"- Clear anomaly target: {anomaly_label}.")

    pending_commentary = _single_pending_commentary_record(snapshot=snapshot)
    if pending_commentary is not None:
        commentary_label = _commentary_specific_label(record=pending_commentary)
        if commentary_label is not None:
            lines.append(f"- Clear commentary target: {commentary_label}.")

    return lines[:8]


def _build_unresolved_tool_selection_message(
    *,
    operator_content: str,
    snapshot: dict[str, Any],
) -> str:
    """Return a natural read-only recovery message when the planner picked no valid action."""

    normalized_content = _searchable_text(operator_content)
    if "workspace" in normalized_content or "entity" in normalized_content:
        choices = _workspace_choice_labels(snapshot=snapshot)
        if choices:
            return (
                "Tell me which workspace you want me to use and I'll handle it here. "
                f"I can use { _join_choice_labels(choices) }."
            )
        return "Tell me which workspace you want me to use and I'll handle it here."

    return (
        "I need one slightly clearer instruction before I act. Tell me the exact step you "
        "want and I'll handle it here."
    )


def _normalize_planned_tool_name(tool_name: object) -> str | None:
    """Return one normalized planner-selected tool name when present."""

    if not isinstance(tool_name, str):
        return None
    normalized = tool_name.strip()
    if not normalized:
        return None
    return normalized.replace("-", "_").replace(" ", "_").lower()


def _infer_tool_name_from_namespace(
    *,
    namespace_name: str,
    operator_content: str,
    tool_arguments: dict[str, Any],
) -> str | None:
    """Return the most likely concrete tool when the planner emitted a namespace label."""

    normalized_content = _searchable_text(operator_content)
    if namespace_name == "workspace_admin":
        return _infer_workspace_admin_tool_name(
            normalized_content=normalized_content,
            tool_arguments=tool_arguments,
        )
    if namespace_name == "close_operator":
        return _infer_close_operator_tool_name(
            normalized_content=normalized_content,
            tool_arguments=tool_arguments,
        )
    if namespace_name == "document_control":
        return _infer_document_control_tool_name(
            normalized_content=normalized_content,
            tool_arguments=tool_arguments,
        )
    if namespace_name == "treatment_and_journals":
        return _infer_treatment_tool_name(
            normalized_content=normalized_content,
            tool_arguments=tool_arguments,
        )
    if namespace_name == "reconciliation_control":
        return _infer_reconciliation_tool_name(
            normalized_content=normalized_content,
            tool_arguments=tool_arguments,
        )
    if namespace_name == "reporting_and_release":
        return _infer_reporting_tool_name(
            normalized_content=normalized_content,
            tool_arguments=tool_arguments,
        )
    return None


def _infer_workspace_admin_tool_name(
    *,
    normalized_content: str,
    tool_arguments: dict[str, Any],
) -> str | None:
    """Resolve the concrete workspace-admin tool from one operator request."""

    update_fields = {
        "name",
        "legal_name",
        "base_currency",
        "country_code",
        "timezone",
        "accounting_standard",
        "autonomy_mode",
    }
    if any(token in normalized_content for token in ("delete", "remove")):
        return "delete_workspace"
    if any(token in normalized_content for token in ("create", "new", "another", "add")):
        return "create_workspace"
    if any(field in tool_arguments for field in update_fields) or any(
        token in normalized_content
        for token in (
            "rename",
            "update",
            "change the workspace",
            "change this workspace",
            "edit the workspace",
            "set the workspace",
            "workspace settings",
            "base currency",
            "timezone",
            "accounting standard",
            "review routing",
            "autonomy mode",
            "human review",
            "reduced interruption",
        )
    ):
        return "update_workspace"
    if (
        "workspace_id" in tool_arguments
        or any(
            token in normalized_content
            for token in (
                "switch",
                "move this chat",
                "move the chat",
                "change workspace",
                "use the ",
                "work on ",
                "go to ",
                "back to ",
            )
        )
    ):
        return "switch_workspace"
    return None


def _infer_close_operator_tool_name(
    *,
    normalized_content: str,
    tool_arguments: dict[str, Any],
) -> str | None:
    """Resolve the concrete close-run lifecycle tool from one operator request."""

    if any(token in normalized_content for token in ("delete", "remove")):
        return "delete_close_run"
    if any(token in normalized_content for token in ("archive",)):
        return "archive_close_run"
    if any(
        token in normalized_content
        for token in (
            "reopen",
            "open it again",
            "enter that approved",
            "enter the approved",
            "work inside the approved",
            "alter it after approval",
        )
    ):
        return "reopen_close_run"
    if any(token in normalized_content for token in ("approve", "sign off", "signoff")):
        return "approve_close_run"
    if "target_phase" in tool_arguments or any(
        token in normalized_content
        for token in (
            "take it back",
            "move it back",
            "back to ",
            "rewind",
            "return it to ",
        )
    ):
        return "rewind_close_run"
    if any(token in normalized_content for token in ("start", "new run", "fresh run", "new close")):
        return "create_close_run"
    if any(
        token in normalized_content
        for token in (
            "work on ",
            "open ",
            "enter ",
            "pin ",
            "select ",
            "use ",
            "switch to ",
        )
    ):
        return "open_close_run"
    if any(
        token in normalized_content
        for token in ("advance", "move forward", "continue to ", "move to ")
    ):
        return "advance_close_run"
    return None


def _infer_document_control_tool_name(
    *,
    normalized_content: str,
    tool_arguments: dict[str, Any],
) -> str | None:
    """Resolve the concrete document-control tool from one operator request."""

    if "field_id" in tool_arguments or any(
        token in normalized_content
        for token in ("correct", "fix the value", "change the value", "edit the field")
    ):
        return "correct_extracted_field"
    if any(
        token in normalized_content
        for token in ("ignore", "uploaded by mistake", "wrong document", "mistaken upload")
    ):
        return "ignore_document"
    if any(
        token in normalized_content
        for token in (
            "all document",
            "all source document",
            "all uploaded document",
            "all parsed document",
            "every document",
            "documents",
        )
    ) and any(
        token in normalized_content for token in ("approve", "reject", "needs info", "review")
    ):
        return "review_documents"
    if any(
        token in normalized_content
        for token in ("approve", "reject", "needs info", "review", "document")
    ):
        return "review_document"
    return None


def _infer_treatment_tool_name(
    *,
    normalized_content: str,
    tool_arguments: dict[str, Any],
) -> str | None:
    """Resolve the concrete treatment or journal tool from one operator request."""

    if "posting_target" in tool_arguments or any(
        token in normalized_content for token in ("apply journal", "post journal", "apply it")
    ):
        return "apply_journal"
    if any(token in normalized_content for token in ("approve journal", "approve the journal")):
        return "approve_journal"
    if any(token in normalized_content for token in ("reject journal", "reject the journal")):
        return "reject_journal"
    if any(
        token in normalized_content
        for token in ("approve recommendation", "approve the recommendation")
    ):
        return "approve_recommendation"
    if any(
        token in normalized_content
        for token in ("reject recommendation", "reject the recommendation")
    ):
        return "reject_recommendation"
    if any(
        phrase in normalized_content
        for phrase in (
            "generate recommendations",
            "generate recommendation",
            "generate accounting recommendations",
            "run recommendations",
            "run recommendation",
            "queue recommendations",
            "queue recommendation",
            "start recommendations",
            "start recommendation",
            "recommendation pass",
            "generate treatment",
            "generate treatments",
            "run treatment",
        )
    ):
        return "generate_recommendations"
    return None


def _infer_reconciliation_tool_name(
    *,
    normalized_content: str,
    tool_arguments: dict[str, Any],
) -> str | None:
    """Resolve the concrete reconciliation tool from one operator request."""

    if "resolution_note" in tool_arguments or "anomaly" in normalized_content:
        return "resolve_reconciliation_anomaly"
    if "disposition" in tool_arguments or any(
        token in normalized_content for token in ("exception", "item", "disposition", "resolve it")
    ):
        return "disposition_reconciliation_item"
    if any(token in normalized_content for token in ("approve", "approved")):
        return "approve_reconciliation"
    if any(token in normalized_content for token in ("run", "reconcile", "reconciliation")):
        return "run_reconciliation"
    return None


def _infer_reporting_tool_name(
    *,
    normalized_content: str,
    tool_arguments: dict[str, Any],
) -> str | None:
    """Resolve the concrete reporting or release tool from one operator request."""

    if "recipient_name" in tool_arguments or any(
        token in normalized_content
        for token in ("distribute", "send the export", "record distribution")
    ):
        return "distribute_export"
    if any(
        token in normalized_content
        for token in (
            "approve commentary",
            "approve the commentary",
            "approve that commentary",
        )
    ):
        return "approve_commentary"
    if "section_key" in tool_arguments and "body" not in tool_arguments:
        return "approve_commentary"
    if "body" in tool_arguments or any(
        token in normalized_content for token in ("commentary", "narrative", "management note")
    ):
        return "update_commentary"
    if "row_payload" in tool_arguments:
        return "upsert_supporting_schedule_row"
    if "row_id" in tool_arguments and "schedule_type" in tool_arguments:
        return "delete_supporting_schedule_row"
    if "status" in tool_arguments and "schedule_type" in tool_arguments:
        return "set_supporting_schedule_status"
    if any(token in normalized_content for token in ("evidence pack",)):
        return "assemble_evidence_pack"
    if any(
        token in normalized_content
        for token in ("export", "release package", "package the export")
    ):
        return "generate_export"
    if any(token in normalized_content for token in ("report", "commentary", "report pack")):
        return "generate_reports"
    return None


def _hydrate_review_document_arguments(
    *,
    tool_arguments: dict[str, Any],
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary,
) -> dict[str, Any]:
    """Fill the common document-review arguments when the target is unambiguous."""

    hydrated = dict(tool_arguments)
    decision = hydrated.get("decision")
    if not isinstance(decision, str):
        inferred_decision = _infer_document_review_decision(operator_content)
        if inferred_decision is not None:
            hydrated["decision"] = inferred_decision
            decision = inferred_decision

    if not isinstance(hydrated.get("document_id"), str):
        document_id = _resolve_document_id_from_snapshot(
            snapshot=snapshot,
            operator_content=operator_content,
            preferred_statuses=("needs_review", "parsed", "uploaded", "processing"),
            operator_memory=operator_memory,
        )
        if document_id is not None:
            hydrated["document_id"] = document_id

    if isinstance(decision, str) and decision.strip().lower() == "approved":
        hydrated.setdefault("verified_complete", True)
        hydrated.setdefault("verified_authorized", True)
        hydrated.setdefault("verified_period", True)

    return hydrated


def _hydrate_review_documents_arguments(
    *,
    tool_arguments: dict[str, Any],
    operator_content: str,
) -> dict[str, Any]:
    """Fill safe defaults for batch document-review requests."""

    hydrated = dict(tool_arguments)
    if not isinstance(hydrated.get("decision"), str):
        inferred_decision = _infer_document_review_decision(operator_content)
        if inferred_decision is not None:
            hydrated["decision"] = inferred_decision
    if str(hydrated.get("decision") or "").strip().lower() == "approved":
        hydrated.setdefault("verified_complete", True)
        hydrated.setdefault("verified_authorized", True)
        hydrated.setdefault("verified_period", True)
    normalized_content = _searchable_text(operator_content)
    if any(
        phrase in normalized_content
        for phrase in (
            "ignore missing",
            "ignore open issue",
            "ignore issues",
            "continue anyway",
            "approve anyway",
        )
    ):
        hydrated.setdefault("include_documents_with_open_issues", True)
    return hydrated


def _infer_document_review_decision(operator_content: str) -> str | None:
    """Infer the intended review decision from one short operator instruction."""

    normalized = _searchable_text(operator_content)
    if any(token in normalized for token in ("approve", "approved", "ok it", "accept")):
        return "approved"
    if any(token in normalized for token in ("reject", "rejected", "decline")):
        return "rejected"
    if any(
        token in normalized
        for token in ("needs info", "need info", "more info", "request info")
    ):
        return "needs_info"
    return None


def _resolve_document_id_from_snapshot(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    preferred_statuses: tuple[str, ...],
    operator_memory: AgentMemorySummary,
) -> str | None:
    """Resolve one document identifier from the snapshot when the target is clear."""

    documents = snapshot.get("documents")
    if not isinstance(documents, list):
        return None

    records = [record for record in documents if isinstance(record, dict)]
    if not records:
        return None

    normalized_content = _searchable_text(operator_content)
    filename_matches = [
        record
        for record in records
        if isinstance(record.get("filename"), str)
        and _filename_matches_text(str(record["filename"]), normalized_content)
    ]
    if len(filename_matches) == 1 and isinstance(filename_matches[0].get("id"), str):
        return str(filename_matches[0]["id"])

    recent_target_id = _resolve_recent_target_id(
        operator_memory=operator_memory,
        snapshot=snapshot,
        target_type="document",
        referential_only=True,
        operator_content=operator_content,
    )
    if recent_target_id is not None and _document_matches_preferred_status(
        records=records,
        document_id=recent_target_id,
        preferred_statuses=preferred_statuses,
    ):
        return recent_target_id

    preferred = [
        record
        for record in records
        if str(record.get("status") or "") in preferred_statuses
    ]
    if len(preferred) == 1 and isinstance(preferred[0].get("id"), str):
        return str(preferred[0]["id"])
    return None


def _document_matches_preferred_status(
    *,
    records: list[dict[str, Any]],
    document_id: str,
    preferred_statuses: tuple[str, ...],
) -> bool:
    """Return whether one remembered document still matches the allowed workflow states."""

    for record in records:
        if str(record.get("id") or "") != document_id:
            continue
        return str(record.get("status") or "") in preferred_statuses
    return False


def _hydrate_recommendation_arguments(
    *,
    tool_name: str,
    tool_arguments: dict[str, Any],
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary,
) -> dict[str, Any]:
    """Resolve clear recommendation targets and rejection reasons from the snapshot."""

    hydrated = dict(tool_arguments)
    if not isinstance(hydrated.get("recommendation_id"), str):
        recommendation_id = _resolve_recommendation_id_from_snapshot(
            snapshot=snapshot,
            operator_content=operator_content,
            preferred_statuses=("pending_review", "draft"),
            operator_memory=operator_memory,
        )
        if recommendation_id is not None:
            hydrated["recommendation_id"] = recommendation_id

    if tool_name == "reject_recommendation" and not isinstance(hydrated.get("reason"), str):
        hydrated["reason"] = _extract_reason_from_operator_content(
            operator_content=operator_content,
            fallback="Rejected by operator instruction in chat.",
        )
    return hydrated


def _resolve_recommendation_id_from_snapshot(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    preferred_statuses: tuple[str, ...],
    operator_memory: AgentMemorySummary,
) -> str | None:
    """Resolve one recommendation identifier from the snapshot when the target is clear."""

    recommendations = snapshot.get("recommendations")
    if not isinstance(recommendations, list):
        return None

    records = [record for record in recommendations if isinstance(record, dict)]
    if not records:
        return None

    normalized_content = _searchable_text(operator_content)
    filename_matches = [
        record
        for record in records
        if isinstance(record.get("document_filename"), str)
        and _filename_matches_text(str(record["document_filename"]), normalized_content)
    ]
    if len(filename_matches) == 1 and isinstance(filename_matches[0].get("id"), str):
        return str(filename_matches[0]["id"])

    reasoning_matches = [
        record
        for record in records
        if isinstance(record.get("reasoning_summary"), str)
        and _text_value_matches_text(str(record["reasoning_summary"]), normalized_content)
    ]
    if len(reasoning_matches) == 1 and isinstance(reasoning_matches[0].get("id"), str):
        return str(reasoning_matches[0]["id"])

    recent_target_id = _resolve_recent_target_id(
        operator_memory=operator_memory,
        snapshot=snapshot,
        target_type="recommendation",
        referential_only=True,
        operator_content=operator_content,
    )
    if recent_target_id is not None:
        return recent_target_id

    preferred = [
        record
        for record in records
        if str(record.get("status") or "") in preferred_statuses
    ]
    if len(preferred) == 1 and isinstance(preferred[0].get("id"), str):
        return str(preferred[0]["id"])
    if preferred_statuses == ("approved",):
        return None
    if len(records) == 1 and isinstance(records[0].get("id"), str):
        return str(records[0]["id"])
    return None


def _hydrate_journal_arguments(
    *,
    tool_name: str,
    tool_arguments: dict[str, Any],
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary,
) -> dict[str, Any]:
    """Resolve clear journal targets, posting defaults, and rejection reasons."""

    hydrated = dict(tool_arguments)
    preferred_statuses = (
        ("approved",)
        if tool_name == "apply_journal"
        else ("pending_review", "draft")
    )
    if not isinstance(hydrated.get("journal_id"), str):
        journal_id = _resolve_journal_id_from_snapshot(
            snapshot=snapshot,
            operator_content=operator_content,
            preferred_statuses=preferred_statuses,
            operator_memory=operator_memory,
        )
        if journal_id is not None:
            hydrated["journal_id"] = journal_id

    if tool_name == "apply_journal" and not isinstance(hydrated.get("posting_target"), str):
        hydrated["posting_target"] = _infer_journal_posting_target(operator_content)

    if tool_name == "reject_journal" and not isinstance(hydrated.get("reason"), str):
        hydrated["reason"] = _extract_reason_from_operator_content(
            operator_content=operator_content,
            fallback="Rejected by operator instruction in chat.",
        )
    return hydrated


def _resolve_journal_id_from_snapshot(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    preferred_statuses: tuple[str, ...],
    operator_memory: AgentMemorySummary,
) -> str | None:
    """Resolve one journal identifier from the snapshot when the target is clear."""

    journals = snapshot.get("journals")
    if not isinstance(journals, list):
        return None

    records = [record for record in journals if isinstance(record, dict)]
    if not records:
        return None

    normalized_content = _searchable_text(operator_content)
    explicit_matches = [
        record
        for record in records
        if (
            isinstance(record.get("journal_number"), str)
            and _text_value_matches_text(str(record["journal_number"]), normalized_content)
        )
        or (
            isinstance(record.get("description"), str)
            and _text_value_matches_text(str(record["description"]), normalized_content)
        )
    ]
    if len(explicit_matches) == 1 and isinstance(explicit_matches[0].get("id"), str):
        if preferred_statuses == ("approved",) and str(
            explicit_matches[0].get("status") or ""
        ) not in preferred_statuses:
            return None
        return str(explicit_matches[0]["id"])

    recent_target_id = _resolve_recent_target_id(
        operator_memory=operator_memory,
        snapshot=snapshot,
        target_type="journal",
        referential_only=True,
        operator_content=operator_content,
    )
    if recent_target_id is not None:
        if preferred_statuses == ("approved",):
            for record in records:
                if (
                    str(record.get("id") or "") == recent_target_id
                    and str(record.get("status") or "") in preferred_statuses
                ):
                    return recent_target_id
            return None
        return recent_target_id

    preferred = [
        record
        for record in records
        if str(record.get("status") or "") in preferred_statuses
    ]
    if len(preferred) == 1 and isinstance(preferred[0].get("id"), str):
        return str(preferred[0]["id"])
    if preferred_statuses == ("approved",):
        return None
    if len(records) == 1 and isinstance(records[0].get("id"), str):
        return str(records[0]["id"])
    return None


def _hydrate_reconciliation_arguments(
    *,
    tool_name: str,
    tool_arguments: dict[str, Any],
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary,
) -> dict[str, Any]:
    """Resolve clear reconciliation targets, dispositions, and reviewer reasons."""

    hydrated = dict(tool_arguments)
    if tool_name == "approve_reconciliation":
        if not isinstance(hydrated.get("reconciliation_id"), str):
            reconciliation_id = _resolve_reconciliation_id_from_snapshot(
                snapshot=snapshot,
                operator_content=operator_content,
                preferred_statuses=("in_review", "blocked", "draft"),
                operator_memory=operator_memory,
            )
            if reconciliation_id is not None:
                hydrated["reconciliation_id"] = reconciliation_id
        return hydrated

    if tool_name == "disposition_reconciliation_item":
        if not isinstance(hydrated.get("item_id"), str):
            item_id = _resolve_reconciliation_item_id_from_snapshot(
                snapshot=snapshot,
                operator_content=operator_content,
                operator_memory=operator_memory,
            )
            if item_id is not None:
                hydrated["item_id"] = item_id
        if not isinstance(hydrated.get("disposition"), str):
            disposition = _infer_reconciliation_disposition(operator_content)
            if disposition is not None:
                hydrated["disposition"] = disposition
        if not isinstance(hydrated.get("reason"), str):
            disposition_value = hydrated.get("disposition")
            fallback = (
                f"Marked as {str(disposition_value).replace('_', ' ')} by operator instruction."
                if isinstance(disposition_value, str)
                else "Disposition recorded by operator instruction in chat."
            )
            hydrated["reason"] = _extract_reason_from_operator_content(
                operator_content=operator_content,
                fallback=fallback,
            )
        return hydrated

    if not isinstance(hydrated.get("anomaly_id"), str):
        anomaly_id = _resolve_reconciliation_anomaly_id_from_snapshot(
            snapshot=snapshot,
            operator_content=operator_content,
            operator_memory=operator_memory,
        )
        if anomaly_id is not None:
            hydrated["anomaly_id"] = anomaly_id
    if not isinstance(hydrated.get("resolution_note"), str):
        hydrated["resolution_note"] = _extract_reason_from_operator_content(
            operator_content=operator_content,
            fallback="Resolved by operator instruction in chat.",
        )
    return hydrated


def _resolve_reconciliation_id_from_snapshot(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    preferred_statuses: tuple[str, ...],
    operator_memory: AgentMemorySummary,
) -> str | None:
    """Resolve one reconciliation identifier from the snapshot when the target is clear."""

    reconciliations = snapshot.get("reconciliations")
    if not isinstance(reconciliations, list):
        return None

    records = [record for record in reconciliations if isinstance(record, dict)]
    if not records:
        return None

    normalized_content = _searchable_text(operator_content)
    type_matches = [
        record
        for record in records
        if isinstance(record.get("type"), str)
        and _text_value_matches_text(
            str(record["type"]).replace("_", " "),
            normalized_content,
        )
    ]
    if len(type_matches) == 1 and isinstance(type_matches[0].get("id"), str):
        return str(type_matches[0]["id"])

    recent_target_id = _resolve_recent_target_id(
        operator_memory=operator_memory,
        snapshot=snapshot,
        target_type="reconciliation",
        referential_only=True,
        operator_content=operator_content,
    )
    if recent_target_id is not None:
        return recent_target_id

    preferred = [
        record
        for record in records
        if str(record.get("status") or "") in preferred_statuses
    ]
    if len(preferred) == 1 and isinstance(preferred[0].get("id"), str):
        return str(preferred[0]["id"])
    if len(records) == 1 and isinstance(records[0].get("id"), str):
        return str(records[0]["id"])
    return None


def _resolve_reconciliation_item_id_from_snapshot(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary,
) -> str | None:
    """Resolve one reconciliation item identifier from the snapshot when the target is clear."""

    items = snapshot.get("reconciliation_items")
    if not isinstance(items, list):
        return None

    records = [record for record in items if isinstance(record, dict)]
    if not records:
        return None

    normalized_content = _searchable_text(operator_content)
    explicit_matches = [
        record
        for record in records
        if (
            isinstance(record.get("source_ref"), str)
            and _text_value_matches_text(str(record["source_ref"]), normalized_content)
        )
        or (
            isinstance(record.get("explanation"), str)
            and _text_value_matches_text(str(record["explanation"]), normalized_content)
        )
    ]
    if len(explicit_matches) == 1 and isinstance(explicit_matches[0].get("id"), str):
        return str(explicit_matches[0]["id"])

    recent_target_id = _resolve_recent_target_id(
        operator_memory=operator_memory,
        snapshot=snapshot,
        target_type="reconciliation_item",
        referential_only=True,
        operator_content=operator_content,
    )
    if recent_target_id is not None:
        return recent_target_id

    pending = [
        record
        for record in records
        if bool(record.get("requires_disposition")) and record.get("disposition") is None
    ]
    if len(pending) == 1 and isinstance(pending[0].get("id"), str):
        return str(pending[0]["id"])
    if len(records) == 1 and isinstance(records[0].get("id"), str):
        return str(records[0]["id"])
    return None


def _resolve_reconciliation_anomaly_id_from_snapshot(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary,
) -> str | None:
    """Resolve one reconciliation anomaly identifier from the snapshot when the target is clear."""

    anomalies = snapshot.get("reconciliation_anomalies")
    if not isinstance(anomalies, list):
        return None

    records = [record for record in anomalies if isinstance(record, dict)]
    if not records:
        return None

    normalized_content = _searchable_text(operator_content)
    explicit_matches = [
        record
        for record in records
        if (
            isinstance(record.get("description"), str)
            and _text_value_matches_text(str(record["description"]), normalized_content)
        )
        or (
            isinstance(record.get("account_code"), str)
            and _text_value_matches_text(str(record["account_code"]), normalized_content)
        )
    ]
    if len(explicit_matches) == 1 and isinstance(explicit_matches[0].get("id"), str):
        return str(explicit_matches[0]["id"])

    recent_target_id = _resolve_recent_target_id(
        operator_memory=operator_memory,
        snapshot=snapshot,
        target_type="reconciliation_anomaly",
        referential_only=True,
        operator_content=operator_content,
    )
    if recent_target_id is not None:
        return recent_target_id
    if len(records) == 1 and isinstance(records[0].get("id"), str):
        return str(records[0]["id"])
    return None


def _hydrate_commentary_arguments(
    *,
    tool_name: str,
    tool_arguments: dict[str, Any],
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary,
) -> dict[str, Any]:
    """Resolve clear commentary targets from the snapshot for update and approval steps."""

    hydrated = dict(tool_arguments)
    if not isinstance(hydrated.get("report_run_id"), str):
        report_run_id = _resolve_report_run_id_from_snapshot(snapshot=snapshot)
        if report_run_id is not None:
            hydrated["report_run_id"] = report_run_id

    report_run_id = hydrated.get("report_run_id")
    if not isinstance(hydrated.get("section_key"), str):
        section_key = _resolve_commentary_section_from_snapshot(
            snapshot=snapshot,
            operator_content=operator_content,
            report_run_id=report_run_id if isinstance(report_run_id, str) else None,
            prefer_unapproved=tool_name == "approve_commentary",
            operator_memory=operator_memory,
        )
        if section_key is not None:
            hydrated["section_key"] = section_key
    return hydrated


def _hydrate_export_arguments(
    *,
    tool_arguments: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Resolve the active export target from the snapshot when the intent is clear."""

    hydrated = dict(tool_arguments)
    if not isinstance(hydrated.get("export_id"), str):
        export_id = _resolve_export_id_from_snapshot(snapshot=snapshot)
        if export_id is not None:
            hydrated["export_id"] = export_id
    return hydrated


def _resolve_export_id_from_snapshot(*, snapshot: dict[str, Any]) -> str | None:
    """Return the latest completed export identifier when the workspace has a clear target."""

    exports = snapshot.get("exports")
    if not isinstance(exports, list):
        return None

    records = [record for record in exports if isinstance(record, dict)]
    if not records:
        return None

    completed = [
        record
        for record in records
        if str(record.get("status") or "") == "completed"
        and isinstance(record.get("id"), str)
    ]
    if completed:
        return str(completed[0]["id"])

    if len(records) == 1 and isinstance(records[0].get("id"), str):
        return str(records[0]["id"])
    return None


def _resolve_report_run_id_from_snapshot(*, snapshot: dict[str, Any]) -> str | None:
    """Resolve the latest report run identifier from the snapshot when appropriate."""

    report_runs = snapshot.get("report_runs")
    if not isinstance(report_runs, list) or not report_runs:
        return None

    records = [record for record in report_runs if isinstance(record, dict)]
    if not records:
        return None
    if isinstance(records[0].get("id"), str):
        return str(records[0]["id"])
    return None


def _resolve_commentary_section_from_snapshot(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    report_run_id: str | None,
    prefer_unapproved: bool,
    operator_memory: AgentMemorySummary,
) -> str | None:
    """Resolve one commentary section key from the snapshot when the target is clear."""

    commentary = snapshot.get("commentary")
    if not isinstance(commentary, list):
        return None

    records = [
        record
        for record in commentary
        if isinstance(record, dict)
        and (
            report_run_id is None
            or str(record.get("report_run_id") or "") == report_run_id
        )
    ]
    if not records:
        return None

    normalized_content = _searchable_text(operator_content)
    explicit_matches = [
        record
        for record in records
        if isinstance(record.get("section_key"), str)
        and _commentary_section_matches_text(
            section_key=str(record["section_key"]),
            normalized_text=normalized_content,
        )
    ]
    if len(explicit_matches) == 1 and isinstance(explicit_matches[0].get("section_key"), str):
        return str(explicit_matches[0]["section_key"])

    recent_target_id = _resolve_recent_target_id(
        operator_memory=operator_memory,
        snapshot=snapshot,
        target_type="commentary",
        referential_only=True,
        operator_content=operator_content,
    )
    if recent_target_id is not None:
        for record in records:
            if str(record.get("id") or "") == recent_target_id and isinstance(
                record.get("section_key"), str
            ):
                return str(record["section_key"])

    if prefer_unapproved:
        pending = [
            record
            for record in records
            if str(record.get("status") or "") != "approved"
        ]
        if len(pending) == 1 and isinstance(pending[0].get("section_key"), str):
            return str(pending[0]["section_key"])

    unique_sections = {
        str(record["section_key"])
        for record in records
        if isinstance(record.get("section_key"), str)
    }
    if len(unique_sections) == 1:
        return next(iter(unique_sections))
    return None


def _hydrate_create_workspace_arguments(
    *,
    tool_arguments: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Fill missing workspace-creation defaults from the current workspace snapshot."""

    hydrated = dict(tool_arguments)
    workspace = snapshot.get("workspace")
    if not isinstance(workspace, dict):
        return hydrated

    if not isinstance(hydrated.get("base_currency"), str) and isinstance(
        workspace.get("base_currency"), str
    ):
        hydrated["base_currency"] = workspace["base_currency"]
    if not isinstance(hydrated.get("country_code"), str) and isinstance(
        workspace.get("country_code"), str
    ):
        hydrated["country_code"] = workspace["country_code"]
    if not isinstance(hydrated.get("timezone"), str) and isinstance(
        workspace.get("timezone"), str
    ):
        hydrated["timezone"] = workspace["timezone"]
    if not isinstance(hydrated.get("autonomy_mode"), str) and isinstance(
        workspace.get("autonomy_mode"), str
    ):
        hydrated["autonomy_mode"] = workspace["autonomy_mode"]
    return hydrated


def _hydrate_workspace_arguments(
    *,
    tool_name: str,
    tool_arguments: dict[str, Any],
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary,
) -> dict[str, Any]:
    """Resolve workspace targets and defaults from the current snapshot."""

    hydrated = dict(tool_arguments)
    workspace_id = hydrated.get("workspace_id")
    if not isinstance(workspace_id, str):
        resolved_workspace_id = _resolve_workspace_id_from_snapshot(
            snapshot=snapshot,
            operator_content=operator_content,
            operator_memory=operator_memory,
        )
        if resolved_workspace_id is not None:
            hydrated["workspace_id"] = resolved_workspace_id
    if tool_name == "update_workspace" and not isinstance(hydrated.get("workspace_id"), str):
        workspace = snapshot.get("workspace")
        if isinstance(workspace, dict) and isinstance(workspace.get("id"), str):
            hydrated["workspace_id"] = str(workspace["id"])
    return hydrated


def _resolve_workspace_id_from_snapshot(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary,
) -> str | None:
    """Resolve one workspace identifier from the snapshot when the target is clear."""

    normalized_content = _searchable_text(operator_content)
    workspaces = snapshot.get("accessible_workspaces")
    current_workspace = snapshot.get("workspace")
    records = (
        [record for record in workspaces if isinstance(record, dict)]
        if isinstance(workspaces, list)
        else []
    )
    if isinstance(current_workspace, dict):
        records = [current_workspace, *records]
    deduped_records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for record in records:
        record_id = record.get("id")
        if not isinstance(record_id, str) or record_id in seen_ids:
            continue
        seen_ids.add(record_id)
        deduped_records.append(record)
    records = deduped_records

    if not records:
        return None

    explicit_matches = [
        record
        for record in records
        if (
            isinstance(record.get("name"), str)
            and _workspace_name_matches_text(str(record["name"]), normalized_content)
        )
        or (
            isinstance(record.get("legal_name"), str)
            and _workspace_name_matches_text(str(record["legal_name"]), normalized_content)
        )
    ]
    if len(explicit_matches) == 1 and isinstance(explicit_matches[0].get("id"), str):
        return str(explicit_matches[0]["id"])

    keyword_matches = [
        record
        for record in records
        if (
            isinstance(record.get("name"), str)
            and _workspace_name_keyword_matches_text(str(record["name"]), normalized_content)
        )
        or (
            isinstance(record.get("legal_name"), str)
            and _workspace_name_keyword_matches_text(
                str(record["legal_name"]), normalized_content
            )
        )
    ]
    if len(keyword_matches) == 1 and isinstance(keyword_matches[0].get("id"), str):
        return str(keyword_matches[0]["id"])

    recent_target_id = _resolve_recent_target_id(
        operator_memory=operator_memory,
        snapshot=snapshot,
        target_type="workspace",
        referential_only=True,
        operator_content=operator_content,
    )
    if recent_target_id is not None:
        return recent_target_id

    if any(
        phrase in normalized_content
        for phrase in ("this workspace", "current workspace", "this entity", "current entity")
    ) and isinstance(current_workspace, dict) and isinstance(current_workspace.get("id"), str):
        return str(current_workspace["id"])

    if len(records) == 1 and isinstance(records[0].get("id"), str):
        return str(records[0]["id"])
    return None


def _hydrate_delete_close_run_arguments(
    *,
    tool_arguments: dict[str, Any],
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary,
) -> dict[str, Any]:
    """Resolve one close run identifier from the current snapshot when the target is clear."""

    hydrated = dict(tool_arguments)
    if isinstance(hydrated.get("close_run_id"), str):
        return hydrated

    resolved_close_run_id = _resolve_close_run_id_from_snapshot(
        snapshot=snapshot,
        operator_content=operator_content,
        operator_memory=operator_memory,
    )
    if resolved_close_run_id is not None:
        hydrated["close_run_id"] = resolved_close_run_id
    return hydrated


def _hydrate_reopen_close_run_arguments(
    *,
    tool_arguments: dict[str, Any],
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary,
) -> dict[str, Any]:
    """Resolve one released close run for reopening when the workspace target is clear."""

    hydrated = dict(tool_arguments)
    if isinstance(hydrated.get("close_run_id"), str):
        return hydrated

    resolved_close_run_id = _resolve_reopen_close_run_id_from_snapshot(
        snapshot=snapshot,
        operator_content=operator_content,
        operator_memory=operator_memory,
    )
    if resolved_close_run_id is not None:
        hydrated["close_run_id"] = resolved_close_run_id
    return hydrated


def _resolve_reopen_close_run_id_from_snapshot(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary,
) -> str | None:
    """Resolve a released close run target from workspace context for reopen requests."""

    resolved_close_run_id = _resolve_close_run_id_from_snapshot(
        snapshot=snapshot,
        operator_content=operator_content,
        operator_memory=operator_memory,
    )
    if resolved_close_run_id is not None:
        return resolved_close_run_id

    close_runs = snapshot.get("entity_close_runs")
    if not isinstance(close_runs, list):
        return None
    released_records = [
        record
        for record in close_runs
        if isinstance(record, dict)
        and str(record.get("status") or "") in {"approved", "exported", "archived"}
        and isinstance(record.get("id"), str)
    ]
    if len(released_records) == 1:
        return str(released_records[0]["id"])
    return None


def _resolve_close_run_id_from_snapshot(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
    operator_memory: AgentMemorySummary,
) -> str | None:
    """Resolve one close run identifier from the workspace snapshot when the target is clear."""

    close_runs = snapshot.get("entity_close_runs")
    if not isinstance(close_runs, list):
        return None

    records = [record for record in close_runs if isinstance(record, dict)]
    if not records:
        return None

    normalized_content = _searchable_text(operator_content)
    inferred_period = _infer_close_run_period_from_text(operator_content)
    explicit_matches = [
        record
        for record in records
        if (
            isinstance(record.get("period_label"), str)
            and _text_value_matches_text(str(record["period_label"]), normalized_content)
        )
        or (
            inferred_period is not None
            and record.get("period_start") == inferred_period[0]
            and record.get("period_end") == inferred_period[1]
        )
        or (
            isinstance(record.get("active_phase"), str)
            and _text_value_matches_text(
                str(record["active_phase"]).replace("_", " "),
                normalized_content,
            )
        )
    ]
    if len(explicit_matches) == 1 and isinstance(explicit_matches[0].get("id"), str):
        return str(explicit_matches[0]["id"])

    recent_target_id = _resolve_recent_target_id(
        operator_memory=operator_memory,
        snapshot=snapshot,
        target_type="close_run",
        referential_only=True,
        operator_content=operator_content,
    )
    if recent_target_id is not None:
        return recent_target_id

    current_close_run_id = snapshot.get("close_run_id")
    if any(
        phrase in normalized_content
        for phrase in ("this close run", "current close run", "this run", "current run")
    ) and isinstance(current_close_run_id, str):
        return current_close_run_id

    if len(records) == 1 and isinstance(records[0].get("id"), str):
        return str(records[0]["id"])
    return None


def _text_value_matches_text(value: str, normalized_text: str) -> bool:
    """Return whether a text field is clearly referenced in free-form operator text."""

    normalized_value = _searchable_text(value)
    if not normalized_value:
        return False
    if normalized_value in normalized_text:
        return True
    tokens = [token for token in normalized_value.split() if len(token) > 2]
    if len(tokens) < 2:
        return False
    return all(token in normalized_text for token in tokens[:4])


def _workspace_name_matches_text(value: str, normalized_text: str) -> bool:
    """Return whether a workspace name is clearly referenced without requiring suffixes."""

    normalized_value = _searchable_text(value)
    if not normalized_value:
        return False
    if normalized_value in normalized_text:
        return True

    tokens = [
        token
        for token in normalized_value.split()
        if len(token) > 2 and token not in {"ltd", "limited", "plc", "inc", "llc"}
    ]
    if len(tokens) < 2:
        return False
    return all(token in normalized_text for token in tokens[:2])


def _workspace_name_keyword_matches_text(value: str, normalized_text: str) -> bool:
    """Return whether a distinctive first workspace token is named by the operator."""

    normalized_value = _searchable_text(value)
    if not normalized_value:
        return False
    tokens = [
        token
        for token in normalized_value.split()
        if len(token) > 2 and token not in {"ltd", "limited", "plc", "inc", "llc"}
    ]
    if not tokens:
        return False
    first_token = tokens[0]
    return len(first_token) >= 4 and first_token in normalized_text


def _infer_journal_posting_target(operator_content: str) -> str:
    """Infer the canonical journal posting target from one operator instruction."""

    normalized = _searchable_text(operator_content)
    if any(token in normalized for token in ("erp", "package", "external", "export")):
        return "external_erp_package"
    return "internal_ledger"


def _infer_reconciliation_disposition(operator_content: str) -> str | None:
    """Infer the intended reconciliation disposition from one operator instruction."""

    normalized = _searchable_text(operator_content)
    if "accepted as is" in normalized or "accept as is" in normalized:
        return "accepted_as_is"
    if "pending info" in normalized or "need more info" in normalized:
        return "pending_info"
    if "escalat" in normalized:
        return "escalated"
    if "adjust" in normalized:
        return "adjusted"
    if any(token in normalized for token in ("resolve", "resolved", "clear it")):
        return "resolved"
    return None


def _extract_reason_from_operator_content(
    *,
    operator_content: str,
    fallback: str,
) -> str:
    """Extract a concise reason clause from operator text or return a canonical fallback."""

    normalized = operator_content.strip()
    if not normalized:
        return fallback

    match = re.search(r"\b(?:because|due to|since|as)\b\s+(.+)$", normalized, re.IGNORECASE)
    if match is not None:
        reason = match.group(1).strip(" .,:;")
        if reason:
            return reason
    return fallback


def _commentary_section_matches_text(*, section_key: str, normalized_text: str) -> bool:
    """Return whether one commentary section is clearly referenced in free-form text."""

    aliases = {
        ReportSectionKey.PROFIT_AND_LOSS.value: (
            "profit and loss",
            "p and l",
            "income statement",
        ),
        ReportSectionKey.BALANCE_SHEET.value: (
            "balance sheet",
            "statement of financial position",
        ),
        ReportSectionKey.CASH_FLOW.value: (
            "cash flow",
            "cashflow",
        ),
        ReportSectionKey.BUDGET_VARIANCE.value: (
            "budget variance",
            "variance",
        ),
        ReportSectionKey.KPI_DASHBOARD.value: (
            "kpi",
            "dashboard",
            "metrics",
        ),
    }
    for alias in aliases.get(section_key, (section_key.replace("_", " "),)):
        if _searchable_text(alias) in normalized_text:
            return True
    return False


def _filename_matches_text(filename: str, normalized_text: str) -> bool:
    """Return whether a filename is clearly referenced in free-form operator text."""

    normalized_filename = _searchable_text(filename)
    if normalized_filename and normalized_filename in normalized_text:
        return True
    stem = normalized_filename.rsplit(".", 1)[0]
    stem_tokens = [token for token in stem.split() if len(token) > 2]
    if len(stem_tokens) < 2:
        return False
    return all(token in normalized_text for token in stem_tokens[:4])


def _searchable_text(value: object) -> str:
    """Normalize text for substring matching across chat commands and filenames."""

    if not isinstance(value, str):
        return ""
    lowered = value.lower().replace("\u2011", "-").replace("_", " ")
    return re.sub(r"[^a-z0-9.]+", " ", lowered).strip()


def _humanize_applied_result(applied_result: dict[str, Any]) -> str:
    """Summarize one tool result in plain operator language."""

    tool_name = applied_result.get("tool")
    if not isinstance(tool_name, str):
        return "I finished that step."

    if tool_name == "review_document":
        filename = _optional_result_text(applied_result, "document_filename") or "the document"
        decision = _optional_result_text(applied_result, "decision") or "reviewed"
        if decision == "approved":
            return f"I approved {filename} for this close run."
        if decision == "rejected":
            return f"I marked {filename} as rejected."
        if decision == "needs_info":
            return f"I left {filename} in review and flagged it for more information."
        return f"I updated the review decision for {filename}."

    if tool_name == "review_documents":
        reviewed_count = _optional_result_int(applied_result, "reviewed_count") or 0
        skipped_count = _optional_result_int(applied_result, "skipped_count") or 0
        failed_count = _optional_result_int(applied_result, "failed_count") or 0
        decision = _optional_result_text(applied_result, "decision") or "reviewed"
        summary = (
            f"I marked {reviewed_count} document"
            f"{'' if reviewed_count == 1 else 's'} as {decision.replace('_', ' ')}"
        )
        notes: list[str] = []
        if skipped_count:
            notes.append(f"skipped {skipped_count}")
        if failed_count:
            notes.append(f"{failed_count} failed")
        if notes:
            summary = f"{summary}; {', '.join(notes)}"
        return f"{summary}."

    if tool_name == "ignore_document":
        filename = _optional_result_text(applied_result, "document_filename") or "the document"
        return f"I marked {filename} as ignored for this close run."

    if tool_name == "correct_extracted_field":
        return "I saved the extraction correction and returned the document to review."

    if tool_name == "create_workspace":
        workspace_name = _optional_result_text(applied_result, "workspace_name")
        if workspace_name is not None:
            return f"I created the {workspace_name} workspace."
        return "I created the new workspace."

    if tool_name == "update_workspace":
        workspace_name = _optional_result_text(applied_result, "workspace_name")
        if workspace_name is not None:
            return f"I updated the {workspace_name} workspace settings."
        return "I updated the workspace settings."

    if tool_name == "switch_workspace":
        workspace_name = _optional_result_text(applied_result, "workspace_name")
        if workspace_name is not None:
            return f"I switched this conversation to the {workspace_name} workspace."
        return "I switched this conversation to the requested workspace."

    if tool_name == "delete_workspace":
        workspace_name = _optional_result_text(applied_result, "deleted_workspace_name")
        if workspace_name is not None:
            return f"I deleted the {workspace_name} workspace."
        return "I deleted that workspace."

    if tool_name == "create_close_run":
        workspace_name = _optional_result_text(applied_result, "workspace_name")
        period_start = _optional_result_text(applied_result, "period_start")
        period_end = _optional_result_text(applied_result, "period_end")
        if period_start and period_end:
            if workspace_name is not None:
                return (
                    f"I started a new close run in {workspace_name} for "
                    f"{period_start} to {period_end}."
                )
            return f"I started a new close run for {period_start} to {period_end}."
        if workspace_name is not None:
            return f"I started a new close run in {workspace_name}."
        return "I started a new close run."

    if tool_name == "open_close_run":
        period_start = _optional_result_text(applied_result, "period_start")
        period_end = _optional_result_text(applied_result, "period_end")
        if period_start and period_end:
            return f"I pinned this thread to the close run for {period_start} to {period_end}."
        return "I pinned this thread to that close run."

    if tool_name == "delete_close_run":
        return "I deleted that close run."

    if tool_name == "advance_close_run":
        active_phase = _optional_result_text(applied_result, "active_phase")
        if active_phase is not None:
            return f"I moved the close run into {_format_phase_label(active_phase)}."
        return "I advanced the close run."

    if tool_name == "rewind_close_run":
        active_phase = _optional_result_text(applied_result, "active_phase")
        if active_phase is not None:
            return f"I moved the close run back to {_format_phase_label(active_phase)}."
        return "I moved the close run back to the earlier workflow step you asked for."

    if tool_name == "reopen_close_run":
        version_no = applied_result.get("version_no")
        if isinstance(version_no, int):
            return f"I reopened this close run as working version {version_no}."
        return "I reopened this close run as a working version."

    if tool_name == "approve_close_run":
        return "I approved this close run."

    if tool_name == "archive_close_run":
        return "I archived this close run."

    if tool_name == "generate_recommendations":
        queued_count = applied_result.get("queued_count")
        if isinstance(queued_count, int):
            return (
                f"I queued recommendation generation for {queued_count} document"
                f"{'' if queued_count == 1 else 's'}."
            )
        return "I queued recommendation generation."

    if tool_name == "run_reconciliation":
        return "I started reconciliation for this close run."

    if tool_name == "generate_reports":
        return "I started report generation for this close run."

    if tool_name == "generate_export":
        if isinstance(applied_result.get("job_id"), str) or isinstance(
            applied_result.get("async_job_group"),
            dict,
        ):
            return "I started packaging the export for this close run."
        return "I generated the export package for this close run."

    if tool_name == "assemble_evidence_pack":
        if isinstance(applied_result.get("job_id"), str) or isinstance(
            applied_result.get("async_job_group"),
            dict,
        ):
            return "I started assembling the evidence pack for this close run."
        return "I assembled the evidence pack for this close run."

    if tool_name == "distribute_export":
        recipient = _optional_result_text(applied_result, "recipient_name")
        if recipient is not None:
            return f"I recorded the export distribution for {recipient}."
        return "I recorded the export distribution."

    if tool_name == "update_commentary":
        section_key = _optional_result_text(applied_result, "section_key")
        if section_key is not None:
            return f"I updated the {_format_report_section_label(section_key)} commentary."
        return "I updated the report commentary."

    if tool_name == "approve_commentary":
        section_key = _optional_result_text(applied_result, "section_key")
        if section_key is not None:
            return f"I approved the {_format_report_section_label(section_key)} commentary."
        return "I approved the report commentary."

    if tool_name == "approve_recommendation":
        if _optional_result_text(applied_result, "journal_id") is not None:
            return "I approved that recommendation and created the journal draft."
        return "I approved that recommendation."

    if tool_name == "reject_recommendation":
        return "I rejected that recommendation."

    if tool_name == "approve_journal":
        journal_number = _optional_result_text(applied_result, "journal_number")
        if journal_number is not None:
            return f"I approved journal {journal_number}."
        return "I approved that journal draft."

    if tool_name == "apply_journal":
        journal_number = _optional_result_text(applied_result, "journal_number")
        posting_target = _optional_result_text(applied_result, "posting_target")
        target_suffix = (
            f" through {posting_target.replace('_', ' ')}"
            if posting_target is not None
            else ""
        )
        if journal_number is not None:
            return f"I applied journal {journal_number}{target_suffix}."
        return f"I applied that journal{target_suffix}."

    if tool_name == "reject_journal":
        journal_number = _optional_result_text(applied_result, "journal_number")
        if journal_number is not None:
            return f"I rejected journal {journal_number}."
        return "I rejected that journal draft."

    if tool_name == "approve_reconciliation":
        status = _optional_result_text(applied_result, "status")
        reconciliation_type = _optional_result_text(applied_result, "reconciliation_type")
        if status == "blocked":
            if reconciliation_type is not None:
                return (
                    f"I tried to approve the {reconciliation_type.replace('_', ' ')} "
                    "reconciliation, but it is still blocked."
                )
            return "I tried to approve that reconciliation, but it is still blocked."
        if reconciliation_type is not None:
            return (
                f"I approved the {reconciliation_type.replace('_', ' ')} reconciliation."
            )
        return "I approved that reconciliation."

    if tool_name == "disposition_reconciliation_item":
        source_ref = _optional_result_text(applied_result, "source_ref")
        disposition = _optional_result_text(applied_result, "disposition")
        if source_ref is not None and disposition is not None:
            return (
                f"I marked reconciliation item {source_ref} as "
                f"{disposition.replace('_', ' ')}."
            )
        return "I recorded the reconciliation disposition."

    if tool_name == "resolve_reconciliation_anomaly":
        description = _optional_result_text(applied_result, "description")
        if description is not None:
            return f"I resolved the reconciliation anomaly: {description}."
        return "I resolved that reconciliation anomaly."

    summary = _summarize_applied_result(applied_result)
    if summary is not None:
        return summary[:1].upper() + summary[1:] + ("." if not summary.endswith(".") else "")
    return f"I completed the {tool_name.replace('_', ' ')} step."


def _format_next_step(snapshot: dict[str, Any] | None) -> str | None:
    """Return one short next-step suggestion from the post-action snapshot."""

    if snapshot is None:
        return None
    readiness = snapshot.get("readiness")
    if not isinstance(readiness, dict):
        return None
    next_actions = readiness.get("next_actions")
    if not isinstance(next_actions, list):
        return None
    for action in next_actions:
        if not isinstance(action, str):
            continue
        cleaned = action.strip()
        if not cleaned or cleaned.startswith("Ask the agent"):
            continue
        if _readiness_action_requires_operator_input(cleaned):
            return f"Next, {_lowercase_leading_character(cleaned)}"
        return f"Next, I can {_lowercase_leading_character(cleaned)}"
    return None


def _readiness_action_requires_operator_input(action: str) -> bool:
    """Return whether a suggested action depends on operator-supplied data or files."""

    normalized = _searchable_text(action)
    if not normalized:
        return False
    if any(
        phrase in normalized
        for phrase in (
            "upload a production chart of accounts",
            "upload a production coa",
            "upload source documents",
            "upload a gl",
            "upload gl",
            "upload a cashbook",
            "upload cashbook",
            "upload a trial balance",
            "upload trial balance",
            "attach a file",
            "provide a file",
        )
    ):
        return True
    return normalized.startswith(("upload ", "provide ", "attach ", "import "))


def _should_suppress_generic_next_step(
    *,
    operator_content: str,
    last_tool_name: str | None,
) -> bool:
    """Return whether a readiness next-step would distract from the current turn."""

    if last_tool_name in {
        "create_workspace",
        "switch_workspace",
        "update_workspace",
        "delete_workspace",
    }:
        return True
    normalized_content = _searchable_text(operator_content)
    if not normalized_content:
        return False
    if _is_document_upload_status_request(operator_content):
        return True
    if "workspace" not in normalized_content and "entity" not in normalized_content:
        return _is_capability_boundary_follow_up(normalized_content)
    return any(
        token in normalized_content
        for token in (
            "create",
            "add",
            "open",
            "start",
            "new",
            "another",
            "delete",
            "remove",
            "switch",
            "rename",
            "update",
        )
    ) or _is_capability_boundary_follow_up(normalized_content)


def _is_capability_boundary_follow_up(normalized_content: str) -> bool:
    """Return whether the operator is challenging an asserted capability boundary."""

    if not any(
        token in normalized_content
        for token in ("coa", "chart of accounts", "charts of accounts", "file", "upload")
    ):
        return False
    return any(
        phrase in normalized_content
        for phrase in (
            "where will you get",
            "where would you get",
            "where do you get",
            "where can you get",
            "how will you",
            "how would you",
            "can you actually",
            "can you upload",
            "will you upload",
            "you said",
            "you claimed",
        )
    )


def _is_terminal_workspace_admin_tool(tool_name: str | None) -> bool:
    """Return whether one workspace-admin mutation should end the current turn."""

    return tool_name in {
        "create_workspace",
        "switch_workspace",
        "update_workspace",
        "delete_workspace",
    }


def _should_continue_after_workspace_admin_tool(
    *,
    tool_name: str | None,
    operator_content: str,
) -> bool:
    """Return whether a workspace-scope change is only the first step in the turn."""

    if tool_name not in {"switch_workspace", "create_workspace", "update_workspace"}:
        return False
    normalized_content = _searchable_text(operator_content)
    if not normalized_content:
        return False
    if " and " not in f" {normalized_content} ":
        return False
    return any(
        phrase in normalized_content
        for phrase in (
            "tell me",
            "more about",
            "summarize",
            "summary",
            "overview",
            "details",
            "describe",
            "explain",
            "status",
            "current state",
            "what",
            "show",
            "list",
            "open",
            "create",
            "start",
            "run",
            "generate",
            "process",
            "review",
            "approve",
            "reconcile",
            "report",
            "export",
        )
    )


def _build_operator_failure_message(
    *,
    error: ChatActionExecutionError,
    tool_name: str | None,
) -> str:
    """Return a natural assistant failure message instead of a raw exception string."""

    tool_phrase = (
        f"the {tool_name.replace('_', ' ')} step"
        if isinstance(tool_name, str) and tool_name.strip()
        else "that request"
    )
    normalized_message = _normalize_operator_facing_text(error.message)
    searchable_message = _searchable_text(normalized_message)
    if error.code is ChatActionExecutionErrorCode.ACCESS_DENIED or any(
        phrase in searchable_message
        for phrase in (
            "not a member",
            "not accessible",
            "access denied",
            "permission",
            "not authorized",
        )
    ):
        return (
            "I couldn't access the workspace or record needed for that request. "
            "I didn't make any changes. If this should be available to you, ask a workspace "
            "owner to grant access; otherwise tell me which accessible workspace to use."
        )
    if error.code is ChatActionExecutionErrorCode.THREAD_NOT_FOUND:
        return (
            "I couldn't find this chat thread in the selected workspace. It may have moved "
            "to another workspace or been deleted. Open the thread from the global assistant "
            "or start a new chat and I can continue from the accessible workspace state."
        )
    if error.code is ChatActionExecutionErrorCode.EXECUTION_FAILED:
        if normalized_message:
            return (
                f"I hit a system error while running {tool_phrase}. "
                f"I didn't make further changes. {normalized_message}"
            )
        return (
            f"I hit a system error while running {tool_phrase}. "
            "I didn't make further changes."
        )
    if "is not registered" in normalized_message:
        return (
            "I couldn't line up the exact workflow step for that request yet. "
            "Tell me the workspace or action you want me to take and I'll handle it here."
        )
    if normalized_message:
        return f"I couldn't finish {tool_phrase} yet. {normalized_message}"
    return f"I couldn't finish {tool_phrase} yet."


def _build_unexpected_operator_failure_message(*, error: Exception) -> str:
    """Return a concise diagnostic for an unexpected operator runtime failure."""

    error_name = type(error).__name__
    detail = _normalize_operator_facing_text(str(error))
    if detail:
        return f"Unexpected {error_name}: {detail}"
    return f"Unexpected {error_name}."


def _build_failure_next_step(snapshot: dict[str, Any] | None) -> str | None:
    """Return one grounded recovery suggestion after an operator-side failure."""

    next_step = _format_next_step(snapshot)
    if next_step is not None:
        return next_step
    if snapshot is None:
        return None
    readiness = snapshot.get("readiness")
    if not isinstance(readiness, dict):
        return None
    blockers = readiness.get("blockers")
    if isinstance(blockers, list):
        for blocker in blockers:
            if isinstance(blocker, str) and blocker.strip():
                return f"First, we need to clear this blocker: {blocker.strip()}"
    return None


def _extract_async_job_group(*, applied_result: dict[str, Any]) -> dict[str, str | int] | None:
    """Return normalized async-group metadata from one applied tool result when present."""

    raw_group = applied_result.get("async_job_group")
    if not isinstance(raw_group, dict):
        return None
    continuation_group_id = raw_group.get("continuation_group_id")
    job_count = raw_group.get("job_count")
    if not isinstance(continuation_group_id, str) or not continuation_group_id.strip():
        return None
    if not isinstance(job_count, int) or job_count <= 0:
        return None
    try:
        UUID(continuation_group_id)
    except ValueError:
        return None
    return {
        "continuation_group_id": continuation_group_id,
        "job_count": job_count,
    }


def _build_async_wait_message(*, applied_result: dict[str, Any]) -> str:
    """Return one operator-facing message for a queued async workflow step."""

    summary = _humanize_applied_result(applied_result)
    return (
        f"{summary} I'll keep going automatically as soon as that background work finishes."
    )


def _build_resume_operator_prompt(
    *,
    objective: str,
    completed_jobs: tuple[JobRecord, ...],
) -> str:
    """Build the hidden operator prompt used when async background work finishes."""

    cleaned_objective = objective.strip()
    status_counts: dict[str, int] = {}
    blocker_notes: list[str] = []
    for job in completed_jobs:
        status_counts[job.status.value] = status_counts.get(job.status.value, 0) + 1
        if job.status.value in {"blocked", "failed", "canceled"}:
            reason = (
                job.blocking_reason
                or job.failure_reason
                or "Background work stopped without a detailed reason."
            )
            blocker_notes.append(
                f"{job.task_name.replace('.', ' ')}: {reason}"
            )

    status_summary = ", ".join(
        f"{count} {status.replace('_', ' ')}"
        for status, count in sorted(status_counts.items())
    )
    blocker_summary = " ".join(blocker_notes[:2])
    continuation_note = (
        "Background work for this request has reached a terminal state. "
        f"Job summary: {status_summary}."
    )
    if blocker_summary:
        continuation_note = f"{continuation_note} Issues: {blocker_summary}"

    return (
        f"{cleaned_objective}\n\n"
        f"{continuation_note} Continue the same operator request using the updated workspace "
        "state. Do not ask the operator to repeat the request. If the original objective was "
        "autonomous or end-to-end close processing, continue to the next available workflow "
        "step and report any completed, skipped, failed, or blocked work."
    )


def _lowercase_leading_character(value: str) -> str:
    """Lowercase the first letter of a sentence while preserving the rest."""

    if not value:
        return value
    return value[:1].lower() + value[1:]


def _optional_result_text(applied_result: dict[str, Any], key: str) -> str | None:
    """Return one string result field when present."""

    value = applied_result.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _optional_result_int(applied_result: dict[str, Any], key: str) -> int | None:
    """Return one integer result field when present."""

    value = applied_result.get(key)
    return value if isinstance(value, int) else None


def _format_report_section_label(section_key: str) -> str:
    """Return a human-readable label for one canonical report section key."""

    for section in ReportSectionKey:
        if section.value == section_key:
            return section.label
    return section_key.replace("_", " ")


def _coerce_execution_error(error: Exception) -> ChatActionExecutionError:
    """Map one domain error into the canonical chat-execution error contract."""

    if isinstance(error, ChatActionExecutionError):
        return error

    status_code = getattr(error, "status_code", 422)
    message = (
        getattr(error, "message", str(error)).strip()
        or "The requested action stopped before it finished."
    )
    code = (
        ChatActionExecutionErrorCode.INVALID_ACTION_PLAN
        if status_code < 500
        else ChatActionExecutionErrorCode.EXECUTION_FAILED
    )
    return ChatActionExecutionError(
        status_code=status_code,
        code=code,
        message=message,
    )


def _json_safe_payload(value: Any) -> Any:
    """Return a JSON-serializable copy of tool payloads and execution results."""

    return json.loads(json.dumps(value, default=str))


def _build_turn_metadata(
    *,
    metadata: dict[str, Any] | None,
    client_turn_id: str | None,
    turn_status: str,
) -> dict[str, Any] | None:
    """Attach stable retry metadata to one persisted chat message."""

    if client_turn_id is None:
        return metadata
    return {
        **dict(metadata or {}),
        "chat_turn_id": client_turn_id,
        "turn_status": turn_status,
    }


def _build_recovered_turn_summary(*, action: ChatActionPlanRecord) -> str:
    """Build a truthful response from the durable action ledger after a retry."""

    if action.status == "pending":
        return (
            "I already prepared that action and it is waiting for confirmation before "
            "anything is applied."
        )
    if action.applied_result is not None:
        return (
            _summarize_applied_result(action.applied_result)
            or "I already completed that action."
        )
    return "I already staged that action, but the stored result is not available."


def _with_thread_approval_policy(
    *,
    context_payload: dict[str, Any],
    mode: str,
    actor_user_id: UUID,
    reason: str | None,
) -> dict[str, Any]:
    """Return context payload with an explicit scoped approval policy."""

    payload = dict(context_payload)
    payload[_THREAD_APPROVAL_POLICY_KEY] = {
        "mode": mode,
        "actor_user_id": str(actor_user_id),
        "reason": reason,
        "updated_at": utc_now().isoformat(),
        "never_auto_approve_tools": sorted(_NEVER_AUTO_APPROVE_TOOLS),
    }
    return payload


def _resolve_preferred_explanation_depth(
    *,
    existing_value: object,
    operator_message: str | None,
) -> str:
    """Infer the operator's preferred explanation depth from recent instructions."""

    if isinstance(operator_message, str):
        normalized = operator_message.strip().lower()
        if any(token in normalized for token in ("detailed", "detail", "thorough", "deep dive")):
            return "detailed"
        if any(
            token in normalized
            for token in ("brief", "short", "quick", "concise", "summary only")
        ):
            return "brief"
    if isinstance(existing_value, str) and existing_value.strip():
        return existing_value
    return DEFAULT_PREFERRED_EXPLANATION_DEPTH


def _resolve_preferred_confirmation_style(
    *,
    existing_value: object,
    operator_message: str | None,
) -> str:
    """Infer the operator's confirmation preference from recent instructions."""

    if isinstance(operator_message, str):
        normalized = operator_message.strip().lower()
        if any(
            token in normalized
            for token in (
                "ask before",
                "confirm first",
                "before you do anything",
                "don't do it yet",
            )
        ):
            return "confirm_before_destructive"
        if any(
            token in normalized
            for token in ("just do it", "go ahead", "handle it", "take care of it")
        ):
            return "direct_when_clear"
    if isinstance(existing_value, str) and existing_value.strip():
        return existing_value
    return DEFAULT_PREFERRED_CONFIRMATION_STYLE


def _truncate_text(value: str | None, *, limit: int = 500) -> str | None:
    """Truncate long text before persisting it into compact thread memory."""

    if value is None:
        return None
    normalized = value.strip()
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1].rstrip()}…"


def _summarize_applied_result(applied_result: dict[str, Any] | None) -> str | None:
    """Return a short summary string for trace metadata."""

    if applied_result is None:
        return None
    parts: list[str] = []
    tool_name = applied_result.get("tool")
    if isinstance(tool_name, str):
        parts.append(f"{tool_name} completed")
    else:
        parts.append("Action completed")

    version_no = applied_result.get("version_no")
    if isinstance(applied_result.get("reopened_close_run_id"), str):
        if isinstance(version_no, int):
            parts.append(f"reopened as version {version_no}")
        else:
            parts.append("reopened into a working version")
    elif isinstance(applied_result.get("switched_workspace_id"), str):
        parts.append("switched the conversation workspace")
    elif isinstance(applied_result.get("created_close_run_id"), str):
        if isinstance(version_no, int):
            parts.append(f"started close run version {version_no}")
        else:
            parts.append("started a new close run")
    elif isinstance(applied_result.get("opened_close_run_id"), str):
        parts.append("pinned the conversation to the close run")
    elif isinstance(applied_result.get("deleted_close_run_id"), str):
        parts.append("deleted the close run")

    rewound_from_phase = applied_result.get("rewound_from_phase")
    active_phase = applied_result.get("active_phase")
    if isinstance(rewound_from_phase, str) and isinstance(active_phase, str):
        parts.append(
            f"moved from {_format_phase_label(rewound_from_phase)} "
            f"to {_format_phase_label(active_phase)}"
        )

    return "; ".join(parts)


def _optional_uuid_from_result(*, applied_result: dict[str, Any], key: str) -> UUID | None:
    """Parse one optional UUID value from an execution result payload."""

    raw_value = applied_result.get(key)
    if not isinstance(raw_value, str):
        return None
    try:
        return UUID(raw_value)
    except ValueError:
        return None


def _optional_uuid_from_arguments(*, arguments: dict[str, Any], key: str) -> UUID | None:
    """Parse one optional UUID value from a planned tool argument payload."""

    raw_value = arguments.get(key)
    if not isinstance(raw_value, str):
        return None
    try:
        return UUID(raw_value)
    except ValueError:
        return None


def _optional_uuid_from_payload(*, payload: dict[str, Any], key: str) -> UUID | None:
    """Parse one optional UUID value from a stored action payload."""

    raw_value = payload.get(key)
    if not isinstance(raw_value, str):
        return None
    try:
        return UUID(raw_value)
    except ValueError:
        return None


def _build_scope_handoff_message(*, applied_result: dict[str, Any]) -> str | None:
    """Return an operator-facing note when a tool reopens, creates, or rewinds workflow scope."""

    reopened_close_run_id = applied_result.get("reopened_close_run_id")
    opened_close_run_id = applied_result.get("opened_close_run_id")
    created_close_run_id = applied_result.get("created_close_run_id")
    deleted_close_run_id = applied_result.get("deleted_close_run_id")
    reopened_from_status = applied_result.get("reopened_from_status")
    version_no = applied_result.get("version_no")
    rewound_from_phase = applied_result.get("rewound_from_phase")
    active_phase = applied_result.get("active_phase")
    period_start = applied_result.get("period_start")
    period_end = applied_result.get("period_end")
    workspace_name = _optional_result_text(applied_result, "workspace_name")

    notes: list[str] = []
    if isinstance(reopened_close_run_id, str):
        if isinstance(version_no, int):
            status_label = _format_close_run_status_label(reopened_from_status)
            if status_label is not None:
                notes.append(
                    "I reopened the "
                    f"{status_label.lower()} close run as working version {version_no}."
                )
            else:
                notes.append(f"I reopened this close run as working version {version_no}.")
        else:
            notes.append("I reopened this close run as a new working version.")
    elif isinstance(opened_close_run_id, str):
        period_suffix = ""
        if isinstance(period_start, str) and isinstance(period_end, str):
            period_suffix = f" for {period_start} to {period_end}"
        workspace_suffix = f" in {workspace_name}" if workspace_name is not None else ""
        notes.append(
            "I pinned this thread to the existing close run"
            f"{workspace_suffix}{period_suffix}."
        )
    elif isinstance(created_close_run_id, str):
        period_suffix = ""
        if isinstance(period_start, str) and isinstance(period_end, str):
            period_suffix = f" for {period_start} to {period_end}"
        workspace_suffix = f" in {workspace_name}" if workspace_name is not None else ""
        if isinstance(version_no, int):
            notes.append(
                f"I started a new close run{workspace_suffix}{period_suffix} "
                f"as working version {version_no}."
            )
        else:
            notes.append(f"I started a new close run{workspace_suffix}{period_suffix}.")
    elif isinstance(deleted_close_run_id, str):
        notes.append(
            "I moved this thread back to the workspace scope because that close run is gone."
        )

    if isinstance(rewound_from_phase, str) and isinstance(active_phase, str):
        notes.append(
            f"I moved the workflow from {_format_phase_label(rewound_from_phase)} "
            f"back to {_format_phase_label(active_phase)} so this request could be applied."
        )
    elif (
        isinstance(reopened_close_run_id, str)
        or isinstance(opened_close_run_id, str)
        or isinstance(created_close_run_id, str)
        or isinstance(deleted_close_run_id, str)
    ) and isinstance(active_phase, str):
        notes.append(
            f"The active phase in that working version is {_format_phase_label(active_phase)}."
        )

    if not notes:
        return None
    return " ".join(notes)


def _format_scope_handoff_message(handoff_message: str | None) -> str:
    """Format one scope-handoff note for assistant-visible chat content."""

    if handoff_message is None:
        return ""
    return f"{handoff_message}\n\n"


def _format_scope_handoff_summary(handoff_message: str | None) -> str:
    """Format one compact handoff suffix for short execution summaries."""

    if handoff_message is None:
        return ""
    return " The workflow scope was adjusted for the request."


def _format_phase_label(value: str) -> str:
    """Resolve one serialized phase value into the operator-facing phase label."""

    try:
        return WorkflowPhase(value).label
    except ValueError:
        return value.replace("_", " ")


def _format_close_run_status_label(value: object) -> str | None:
    """Resolve one serialized close-run status into the operator-facing label."""

    if not isinstance(value, str):
        return None
    try:
        return CloseRunStatus(value).label
    except ValueError:
        return value.replace("_", " ")


__all__ = [
    "ChatActionExecutionError",
    "ChatActionExecutionErrorCode",
    "ChatActionExecutor",
    "ChatExecutionOutcome",
    "McpToolCallOutcome",
]
