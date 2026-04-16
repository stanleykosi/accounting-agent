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
from services.close_runs.service import CloseRunService, CloseRunServiceError
from services.coa.service import CoaRepository
from services.common.enums import CloseRunStatus, WorkflowPhase
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
        close_run_repository: CloseRunRepository,
        document_review_service: DocumentReviewService,
        document_repository: DocumentRepository,
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
        self._close_run_service = close_run_service

        self._workspace_builder = AccountingWorkspaceContextBuilder(
            action_repository=action_repository,
            close_run_service=close_run_service,
            coa_repository=CoaRepository(db_session=db_session),
            document_repository=document_repository,
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
            document_review_service=document_review_service,
            document_repository=document_repository,
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
                snapshot = self._snapshot_for_thread(
                    actor_user=actor_user,
                    entity_id=entity_id,
                    close_run_id=thread.close_run_id,
                    thread_id=thread_id,
                )
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
                        summary=snapshot.get("progress_summary"),
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
                    snapshot=snapshot,
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
                assistant_content = (
                    f"{assistant_content}\n\n"
                    "This action is staged for approval before the system applies it."
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
                assistant_content = (
                    f"{assistant_content}\n\n"
                    f"{_format_scope_handoff_message(handoff_message)}"
                    f"{_format_execution_result(applied_result)}"
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
                assistant_response=action.planning.assistant_response,
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
        except (
            ChatActionExecutionError,
            DocumentReviewServiceError,
            RecommendationApplyError,
            CloseRunServiceError,
            ExportServiceError,
            ReportServiceError,
            JobServiceError,
            SupportingScheduleServiceError,
        ):
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
        except (
            ChatActionExecutionError,
            DocumentReviewServiceError,
            RecommendationApplyError,
            CloseRunServiceError,
            ExportServiceError,
            ReportServiceError,
            JobServiceError,
        ):
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
        target_close_run_id = reopened_close_run_id or created_close_run_id
        if target_close_run_id is None:
            return grounding, thread, None

        reopened_grounding = self._grounding.resolve_context(
            entity_id=entity_id,
            close_run_id=target_close_run_id,
            user_id=actor_user.id,
        )
        previous_close_run_id = thread.close_run_id
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
                    "Treat this as one dynamic agent surface: answer directly when the "
                    "operator needs analysis, and select a deterministic tool only when "
                    "the request clearly asks to change workflow state or trigger an "
                    "available operation."
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
                    "changes, you may use the reopen tool when the current run is already "
                    "approved, exported, or archived."
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
                    "document tool when the snapshot shows one clear matching document. If more "
                    "than one document could match, ask one short clarifying question."
                ),
                (
                    "When the operator asks to start a new or fresh close run, use the "
                    "create_close_run tool with explicit ISO period_start and period_end values. "
                    "Resolve relative phrases like 'this month', 'next month', or named months "
                    "against the current UTC date above. If the intended period is still "
                    "ambiguous, ask one short clarifying question."
                ),
                (
                    "If the operator explicitly wants another open run for the same period, set "
                    "allow_duplicate_period=true and include a concise duplicate_period_reason. "
                    "Otherwise do not create a duplicate run."
                ),
                (
                    "You only have the capabilities listed in Available tools and the "
                    "workspace snapshot. Do not claim hidden abilities, external access, "
                    "or background jobs that are not represented there."
                ),
                (
                    "Choose mode=tool only when the request asks to change workflow "
                    "state or trigger a deterministic workflow."
                ),
                (
                    "Choose mode=read_only for analysis, explanation, status "
                    "narration, missing identifiers, or ambiguous requests."
                ),
                (
                    "Greetings, status checks, and help requests should still return a "
                    "useful read_only response grounded in the current workspace."
                ),
                (
                    "When choosing a tool, use only the registered deterministic "
                    "tools and include JSON-safe arguments."
                ),
                (
                    "If a required identifier is missing, do not invent it. Respond "
                    "in read_only mode and say exactly what identifier is needed."
                ),
                (
                    "When you choose a tool, explain in plain operator language what you are "
                    "about to change, why it fits the current workflow state, and what happens next."
                ),
                (
                    "If the operator states a desired outcome but multiple actions could fit, "
                    "do not guess. Respond in read_only mode with one short clarifying question."
                ),
                (
                    "Keep the operator oriented: mention completed phases, the active phase, "
                    "pending approvals, and in-flight processing whenever that helps them "
                    "understand what the system is doing."
                ),
                (
                    "If the operator asks what you can do next, summarize the available "
                    "tools, approvals, blockers, and workflow steps from the current "
                    "workspace snapshot."
                ),
                (
                    "If a requested change would move the workflow backward or invalidate an "
                    "earlier gate, say that clearly so the operator understands why the agent "
                    "is rewinding or staging review."
                ),
                (
                    "Use progress_summary, coa summary, readiness blockers, workflow phase "
                    "states, recent jobs, exports, and recent actions to explain current "
                    "state and next steps."
                ),
                (
                    "If the active chart of accounts is missing, do not choose a tool for "
                    "recommendation generation, journals, reconciliation, reporting, or "
                    "exports. Respond in read_only mode and ask the operator to upload or "
                    "sync the production COA from the workbench."
                ),
                (
                    "If the active chart of accounts is fallback-only, you may explain "
                    "state, parsing, and intake progress, but for precision-sensitive "
                    "coding, journals, reconciliation, reporting, and sign-off you should "
                    "direct the operator to upload or sync the production COA first."
                ),
                (
                    "If source documents are missing, direct the operator to the "
                    "intake controls in the workbench and explain that parsing starts "
                    "automatically after upload."
                ),
                (
                    "High-risk state changes stage for approval; low-risk operational "
                    "generation actions may execute immediately."
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

    return f"Execution result:\n```json\n{json.dumps(applied_result, indent=2, default=str)}\n```"


def _format_approval_message(
    assistant_response: object,
    applied_result: dict[str, Any],
    *,
    handoff_message: str | None = None,
) -> str:
    """Render the assistant follow-up after a human-approved action executes."""

    base = (
        assistant_response
        if isinstance(assistant_response, str)
        else "Approved action executed."
    )
    return f"{base}\n\n{_format_scope_handoff_message(handoff_message)}{_format_execution_result(applied_result)}"


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
                    f"I reopened the {status_label.lower()} close run as working version {version_no}."
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

    if isinstance(rewound_from_phase, str) and isinstance(active_phase, str):
        notes.append(
            f"I moved the workflow from {_format_phase_label(rewound_from_phase)} "
            f"back to {_format_phase_label(active_phase)} so this request could be applied."
        )
    elif (
        isinstance(reopened_close_run_id, str) or isinstance(created_close_run_id, str)
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
