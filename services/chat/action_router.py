"""
Purpose: Classify chat message intents and route action requests through the
review pipeline based on autonomy mode and policy checks.
Scope: Intent detection, action execution plan construction, proposed-edit
persistence, and autonomy-aware routing decisions for chat-originated actions.
Dependencies: Chat repository, action repository, model gateway, audit service,
entity membership checks, and the chat action contract models.

Design notes:
- The action router never bypasses business rules -- autonomy mode only changes
  whether a proposed change goes to pending_review or can apply to working state.
- Every action is persisted as a ChatActionPlan so the review queue can surface
  it alongside system-generated recommendations.
- Read-only analysis questions are handled directly by the chat service; this
  router activates only when an action intent is detected.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import Field, ValidationError
from services.chat.action_models import (
    CHAT_ACTION_INTENTS,
    ChatActionExecutionPlan,
    ChatActionIntent,
    ChatApprovalRequest,
    ChatDocumentRequest,
    ChatReconciliationAction,
    ChatReportAction,
    ProposedEditPayload,
)
from services.chat.grounding import ChatGroundingService, GroundingContextRecord
from services.common.enums import AutonomyMode, WorkflowPhase
from services.common.types import utc_now
from services.contracts.api_models import ContractModel
from services.db.models.audit import AuditSourceSurface
from services.db.repositories.chat_action_repo import (
    ChatActionPlanRecord,
)
from services.model_gateway.client import ModelResponseValidationError


class ChatActionRouterErrorCode(StrEnum):
    """Enumerate the stable error codes surfaced by the chat action router."""

    THREAD_NOT_FOUND = "thread_not_found"
    THREAD_ACCESS_DENIED = "thread_access_denied"
    INTENT_CLASSIFICATION_FAILED = "intent_classification_failed"
    INVALID_ACTION_PAYLOAD = "invalid_action_payload"
    TARGET_NOT_FOUND = "target_not_found"
    POLICY_BLOCKED = "policy_blocked"
    AUTONOMY_VIOLATION = "autonomy_violation"


class ChatActionRouterError(Exception):
    """Represent an expected action-routing failure that API routes expose cleanly."""

    def __init__(
        self,
        *,
        status_code: int,
        code: ChatActionRouterErrorCode,
        message: str,
    ) -> None:
        """Capture the HTTP status, stable error code, and recovery message."""
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class ModelGatewayProtocol(Protocol):
    """Describe the model gateway operations required for intent classification."""

    def complete(self, *, messages: list[dict[str, str]]) -> str:
        """Send a chat-completion request and return the assistant content string."""

    def complete_structured(
        self,
        *,
        messages: list[dict[str, str]],
        response_model: type[ContractModel],
    ) -> ContractModel:
        """Send a schema-enforced completion and return the validated result."""


class _ActionIntentClassificationResponse(ContractModel):
    """Capture the structured intent-classification result returned by the model."""

    intent: Literal[
        "proposed_edit",
        "approval_request",
        "document_request",
        "explanation",
        "workflow_action",
        "reconciliation_query",
        "report_action",
    ] = Field(description="Classified intent label or 'explanation' for read-only chat.")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Classifier confidence for the detected intent.",
    )
    target_phase: WorkflowPhase | None = Field(
        default=None,
        description="Workflow phase related to the request when detectable.",
    )
    target_type: str | None = Field(
        default=None,
        description="Business object type referenced by the operator, if any.",
    )
    target_id: UUID | None = Field(
        default=None,
        description="Business object identifier referenced by the operator, if any.",
    )
    requires_review: bool = Field(
        default=True,
        description="Whether the requested action should route through review.",
    )
    reasoning: str = Field(
        min_length=1,
        max_length=200,
        description="Short explanation of why the message was classified this way.",
    )


class ChatActionRepositoryProtocol(Protocol):
    """Describe the persistence operations required by the action router."""

    def create_action_plan(
        self,
        *,
        thread_id: UUID,
        message_id: UUID | None,
        entity_id: UUID,
        close_run_id: UUID | None,
        actor_user_id: UUID,
        intent: str,
        target_type: str | None,
        target_id: UUID | None,
        payload: dict[str, Any],
        confidence: float,
        autonomy_mode: str,
        requires_human_approval: bool,
        reasoning: str,
    ) -> ChatActionPlanRecord:
        """Persist a new action execution plan."""

    def get_action_plan_for_thread(
        self,
        *,
        action_plan_id: UUID,
        thread_id: UUID,
    ) -> ChatActionPlanRecord | None:
        """Return one action plan when it belongs to the specified thread."""

    def list_pending_actions_for_thread(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        limit: int = 50,
    ) -> tuple[ChatActionPlanRecord, ...]:
        """Return pending action plans for a thread scoped to an entity."""

    def update_action_plan_status(
        self,
        *,
        action_plan_id: UUID,
        status: str,
        applied_result: dict[str, Any] | None = None,
        rejected_reason: str | None = None,
        superseded_by_id: UUID | None = None,
    ) -> ChatActionPlanRecord | None:
        """Transition an action plan to a new review status."""

    def supersede_pending_actions_for_target(
        self,
        *,
        target_type: str,
        target_id: UUID,
        superseded_by_id: UUID,
    ) -> int:
        """Mark pending actions for a target as superseded."""

    def commit(self) -> None:
        """Commit the current transaction."""

    def rollback(self) -> None:
        """Rollback the current transaction."""


class ChatRepositoryProtocol(Protocol):
    """Describe the chat message persistence required by the action router."""

    def get_thread_for_entity(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
    ) -> Any | None:
        """Return one thread when it belongs to the specified entity."""

    def create_message(
        self,
        *,
        thread_id: UUID,
        role: str,
        content: str,
        message_type: str,
        linked_action_id: UUID | None,
        grounding_payload: dict[str, Any],
        model_metadata: dict[str, Any] | None,
    ) -> Any:
        """Persist a new chat message."""

    def get_message_count_for_thread(self, *, thread_id: UUID) -> int:
        """Return the total message count for a thread."""

    def get_last_message_time_for_thread(self, *, thread_id: UUID) -> Any:
        """Return the timestamp of the most recent message."""

    def commit(self) -> None:
        """Commit the current transaction."""

    def rollback(self) -> None:
        """Rollback the current transaction."""


class EntityMembershipProtocol(Protocol):
    """Describe the membership check required to gate action access."""

    def get_entity_for_user(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> Any | None:
        """Return entity access when the user is a member, or None."""


class ChatActionRouter:
    """Classify chat message intents and route action requests through review.

    This service sits between the read-only chat service and the downstream
    approval/recommendation pipelines. When a user message contains an action
    intent (proposed edit, approval request, etc.), this router:

    1. Classifies the intent using the model gateway
    2. Constructs a structured action execution plan
    3. Persists it as a ChatActionPlan for the review queue
    4. Routes based on autonomy mode:
       - human_review: always pending_review
       - reduced_interruption: low-risk actions may update working state
    """

    def __init__(
        self,
        *,
        action_repository: ChatActionRepositoryProtocol,
        chat_repository: ChatRepositoryProtocol,
        model_gateway: ModelGatewayProtocol,
        grounding_service: ChatGroundingService,
        entity_repo: EntityMembershipProtocol,
    ) -> None:
        """Capture persistence boundaries, model gateway, and grounding resolver."""
        self._action_repo = action_repository
        self._chat_repo = chat_repository
        self._model_gateway = model_gateway
        self._grounding = grounding_service
        self._entity_repo = entity_repo

    def classify_action_intent(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        user_id: UUID,
        content: str,
        grounding: GroundingContextRecord,
    ) -> ChatActionIntent | None:
        """Classify whether a user message contains an action intent.

        Returns a ChatActionIntent when the message requests a workflow action
        (proposed edit, approval, etc.), or None when it is pure read-only
        analysis.

        The classification uses a structured prompt that asks the model to
        identify intent, target type, target ID, and whether the action
        requires review.
        """
        self._require_entity_membership(entity_id=entity_id, user_id=user_id)

        thread = self._chat_repo.get_thread_for_entity(
            thread_id=thread_id,
            entity_id=entity_id,
        )
        if thread is None:
            raise ChatActionRouterError(
                status_code=404,
                code=ChatActionRouterErrorCode.THREAD_NOT_FOUND,
                message="That chat thread does not exist or is not in this workspace.",
            )

        # Build classification prompt
        context_lines = self._build_classification_context(grounding)
        classification_prompt = self._build_classification_prompt(
            context_lines=context_lines,
            user_message=content,
        )

        try:
            classification = self._model_gateway.complete_structured(
                messages=[{"role": "system", "content": classification_prompt}],
                response_model=_ActionIntentClassificationResponse,
            )
        except (ModelResponseValidationError, ValidationError) as error:
            raise ChatActionRouterError(
                status_code=422,
                code=ChatActionRouterErrorCode.INTENT_CLASSIFICATION_FAILED,
                message="The intent classification response was invalid.",
            ) from error
        except Exception as error:
            raise ChatActionRouterError(
                status_code=503,
                code=ChatActionRouterErrorCode.INTENT_CLASSIFICATION_FAILED,
                message="The intent classification service is unavailable.",
            ) from error

        if classification.intent == "explanation" or classification.confidence < 0.3:
            return None

        try:
            return ChatActionIntent(
                intent=classification.intent,
                confidence=classification.confidence,
                target_phase=classification.target_phase,
                target_type=classification.target_type,
                target_id=classification.target_id,
                requires_review=classification.requires_review,
            )
        except ValidationError as error:
            raise ChatActionRouterError(
                status_code=422,
                code=ChatActionRouterErrorCode.INTENT_CLASSIFICATION_FAILED,
                message="The intent classification response was invalid.",
            ) from error

    def build_execution_plan(
        self,
        *,
        thread_id: UUID,
        message_id: UUID | None,
        entity_id: UUID,
        close_run_id: UUID | None,
        actor_user_id: UUID,
        intent: ChatActionIntent,
        grounding: GroundingContextRecord,
        reasoning: str,
        proposed_edit: ProposedEditPayload | None = None,
        approval_request: ChatApprovalRequest | None = None,
        document_request: ChatDocumentRequest | None = None,
        reconciliation_action: ChatReconciliationAction | None = None,
        report_action: ChatReportAction | None = None,
    ) -> ChatActionExecutionPlan:
        """Construct a validated action execution plan from classified intent.

        This method assembles the full structured plan that will be persisted
        and routed through the review pipeline.
        """
        requires_human_approval = self._determine_approval_requirement(
            intent=intent,
            autonomy_mode=grounding.autonomy_mode,
            proposed_edit=proposed_edit,
        )

        return ChatActionExecutionPlan(
            thread_id=thread_id,
            message_id=message_id,
            intent=intent,
            autonomy_mode=AutonomyMode(grounding.autonomy_mode),
            proposed_edit=proposed_edit,
            approval_request=approval_request,
            document_request=document_request,
            reconciliation_action=reconciliation_action,
            report_action=report_action,
            reasoning=reasoning,
            requires_human_approval=requires_human_approval,
            status="pending",
        )

    def persist_action_plan(
        self,
        *,
        plan: ChatActionExecutionPlan,
        entity_id: UUID,
        actor_user_id: UUID,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> ChatActionPlanRecord:
        """Persist an action execution plan and emit an audit event.

        The plan is stored independently of the chat message history so the
        review queue can surface it alongside system-generated recommendations.
        The entity_id is passed explicitly because intent target_type values
        (e.g. 'recommendation', 'journal') are not entity-scoped identifiers.
        """
        try:
            payload = self._build_action_payload(plan)
            record = self._action_repo.create_action_plan(
                thread_id=plan.thread_id,
                message_id=plan.message_id,
                entity_id=entity_id,
                close_run_id=plan.intent.target_id
                if plan.intent.intent == "document_request"
                else None,
                actor_user_id=actor_user_id,
                intent=plan.intent.intent,
                target_type=plan.intent.target_type,
                target_id=plan.intent.target_id,
                payload=payload,
                confidence=plan.intent.confidence,
                autonomy_mode=plan.autonomy_mode.value,
                requires_human_approval=plan.requires_human_approval,
                reasoning=plan.reasoning,
            )
            self._action_repo.commit()
        except Exception:
            self._action_repo.rollback()
            raise

        return record

    def approve_action_plan(
        self,
        *,
        action_plan_id: UUID,
        thread_id: UUID,
        entity_id: UUID,
        actor_user_id: UUID,
        reason: str | None = None,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> ChatActionPlanRecord:
        """Approve a pending chat-originated action plan.

        This transitions the plan to 'approved' status and, for proposed edits,
        may trigger downstream materialization (e.g., creating a recommendation
        from the proposed edit).

        Requires explicit entity membership so a user cannot approve action
        plans from workspaces they do not belong to.
        """
        self._require_entity_membership(entity_id=entity_id, user_id=actor_user_id)
        plan = self._action_repo.get_action_plan_for_thread(
            action_plan_id=action_plan_id,
            thread_id=thread_id,
        )
        if plan is None:
            raise ChatActionRouterError(
                status_code=404,
                code=ChatActionRouterErrorCode.THREAD_NOT_FOUND,
                message="That action plan does not exist or is not in this thread.",
            )

        if plan.entity_id != entity_id:
            raise ChatActionRouterError(
                status_code=403,
                code=ChatActionRouterErrorCode.THREAD_ACCESS_DENIED,
                message="That action plan does not belong to this workspace.",
            )

        if plan.status != "pending":
            raise ChatActionRouterError(
                status_code=409,
                code=ChatActionRouterErrorCode.POLICY_BLOCKED,
                message=(
                    f"Cannot approve action plan in '{plan.status}' status. "
                    "Only 'pending' actions can be approved."
                ),
            )

        try:
            record = self._action_repo.update_action_plan_status(
                action_plan_id=action_plan_id,
                status="approved",
                applied_result={
                    "approved_by": str(actor_user_id),
                    "reason": reason,
                    "source_surface": source_surface.value,
                },
            )
            if record is None:
                raise ChatActionRouterError(
                    status_code=404,
                    code=ChatActionRouterErrorCode.THREAD_NOT_FOUND,
                    message="Action plan not found after update.",
                )
            self._action_repo.commit()
        except ChatActionRouterError:
            raise
        except Exception:
            self._action_repo.rollback()
            raise

        return record

    def reject_action_plan(
        self,
        *,
        action_plan_id: UUID,
        thread_id: UUID,
        entity_id: UUID,
        actor_user_id: UUID,
        reason: str,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> ChatActionPlanRecord:
        """Reject a pending chat-originated action plan with a required reason.

        Requires explicit entity membership so a user cannot reject action
        plans from workspaces they do not belong to.
        """
        self._require_entity_membership(entity_id=entity_id, user_id=actor_user_id)
        plan = self._action_repo.get_action_plan_for_thread(
            action_plan_id=action_plan_id,
            thread_id=thread_id,
        )
        if plan is None:
            raise ChatActionRouterError(
                status_code=404,
                code=ChatActionRouterErrorCode.THREAD_NOT_FOUND,
                message="That action plan does not exist or is not in this thread.",
            )

        if plan.entity_id != entity_id:
            raise ChatActionRouterError(
                status_code=403,
                code=ChatActionRouterErrorCode.THREAD_ACCESS_DENIED,
                message="That action plan does not belong to this workspace.",
            )

        if plan.status != "pending":
            raise ChatActionRouterError(
                status_code=409,
                code=ChatActionRouterErrorCode.POLICY_BLOCKED,
                message=(
                    f"Cannot reject action plan in '{plan.status}' status. "
                    "Only 'pending' actions can be rejected."
                ),
            )

        try:
            record = self._action_repo.update_action_plan_status(
                action_plan_id=action_plan_id,
                status="rejected",
                rejected_reason=reason,
            )
            if record is None:
                raise ChatActionRouterError(
                    status_code=404,
                    code=ChatActionRouterErrorCode.THREAD_NOT_FOUND,
                    message="Action plan not found after update.",
                )
            self._action_repo.commit()
        except ChatActionRouterError:
            raise
        except Exception:
            self._action_repo.rollback()
            raise

        return record

    def list_pending_actions(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        user_id: UUID,
        limit: int = 50,
    ) -> tuple[ChatActionPlanRecord, ...]:
        """Return pending action plans for a thread for review-queue rendering.

        Requires explicit entity membership and further constrains the query
        by entity_id so that action plans from other workspaces cannot leak
        through even if the caller supplies a valid thread_id from elsewhere.
        """
        self._require_entity_membership(entity_id=entity_id, user_id=user_id)
        thread = self._chat_repo.get_thread_for_entity(
            thread_id=thread_id,
            entity_id=entity_id,
        )
        if thread is None:
            raise ChatActionRouterError(
                status_code=404,
                code=ChatActionRouterErrorCode.THREAD_NOT_FOUND,
                message="That chat thread does not exist or is not in this workspace.",
            )
        plans = self._action_repo.list_pending_actions_for_thread(
            thread_id=thread_id,
            entity_id=entity_id,
            limit=max(limit * 3, limit),
        )
        thread_close_run_id = _thread_close_run_id(thread)
        return tuple(
            plan for plan in plans if plan.close_run_id == thread_close_run_id
        )[:limit]

    def _build_classification_context(
        self,
        grounding: GroundingContextRecord,
    ) -> list[str]:
        """Build context lines for the intent classification prompt."""
        lines = [
            f"Entity: {grounding.entity_name}",
            f"Autonomy mode: {grounding.autonomy_mode}",
            f"Base currency: {grounding.base_currency}",
            f"Current UTC date: {utc_now().date().isoformat()}",
        ]
        if grounding.close_run and grounding.period_label:
            lines.append(f"Close run period: {grounding.period_label}")
        return lines

    def _build_classification_prompt(
        self,
        *,
        context_lines: list[str],
        user_message: str,
    ) -> str:
        """Build the structured prompt for intent classification.

        The model must respond with a JSON object containing:
        - intent: one of the CHAT_ACTION_INTENTS values
        - confidence: 0.0-1.0 confidence score
        - target_type: optional business object type
        - target_id: optional UUID of the target object
        - requires_review: whether the action needs human review
        - reasoning: brief explanation of the classification
        """
        context_block = "\n".join(f"- {line}" for line in context_lines)
        valid_intents = ", ".join(f'"{i}"' for i in CHAT_ACTION_INTENTS)

        return f"""You are an intent classifier for an accounting AI Agent chat system.
