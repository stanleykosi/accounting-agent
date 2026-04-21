"""
Purpose: Regression coverage for chat action scope handoff and approval execution.
Scope: Pending approval scope resolution and thread handoff rebinding behavior.
Dependencies: ChatActionExecutor and lightweight repository/grounding doubles.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

from services.agents.models import AgentPlanningResult
from services.chat.action_execution import (
    ChatActionExecutionError,
    ChatActionExecutionErrorCode,
    ChatActionExecutor,
)
from services.db.repositories.chat_action_repo import ChatActionPlanRecord
from services.db.repositories.entity_repo import EntityUserRecord


def test_resolve_action_execution_scopes_uses_current_thread_scope_and_original_source() -> None:
    """Approvals should execute in the current thread scope and remap from the source run."""

    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    source_close_run_id = uuid4()
    current_close_run_id = uuid4()
    plan = _build_plan(close_run_id=current_close_run_id)

    (
        execution_close_run_id,
        original_source_close_run_id,
    ) = executor._resolve_action_execution_scopes(
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
        thread=SimpleNamespace(
            close_run_id=previous_close_run_id,
            context_payload={"mode": "chat"},
        ),
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
        thread=SimpleNamespace(
            close_run_id=previous_close_run_id,
            context_payload={"mode": "chat"},
        ),
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


def test_hydrate_planning_result_resolves_single_document_and_review_flags() -> None:
    """The chat executor should turn 'approve it' into an executable document review."""

    document_id = uuid4()
    executor = ChatActionExecutor.__new__(ChatActionExecutor)

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="**I'll take care of that.**",
            reasoning="The operator explicitly asked to approve the remaining document.",
            tool_name="review_document",
            tool_arguments={},
        ),
        snapshot={
            "documents": [
                {
                    "id": str(document_id),
                    "filename": "invoice-axis-haulage-2026-03.pdf",
                    "status": "needs_review",
                    "document_type": "invoice",
                }
            ]
        },
        operator_content="approve it",
    )

    assert hydrated.tool_arguments["document_id"] == str(document_id)
    assert hydrated.tool_arguments["decision"] == "approved"
    assert hydrated.tool_arguments["verified_complete"] is True
    assert hydrated.tool_arguments["verified_authorized"] is True
    assert hydrated.tool_arguments["verified_period"] is True
    assert "*" not in hydrated.assistant_response


def test_hydrate_planning_result_resolves_recommendation_rejection_in_chat() -> None:
    """The chat executor should resolve a single recommendation and fill a safe reason."""

    recommendation_id = uuid4()
    executor = ChatActionExecutor.__new__(ChatActionExecutor)

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="**I'll reject it.**",
            reasoning="Only one recommendation is pending review in the workspace.",
            tool_name="reject_recommendation",
            tool_arguments={},
        ),
        snapshot={
            "recommendations": [
                {
                    "id": str(recommendation_id),
                    "status": "pending_review",
                    "recommendation_type": "gl_coding",
                    "document_filename": "invoice-axis-haulage-2026-03.pdf",
                    "reasoning_summary": "Code the haulage invoice to transport expense.",
                }
            ]
        },
        operator_content="reject it",
    )

    assert hydrated.tool_arguments["recommendation_id"] == str(recommendation_id)
    assert (
        hydrated.tool_arguments["reason"] == "Rejected by operator instruction in chat."
    )
    assert "*" not in hydrated.assistant_response


def test_hydrate_planning_result_resolves_journal_apply_to_internal_ledger() -> None:
    """The chat executor should resolve one approved journal and default the posting target."""

    journal_id = uuid4()
    executor = ChatActionExecutor.__new__(ChatActionExecutor)

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll post it now.",
            reasoning="There is one approved journal ready to apply.",
            tool_name="apply_journal",
            tool_arguments={},
        ),
        snapshot={
            "journals": [
                {
                    "id": str(journal_id),
                    "status": "approved",
                    "journal_number": "JE-2026-00001",
                    "description": "Haulage expense accrual",
                }
            ]
        },
        operator_content="apply it",
    )

    assert hydrated.tool_arguments["journal_id"] == str(journal_id)
    assert hydrated.tool_arguments["posting_target"] == "internal_ledger"


def test_hydrate_planning_result_resolves_single_reconciliation_item_disposition() -> None:
    """The chat executor should resolve one pending reconciliation exception in chat."""

    item_id = uuid4()
    executor = ChatActionExecutor.__new__(ChatActionExecutor)

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll clear that exception.",
            reasoning="There is one unresolved reconciliation item in scope.",
            tool_name="disposition_reconciliation_item",
            tool_arguments={},
        ),
        snapshot={
            "reconciliation_items": [
                {
                    "id": str(item_id),
                    "source_ref": "BANK-2026-03-001",
                    "match_status": "exception",
                    "requires_disposition": True,
                    "disposition": None,
                    "explanation": "Timing difference on bank statement import.",
                }
            ]
        },
        operator_content="resolve it",
    )

    assert hydrated.tool_arguments["item_id"] == str(item_id)
    assert hydrated.tool_arguments["disposition"] == "resolved"
    assert (
        hydrated.tool_arguments["reason"]
        == "Marked as resolved by operator instruction."
    )


def test_hydrate_planning_result_resolves_commentary_section_from_chat() -> None:
    """The chat executor should resolve the latest report run and commentary section."""

    report_run_id = uuid4()
    executor = ChatActionExecutor.__new__(ChatActionExecutor)

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll approve that section now.",
            reasoning="The operator referenced one draft commentary section by name.",
            tool_name="approve_commentary",
            tool_arguments={},
        ),
        snapshot={
            "report_runs": [
                {
                    "id": str(report_run_id),
                    "status": "completed",
                    "version_no": 1,
                }
            ],
            "commentary": [
                {
                    "id": str(uuid4()),
                    "report_run_id": str(report_run_id),
                    "report_version_no": 1,
                    "section_key": "cash_flow",
                    "status": "draft",
                    "body": "Operating cash flow improved.",
                }
            ],
        },
        operator_content="approve the cash flow commentary",
    )

    assert hydrated.tool_arguments["report_run_id"] == str(report_run_id)
    assert hydrated.tool_arguments["section_key"] == "cash_flow"


def test_hydrate_planning_result_defaults_workspace_update_to_current_scope() -> None:
    """Workspace updates should default to the current workspace when no target is named."""

    workspace_id = uuid4()
    executor = ChatActionExecutor.__new__(ChatActionExecutor)

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll update the workspace now.",
            reasoning="The operator asked to update the current workspace settings.",
            tool_name="update_workspace",
            tool_arguments={"name": "Apex Meridian West Africa"},
        ),
        snapshot={
            "workspace": {
                "id": str(workspace_id),
                "base_currency": "NGN",
                "country_code": "NG",
                "timezone": "Africa/Lagos",
                "autonomy_mode": "human_review",
            }
        },
        operator_content="rename this workspace to Apex Meridian West Africa",
    )

    assert hydrated.tool_arguments["workspace_id"] == str(workspace_id)


def test_hydrate_planning_result_fills_create_workspace_defaults_from_current_scope() -> None:
    """Workspace creation should inherit canonical defaults from the current workspace."""

    executor = ChatActionExecutor.__new__(ChatActionExecutor)

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll create that workspace.",
            reasoning="The operator named a workspace and omitted optional setup defaults.",
            tool_name="create_workspace",
            tool_arguments={"name": "Apex Meridian Ghana Ltd"},
        ),
        snapshot={
            "workspace": {
                "id": str(uuid4()),
                "base_currency": "NGN",
                "country_code": "NG",
                "timezone": "Africa/Lagos",
                "autonomy_mode": "human_review",
            }
        },
        operator_content="create a new workspace called Apex Meridian Ghana Ltd",
    )

    assert hydrated.tool_arguments["base_currency"] == "NGN"
    assert hydrated.tool_arguments["country_code"] == "NG"
    assert hydrated.tool_arguments["timezone"] == "Africa/Lagos"
    assert hydrated.tool_arguments["autonomy_mode"] == "human_review"


def test_hydrate_planning_result_resolves_named_workspace_delete() -> None:
    """Workspace deletion should resolve a named accessible workspace from the snapshot."""

    target_workspace_id = uuid4()
    executor = ChatActionExecutor.__new__(ChatActionExecutor)

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll prepare that deletion.",
            reasoning="The operator named a single accessible workspace to delete.",
            tool_name="delete_workspace",
            tool_arguments={},
        ),
        snapshot={
            "workspace": {
                "id": str(uuid4()),
                "name": "Apex Meridian Nigeria Ltd",
            },
            "accessible_workspaces": [
                {
                    "id": str(target_workspace_id),
                    "name": "Zenith Shared Services Ltd",
                }
            ],
        },
        operator_content="delete the Zenith Shared Services workspace",
    )

    assert hydrated.tool_arguments["workspace_id"] == str(target_workspace_id)


def test_handoff_thread_scope_moves_to_workspace_after_close_run_delete() -> None:
    """Deleting the active close run should move the chat thread back to workspace scope."""

    deleted_close_run_id = uuid4()
    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    thread_id = uuid4()
    entity_id = uuid4()
    fake_action_repo = _FakeActionRepository()
    fake_chat_repo = _FakeChatRepository(reopened_close_run_id=deleted_close_run_id)
    fake_grounding = _FakeGroundingService(
        reopened_close_run_id=deleted_close_run_id,
        entity_id=entity_id,
    )
    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._action_repo = fake_action_repo
    executor._chat_repo = fake_chat_repo
    executor._grounding = fake_grounding

    _, updated_thread, handoff_message = executor._handoff_thread_scope_if_needed(
        actor_user=actor_user,
        entity_id=entity_id,
        thread_id=thread_id,
        thread=SimpleNamespace(
            close_run_id=deleted_close_run_id,
            context_payload={"mode": "chat"},
        ),
        grounding=SimpleNamespace(context=SimpleNamespace()),
        applied_result={
            "deleted_close_run_id": str(deleted_close_run_id),
        },
    )

    assert fake_action_repo.supersede_calls == [
        {
            "thread_id": thread_id,
            "close_run_id": deleted_close_run_id,
        }
    ]
    assert updated_thread.close_run_id is None
    assert handoff_message is not None
    assert "workspace scope" in handoff_message


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
        close_run_id: UUID | None,
        context_payload: dict[str, object],
    ):
        del thread_id
        return SimpleNamespace(close_run_id=close_run_id, context_payload=context_payload)


class _FakeGroundingService:
    def __init__(self, *, reopened_close_run_id: UUID, entity_id: UUID | None = None) -> None:
        self.reopened_close_run_id = reopened_close_run_id
        self.entity_id = entity_id

    def resolve_context(self, **kwargs):
        close_run_id = kwargs.get("close_run_id")
        resolved_entity_id = kwargs.get("entity_id")
        return SimpleNamespace(
            context=SimpleNamespace(
                entity_id=str(resolved_entity_id or self.entity_id or uuid4()),
                entity_name="Apex Meridian Nigeria Ltd",
                close_run_id=str(close_run_id) if close_run_id is not None else None,
                period_label="Mar 2026" if close_run_id is not None else None,
                autonomy_mode="human_review",
                base_currency="NGN",
            )
        )

    def build_context_payload(self, *, context: object) -> dict[str, str]:
        resolved_context = context
        payload = {
            "entity_id": resolved_context.entity_id,
            "entity_name": resolved_context.entity_name,
            "autonomy_mode": resolved_context.autonomy_mode,
            "base_currency": resolved_context.base_currency,
        }
        close_run_id = resolved_context.close_run_id
        period_label = resolved_context.period_label
        if close_run_id is not None:
            payload["close_run_id"] = close_run_id
        if period_label is not None:
            payload["period_label"] = period_label
        return payload
