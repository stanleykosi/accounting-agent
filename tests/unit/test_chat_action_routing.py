"""
Purpose: Unit tests for the chat action router, proposed changes service,
and action model contracts.
Scope: Intent classification, execution plan construction, autonomy-mode
routing decisions, proposed edit approval/rejection transitions, and guard
behavior for stale or invalid action plans.
Dependencies: pytest, Pydantic models, chat service classes, and repository
protocol implementations.

Test categories:
1. Action model contract validation
2. Intent classification parsing
3. Execution plan construction
4. Autonomy-mode routing decisions
5. Proposed changes lifecycle transitions
6. Edge cases: stale approvals, duplicate targets, invalid transitions
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from services.chat.action_models import (
    ChatActionExecutionPlan,
    ChatActionIntent,
    ChatApprovalRequest,
    ChatDocumentRequest,
    ProposedEditPayload,
    SendChatActionRequest,
)
from services.chat.action_router import (
    ChatActionRouter,
    ChatActionRouterError,
    ChatActionRouterErrorCode,
)
from services.chat.proposed_changes import (
    ProposedChangesError,
    ProposedChangesErrorCode,
    ProposedChangesService,
)
from services.common.enums import AutonomyMode, DocumentType, WorkflowPhase


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeGroundingContext:
    entity_id: str
    entity_name: str
    autonomy_mode: str
    base_currency: str
    close_run: Any | None = None
    close_run_id: str | None = None
    period_label: str | None = None


@dataclass(frozen=True)
class FakeActionPlanRecord:
    id: UUID
    thread_id: UUID
    message_id: UUID | None
    entity_id: UUID
    close_run_id: UUID | None
    actor_user_id: UUID
    intent: str
    target_type: str | None
    target_id: UUID | None
    payload: dict[str, Any]
    confidence: float
    autonomy_mode: str
    status: str
    requires_human_approval: bool
    reasoning: str
    applied_result: dict[str, Any] | None
    rejected_reason: str | None
    superseded_by_id: UUID | None
    created_at: datetime
    updated_at: datetime


class FakeActionRepository:
    def __init__(self) -> None:
        self._plans: dict[UUID, FakeActionPlanRecord] = {}

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
    ) -> FakeActionPlanRecord:
        now = datetime.now(timezone.utc)
        plan = FakeActionPlanRecord(
            id=uuid4(),
            thread_id=thread_id,
            message_id=message_id,
            entity_id=entity_id,
            close_run_id=close_run_id,
            actor_user_id=actor_user_id,
            intent=intent,
            target_type=target_type,
            target_id=target_id,
            payload=payload,
            confidence=confidence,
            autonomy_mode=autonomy_mode,
            requires_human_approval=requires_human_approval,
            status="pending",
            reasoning=reasoning,
            applied_result=None,
            rejected_reason=None,
            superseded_by_id=None,
            created_at=now,
            updated_at=now,
        )
        self._plans[plan.id] = plan
        return plan

    def get_action_plan_by_id(
        self,
        *,
        action_plan_id: UUID,
    ) -> FakeActionPlanRecord | None:
        return self._plans.get(action_plan_id)

    def get_action_plan_for_thread(
        self,
        *,
        action_plan_id: UUID,
        thread_id: UUID,
    ) -> FakeActionPlanRecord | None:
        plan = self._plans.get(action_plan_id)
        if plan is None or plan.thread_id != thread_id:
            return None
        return plan

    def list_pending_actions_for_thread(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        limit: int = 50,
    ) -> tuple[FakeActionPlanRecord, ...]:
        return tuple(
            p
            for p in self._plans.values()
            if p.thread_id == thread_id
            and p.entity_id == entity_id
            and p.status == "pending"
        )[:limit]

    def list_actions_for_target(
        self,
        *,
        target_type: str,
        target_id: UUID,
        status: str | None = None,
        limit: int = 50,
    ) -> tuple[FakeActionPlanRecord, ...]:
        results = []
        for p in self._plans.values():
            if p.target_type != target_type or p.target_id != target_id:
                continue
            if status is not None and p.status != status:
                continue
            results.append(p)
        return tuple(results[:limit])

    def update_action_plan_status(
        self,
        *,
        action_plan_id: UUID,
        status: str,
        applied_result: dict[str, Any] | None = None,
        rejected_reason: str | None = None,
        superseded_by_id: UUID | None = None,
    ) -> FakeActionPlanRecord | None:
        plan = self._plans.get(action_plan_id)
        if plan is None:
            return None
        plan_dict: dict[str, Any] = dict(plan.__dict__)
        plan_dict["status"] = status
        if applied_result is not None:
            plan_dict["applied_result"] = applied_result
        if rejected_reason is not None:
            plan_dict["rejected_reason"] = rejected_reason
        if superseded_by_id is not None:
            plan_dict["superseded_by_id"] = superseded_by_id
        plan_dict["updated_at"] = datetime.now(timezone.utc)
        updated = FakeActionPlanRecord(**plan_dict)
        self._plans[action_plan_id] = updated
        return updated

    def supersede_pending_actions_for_target(
        self,
        *,
        target_type: str,
        target_id: UUID,
        superseded_by_id: UUID,
    ) -> int:
        count = 0
        for p in self._plans.values():
            if (
                p.target_type == target_type
                and p.target_id == target_id
                and p.status == "pending"
            ):
                p_dict: dict[str, Any] = dict(p.__dict__)
                p_dict["status"] = "superseded"
                p_dict["superseded_by_id"] = superseded_by_id
                p_dict["updated_at"] = datetime.now(timezone.utc)
                self._plans[p.id] = FakeActionPlanRecord(**p_dict)
                count += 1
        return count

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


class FakeChatRepository:
    def __init__(self) -> None:
        self._threads: dict[UUID, dict[str, Any]] = {}

    def get_thread_for_entity(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
    ) -> dict[str, Any] | None:
        thread = self._threads.get(thread_id)
        if thread is None or thread.get("entity_id") != entity_id:
            return None
        return thread

    def create_thread(self, **kwargs: Any) -> dict[str, Any]:
        thread = {
            "id": uuid4(),
            "entity_id": kwargs.get("entity_id"),
            "close_run_id": kwargs.get("close_run_id"),
            "context_payload": kwargs.get("context_payload", {}),
            "title": kwargs.get("title"),
        }
        self._threads[thread["id"]] = thread
        return thread

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


class FakeModelGateway:
    def __init__(self, response: str | None = None) -> None:
        self.response = response or '{"intent": "explanation", "confidence": 0.8, "reasoning": "Test"}'
        self.calls: list[dict[str, Any]] = []

    def complete(self, *, messages: list[dict[str, str]]) -> str:
        self.calls.append({"messages": messages})
        return self.response

    def complete_structured(self, *, messages: list[dict[str, str]], response_model):
        self.calls.append({"messages": messages, "response_model": response_model})
        return response_model.model_validate(json.loads(self.response))


class FakeEntityRepo:
    def __init__(self, *, allow_access: bool = True) -> None:
        self._allow_access = allow_access

    def get_entity_for_user(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any] | None:
        if self._allow_access:
            return {"id": entity_id, "name": "Test Entity"}
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def user_id() -> UUID:
    return uuid4()


@pytest.fixture()
def entity_id() -> UUID:
    return uuid4()


@pytest.fixture()
def thread_id() -> UUID:
    return uuid4()


@pytest.fixture()
def action_repo() -> FakeActionRepository:
    return FakeActionRepository()


@pytest.fixture()
def chat_repo(user_id: UUID, entity_id: UUID) -> FakeChatRepository:
    repo = FakeChatRepository()
    repo._threads = {
        uuid4(): {
            "id": uuid4(),
            "entity_id": entity_id,
            "close_run_id": None,
            "context_payload": {
                "entity_id": str(entity_id),
                "entity_name": "Test Entity",
                "autonomy_mode": AutonomyMode.HUMAN_REVIEW.value,
                "base_currency": "NGN",
            },
            "title": "Test Thread",
        }
    }
    return repo


@pytest.fixture()
def model_gateway() -> FakeModelGateway:
    return FakeModelGateway()


@pytest.fixture()
def entity_repo() -> FakeEntityRepo:
    return FakeEntityRepo(allow_access=True)


# ---------------------------------------------------------------------------
# Tests: Action model contract validation
# ---------------------------------------------------------------------------


class TestActionModelContracts:
    """Validate Pydantic contract boundaries for chat action models."""

    def test_proposed_edit_requires_value_change(self) -> None:
        """Proposed edits must actually change the field value."""
        with pytest.raises(ValueError, match="must change"):
            ProposedEditPayload(
                target_type="recommendation",
                target_id=uuid4(),
                field_path="payload.account_code",
                current_value="4000",
                proposed_value="4000",
                reasoning="No actual change",
            )

    def test_proposed_edit_accepts_valid_change(self) -> None:
        """Valid proposed edits with different values are accepted."""
        edit = ProposedEditPayload(
            target_type="recommendation",
            target_id=uuid4(),
            field_path="payload.account_code",
            current_value="4000",
            proposed_value="5000",
            reasoning="Corrected account code",
        )
        assert edit.field_path == "payload.account_code"
        assert edit.current_value == "4000"
        assert edit.proposed_value == "5000"

    def test_proposed_edit_strips_field_path(self) -> None:
        """Field paths are trimmed of whitespace."""
        edit = ProposedEditPayload(
            target_type="recommendation",
            target_id=uuid4(),
            field_path="  payload.account_code  ",
            current_value="4000",
            proposed_value="5000",
            reasoning="Test",
        )
        assert edit.field_path == "payload.account_code"

    def test_send_action_request_strips_content(self) -> None:
        """Message content is stripped of leading/trailing whitespace."""
        req = SendChatActionRequest(content="  approve this journal  ")
        assert req.content == "approve this journal"

    def test_send_action_request_rejects_blank_content(self) -> None:
        """Blank or whitespace-only content is rejected."""
        with pytest.raises(ValueError, match="cannot be blank"):
            SendChatActionRequest(content="   ")

    def test_chat_approval_request_valid(self) -> None:
        """Approval requests accept valid payloads."""
        req = ChatApprovalRequest(
            target_type="recommendation",
            target_id=uuid4(),
            requested_action="approve",
            confidence=0.9,
            reason="Looks correct",
        )
        assert req.requested_action == "approve"
        assert req.confidence == 0.9

    def test_chat_document_request_valid(self) -> None:
        """Document requests accept valid payloads."""
        req = ChatDocumentRequest(
            close_run_id=uuid4(),
            document_types=[DocumentType.INVOICE],
            reason="Missing invoices for March",
            blocking=True,
        )
        assert req.blocking is True
        assert len(req.document_types) == 1

    def test_action_intent_low_confidence_rejected(self) -> None:
        """Intent confidence below 0.3 raises a validation error."""
        with pytest.raises(ValueError, match="too low"):
            ChatActionIntent(
                intent="proposed_edit",
                confidence=0.2,
            )


# ---------------------------------------------------------------------------
# Tests: Autonomy-mode routing decisions
# ---------------------------------------------------------------------------


class TestAutonomyModeRouting:
    """Validate that autonomy mode correctly determines approval requirements."""

    def test_human_review_always_requires_approval(self) -> None:
        """In human_review mode, all actions require human approval."""
        router = ChatActionRouter(
            action_repository=MagicMock(),
            chat_repository=MagicMock(),
            model_gateway=MagicMock(),
            grounding_service=MagicMock(),
            entity_repo=MagicMock(),
        )

        intents = [
            "proposed_edit",
            "approval_request",
            "document_request",
            "explanation",
            "workflow_action",
            "reconciliation_query",
            "report_action",
        ]

        for intent_str in intents:
            intent = ChatActionIntent(
                intent=intent_str,  # type: ignore[arg-type]
                confidence=0.8,
            )
            requires = router._determine_approval_requirement(
                intent=intent,
                autonomy_mode=AutonomyMode.HUMAN_REVIEW.value,
                proposed_edit=None,
            )
            assert requires is True, f"{intent_str} should require approval in human_review mode"

    def test_reduced_interruption_proposed_edit_requires_approval(self) -> None:
        """In reduced_interruption mode, proposed edits still require approval."""
        router = ChatActionRouter(
            action_repository=MagicMock(),
            chat_repository=MagicMock(),
            model_gateway=MagicMock(),
            grounding_service=MagicMock(),
            entity_repo=MagicMock(),
        )
        intent = ChatActionIntent(
            intent="proposed_edit",
            confidence=0.8,
        )
        requires = router._determine_approval_requirement(
            intent=intent,
            autonomy_mode=AutonomyMode.REDUCED_INTERRUPTION.value,
            proposed_edit=None,
        )
        assert requires is True

    def test_reduced_interruption_explanation_no_approval(self) -> None:
        """In reduced_interruption mode, explanations do not require approval."""
        router = ChatActionRouter(
            action_repository=MagicMock(),
            chat_repository=MagicMock(),
            model_gateway=MagicMock(),
            grounding_service=MagicMock(),
            entity_repo=MagicMock(),
        )
        intent = ChatActionIntent(
            intent="explanation",
            confidence=0.8,
        )
        requires = router._determine_approval_requirement(
            intent=intent,
            autonomy_mode=AutonomyMode.REDUCED_INTERRUPTION.value,
        )
        assert requires is False


# ---------------------------------------------------------------------------
# Tests: Execution plan construction
# ---------------------------------------------------------------------------


class TestExecutionPlanConstruction:
    """Validate action execution plan assembly from classified intents."""

    def test_build_plan_from_proposed_edit(self, thread_id: UUID, entity_id: UUID) -> None:
        """A proposed edit intent produces a plan with the edit payload."""
        router = ChatActionRouter(
            action_repository=MagicMock(),
            chat_repository=MagicMock(),
            model_gateway=MagicMock(),
            grounding_service=MagicMock(),
            entity_repo=MagicMock(),
        )

        target_id = uuid4()
        intent = ChatActionIntent(
            intent="proposed_edit",
            confidence=0.85,
            target_type="recommendation",
            target_id=target_id,
        )
        proposed_edit = ProposedEditPayload(
            target_type="recommendation",
            target_id=target_id,
            field_path="payload.account_code",
            current_value="4000",
            proposed_value="5000",
            reasoning="Corrected account code",
        )

        grounding = FakeGroundingContext(
            entity_id=str(entity_id),
            entity_name="Test Entity",
            autonomy_mode=AutonomyMode.HUMAN_REVIEW.value,
            base_currency="NGN",
        )

        plan = router.build_execution_plan(
            thread_id=thread_id,
            message_id=None,
            entity_id=entity_id,
            close_run_id=None,
            actor_user_id=uuid4(),
            intent=intent,
            grounding=grounding,  # type: ignore[arg-type]
            reasoning="Test plan",
            proposed_edit=proposed_edit,
        )

        assert plan.intent.intent == "proposed_edit"
        assert plan.proposed_edit is not None
        assert plan.proposed_edit.field_path == "payload.account_code"
        assert plan.requires_human_approval is True


# ---------------------------------------------------------------------------
# Tests: Proposed changes lifecycle transitions
# ---------------------------------------------------------------------------


class TestProposedChangesLifecycle:
    """Validate the full lifecycle of proposed edit action plans."""

    def test_create_and_approve_proposed_edit(
        self,
        action_repo: FakeActionRepository,
        user_id: UUID,
        entity_id: UUID,
        thread_id: UUID,
    ) -> None:
        """A pending proposed edit can be approved and transitions correctly."""
        service = ProposedChangesService(action_repository=action_repo)

        # Create a proposed edit action plan
        plan = action_repo.create_action_plan(
            thread_id=thread_id,
            message_id=None,
            entity_id=entity_id,
            close_run_id=None,
            actor_user_id=user_id,
            intent="proposed_edit",
            target_type="recommendation",
            target_id=uuid4(),
            payload={
                "proposed_edit": {
                    "target_type": "recommendation",
                    "target_id": str(uuid4()),
                    "field_path": "payload.account_code",
                    "current_value": "4000",
                    "proposed_value": "5000",
                    "reasoning": "Corrected",
                    "evidence_refs": [],
                }
            },
            confidence=0.85,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW.value,
            requires_human_approval=True,
            reasoning="Test edit",
        )

        # Approve the plan
        approved = service.approve_proposed_edit(
            action_plan_id=plan.id,
            actor_user_id=user_id,
            reason="Looks correct",
        )

        assert approved.status == "approved"
        assert approved.applied_result is not None
        assert approved.applied_result["approved_by"] == str(user_id)

    def test_reject_proposed_edit_requires_reason(
        self,
        action_repo: FakeActionRepository,
        user_id: UUID,
        entity_id: UUID,
        thread_id: UUID,
    ) -> None:
        """Rejecting a proposed edit requires a non-empty reason."""
        service = ProposedChangesService(action_repository=action_repo)

        plan = action_repo.create_action_plan(
            thread_id=thread_id,
            message_id=None,
            entity_id=entity_id,
            close_run_id=None,
            actor_user_id=user_id,
            intent="proposed_edit",
            target_type="recommendation",
            target_id=uuid4(),
            payload={},
            confidence=0.8,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW.value,
            requires_human_approval=True,
            reasoning="Test edit",
        )

        rejected = service.reject_proposed_edit(
            action_plan_id=plan.id,
            actor_user_id=user_id,
            reason="Incorrect account code",
        )

        assert rejected.status == "rejected"
        assert rejected.rejected_reason == "Incorrect account code"

    def test_cannot_approve_already_approved(
        self,
        action_repo: FakeActionRepository,
        user_id: UUID,
        entity_id: UUID,
        thread_id: UUID,
    ) -> None:
        """Approving an already-approved plan raises an invalid transition error."""
        service = ProposedChangesService(action_repository=action_repo)

        plan = action_repo.create_action_plan(
            thread_id=thread_id,
            message_id=None,
            entity_id=entity_id,
            close_run_id=None,
            actor_user_id=user_id,
            intent="proposed_edit",
            target_type="recommendation",
            target_id=uuid4(),
            payload={},
            confidence=0.8,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW.value,
            requires_human_approval=True,
            reasoning="Test",
        )

        # First approval
        service.approve_proposed_edit(
            action_plan_id=plan.id,
            actor_user_id=user_id,
        )

        # Second approval should fail
        with pytest.raises(ProposedChangesError) as exc_info:
            service.approve_proposed_edit(
                action_plan_id=plan.id,
                actor_user_id=user_id,
            )

        assert exc_info.value.code == ProposedChangesErrorCode.INVALID_TRANSITION

    def test_cannot_approve_non_proposed_edit(
        self,
        action_repo: FakeActionRepository,
        user_id: UUID,
        entity_id: UUID,
        thread_id: UUID,
    ) -> None:
        """Approving a non-proposed-edit intent raises a validation error."""
        service = ProposedChangesService(action_repository=action_repo)

        plan = action_repo.create_action_plan(
            thread_id=thread_id,
            message_id=None,
            entity_id=entity_id,
            close_run_id=None,
            actor_user_id=user_id,
            intent="explanation",  # Not a proposed_edit
            target_type=None,
            target_id=None,
            payload={},
            confidence=0.8,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW.value,
            requires_human_approval=False,
            reasoning="Test",
        )

        with pytest.raises(ProposedChangesError) as exc_info:
            service.approve_proposed_edit(
                action_plan_id=plan.id,
                actor_user_id=user_id,
            )

        assert exc_info.value.code == ProposedChangesErrorCode.VALIDATION_FAILED

    def test_supersede_pending_actions_for_target(
        self,
        action_repo: FakeActionRepository,
        user_id: UUID,
        entity_id: UUID,
        thread_id: UUID,
    ) -> None:
        """Creating a new action supersedes pending actions for the same target."""
        target_id = uuid4()

        # Create first pending action for target
        action_repo.create_action_plan(
            thread_id=thread_id,
            message_id=None,
            entity_id=entity_id,
            close_run_id=None,
            actor_user_id=user_id,
            intent="proposed_edit",
            target_type="recommendation",
            target_id=target_id,
            payload={},
            confidence=0.7,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW.value,
            requires_human_approval=True,
            reasoning="First action",
        )

        # Create second pending action for same target
        new_plan = action_repo.create_action_plan(
            thread_id=thread_id,
            message_id=None,
            entity_id=entity_id,
            close_run_id=None,
            actor_user_id=user_id,
            intent="proposed_edit",
            target_type="recommendation",
            target_id=target_id,
            payload={},
            confidence=0.85,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW.value,
            requires_human_approval=True,
            reasoning="Second action",
        )

        # Supersede the first with the second
        count = action_repo.supersede_pending_actions_for_target(
            target_type="recommendation",
            target_id=target_id,
            superseded_by_id=new_plan.id,
        )

        assert count >= 1

    def test_list_pending_for_target(
        self,
        action_repo: FakeActionRepository,
        user_id: UUID,
        entity_id: UUID,
        thread_id: UUID,
    ) -> None:
        """Only pending actions for a target are returned."""
        target_id = uuid4()

        action_repo.create_action_plan(
            thread_id=thread_id,
            message_id=None,
            entity_id=entity_id,
            close_run_id=None,
            actor_user_id=user_id,
            intent="proposed_edit",
            target_type="recommendation",
            target_id=target_id,
            payload={},
            confidence=0.8,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW.value,
            requires_human_approval=True,
            reasoning="Pending action",
        )

        pending = action_repo.list_actions_for_target(
            target_type="recommendation",
            target_id=target_id,
            status="pending",
        )

        assert len(pending) == 1
        assert pending[0].status == "pending"


# ---------------------------------------------------------------------------
# Tests: Entity membership guards
# ---------------------------------------------------------------------------


class TestMembershipGuards:
    """Validate that action routing enforces entity membership."""

    def test_access_denied_for_non_member(self) -> None:
        """The router rejects action classification for non-members."""
        entity_repo = FakeEntityRepo(allow_access=False)
        chat_repo = FakeChatRepository()
        router = ChatActionRouter(
            action_repository=FakeActionRepository(),
            chat_repository=chat_repo,
            model_gateway=FakeModelGateway(),
            grounding_service=MagicMock(),
            entity_repo=entity_repo,
        )

        grounding = FakeGroundingContext(
            entity_id=str(uuid4()),
            entity_name="Test Entity",
            autonomy_mode=AutonomyMode.HUMAN_REVIEW.value,
            base_currency="NGN",
        )

        with pytest.raises(ChatActionRouterError) as exc_info:
            router.classify_action_intent(
                thread_id=uuid4(),
                entity_id=uuid4(),
                user_id=uuid4(),
                content="approve this",
                grounding=grounding,  # type: ignore[arg-type]
            )

        assert exc_info.value.code == ChatActionRouterErrorCode.THREAD_ACCESS_DENIED
        assert exc_info.value.status_code == 403


class TestIntentClassification:
    """Validate that the router uses structured classification for dynamic chat routing."""

    def test_prompt_includes_natural_language_action_examples(self) -> None:
        """The classifier prompt should explicitly support normal operator phrasing."""

        router = ChatActionRouter(
            action_repository=FakeActionRepository(),
            chat_repository=FakeChatRepository(),
            model_gateway=FakeModelGateway(),
            grounding_service=MagicMock(),
            entity_repo=FakeEntityRepo(allow_access=True),
        )

        prompt = router._build_classification_prompt(
            context_lines=["Entity: Test Entity"],
            user_message="I need the reports",
        )

        assert "I need the reports" in prompt
        assert "get this ready for sign-off?" in prompt
        assert "reopen this close run so we can make changes" in prompt
        assert "take this back to reconciliation" in prompt
        assert "take this back to collection so I can upload more files" in prompt
        assert "start over from document intake" in prompt
        assert "start a new April close run" in prompt
        assert "open a fresh run for this month" in prompt
        assert "create another run for this period" in prompt
        assert "archive this run" in prompt
        assert "ignore the PDF I uploaded by mistake" in prompt
        assert "asking the system to make progress" in prompt

    def test_explanation_intent_returns_none(self, entity_id: UUID, user_id: UUID) -> None:
        """Purely informational messages should stay on the read-only path."""

        thread_id = uuid4()
        chat_repo = FakeChatRepository()
        chat_repo._threads = {
            thread_id: {
                "id": thread_id,
                "entity_id": entity_id,
                "close_run_id": None,
                "context_payload": {},
                "title": "General thread",
            }
        }
        router = ChatActionRouter(
            action_repository=FakeActionRepository(),
            chat_repository=chat_repo,
            model_gateway=FakeModelGateway(
                response='{"intent": "explanation", "confidence": 0.92, "reasoning": "Question only"}'
            ),
            grounding_service=MagicMock(),
            entity_repo=FakeEntityRepo(allow_access=True),
        )
        grounding = FakeGroundingContext(
            entity_id=str(entity_id),
            entity_name="Test Entity",
            autonomy_mode=AutonomyMode.HUMAN_REVIEW.value,
            base_currency="NGN",
        )

        result = router.classify_action_intent(
            thread_id=thread_id,
            entity_id=entity_id,
            user_id=user_id,
            content="What can you do next in this close run?",
            grounding=grounding,  # type: ignore[arg-type]
        )

        assert result is None

    def test_workflow_intent_returns_action(self, entity_id: UUID, user_id: UUID) -> None:
        """Workflow requests should produce a structured action intent."""

        thread_id = uuid4()
        chat_repo = FakeChatRepository()
        chat_repo._threads = {
            thread_id: {
                "id": thread_id,
                "entity_id": entity_id,
                "close_run_id": None,
                "context_payload": {},
                "title": "Workflow thread",
            }
        }
        router = ChatActionRouter(
            action_repository=FakeActionRepository(),
            chat_repository=chat_repo,
            model_gateway=FakeModelGateway(
                response=(
                    '{"intent": "workflow_action", "confidence": 0.94, '
                    '"target_phase": "reconciliation", "requires_review": true, '
                    '"reasoning": "Operator requested a workflow advance"}'
                )
            ),
            grounding_service=MagicMock(),
            entity_repo=FakeEntityRepo(allow_access=True),
        )
        grounding = FakeGroundingContext(
            entity_id=str(entity_id),
            entity_name="Test Entity",
            autonomy_mode=AutonomyMode.HUMAN_REVIEW.value,
            base_currency="NGN",
        )

        result = router.classify_action_intent(
            thread_id=thread_id,
            entity_id=entity_id,
            user_id=user_id,
            content="Advance the close run into reconciliation.",
            grounding=grounding,  # type: ignore[arg-type]
        )

        assert result is not None
        assert result.intent == "workflow_action"
        assert result.target_phase == WorkflowPhase.RECONCILIATION
        assert result.requires_review is True

    def test_invalid_structured_intent_returns_router_error(
        self,
        entity_id: UUID,
        user_id: UUID,
    ) -> None:
        """Unsupported structured intent labels should fail as classification errors."""

        thread_id = uuid4()
        chat_repo = FakeChatRepository()
        chat_repo._threads = {
            thread_id: {
                "id": thread_id,
                "entity_id": entity_id,
                "close_run_id": None,
                "context_payload": {},
                "title": "Workflow thread",
            }
        }
        router = ChatActionRouter(
            action_repository=FakeActionRepository(),
            chat_repository=chat_repo,
            model_gateway=FakeModelGateway(
                response='{"intent": "workflow", "confidence": 0.94, "reasoning": "Near miss"}'
            ),
            grounding_service=MagicMock(),
            entity_repo=FakeEntityRepo(allow_access=True),
        )
        grounding = FakeGroundingContext(
            entity_id=str(entity_id),
            entity_name="Test Entity",
            autonomy_mode=AutonomyMode.HUMAN_REVIEW.value,
            base_currency="NGN",
        )

        with pytest.raises(ChatActionRouterError) as exc_info:
            router.classify_action_intent(
                thread_id=thread_id,
                entity_id=entity_id,
                user_id=user_id,
                content="Advance the close run into reconciliation.",
                grounding=grounding,  # type: ignore[arg-type]
            )

        assert exc_info.value.code == ChatActionRouterErrorCode.INTENT_CLASSIFICATION_FAILED
        assert exc_info.value.status_code == 422

    def test_list_pending_actions_scopes_to_current_close_run_after_thread_handoff(
        self,
        entity_id: UUID,
        user_id: UUID,
    ) -> None:
        """Pending approvals from an older run should not leak into a thread's current run scope."""

        thread_id = uuid4()
        current_close_run_id = uuid4()
        previous_close_run_id = uuid4()
        action_repo = FakeActionRepository()
        chat_repo = FakeChatRepository()
        chat_repo._threads = {
            thread_id: {
                "id": thread_id,
                "entity_id": entity_id,
                "close_run_id": current_close_run_id,
                "context_payload": {},
                "title": "Close Run Thread",
            }
        }
        now = datetime.now(timezone.utc)
        current_plan = FakeActionPlanRecord(
            id=uuid4(),
            thread_id=thread_id,
            message_id=None,
            entity_id=entity_id,
            close_run_id=current_close_run_id,
            actor_user_id=user_id,
            intent="workflow_action",
            target_type=None,
            target_id=None,
            payload={},
            confidence=0.9,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW.value,
            status="pending",
            requires_human_approval=True,
            reasoning="current",
            applied_result=None,
            rejected_reason=None,
            superseded_by_id=None,
            created_at=now,
            updated_at=now,
        )
        previous_plan = FakeActionPlanRecord(
            id=uuid4(),
            thread_id=thread_id,
            message_id=None,
            entity_id=entity_id,
            close_run_id=previous_close_run_id,
            actor_user_id=user_id,
            intent="workflow_action",
            target_type=None,
            target_id=None,
            payload={},
            confidence=0.9,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW.value,
            status="pending",
            requires_human_approval=True,
            reasoning="previous",
            applied_result=None,
            rejected_reason=None,
            superseded_by_id=None,
            created_at=now,
            updated_at=now,
        )
        action_repo._plans = {
            current_plan.id: current_plan,
            previous_plan.id: previous_plan,
        }
        router = ChatActionRouter(
            action_repository=action_repo,
            chat_repository=chat_repo,
            model_gateway=FakeModelGateway(),
            grounding_service=MagicMock(),
            entity_repo=FakeEntityRepo(allow_access=True),
        )

        plans = router.list_pending_actions(
            thread_id=thread_id,
            entity_id=entity_id,
            user_id=user_id,
        )

        assert plans == (current_plan,)

    def test_approve_requires_entity_membership(
        self,
        action_repo: FakeActionRepository,
        user_id: UUID,
        entity_id: UUID,
        thread_id: UUID,
    ) -> None:
        """Approving an action plan from another entity is denied."""
        router = ChatActionRouter(
            action_repository=action_repo,
            chat_repository=FakeChatRepository(),
            model_gateway=FakeModelGateway(),
            grounding_service=MagicMock(),
            entity_repo=FakeEntityRepo(allow_access=False),
        )

        plan = action_repo.create_action_plan(
            thread_id=thread_id,
            message_id=None,
            entity_id=entity_id,
            close_run_id=None,
            actor_user_id=user_id,
            intent="proposed_edit",
            target_type="recommendation",
            target_id=uuid4(),
            payload={},
            confidence=0.8,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW.value,
            requires_human_approval=True,
            reasoning="Test",
        )

        with pytest.raises(ChatActionRouterError) as exc_info:
            router.approve_action_plan(
                action_plan_id=plan.id,
                thread_id=thread_id,
                entity_id=entity_id,
                actor_user_id=user_id,
                source_surface=MagicMock(value="desktop"),
                trace_id=None,
            )

        assert exc_info.value.code == ChatActionRouterErrorCode.THREAD_ACCESS_DENIED
        assert exc_info.value.status_code == 403

    def test_reject_requires_entity_membership(
        self,
        action_repo: FakeActionRepository,
        user_id: UUID,
        entity_id: UUID,
        thread_id: UUID,
    ) -> None:
        """Rejecting an action plan from another entity is denied."""
        router = ChatActionRouter(
            action_repository=action_repo,
            chat_repository=FakeChatRepository(),
            model_gateway=FakeModelGateway(),
            grounding_service=MagicMock(),
            entity_repo=FakeEntityRepo(allow_access=False),
        )

        plan = action_repo.create_action_plan(
            thread_id=thread_id,
            message_id=None,
            entity_id=entity_id,
            close_run_id=None,
            actor_user_id=user_id,
            intent="proposed_edit",
            target_type="recommendation",
            target_id=uuid4(),
            payload={},
            confidence=0.8,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW.value,
            requires_human_approval=True,
            reasoning="Test",
        )

        with pytest.raises(ChatActionRouterError) as exc_info:
            router.reject_action_plan(
                action_plan_id=plan.id,
                thread_id=thread_id,
                entity_id=entity_id,
                actor_user_id=user_id,
                reason="Wrong entity",
                source_surface=MagicMock(value="desktop"),
                trace_id=None,
            )

        assert exc_info.value.code == ChatActionRouterErrorCode.THREAD_ACCESS_DENIED
        assert exc_info.value.status_code == 403

    def test_approve_requires_plan_entity_match(
        self,
        action_repo: FakeActionRepository,
        user_id: UUID,
        entity_id: UUID,
        thread_id: UUID,
    ) -> None:
        """Approving with a mismatched entity_id is denied even if the user has membership."""
        other_entity_id = uuid4()
        entity_repo = FakeEntityRepo(allow_access=True)
        router = ChatActionRouter(
            action_repository=action_repo,
            chat_repository=FakeChatRepository(),
            model_gateway=FakeModelGateway(),
            grounding_service=MagicMock(),
            entity_repo=entity_repo,
        )

        plan = action_repo.create_action_plan(
            thread_id=thread_id,
            message_id=None,
            entity_id=entity_id,
            close_run_id=None,
            actor_user_id=user_id,
            intent="proposed_edit",
            target_type="recommendation",
            target_id=uuid4(),
            payload={},
            confidence=0.8,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW.value,
            requires_human_approval=True,
            reasoning="Test",
        )

        with pytest.raises(ChatActionRouterError) as exc_info:
            router.approve_action_plan(
                action_plan_id=plan.id,
                thread_id=thread_id,
                entity_id=other_entity_id,
                actor_user_id=user_id,
                source_surface=MagicMock(value="desktop"),
                trace_id=None,
            )

        assert exc_info.value.code == ChatActionRouterErrorCode.THREAD_ACCESS_DENIED
        assert exc_info.value.status_code == 403