Your job is to analyze a user message and determine whether it contains an action
request that should be routed through the review pipeline, or whether it is a
read-only analysis question.

Context:
{context_block}

Valid action intents: {valid_intents}

Action intent definitions:
- proposed_edit: The user wants to change an accounting value
  (account code, amount, description, etc.)
- approval_request: The user wants to approve or reject a reviewable item
- document_request: The user is requesting or identifying missing source documents
- explanation: The user wants to understand why an accounting decision was made
- workflow_action: The user wants to trigger a workflow action
  (advance phase, start reconciliation, etc.)
- reconciliation_query: The user is asking about reconciliation status or exceptions
- report_action: The user wants to regenerate, edit, or export a report

Rules:
1. If the message is purely informational or a question, respond with intent "explanation".
2. If the message requests a change to accounting state, classify the specific action intent.
3. Always set requires_review=true for proposed edits and approval requests.
4. If the message references a specific business object by ID, include it as target_id.
5. Keep reasoning concise (max 200 characters).
6. Natural operator phrasing counts as action intent when it implies a supported workflow step.
7. Examples of action phrasing include: "I need the reports",
   "can you get this ready for sign-off?", "we need to finish reconciliation",
   "please prepare the export pack", "reopen this close run so we can make changes",
   "take this back to reconciliation",
   "take this back to collection so I can upload more files",
   "start over from document intake", "start a new April close run",
   "open a fresh run for this month", "create another run for this period",
   "archive this run", and "ignore the PDF I uploaded by mistake".
