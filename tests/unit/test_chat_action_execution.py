"""
Purpose: Regression coverage for chat action scope handoff and approval execution.
Scope: Pending approval scope resolution and thread handoff rebinding behavior.
Dependencies: ChatActionExecutor and lightweight repository/grounding doubles.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

from services.chat.action_execution import (
    ChatActionExecutionError,
    ChatActionExecutionErrorCode,
    ChatActionExecutor,
)
from services.db.repositories.chat_action_repo import ChatActionPlanRecord
from services.db.repositories.entity_repo import EntityUserRecord


def test_resolve_action_execution_scopes_uses_current_thread_scope_and_original_source() -> None:
    """Approvals should execute against the current thread scope while remapping from the source run."""

    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    source_close_run_id = uuid4()
    current_close_run_id = uuid4()
    plan = _build_plan(close_run_id=current_close_run_id)

    execution_close_run_id, original_source_close_run_id = executor._resolve_action_execution_scopes(
        thread=SimpleNamespace(close_run_id=current_close_run_id),
        plan=plan,
        payload={"source_close_run_id": str(source_close_run_id)},
    )

    assert execution_close_run_id == current_close_run_id
    assert original_source_close_run_id == source_close_run_id


def test_handoff_thread_scope_rebinds_pending_actions_to_reopened_close_run() -> None:
    """Thread handoff should move pending approvals onto the reopened close run."""

    previous_close_run_id = uuid4()
    reopened_close_run_id = uuid4()
    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    thread_id = uuid4()
    entity_id = uuid4()
    fake_action_repo = _FakeActionRepository()
    fake_chat_repo = _FakeChatRepository(reopened_close_run_id=reopened_close_run_id)
    fake_grounding = _FakeGroundingService(reopened_close_run_id=reopened_close_run_id)
    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._action_repo = fake_action_repo
    executor._chat_repo = fake_chat_repo
    executor._grounding = fake_grounding

    _, updated_thread, handoff_message = executor._handoff_thread_scope_if_needed(
        actor_user=actor_user,
        entity_id=entity_id,
        thread_id=thread_id,
        thread=SimpleNamespace(close_run_id=previous_close_run_id, context_payload={"mode": "chat"}),
        grounding=SimpleNamespace(context=SimpleNamespace()),
        applied_result={
            "reopened_close_run_id": str(reopened_close_run_id),
            "version_no": 2,
            "reopened_from_status": "approved",
            "active_phase": "processing",
        },
    )

    assert fake_action_repo.rebind_calls == [
        {
            "thread_id": thread_id,
            "from_close_run_id": previous_close_run_id,
            "to_close_run_id": reopened_close_run_id,
        }
    ]
    assert updated_thread.close_run_id == reopened_close_run_id
    assert handoff_message is not None
    assert "working version 2" in handoff_message


def test_handoff_thread_scope_supersedes_old_pending_actions_for_created_close_run() -> None:
    """Starting a brand-new run should retire stale pending approvals from the earlier run."""

    previous_close_run_id = uuid4()
    created_close_run_id = uuid4()
    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    thread_id = uuid4()
    entity_id = uuid4()
    fake_action_repo = _FakeActionRepository()
    fake_chat_repo = _FakeChatRepository(reopened_close_run_id=created_close_run_id)
    fake_grounding = _FakeGroundingService(reopened_close_run_id=created_close_run_id)
    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._action_repo = fake_action_repo
    executor._chat_repo = fake_chat_repo
    executor._grounding = fake_grounding

    _, updated_thread, handoff_message = executor._handoff_thread_scope_if_needed(
        actor_user=actor_user,
        entity_id=entity_id,
        thread_id=thread_id,
        thread=SimpleNamespace(close_run_id=previous_close_run_id, context_payload={"mode": "chat"}),
        grounding=SimpleNamespace(context=SimpleNamespace()),
        applied_result={
            "created_close_run_id": str(created_close_run_id),
            "version_no": 1,
            "period_start": "2026-04-01",
            "period_end": "2026-04-30",
            "active_phase": "collection",
        },
    )

    assert fake_action_repo.rebind_calls == []
    assert fake_action_repo.supersede_calls == [
        {
            "thread_id": thread_id,
            "close_run_id": previous_close_run_id,
        }
    ]
    assert updated_thread.close_run_id == created_close_run_id
    assert handoff_message is not None
    assert "started a new close run" in handoff_message


def test_approve_action_plan_rejects_stale_scope_after_thread_handoff() -> None:
    """Approving a stale pending action should fail once the thread has moved to another run."""

    thread_id = uuid4()
    previous_close_run_id = uuid4()
    current_close_run_id = uuid4()
    action_plan_id = uuid4()
    entity_id = uuid4()
    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")

    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._action_repo = SimpleNamespace(
        get_action_plan_for_thread=lambda **kwargs: _build_plan(
            close_run_id=previous_close_run_id,
            action_plan_id=action_plan_id,
            thread_id=thread_id,
            entity_id=entity_id,
        )
    )
    executor._chat_repo = SimpleNamespace(
        get_thread_for_entity=lambda **kwargs: SimpleNamespace(close_run_id=current_close_run_id)
    )

    try:
        executor.approve_action_plan(
            action_plan_id=action_plan_id,
            thread_id=thread_id,
            entity_id=entity_id,
            actor_user=actor_user,
            reason=None,
            source_surface="desktop",
            trace_id="trace-stale-scope",
        )
    except ChatActionExecutionError as error:
        assert error.status_code == 409
        assert error.code is ChatActionExecutionErrorCode.INVALID_ACTION_PLAN
        assert "previous close-run scope" in error.message
    else:
        raise AssertionError("Expected stale-scope approval to be rejected.")


def _build_plan(
    *,
    close_run_id: UUID,
    action_plan_id: UUID | None = None,
    thread_id: UUID | None = None,
    entity_id: UUID | None = None,
) -> ChatActionPlanRecord:
    """Return one minimal immutable action plan record for scope-resolution tests."""

    now = datetime(2026, 4, 16, 12, 0, tzinfo=UTC)
    return ChatActionPlanRecord(
        id=action_plan_id or uuid4(),
        thread_id=thread_id or uuid4(),
        message_id=None,
        entity_id=entity_id or uuid4(),
        close_run_id=close_run_id,
        actor_user_id=uuid4(),
        intent="workflow_action",
        target_type=None,
        target_id=None,
        payload={},
        confidence=1.0,
        autonomy_mode="human_review",
        status="pending",
        requires_human_approval=True,
        reasoning="reasoning",
        applied_result=None,
        rejected_reason=None,
        superseded_by_id=None,
        created_at=now,
        updated_at=now,
    )


class _FakeActionRepository:
    def __init__(self) -> None:
        self.rebind_calls: list[dict[str, UUID]] = []
        self.supersede_calls: list[dict[str, UUID]] = []

    def rebind_pending_actions_to_close_run(
        self,
        *,
        thread_id: UUID,
        from_close_run_id: UUID,
        to_close_run_id: UUID,
    ) -> int:
        self.rebind_calls.append(
            {
                "thread_id": thread_id,
                "from_close_run_id": from_close_run_id,
                "to_close_run_id": to_close_run_id,
            }
        )
        return 1

    def supersede_pending_actions_for_close_run_scope(
        self,
        *,
        thread_id: UUID,
        close_run_id: UUID,
    ) -> int:
        self.supersede_calls.append(
            {
                "thread_id": thread_id,
                "close_run_id": close_run_id,
            }
        )
        return 1


class _FakeChatRepository:
    def __init__(self, *, reopened_close_run_id: UUID) -> None:
        self.reopened_close_run_id = reopened_close_run_id

    def update_thread_scope(
        self,
        *,
        thread_id: UUID,
        close_run_id: UUID,
        context_payload: dict[str, object],
    ):
        del thread_id
        return SimpleNamespace(close_run_id=close_run_id, context_payload=context_payload)


class _FakeGroundingService:
    def __init__(self, *, reopened_close_run_id: UUID) -> None:
        self.reopened_close_run_id = reopened_close_run_id

    def resolve_context(self, **kwargs):
        del kwargs
        return SimpleNamespace(context=SimpleNamespace())

    def build_context_payload(self, *, context: object) -> dict[str, str]:
        del context
        return {"close_run_id": str(self.reopened_close_run_id)}
