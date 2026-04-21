"""
Purpose: Adapt chat threads and approval workflows onto the reusable agent
runtime and accounting tool registry.
Scope: Thread loading, message persistence, planner invocation, staged action
plans, approval execution, and assistant message creation.
Dependencies: Chat repositories, grounding, agent kernel, and accounting
workflow services.
"""

from __future__ import annotations

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
from services.chat.grounding import ChatGroundingService, GroundingContextRecord
from services.close_runs.delete_service import CloseRunDeleteService
from services.close_runs.service import CloseRunService, CloseRunServiceError
from services.coa.service import CoaRepository, CoaService
from services.common.enums import CloseRunStatus, ReportSectionKey, WorkflowPhase
from services.common.types import utc_now
from services.contracts.chat_models import (
    AgentCoaSummary,
    AgentMemorySummary,
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
from services.jobs.service import JobService, JobServiceError
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

        self._ensure_entity_coa_available(actor_user=actor_user, entity_id=entity_id)
        grounding, thread = self._load_thread_context(
            thread_id=thread_id,
            entity_id=entity_id,
            user_id=actor_user.id,
        )

        try:
            user_message = self._chat_repo.create_message(
                thread_id=thread_id,
                role="user",
                content=content,
                message_type="action",
                linked_action_id=None,
                grounding_payload=dict(message_grounding_payload or {}),
                model_metadata=None,
            )
            planning = self._plan_action(
                thread_id=thread_id,
                entity_id=entity_id,
                actor_user=actor_user,
                content=content,
                grounding=grounding,
            )
            pre_action_snapshot = self._snapshot_for_thread(
                actor_user=actor_user,
                entity_id=entity_id,
                close_run_id=thread.close_run_id,
                thread_id=thread_id,
            )
            planning = self._hydrate_planning_result(
                planning=planning,
                snapshot=pre_action_snapshot,
                operator_content=content,
            )
            action = self._resolve_action(planning=planning)
            execution_context = self._build_execution_context(
                actor_user=actor_user,
                entity_id=entity_id,
                close_run_id=thread.close_run_id,
                source_close_run_id=thread.close_run_id,
                thread_id=thread_id,
                trace_id=trace_id,
                source_surface=source_surface,
            )

            if action is None:
                assistant_message = self._chat_repo.create_message(
                    thread_id=thread_id,
                    role="assistant",
                    content=planning.assistant_response,
                    message_type="analysis",
                    linked_action_id=None,
                    grounding_payload=self._build_grounding_payload(grounding),
                    model_metadata=self._build_trace_metadata(
                        trace_id=trace_id,
                        mode="planner",
                        tool_name=None,
                        action_status="read_only",
                        summary=pre_action_snapshot.get("progress_summary"),
                    ),
                )
                self._update_thread_memory(
                    thread_id=thread_id,
                    existing_payload=thread.context_payload,
                    operator_message=content,
                    assistant_response=planning.assistant_response,
                    tool_name=None,
                    action_status="read_only",
                    trace_id=trace_id,
                    snapshot=pre_action_snapshot,
                )
                self._chat_repo.commit()
                return ChatExecutionOutcome(
                    assistant_message_id=serialize_uuid(assistant_message.id),
                    assistant_content=assistant_message.content,
                    action_plan=None,
                    is_read_only=True,
                )

            requires_human_approval = self._requires_human_approval(
                action=action,
                execution_context=execution_context,
            )
            record = self._action_repo.create_action_plan(
                thread_id=thread_id,
                message_id=user_message.id,
                entity_id=entity_id,
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
                },
                confidence=1.0,
                autonomy_mode=grounding.context.autonomy_mode,
                requires_human_approval=requires_human_approval,
                reasoning=action.planning.reasoning,
            )

            assistant_content = action.planning.assistant_response
            final_record = record
            applied_result: dict[str, Any] | None = None
            if requires_human_approval:
                assistant_content = _compose_assistant_content(
                    assistant_response=assistant_content,
                    handoff_message=None,
                    result_summary=(
                        "I have the change ready and I'm holding it for confirmation "
                        "before I apply it."
                    ),
                    next_step=None,
                )
            else:
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
                snapshot = self._snapshot_for_thread(
                    actor_user=actor_user,
                    entity_id=entity_id,
                    close_run_id=thread.close_run_id,
                    thread_id=thread_id,
                )
                assistant_content = _compose_assistant_content(
                    assistant_response=assistant_content,
                    handoff_message=handoff_message,
                    result_summary=_format_execution_result(applied_result),
                    next_step=_format_next_step(snapshot),
                )

            if applied_result is None:
                snapshot = pre_action_snapshot

            assistant_message = self._chat_repo.create_message(
                thread_id=thread_id,
                role="assistant",
                content=assistant_content,
                message_type="action",
                linked_action_id=final_record.id,
                grounding_payload=self._build_grounding_payload(grounding),
                model_metadata={
                    **self._build_trace_metadata(
                        trace_id=trace_id,
                        mode="planner",
                        tool_name=action.tool.name,
                        action_status="pending" if requires_human_approval else "applied",
                        summary=_summarize_applied_result(applied_result),
                    ),
                    "requires_human_approval": requires_human_approval,
                },
            )
            self._update_thread_memory(
                thread_id=thread_id,
                existing_payload=thread.context_payload,
                operator_message=content,
                assistant_response=assistant_content,
                tool_name=action.tool.name,
                action_status="pending" if requires_human_approval else "applied",
                trace_id=trace_id,
                snapshot=snapshot,
            )
            self._db_session.commit()
            return ChatExecutionOutcome(
                assistant_message_id=serialize_uuid(assistant_message.id),
                assistant_content=assistant_message.content,
                action_plan=final_record,
                is_read_only=False,
            )
        except ChatActionExecutionError:
            self._db_session.rollback()
            raise
        except Exception as error:
            self._db_session.rollback()
            raise ChatActionExecutionError(
                status_code=500,
                code=ChatActionExecutionErrorCode.EXECUTION_FAILED,
                message="The chat action could not be completed.",
            ) from error

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
            memory=self._memory_from_context_payload(thread.context_payload),
            tools=tuple(
                AgentToolManifestItem(
                    name=tool.name,
                    prompt_signature=tool.prompt_signature,
                    description=tool.description,
                    intent=tool.intent,
                    requires_human_approval=tool.requires_human_approval,
                    input_schema=tool.input_schema,
                )
                for tool in self._tool_registry.list_tools()
            ),
            recent_traces=self._build_recent_traces(messages),
            mcp_manifest=self._build_mcp_manifest(),
        )

    def list_registered_tools(self) -> tuple[AgentToolManifestItem, ...]:
        """Return the portable tool manifest exposed by the accounting agent."""

        return tuple(
            AgentToolManifestItem(
                name=tool.name,
                prompt_signature=tool.prompt_signature,
                description=tool.description,
                intent=tool.intent,
                requires_human_approval=tool.requires_human_approval,
                input_schema=tool.input_schema,
            )
            for tool in self._tool_registry.list_tools()
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
        if target_close_run_id is None and deleted_close_run_id is None:
            return grounding, thread, None

        previous_close_run_id = thread.close_run_id
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

        recent_tool_names = list(existing_payload.get("agent_recent_tool_names", []))
        if tool_name is not None:
            recent_tool_names.append(tool_name)
        compact_recent_tools = tuple(recent_tool_names[-5:])
        updated_payload = {
            **existing_payload,
            "agent_memory": {
                "last_operator_message": operator_message,
                "last_assistant_response": _truncate_text(assistant_response),
                "last_tool_name": tool_name,
                "last_action_status": action_status,
                "last_trace_id": trace_id,
                "pending_action_count": int(snapshot.get("pending_action_count", 0)),
                "progress_summary": snapshot.get("progress_summary"),
                "recent_tool_names": compact_recent_tools,
                "updated_at": utc_now().isoformat(),
            },
            "agent_recent_tool_names": compact_recent_tools,
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
        if not isinstance(memory, dict):
            return AgentMemorySummary()
        return AgentMemorySummary(**memory)

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

        metadata: dict[str, Any] = {
            "provider": "system" if mode in {"approval", "rejection"} else "openrouter",
            "mode": mode,
            "action_status": action_status,
        }
        if tool_name is not None:
            metadata["tool"] = tool_name
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
                    trace_id=(
                        str(metadata.get("trace_id"))
                        if metadata.get("trace_id") is not None
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
                )
            )
            if len(traces) >= 20:
                break
        return tuple(traces)

    def _build_mcp_manifest(self) -> dict[str, Any]:
        """Build a portable MCP-style manifest for the registered accounting tools."""

        return {
            "protocol": "model-context-protocol",
            "version": "draft",
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.input_schema,
                }
                for tool in self._tool_registry.list_tools()
            ],
        }

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
                instructions=self._build_planner_instructions(grounding=grounding),
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
    ) -> str:
        """Build the system instructions consumed by the generic agent kernel."""

        return "\n".join(
            [
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
                    "paragraphs. Avoid markdown bullets, bold markers, tables, or rigid "
                    "templates unless the operator explicitly asks for structure."
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
            ]
        )

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
        elif planning.tool_name == "create_workspace":
            tool_arguments = _hydrate_create_workspace_arguments(
                tool_arguments=tool_arguments,
                snapshot=snapshot,
            )
        elif planning.tool_name in {"update_workspace", "delete_workspace"}:
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
        return "I generated the export package for this close run."

    if tool_name == "assemble_evidence_pack":
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