8. Requests to continue, resume, finish, reopen, archive, create a new run,
   move the workflow forward, or move it backward should be treated as action intent
   when the message is outcome-seeking.
9. Only use intent "explanation" when the operator is asking to understand or inspect
   state, not when they are asking the system to make progress.
10. Relative periods like "this month", "next month", or named months should still
    count as workflow_action when the operator is asking to start a new run.

Respond with a JSON object only. Do not include markdown code blocks or extra text.

User message: "{user_message}"

JSON response:"""

    def _determine_approval_requirement(
        self,
        *,
        intent: ChatActionIntent,
        autonomy_mode: str,
        proposed_edit: ProposedEditPayload | None = None,
    ) -> bool:
        """Determine whether an action requires explicit human approval.

        Rules:
        - In human_review mode: ALL actions require human approval
        - In reduced_interruption mode:
          - proposed_edit: requires approval (changes state)
          - approval_request: does not require additional approval (it IS the approval)
          - document_request: does not require approval (informational)
          - explanation: does not require approval (read-only)
          - workflow_action: requires approval (triggers state change)
          - reconciliation_query: does not require approval (read-only)
          - report_action: varies by specific action
        """
        if autonomy_mode == AutonomyMode.HUMAN_REVIEW.value:
            return True

        # reduced_interruption mode
        if intent.intent == "proposed_edit":
            return True
        if intent.intent == "approval_request":
            return False
        if intent.intent == "document_request":
            return False
        if intent.intent == "explanation":
            return False
        if intent.intent == "workflow_action":
            return True
        if intent.intent == "reconciliation_query":
            return False
        if intent.intent == "report_action":
            # Report regeneration requires approval; commentary edits may not
            if report_action := intent.intent:
                if report_action == "report_action":
                    return True
            return False

        return True  # Default: require approval

    def _build_action_payload(
        self,
        plan: ChatActionExecutionPlan,
    ) -> dict[str, Any]:
        """Serialize the action execution plan into the JSONB payload column."""
        payload: dict[str, Any] = {
            "intent": plan.intent.model_dump(),
            "autonomy_mode": plan.autonomy_mode.value,
            "reasoning": plan.reasoning,
            "requires_human_approval": plan.requires_human_approval,
            "status": plan.status,
        }

        if plan.proposed_edit is not None:
            payload["proposed_edit"] = plan.proposed_edit.model_dump()
        if plan.approval_request is not None:
            payload["approval_request"] = plan.approval_request.model_dump()
        if plan.document_request is not None:
            payload["document_request"] = plan.document_request.model_dump()
        if plan.reconciliation_action is not None:
            payload["reconciliation_action"] = plan.reconciliation_action.model_dump()
        if plan.report_action is not None:
            payload["report_action"] = plan.report_action.model_dump()

        return payload

    def _require_entity_membership(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> None:
        """Fail fast when the caller is not a member of the target entity."""
        access = self._entity_repo.get_entity_for_user(
            entity_id=entity_id,
            user_id=user_id,
        )
        if access is None:
            raise ChatActionRouterError(
                status_code=403,
                code=ChatActionRouterErrorCode.THREAD_ACCESS_DENIED,
                message="You are not a member of this workspace.",
            )


def _thread_close_run_id(thread: Any) -> UUID | None:
    """Return a thread's active close-run scope from either a record or dict."""

    if isinstance(thread, dict):
        return thread.get("close_run_id")
    return getattr(thread, "close_run_id", None)


__all__ = [
    "ChatActionRouter",
    "ChatActionRouterError",
    "ChatActionRouterErrorCode",
]
