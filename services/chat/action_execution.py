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

            requires_human_approval = self._kernel.requires_human_approval(action=action)
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
                    actor_user=actor_user,
                    entity_id=entity_id,
                    close_run_id=thread.close_run_id,
                    thread_id=thread_id,
                    trace_id=trace_id,
                    source_surface=source_surface,
                )
                final_record = self._action_repo.update_action_plan_status(
                    action_plan_id=record.id,
                    status="applied",
                    applied_result=applied_result,
                ) or record
                assistant_content = (
                    f"{assistant_content}\n\n{_format_execution_result(applied_result)}"
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

        payload = dict(plan.payload)
        planning = self._planning_from_payload(payload)
        action = self._resolve_action(planning=planning)
        if action is None:
            raise ChatActionExecutionError(
                status_code=422,
                code=ChatActionExecutionErrorCode.INVALID_ACTION_PLAN,
                message="The stored chat action payload is incomplete.",
            )

        applied_result = self._execute_action(
            action=action,
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=plan.close_run_id,
            thread_id=thread_id,
            trace_id=trace_id,
            source_surface=source_surface,
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
            content=_format_approval_message(payload.get("assistant_response"), applied_result),
            message_type="action",
            linked_action_id=updated.id,
            grounding_payload={},
            model_metadata=self._build_trace_metadata(
                trace_id=trace_id,
                mode="approval",
                tool_name=action.tool.name,
                action_status="applied",
                summary=_summarize_applied_result(applied_result),
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
            requires_human_approval = self._kernel.requires_human_approval(action=action)
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
                    actor_user=actor_user,
                    entity_id=entity_id,
                    close_run_id=thread.close_run_id,
                    thread_id=thread_id,
                    trace_id=trace_id,
                    source_surface=source_surface,
                )
                final_record = self._action_repo.update_action_plan_status(
                    action_plan_id=record.id,
                    status="applied",
                    applied_result=applied_result,
                ) or record
                summary = _summarize_applied_result(applied_result) or summary

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
                "You are fully aware of the current system state described below.",
                (
                    "Choose mode=tool only when the request asks to change workflow "
                    "state or trigger a deterministic workflow."
                ),
                (
                    "Choose mode=read_only for analysis, explanation, status "
                    "narration, missing identifiers, or ambiguous requests."
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
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID | None,
        thread_id: UUID,
        trace_id: str | None,
        source_surface: AuditSourceSurface,
    ) -> dict[str, Any]:
        """Execute one resolved action through the generic agent kernel."""

        try:
            return self._kernel.execute(
                action=action,
                execution_context=AgentExecutionContext(
                    actor=actor_user,
                    entity_id=entity_id,
                    close_run_id=close_run_id,
                    thread_id=thread_id,
                    trace_id=trace_id,
                    source_surface=source_surface,
                ),
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
) -> str:
    """Render the assistant follow-up after a human-approved action executes."""

    base = (
        assistant_response
        if isinstance(assistant_response, str)
        else "Approved action executed."
    )
    return f"{base}\n\n{_format_execution_result(applied_result)}"


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
    tool_name = applied_result.get("tool")
    if isinstance(tool_name, str):
        return f"{tool_name} completed"
    return "Action completed"


__all__ = [
    "ChatActionExecutionError",
    "ChatActionExecutionErrorCode",
    "ChatActionExecutor",
    "ChatExecutionOutcome",
    "McpToolCallOutcome",
]
