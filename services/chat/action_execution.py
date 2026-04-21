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
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

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


_MAX_OPERATOR_LOOP_STEPS = 4
_OPERATOR_PLANNER_POLICY_VERSION = "2026-04-21.operator-planner.v1"
_OPERATOR_CONFIRMATION_POLICY_VERSION = "2026-04-21.operator-confirmation.v1"
_OPERATOR_EVAL_SCHEMA_VERSION = "2026-04-21.operator-eval.v1"
_MCP_MANIFEST_VERSION = "2025-11-25"


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

    def send_action_message(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        actor_user: EntityUserRecord,
        content: str,
        message_grounding_payload: dict[str, Any] | None = None,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> ChatExecutionOutcome:
        """Plan a chat response and optionally execute the selected deterministic tool."""

        return self._run_operator_turn(
            thread_id=thread_id,
            entity_id=entity_id,
            actor_user=actor_user,
            content=content,
            operator_message_for_memory=content,
            message_grounding_payload=message_grounding_payload,
            persist_user_message=True,
            source_surface=source_surface,
            trace_id=trace_id,
        )

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
        message_grounding_payload: dict[str, Any] | None,
        persist_user_message: bool,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> ChatExecutionOutcome:
        """Execute one bounded operator turn with optional user-message persistence."""

        active_entity_id = entity_id
        self._ensure_entity_coa_available(actor_user=actor_user, entity_id=active_entity_id)
        grounding, thread = self._load_thread_context(
            thread_id=thread_id,
            entity_id=active_entity_id,
            user_id=actor_user.id,
        )
        user_message = None
        final_record: ChatActionPlanRecord | None = None
        last_tool_name: str | None = None
        last_action_status = "read_only"
        last_snapshot: dict[str, Any] | None = None
        applied_results: list[dict[str, Any]] = []
        completed_summaries: list[str] = []
        seen_action_signatures: set[str] = set()

        try:
            if persist_user_message:
                user_message = self._chat_repo.create_message(
                    thread_id=thread_id,
                    role="user",
                    content=content,
                    message_type="action",
                    linked_action_id=None,
                    grounding_payload=dict(message_grounding_payload or {}),
                    model_metadata=None,
                )
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
                    thread_context_payload=thread.context_payload,
                    loop_context=loop_context,
                )
                planning = self._hydrate_planning_result(
                    planning=planning,
                    snapshot=last_snapshot,
                    operator_content=content,
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
                            "I completed the useful steps I could safely take in this turn "
                            "and stopped before repeating the same action."
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

                execution_context = self._build_execution_context(
                    actor_user=actor_user,
                    entity_id=active_entity_id,
                    close_run_id=thread.close_run_id,
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
                record = self._action_repo.create_action_plan(
                    thread_id=thread_id,
                    message_id=action_message_id,
                    entity_id=active_entity_id,
                    close_run_id=thread.close_run_id,
                    actor_user_id=actor_user.id,
                    intent=action.tool.intent,
                    target_type=action.target_type,
                    target_id=action.target_id,
                    payload={
                        "tool_name": action.tool.name,
                        "tool_arguments": action.planning.tool_arguments,
                        "assistant_response": action.planning.assistant_response,
                        "reasoning": action.planning.reasoning,
                        "requires_human_approval": requires_human_approval,
                        "loop_iteration": iteration,
                        "turn_objective": _truncate_text(content, limit=300),
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

                if requires_human_approval:
                    assistant_content = _compose_assistant_content(
                        assistant_response=_build_pending_confirmation_message(
                            tool_name=action.tool.name,
                            tool_arguments=action.planning.tool_arguments,
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

                applied_result = self._execute_action(
                    action=action,
                    execution_context=execution_context,
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
                message_grounding_payload=message_grounding_payload,
                persist_user_message=persist_user_message,
                trace_id=trace_id,
                error=error,
                tool_name=last_tool_name,
            )
            if surfaced_outcome is not None:
                return surfaced_outcome
            raise
        except Exception as error:
            self._db_session.rollback()
            raise ChatActionExecutionError(
                status_code=500,
                code=ChatActionExecutionErrorCode.EXECUTION_FAILED,
                message="The chat action could not be completed.",
            ) from error

    def _surface_operator_error_in_thread(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        actor_user: EntityUserRecord,
        content: str,
        operator_message_for_memory: str | None,
        message_grounding_payload: dict[str, Any] | None,
        persist_user_message: bool,
        trace_id: str | None,
        error: ChatActionExecutionError,
        tool_name: str | None,
    ) -> ChatExecutionOutcome | None:
        """Persist a natural assistant failure reply instead of leaking a raw action error."""

        if error.code in {
            ChatActionExecutionErrorCode.THREAD_NOT_FOUND,
            ChatActionExecutionErrorCode.ACCESS_DENIED,
        }:
            return None

        try:
            grounding, thread = self._load_thread_context(
                thread_id=thread_id,
                entity_id=entity_id,
                user_id=actor_user.id,
            )
            if persist_user_message:
                self._chat_repo.create_message(
                    thread_id=thread_id,
                    role="user",
                    content=content,
                    message_type="action",
                    linked_action_id=None,
                    grounding_payload=dict(message_grounding_payload or {}),
                    model_metadata=None,
                )
            snapshot = self._snapshot_for_thread(
                actor_user=actor_user,
                entity_id=entity_id,
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
        applied_result = self._execute_action(
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

        self._chat_repo.create_message(
            thread_id=thread_id,
            role="assistant",
            content=_format_approval_message(
                payload.get("assistant_response"),
                applied_result,
                handoff_message=handoff_message,
                snapshot=(
                    self._snapshot_for_thread(
                        actor_user=actor_user,
                        entity_id=entity_id,
                        close_run_id=thread.close_run_id,
                        thread_id=thread_id,
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
                existing_payload=thread.context_payload,
                operator_message=None,
                assistant_response=(
                    payload.get("assistant_response")
                    if isinstance(payload.get("assistant_response"), str)
                    else None
                ),
                tool_name=action.tool.name,
                action_status="applied",
                trace_id=trace_id,
                snapshot=snapshot,
            )
        self._db_session.commit()
        return updated

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
                tool_name=None,
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
                grounding_payload={"tool_arguments": tool_arguments},
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
                    "tool_arguments": action.planning.tool_arguments,
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
                applied_result = self._execute_action(
                    action=action,
                    execution_context=execution_context,
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
                message="The deterministic tool call could not be completed.",
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

        return self._toolset.requires_human_approval_for_invocation(
            tool_name=action.tool.name,
            tool_arguments=action.planning.tool_arguments,
            context=execution_context,
        )

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
        created_close_run_id = _optional_uuid_from_result(
            applied_result=applied_result,
            key="created_close_run_id",
        )
        deleted_close_run_id = _optional_uuid_from_result(
            applied_result=applied_result,
            key="deleted_close_run_id",
        )
        target_close_run_id = reopened_close_run_id or created_close_run_id
        if (
            switched_workspace_id is None
            and target_close_run_id is None
            and deleted_close_run_id is None
        ):
            return grounding, thread, None

        previous_close_run_id = thread.close_run_id
        if switched_workspace_id is not None:
            workspace_grounding = self._grounding.resolve_context(
                entity_id=switched_workspace_id,
                close_run_id=None,
                user_id=actor_user.id,
            )
            updated_payload = {
                **thread.context_payload,
                **self._grounding.build_context_payload(context=workspace_grounding.context),
            }
            updated_thread = self._chat_repo.update_thread_scope(
                thread_id=thread_id,
                entity_id=switched_workspace_id,
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
            entity_id=entity_id,
            close_run_id=target_close_run_id,
            user_id=actor_user.id,
        )
        updated_payload = {
            **thread.context_payload,
            **self._grounding.build_context_payload(context=reopened_grounding.context),
        }
        updated_thread = self._chat_repo.update_thread_scope(
            thread_id=thread_id,
            entity_id=entity_id,
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
        tool_definition = self._resolve_tool_definition(tool_name=tool_name)
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
        if operator_message is not None and operator_message.strip():
            recent_objectives.append(_truncate_text(operator_message.strip(), limit=160))
        compact_recent_tools = tuple(recent_tool_names[-5:])
        compact_recent_namespaces = tuple(recent_tool_namespaces[-5:])
        compact_recent_objectives = compact_recent_values(recent_objectives, limit=4)
        compact_recent_entities = compact_recent_values(recent_entity_names, limit=4)
        compact_recent_periods = compact_recent_values(recent_period_labels, limit=4)
        active_async_turn = get_active_async_turn(context_payload=existing_payload)
        last_async_turn = (
            dict(existing_payload.get("agent_last_async_turn"))
            if isinstance(existing_payload.get("agent_last_async_turn"), dict)
            else None
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
            raise ChatActionExecutionError(
                status_code=403,
                code=ChatActionExecutionErrorCode.ACCESS_DENIED,
                message="You are not a member of this workspace.",
            )

        thread = self._chat_repo.get_thread_for_entity(thread_id=thread_id, entity_id=entity_id)
        if thread is None:
            raise ChatActionExecutionError(
                status_code=404,
                code=ChatActionExecutionErrorCode.THREAD_NOT_FOUND,
                message="That chat thread does not exist in this workspace.",
            )

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
        thread_context_payload: dict[str, Any],
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

        try:
            return self._kernel.plan(
                instructions=self._build_planner_instructions(
                    grounding=grounding,
                    operator_memory=self._memory_for_thread(
                        thread_id=thread_id,
                        entity_id=entity_id,
                        actor_user_id=actor_user.id,
                        context_payload=thread_context_payload,
                    ),
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
        operator_memory: AgentMemorySummary,
        loop_context: _OperatorLoopContext | None = None,
    ) -> str:
        """Build the system instructions consumed by the generic agent kernel."""

        specialist_lines = [
            (
                f"- {namespace.label} ({namespace.name}) via {namespace.specialist_name}: "
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
                    "changes, you may reopen it and continue inside the same thread."
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
                    "Choose mode=tool when the operator is asking you to make a change, "
                    "trigger a workflow step, approve or reject work, generate an artifact, "
                    "or otherwise do something the registered actions can accomplish."
                ),
                (
                    "Choose mode=read_only for analysis, explanation, status narration, "
                    "missing identifiers, or ambiguous requests."
                ),
                (
                    "Greetings, status checks, and help requests should still return a "
                    "useful read_only response grounded in the current workspace."
                ),
                (
                    "When you choose mode=tool, use only the registered deterministic "
                    "actions and include JSON-safe arguments."
                ),
                (
                    "If a required identifier is missing and there is no single clear target in "
                    "the snapshot, do not invent it. Respond in read_only mode and ask one "
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
                    "Keep the tone natural and teammate-like. Default to short conversational "
                    "paragraphs. Match the operator's preferred explanation depth when it is "
                    "known. Avoid markdown bullets, bold markers, tables, or rigid templates "
                    "unless the operator explicitly asks for structure."
                ),
                (
                    "When you choose mode=tool, the assistant_response must be brief and "
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
                    "Respond in read_only mode and ask the operator to upload a production COA "
                    "from the entity workspace first."
                ),
                (
                    "If source documents are missing, tell the operator they can upload them "
                    "through chat or from the document workspace and that parsing starts "
                    "automatically after upload."
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
                        "that materially advances the same objective, choose mode=tool for "
                        "the single best next action."
                    ),
                    (
                        "If the main objective is now waiting on human approval, asynchronous "
                        "processing, missing inputs, ambiguity, or a blocker, choose mode="
                        "read_only and explain the current state briefly."
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
    ) -> AgentPlanningResult:
        """Normalize operator-facing text and fill missing low-ambiguity tool arguments."""

        normalized_response = _normalize_operator_facing_text(planning.assistant_response)
        if planning.mode != "tool" or planning.tool_name is None:
            return planning.model_copy(update={"assistant_response": normalized_response})

        tool_arguments = dict(planning.tool_arguments)
        if planning.tool_name == "review_document":
            tool_arguments = _hydrate_review_document_arguments(
                tool_arguments=tool_arguments,
                snapshot=snapshot,
                operator_content=operator_content,
            )
        elif planning.tool_name == "ignore_document":
            document_id = _resolve_document_id_from_snapshot(
                snapshot=snapshot,
                operator_content=operator_content,
                preferred_statuses=("needs_review", "uploaded", "processing", "parsed"),
            )
            if document_id is not None and not isinstance(tool_arguments.get("document_id"), str):
                tool_arguments["document_id"] = document_id
        elif planning.tool_name in {"approve_recommendation", "reject_recommendation"}:
            tool_arguments = _hydrate_recommendation_arguments(
                tool_name=planning.tool_name,
                tool_arguments=tool_arguments,
                snapshot=snapshot,
                operator_content=operator_content,
            )
        elif planning.tool_name in {"approve_journal", "apply_journal", "reject_journal"}:
            tool_arguments = _hydrate_journal_arguments(
                tool_name=planning.tool_name,
                tool_arguments=tool_arguments,
                snapshot=snapshot,
                operator_content=operator_content,
            )
        elif planning.tool_name in {
            "approve_reconciliation",
            "disposition_reconciliation_item",
            "resolve_reconciliation_anomaly",
        }:
            tool_arguments = _hydrate_reconciliation_arguments(
                tool_name=planning.tool_name,
                tool_arguments=tool_arguments,
                snapshot=snapshot,
                operator_content=operator_content,
            )
        elif planning.tool_name in {"update_commentary", "approve_commentary"}:
            tool_arguments = _hydrate_commentary_arguments(
                tool_name=planning.tool_name,
                tool_arguments=tool_arguments,
                snapshot=snapshot,
                operator_content=operator_content,
            )
        elif planning.tool_name == "distribute_export":
            tool_arguments = _hydrate_export_arguments(
                tool_arguments=tool_arguments,
                snapshot=snapshot,
            )
        elif planning.tool_name == "create_workspace":
            tool_arguments = _hydrate_create_workspace_arguments(
                tool_arguments=tool_arguments,
                snapshot=snapshot,
            )
        elif planning.tool_name in {"switch_workspace", "update_workspace", "delete_workspace"}:
            tool_arguments = _hydrate_workspace_arguments(
                tool_name=planning.tool_name,
                tool_arguments=tool_arguments,
                snapshot=snapshot,
                operator_content=operator_content,
            )
        elif planning.tool_name == "delete_close_run":
            tool_arguments = _hydrate_delete_close_run_arguments(
                tool_arguments=tool_arguments,
                snapshot=snapshot,
                operator_content=operator_content,
            )

        return planning.model_copy(
            update={
                "assistant_response": normalized_response,
                "tool_arguments": tool_arguments,
            }
        )

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

    journals = snapshot.get("journals")
    if not isinstance(journals, list):
        return []
    labels: list[str] = []
    for record in journals:
        if not isinstance(record, dict):
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


def _hydrate_review_document_arguments(
    *,
    tool_arguments: dict[str, Any],
    snapshot: dict[str, Any],
    operator_content: str,
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
        )
        if document_id is not None:
            hydrated["document_id"] = document_id

    if isinstance(decision, str) and decision.strip().lower() == "approved":
        hydrated.setdefault("verified_complete", True)
        hydrated.setdefault("verified_authorized", True)
        hydrated.setdefault("verified_period", True)

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

    preferred = [
        record
        for record in records
        if str(record.get("status") or "") in preferred_statuses
    ]
    if len(preferred) == 1 and isinstance(preferred[0].get("id"), str):
        return str(preferred[0]["id"])
    return None


def _hydrate_recommendation_arguments(
    *,
    tool_name: str,
    tool_arguments: dict[str, Any],
    snapshot: dict[str, Any],
    operator_content: str,
) -> dict[str, Any]:
    """Resolve clear recommendation targets and rejection reasons from the snapshot."""

    hydrated = dict(tool_arguments)
    if not isinstance(hydrated.get("recommendation_id"), str):
        recommendation_id = _resolve_recommendation_id_from_snapshot(
            snapshot=snapshot,
            operator_content=operator_content,
            preferred_statuses=("pending_review", "draft"),
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


def _hydrate_journal_arguments(
    *,
    tool_name: str,
    tool_arguments: dict[str, Any],
    snapshot: dict[str, Any],
    operator_content: str,
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
        return str(explicit_matches[0]["id"])

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


def _hydrate_reconciliation_arguments(
    *,
    tool_name: str,
    tool_arguments: dict[str, Any],
    snapshot: dict[str, Any],
    operator_content: str,
) -> dict[str, Any]:
    """Resolve clear reconciliation targets, dispositions, and reviewer reasons."""

    hydrated = dict(tool_arguments)
    if tool_name == "approve_reconciliation":
        if not isinstance(hydrated.get("reconciliation_id"), str):
            reconciliation_id = _resolve_reconciliation_id_from_snapshot(
                snapshot=snapshot,
                operator_content=operator_content,
                preferred_statuses=("in_review", "blocked", "draft"),
            )
            if reconciliation_id is not None:
                hydrated["reconciliation_id"] = reconciliation_id
        return hydrated

    if tool_name == "disposition_reconciliation_item":
        if not isinstance(hydrated.get("item_id"), str):
            item_id = _resolve_reconciliation_item_id_from_snapshot(
                snapshot=snapshot,
                operator_content=operator_content,
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
    if len(records) == 1 and isinstance(records[0].get("id"), str):
        return str(records[0]["id"])
    return None


def _hydrate_commentary_arguments(
    *,
    tool_name: str,
    tool_arguments: dict[str, Any],
    snapshot: dict[str, Any],
    operator_content: str,
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
) -> dict[str, Any]:
    """Resolve workspace targets and defaults from the current snapshot."""

    hydrated = dict(tool_arguments)
    workspace_id = hydrated.get("workspace_id")
    if not isinstance(workspace_id, str):
        resolved_workspace_id = _resolve_workspace_id_from_snapshot(
            snapshot=snapshot,
            operator_content=operator_content,
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
) -> dict[str, Any]:
    """Resolve one close run identifier from the current snapshot when the target is clear."""

    hydrated = dict(tool_arguments)
    if isinstance(hydrated.get("close_run_id"), str):
        return hydrated

    resolved_close_run_id = _resolve_close_run_id_from_snapshot(
        snapshot=snapshot,
        operator_content=operator_content,
    )
    if resolved_close_run_id is not None:
        hydrated["close_run_id"] = resolved_close_run_id
    return hydrated


def _resolve_close_run_id_from_snapshot(
    *,
    snapshot: dict[str, Any],
    operator_content: str,
) -> str | None:
    """Resolve one close run identifier from the workspace snapshot when the target is clear."""

    close_runs = snapshot.get("entity_close_runs")
    if not isinstance(close_runs, list):
        return None

    records = [record for record in close_runs if isinstance(record, dict)]
    if not records:
        return None

    normalized_content = _searchable_text(operator_content)
    explicit_matches = [
        record
        for record in records
        if (
            isinstance(record.get("period_label"), str)
            and _text_value_matches_text(str(record["period_label"]), normalized_content)
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
    return all(token in normalized_text for token in tokens[:3])


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
        period_start = _optional_result_text(applied_result, "period_start")
        period_end = _optional_result_text(applied_result, "period_end")
        if period_start and period_end:
            return f"I started a new close run for {period_start} to {period_end}."
        return "I started a new close run."

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
        return f"Next, I can {_lowercase_leading_character(cleaned)}"
    return None


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
    if normalized_message:
        return f"I couldn't finish {tool_phrase} yet. {normalized_message}"
    return f"I couldn't finish {tool_phrase} yet."


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
        "state. Do not ask the operator to repeat the request."
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
        or "The requested action could not be completed."
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
    created_close_run_id = applied_result.get("created_close_run_id")
    deleted_close_run_id = applied_result.get("deleted_close_run_id")
    reopened_from_status = applied_result.get("reopened_from_status")
    version_no = applied_result.get("version_no")
    rewound_from_phase = applied_result.get("rewound_from_phase")
    active_phase = applied_result.get("active_phase")
    period_start = applied_result.get("period_start")
    period_end = applied_result.get("period_end")

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
    elif isinstance(created_close_run_id, str):
        period_suffix = ""
        if isinstance(period_start, str) and isinstance(period_end, str):
            period_suffix = f" for {period_start} to {period_end}"
        if isinstance(version_no, int):
            notes.append(
                f"I started a new close run{period_suffix} as working version {version_no}."
            )
        else:
            notes.append(f"I started a new close run{period_suffix}.")
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
