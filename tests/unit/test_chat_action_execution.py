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
from services.chat.continuation_state import (
    build_pending_async_turn_payload,
    new_chat_operator_continuation,
)
from services.chat.operator_memory import seed_context_payload_with_operator_memory
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
            entity_id=entity_id,
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
            entity_id=entity_id,
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
        operator_memory=executor._memory_from_context_payload({}),
    )

    assert hydrated.tool_arguments["document_id"] == str(document_id)
    assert hydrated.tool_arguments["decision"] == "approved"
    assert hydrated.tool_arguments["verified_complete"] is True
    assert hydrated.tool_arguments["verified_authorized"] is True
    assert hydrated.tool_arguments["verified_period"] is True
    assert "*" not in hydrated.assistant_response


def test_handoff_thread_scope_moves_thread_to_switched_workspace() -> None:
    """Workspace switching should move the thread anchor to the requested workspace scope."""

    previous_entity_id = uuid4()
    switched_entity_id = uuid4()
    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    thread_id = uuid4()
    fake_action_repo = _FakeActionRepository()
    fake_chat_repo = _FakeChatRepository(reopened_close_run_id=uuid4())
    fake_grounding = _FakeGroundingService(
        reopened_close_run_id=uuid4(),
        entity_id=switched_entity_id,
    )
    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._action_repo = fake_action_repo
    executor._chat_repo = fake_chat_repo
    executor._grounding = fake_grounding

    _, updated_thread, handoff_message = executor._handoff_thread_scope_if_needed(
        actor_user=actor_user,
        entity_id=previous_entity_id,
        thread_id=thread_id,
        thread=SimpleNamespace(
            entity_id=previous_entity_id,
            close_run_id=None,
            context_payload={"mode": "chat"},
        ),
        grounding=SimpleNamespace(context=SimpleNamespace()),
        applied_result={
            "switched_workspace_id": str(switched_entity_id),
            "workspace_name": "Zenith Shared Services",
        },
    )

    assert updated_thread.entity_id == switched_entity_id
    assert updated_thread.close_run_id is None
    assert handoff_message is None


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
        operator_memory=executor._memory_from_context_payload({}),
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
        operator_memory=executor._memory_from_context_payload({}),
    )

    assert hydrated.tool_arguments["journal_id"] == str(journal_id)
    assert hydrated.tool_arguments["posting_target"] == "internal_ledger"


def test_hydrate_planning_result_does_not_resolve_apply_journal_to_unapproved_singleton() -> None:
    """Applying journals should not auto-target a lone draft or pending-review journal."""

    executor = ChatActionExecutor.__new__(ChatActionExecutor)

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll post it now.",
            reasoning="The operator asked to apply the current journal.",
            tool_name="apply_journal",
            tool_arguments={},
        ),
        snapshot={
            "journals": [
                {
                    "id": str(uuid4()),
                    "status": "pending_review",
                    "journal_number": "JE-2026-00002",
                    "description": "Draft transport accrual",
                }
            ]
        },
        operator_content="apply it",
        operator_memory=executor._memory_from_context_payload({}),
    )

    assert "journal_id" not in hydrated.tool_arguments
    assert hydrated.tool_arguments["posting_target"] == "internal_ledger"


def test_hydrate_planning_result_does_not_apply_named_unapproved_journal() -> None:
    """Even an explicit journal mention should not post before approval."""

    journal_id = uuid4()
    executor = ChatActionExecutor.__new__(ChatActionExecutor)

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll post it now.",
            reasoning="The operator named the journal to apply.",
            tool_name="apply_journal",
            tool_arguments={},
        ),
        snapshot={
            "journals": [
                {
                    "id": str(journal_id),
                    "status": "pending_review",
                    "journal_number": "JE-2026-00002",
                    "description": "Draft transport accrual",
                }
            ]
        },
        operator_content="apply JE-2026-00002",
        operator_memory=executor._memory_from_context_payload({}),
    )

    assert "journal_id" not in hydrated.tool_arguments
    assert hydrated.tool_arguments["posting_target"] == "internal_ledger"


def test_hydrate_planning_result_does_not_apply_remembered_unapproved_journal() -> None:
    """Remembered journal focus should still respect apply_journal approval policy."""

    journal_id = uuid4()
    executor = ChatActionExecutor.__new__(ChatActionExecutor)

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll post it now.",
            reasoning="The operator is following up on the current journal.",
            tool_name="apply_journal",
            tool_arguments={},
        ),
        snapshot={
            "journals": [
                {
                    "id": str(journal_id),
                    "status": "pending_review",
                    "journal_number": "JE-2026-00002",
                    "description": "Draft transport accrual",
                }
            ]
        },
        operator_content="apply it",
        operator_memory=executor._memory_from_context_payload(
            {
                "agent_memory": {
                    "last_target_type": "journal",
                    "last_target_id": str(journal_id),
                    "last_target_label": "journal JE-2026-00002",
                }
            }
        ),
    )

    assert "journal_id" not in hydrated.tool_arguments
    assert hydrated.tool_arguments["posting_target"] == "internal_ledger"


def test_build_runtime_clarification_for_apply_journal_prefers_approval_guidance() -> None:
    """Applying an unapproved singleton journal should surface approval guidance."""

    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._tool_registry = _build_fake_tool_registry("apply_journal")

    clarification = executor._build_runtime_clarification(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll post it now.",
            reasoning="The operator asked to apply the current journal.",
            tool_name="apply_journal",
            tool_arguments={"posting_target": "internal_ledger"},
        ),
        snapshot={
            "journals": [
                {
                    "id": str(uuid4()),
                    "status": "pending_review",
                    "journal_number": "JE-2026-00002",
                    "description": "Draft transport accrual",
                }
            ]
        },
    )

    assert clarification is not None
    assert "There isn't an approved journal ready to post yet." in clarification
    assert "approve je-2026-00002 first" in clarification.lower()


def test_hydrate_planning_result_resolves_export_distribution_target() -> None:
    """The chat executor should resolve the latest completed export before distribution."""

    export_id = uuid4()
    executor = ChatActionExecutor.__new__(ChatActionExecutor)

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="**I'll record the release.**",
            reasoning="There is one completed export ready for distribution.",
            tool_name="distribute_export",
            tool_arguments={
                "recipient_name": "Adaobi Nwosu",
                "recipient_email": "adaobi@example.com",
            },
        ),
        snapshot={
            "exports": [
                {
                    "id": str(export_id),
                    "status": "completed",
                    "distribution_count": 0,
                }
            ]
        },
        operator_content="Send it to Adaobi.",
        operator_memory=executor._memory_from_context_payload({}),
    )

    assert hydrated.tool_arguments["export_id"] == str(export_id)
    assert "*" not in hydrated.assistant_response


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
        operator_memory=executor._memory_from_context_payload({}),
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
        operator_memory=executor._memory_from_context_payload({}),
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
        operator_memory=executor._memory_from_context_payload({}),
    )

    assert hydrated.tool_arguments["workspace_id"] == str(workspace_id)


def test_hydrate_planning_result_repairs_workspace_namespace_to_switch_workspace() -> None:
    """Namespace leakage should repair onto the concrete workspace-switch tool."""

    target_workspace_id = uuid4()
    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._tool_registry = _build_fake_tool_registry(
        "switch_workspace",
        "create_workspace",
        "update_workspace",
        "delete_workspace",
    )

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll switch the workspace.",
            reasoning="The operator asked to move this chat onto another workspace.",
            tool_name="workspace_admin",
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
                    "name": "Polymarket",
                }
            ],
        },
        operator_content="switch back to polymarket workspace",
        operator_memory=executor._memory_from_context_payload({}),
    )

    assert hydrated.mode == "tool"
    assert hydrated.tool_name == "switch_workspace"
    assert hydrated.tool_arguments["workspace_id"] == str(target_workspace_id)


def test_hydrate_planning_result_answers_current_workspace_status_read_only() -> None:
    """Explicit workspace-status questions should stay read-only even if the planner drifts."""

    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._tool_registry = _build_fake_tool_registry(
        "switch_workspace",
        "create_workspace",
        "update_workspace",
        "delete_workspace",
    )

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll check that workspace.",
            reasoning="The operator asked about the current workspace state.",
            tool_name="workspace_admin",
            tool_arguments={},
        ),
        snapshot={
            "workspace": {
                "id": str(uuid4()),
                "name": "Polymarket",
            },
            "close_run_id": None,
        },
        operator_content="which workspace are you currently on?",
        operator_memory=executor._memory_from_context_payload({}),
    )

    assert hydrated.mode == "read_only"
    assert hydrated.tool_name is None
    assert hydrated.tool_arguments == {}
    assert hydrated.assistant_response == "This chat is currently anchored to Polymarket."


def test_hydrate_planning_result_answers_close_blockers_read_only() -> None:
    """Common blocker questions should resolve directly from readiness state."""

    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._tool_registry = _build_fake_tool_registry(
        "review_document",
        "approve_recommendation",
        "approve_journal",
    )

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll inspect the current blockers.",
            reasoning="The operator asked for the blocker state.",
            tool_name="review_document",
            tool_arguments={},
        ),
        snapshot={
            "close_run_id": str(uuid4()),
            "readiness": {
                "blockers": [
                    "Collection is blocked by no approved source documents yet."
                ],
                "warnings": [],
                "next_actions": [
                    "Review the remaining source document and approve it if it is complete."
                ],
            },
        },
        operator_content="what is blocking this close right now?",
        operator_memory=executor._memory_from_context_payload({}),
    )

    assert hydrated.mode == "read_only"
    assert hydrated.tool_name is None
    assert "blocked by no approved source documents yet" in hydrated.assistant_response
    assert "next best move" in hydrated.assistant_response.lower()


def test_hydrate_planning_result_answers_next_step_read_only() -> None:
    """Next-step questions should come straight from readiness instead of a tool call."""

    executor = ChatActionExecutor.__new__(ChatActionExecutor)

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll figure out the next step.",
            reasoning="The operator wants the next best action.",
            tool_name="generate_recommendations",
            tool_arguments={},
        ),
        snapshot={
            "readiness": {
                "blockers": [],
                "warnings": [],
                "next_actions": [
                    "Generate accounting recommendations for the parsed document set."
                ],
            }
        },
        operator_content="what should we do next?",
        operator_memory=executor._memory_from_context_payload({}),
    )

    assert hydrated.mode == "read_only"
    assert hydrated.tool_name is None
    assert (
        hydrated.assistant_response
        == (
            "The next best move is to generate accounting recommendations "
            "for the parsed document set"
        )
    )


def test_hydrate_planning_result_clarifies_cross_domain_approve_it() -> None:
    """Generic approve-it requests should clarify when several domains have one clear target."""

    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._tool_registry = _build_fake_tool_registry(
        "review_document",
        "approve_recommendation",
        "approve_journal",
    )

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll approve that now.",
            reasoning="The operator asked to approve the current pending item.",
            tool_name="review_document",
            tool_arguments={},
        ),
        snapshot={
            "documents": [
                {
                    "id": str(uuid4()),
                    "filename": "invoice-axis-haulage-2026-03.pdf",
                    "status": "needs_review",
                }
            ],
            "recommendations": [
                {
                    "id": str(uuid4()),
                    "status": "pending_review",
                    "document_filename": "payslip-adaobi-nwosu-2026-03.pdf",
                }
            ],
            "journals": [],
        },
        operator_content="approve it",
        operator_memory=executor._memory_from_context_payload({}),
    )

    assert hydrated.mode == "read_only"
    assert hydrated.tool_name is None
    assert "Which one do you want?" in hydrated.assistant_response
    assert "document" in hydrated.assistant_response
    assert "recommendation" in hydrated.assistant_response


def test_hydrate_planning_result_prefers_last_thread_target_for_referential_follow_up() -> None:
    """A remembered thread-local target should beat cross-domain ambiguity on follow-up turns."""

    document_id = uuid4()
    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._tool_registry = _build_fake_tool_registry(
        "review_document",
        "approve_recommendation",
        "approve_journal",
    )

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll approve that now.",
            reasoning="The operator is following up on the item already in focus.",
            tool_name="review_document",
            tool_arguments={},
        ),
        snapshot={
            "documents": [
                {
                    "id": str(document_id),
                    "filename": "invoice-axis-haulage-2026-03.pdf",
                    "status": "needs_review",
                }
            ],
            "recommendations": [
                {
                    "id": str(uuid4()),
                    "status": "pending_review",
                    "document_filename": "payslip-adaobi-nwosu-2026-03.pdf",
                }
            ],
            "journals": [],
        },
        operator_content="approve it",
        operator_memory=executor._memory_from_context_payload(
            {
                "agent_memory": {
                    "last_target_type": "document",
                    "last_target_id": str(document_id),
                    "last_target_label": (
                        "the document invoice-axis-haulage-2026-03.pdf"
                    ),
                }
            }
        ),
    )

    assert hydrated.mode == "tool"
    assert hydrated.tool_name == "review_document"
    assert hydrated.tool_arguments["document_id"] == str(document_id)
    assert hydrated.tool_arguments["decision"] == "approved"


def test_hydrate_planning_result_prefers_explicit_document_match_over_remembered_target() -> None:
    """Explicit document mentions should beat remembered referential targets."""

    remembered_document_id = uuid4()
    named_document_id = uuid4()
    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._tool_registry = _build_fake_tool_registry(
        "review_document",
        "approve_recommendation",
        "approve_journal",
    )

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll approve that now.",
            reasoning=(
                "The operator referenced a specific document while a prior target is still "
                "in memory."
            ),
            tool_name="review_document",
            tool_arguments={},
        ),
        snapshot={
            "documents": [
                {
                    "id": str(remembered_document_id),
                    "filename": "invoice-axis-haulage-2026-03.pdf",
                    "status": "needs_review",
                },
                {
                    "id": str(named_document_id),
                    "filename": "invoice-april-generator-overhaul-2026-04.pdf",
                    "status": "needs_review",
                },
            ],
            "recommendations": [],
            "journals": [],
        },
        operator_content="approve this invoice-april-generator-overhaul-2026-04.pdf",
        operator_memory=executor._memory_from_context_payload(
            {
                "agent_memory": {
                    "last_target_type": "document",
                    "last_target_id": str(remembered_document_id),
                    "last_target_label": "the document invoice-axis-haulage-2026-03.pdf",
                }
            }
        ),
    )

    assert hydrated.mode == "tool"
    assert hydrated.tool_name == "review_document"
    assert hydrated.tool_arguments["document_id"] == str(named_document_id)
    assert hydrated.tool_arguments["decision"] == "approved"


def test_hydrate_planning_result_skips_stale_remembered_document_targets() -> None:
    """Referential document follow-ups should ignore remembered stale review targets."""

    remembered_document_id = uuid4()
    reviewable_document_id = uuid4()
    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._tool_registry = _build_fake_tool_registry(
        "review_document",
        "approve_recommendation",
        "approve_journal",
    )

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll approve that now.",
            reasoning="The operator is following up on the remaining reviewable document.",
            tool_name="review_document",
            tool_arguments={},
        ),
        snapshot={
            "documents": [
                {
                    "id": str(remembered_document_id),
                    "filename": "invoice-axis-haulage-2026-03.pdf",
                    "status": "approved",
                },
                {
                    "id": str(reviewable_document_id),
                    "filename": "invoice-april-generator-overhaul-2026-04.pdf",
                    "status": "needs_review",
                },
            ],
            "recommendations": [],
            "journals": [],
        },
        operator_content="approve it",
        operator_memory=executor._memory_from_context_payload(
            {
                "agent_memory": {
                    "last_target_type": "document",
                    "last_target_id": str(remembered_document_id),
                    "last_target_label": "the document invoice-axis-haulage-2026-03.pdf",
                }
            }
        ),
    )

    assert hydrated.mode == "tool"
    assert hydrated.tool_name == "review_document"
    assert hydrated.tool_arguments["document_id"] == str(reviewable_document_id)
    assert hydrated.tool_arguments["decision"] == "approved"


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
        operator_memory=executor._memory_from_context_payload({}),
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
        operator_memory=executor._memory_from_context_payload({}),
    )

    assert hydrated.tool_arguments["workspace_id"] == str(target_workspace_id)


def test_hydrate_planning_result_creates_close_run_from_period_follow_up() -> None:
    """A period-only reply should complete the remembered cross-workspace create request."""

    current_workspace_id = str(uuid4())
    target_workspace_id = str(uuid4())
    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._tool_registry = _build_fake_tool_registry("create_close_run")

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="read_only",
            assistant_response="For which period should I open it?",
            reasoning="The operator is answering a previous clarification.",
            tool_name=None,
            tool_arguments={},
        ),
        snapshot={
            "workspace": {
                "id": current_workspace_id,
                "name": "Polymarket",
            },
            "accessible_workspaces": [
                {
                    "id": current_workspace_id,
                    "name": "Polymarket",
                },
                {
                    "id": target_workspace_id,
                    "name": "Apex Meridian Distribution Limited",
                },
            ],
        },
        operator_content="yes for april 2026",
        operator_memory=executor._memory_from_context_payload(
            {
                "agent_memory": {
                    "last_operator_message": (
                        "create a new close run for apex meridian"
                    ),
                    "last_assistant_response": (
                        "For which period would you like to create the new close run "
                        "for Apex Meridian Distribution Limited?"
                    ),
                    "working_subtask": "Create the next close run",
                },
                "agent_recent_objectives": (
                    "create a new close run for apex meridian",
                ),
            }
        ),
    )

    assert hydrated.mode == "tool"
    assert hydrated.tool_name == "create_close_run"
    assert hydrated.tool_arguments["workspace_id"] == target_workspace_id
    assert hydrated.tool_arguments["period_start"] == "2026-04-01"
    assert hydrated.tool_arguments["period_end"] == "2026-04-30"


def test_hydrate_planning_result_answers_close_runs_across_workspaces() -> None:
    """Cross-workspace close-run status should include approved runs outside the current entity."""

    executor = ChatActionExecutor.__new__(ChatActionExecutor)

    hydrated = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll inspect the close runs.",
            reasoning="The operator asked for close-run status.",
            tool_name="create_close_run",
            tool_arguments={},
        ),
        snapshot={
            "accessible_workspace_close_runs": [
                {
                    "workspace": {"id": str(uuid4()), "name": "Polymarket"},
                    "close_runs": [
                        {
                            "id": str(uuid4()),
                            "status": "draft",
                            "period_label": "Mar 2026",
                            "active_phase": "collection",
                        }
                    ],
                },
                {
                    "workspace": {
                        "id": str(uuid4()),
                        "name": "Apex Meridian Distribution Limited",
                    },
                    "close_runs": [
                        {
                            "id": str(uuid4()),
                            "status": "approved",
                            "period_label": "Mar 2026",
                            "active_phase": None,
                        }
                    ],
                },
            ],
        },
        operator_content="Summarize the close runs across my workspaces.",
        operator_memory=executor._memory_from_context_payload({}),
    )

    assert hydrated.mode == "read_only"
    assert hydrated.tool_name is None
    assert "Polymarket: Mar 2026 (draft, Collection)" in hydrated.assistant_response
    assert "Apex Meridian Distribution Limited: Mar 2026 (approved)" in (
        hydrated.assistant_response
    )


def test_send_action_message_asks_for_workspace_clarification_before_delete() -> None:
    """Ambiguous governed actions should ask one compact clarification instead of failing."""

    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    thread_id = uuid4()
    entity_id = uuid4()
    thread = SimpleNamespace(
        id=thread_id,
        entity_id=entity_id,
        close_run_id=None,
        context_payload={},
    )
    grounding = SimpleNamespace(
        entity=SimpleNamespace(name="Apex Meridian Nigeria Ltd"),
        context=SimpleNamespace(
            entity_id=str(entity_id),
            entity_name="Apex Meridian Nigeria Ltd",
            close_run_id=None,
            period_label=None,
            autonomy_mode="human_review",
            base_currency="NGN",
        ),
    )
    db_session = _FakeLoopDbSession()
    chat_repo = _FakeLoopChatRepository()
    memory_updates: list[dict[str, object]] = []
    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._db_session = db_session
    executor._chat_repo = chat_repo
    executor._action_repo = SimpleNamespace()
    executor._tool_registry = SimpleNamespace(
        get_tool=lambda **kwargs: SimpleNamespace(input_schema={"required": ["workspace_id"]})
    )
    executor._ensure_entity_coa_available = lambda **kwargs: None
    executor._load_thread_context = lambda **kwargs: (grounding, thread)  # type: ignore[method-assign]
    executor._handle_pending_plan_reply = lambda **kwargs: None  # type: ignore[method-assign]
    executor._snapshot_for_thread = lambda **kwargs: {  # type: ignore[method-assign]
        "workspace": {"id": str(uuid4()), "name": "Apex Meridian Nigeria Ltd"},
        "accessible_workspaces": [
            {"id": str(uuid4()), "name": "Apex Meridian Nigeria Ltd"},
            {"id": str(uuid4()), "name": "Apex Meridian Ghana Ltd"},
        ],
    }
    executor._plan_action = lambda **kwargs: AgentPlanningResult(  # type: ignore[method-assign]
        mode="tool",
        assistant_response="I'll delete that workspace.",
        reasoning="The operator asked to delete a workspace.",
        tool_name="delete_workspace",
        tool_arguments={},
    )
    executor._build_grounding_payload = lambda *args, **kwargs: {}  # type: ignore[method-assign]
    executor._build_trace_metadata = lambda **kwargs: {}  # type: ignore[method-assign]
    executor._update_thread_memory = lambda **kwargs: memory_updates.append(kwargs)  # type: ignore[method-assign]
    executor._resolve_action = lambda **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("The runtime should clarify before resolving the tool.")
    )

    outcome = executor.send_action_message(
        thread_id=thread_id,
        entity_id=entity_id,
        actor_user=actor_user,
        content="Delete the workspace.",
        source_surface="desktop",
        trace_id="trace-clarify-workspace",
    )

    assert outcome.is_read_only is True
    assert "Which workspace should I use?" in outcome.assistant_content
    assert "Apex Meridian Nigeria Ltd" in outcome.assistant_content
    assert "Apex Meridian Ghana Ltd" in outcome.assistant_content
    assert memory_updates[-1]["action_status"] == "read_only"


def test_handle_pending_plan_reply_confirms_single_pending_action() -> None:
    """A single pending governed action should be confirmable directly from chat."""

    thread_id = uuid4()
    entity_id = uuid4()
    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    pending_plan = _build_plan(
        close_run_id=uuid4(),
        action_plan_id=uuid4(),
        thread_id=thread_id,
        entity_id=entity_id,
    )
    approved_calls: list[dict[str, object]] = []
    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._action_repo = SimpleNamespace(
        list_pending_actions_for_thread=lambda **kwargs: (pending_plan,),
    )
    executor.approve_action_plan = lambda **kwargs: approved_calls.append(kwargs) or pending_plan  # type: ignore[method-assign]
    executor.reject_action_plan = lambda **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("Reject should not be called for confirm.")
    )
    assistant_message = SimpleNamespace(id=uuid4(), content="I archived this close run.")
    executor._chat_repo = SimpleNamespace(
        list_messages_for_thread=lambda **kwargs: (assistant_message,),
        get_thread_by_id=lambda **kwargs: SimpleNamespace(
            entity_id=entity_id,
            close_run_id=pending_plan.close_run_id,
        ),
    )

    outcome = executor._handle_pending_plan_reply(
        thread_id=thread_id,
        entity_id=entity_id,
        actor_user=actor_user,
        content="confirm",
        source_surface="desktop",
        trace_id="trace-confirm-pending",
    )

    assert outcome is not None
    assert outcome.assistant_content == assistant_message.content
    assert outcome.is_read_only is False
    assert approved_calls == [
        {
            "action_plan_id": pending_plan.id,
            "thread_id": thread_id,
            "entity_id": entity_id,
            "actor_user": actor_user,
            "reason": "Confirmed by operator in chat.",
            "source_surface": "desktop",
            "trace_id": "trace-confirm-pending",
        }
    ]


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
            entity_id=entity_id,
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


def test_handoff_thread_scope_moves_to_created_close_run_workspace() -> None:
    """Creating a close run in another workspace should re-anchor the thread there."""

    created_close_run_id = uuid4()
    source_entity_id = uuid4()
    target_entity_id = uuid4()
    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    thread_id = uuid4()
    fake_action_repo = _FakeActionRepository()
    fake_chat_repo = _FakeChatRepository(reopened_close_run_id=created_close_run_id)
    fake_grounding = _FakeGroundingService(
        reopened_close_run_id=created_close_run_id,
        entity_id=target_entity_id,
    )
    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._action_repo = fake_action_repo
    executor._chat_repo = fake_chat_repo
    executor._grounding = fake_grounding

    _, updated_thread, handoff_message = executor._handoff_thread_scope_if_needed(
        actor_user=actor_user,
        entity_id=source_entity_id,
        thread_id=thread_id,
        thread=SimpleNamespace(
            entity_id=source_entity_id,
            close_run_id=None,
            context_payload={"entity_name": "Polymarket"},
        ),
        grounding=SimpleNamespace(context=SimpleNamespace()),
        applied_result={
            "tool": "create_close_run",
            "created_close_run_id": str(created_close_run_id),
            "created_workspace_id": str(target_entity_id),
            "workspace_name": "Apex Meridian Distribution Limited",
            "period_start": "2026-04-01",
            "period_end": "2026-04-30",
            "active_phase": "collection",
            "version_no": 1,
        },
    )

    assert updated_thread.entity_id == target_entity_id
    assert updated_thread.close_run_id == created_close_run_id
    assert handoff_message is not None
    assert "Apex Meridian Distribution Limited" in handoff_message
    assert fake_action_repo.supersede_calls == []


def test_send_action_message_executes_multiple_steps_before_replying() -> None:
    """The operator lane should chain safe actions before returning one reply."""

    entity_id = uuid4()
    close_run_id = uuid4()
    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    thread = SimpleNamespace(
        entity_id=entity_id,
        close_run_id=close_run_id,
        context_payload={},
    )
    grounding = SimpleNamespace(
        entity=SimpleNamespace(name="Apex Meridian Nigeria Ltd"),
        context=SimpleNamespace(
            entity_id=str(entity_id),
            entity_name="Apex Meridian Nigeria Ltd",
            close_run_id=str(close_run_id),
            period_label="Mar 2026",
            autonomy_mode="human_review",
            base_currency="NGN",
        ),
    )
    snapshots = iter(
        (
            {
                "progress_summary": "One document is awaiting review.",
                "readiness": {
                    "next_actions": ["Generate recommendations for the approved documents."]
                },
            },
            {
                "progress_summary": "Recommendation generation is ready.",
                "readiness": {
                    "next_actions": ["Run reconciliation for the current close run."]
                },
            },
            {
                "progress_summary": "The close is ready for reconciliation.",
                "readiness": {
                    "next_actions": ["Run reconciliation for the current close run."]
                },
            },
        )
    )
    plans = iter(
        (
            AgentPlanningResult(
                mode="tool",
                assistant_response="I'll clear the remaining document review first.",
                reasoning="One document is clearly awaiting review.",
                tool_name="review_document",
                tool_arguments={"document_id": str(uuid4()), "decision": "approved"},
            ),
            AgentPlanningResult(
                mode="tool",
                assistant_response="Then I'll queue the recommendation pass.",
                reasoning="The next safe step is recommendation generation.",
                tool_name="generate_recommendations",
                tool_arguments={},
            ),
            AgentPlanningResult(
                mode="read_only",
                assistant_response="The close is ready for reconciliation now.",
                reasoning="The main objective for this turn is complete.",
                tool_name=None,
                tool_arguments={},
            ),
        )
    )
    db_session = _FakeLoopDbSession()
    chat_repo = _FakeLoopChatRepository()
    action_repo = _FakeLoopActionRepository(close_run_id=close_run_id)
    load_calls: list[int] = []
    memory_updates: list[dict[str, object]] = []

    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._db_session = db_session
    executor._chat_repo = chat_repo
    executor._action_repo = action_repo
    executor._ensure_entity_coa_available = lambda **kwargs: None
    executor._load_thread_context = lambda **kwargs: (  # type: ignore[method-assign]
        load_calls.append(1),
        (grounding, thread),
    )[1]
    executor._snapshot_for_thread = lambda **kwargs: next(snapshots)  # type: ignore[method-assign]
    executor._plan_action = lambda **kwargs: next(plans)  # type: ignore[method-assign]
    executor._hydrate_planning_result = lambda **kwargs: kwargs["planning"]  # type: ignore[method-assign]
    executor._resolve_action = lambda **kwargs: _resolve_fake_action(  # type: ignore[method-assign]
        kwargs["planning"]
    )
    executor._build_execution_context = lambda **kwargs: SimpleNamespace()  # type: ignore[method-assign]
    executor._requires_human_approval = lambda **kwargs: False  # type: ignore[method-assign]
    executor._execute_action = lambda **kwargs: _execute_fake_loop_action(  # type: ignore[method-assign]
        kwargs["action"].tool.name
    )
    executor._handoff_thread_scope_if_needed = lambda **kwargs: (  # type: ignore[method-assign]
        grounding,
        thread,
        None,
    )
    executor._build_grounding_payload = lambda *args, **kwargs: {}  # type: ignore[method-assign]
    executor._build_trace_metadata = lambda **kwargs: {}  # type: ignore[method-assign]
    executor._update_thread_memory = lambda **kwargs: memory_updates.append(kwargs)  # type: ignore[method-assign]

    outcome = executor.send_action_message(
        thread_id=uuid4(),
        entity_id=entity_id,
        actor_user=actor_user,
        content="Finish the intake work and get this ready for reconciliation.",
        source_surface="desktop",
        trace_id="trace-loop",
    )

    assert outcome.is_read_only is False
    assert outcome.action_plan is not None
    assert "I approved invoice.pdf for this close run." in outcome.assistant_content
    assert "I queued recommendation generation for 1 document." in outcome.assistant_content
    assert "The close is ready for reconciliation now." in outcome.assistant_content
    assert len(chat_repo.messages) == 2
    assert db_session.commit_calls == 3
    assert len(load_calls) == 3
    assert memory_updates[-1]["action_status"] == "applied"


def test_send_action_message_returns_partial_progress_when_later_step_blocks() -> None:
    """A later failure should keep earlier loop progress and respond naturally."""

    entity_id = uuid4()
    close_run_id = uuid4()
    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    thread = SimpleNamespace(
        entity_id=entity_id,
        close_run_id=close_run_id,
        context_payload={},
    )
    grounding = SimpleNamespace(
        entity=SimpleNamespace(name="Apex Meridian Nigeria Ltd"),
        context=SimpleNamespace(
            entity_id=str(entity_id),
            entity_name="Apex Meridian Nigeria Ltd",
            close_run_id=str(close_run_id),
            period_label="Mar 2026",
            autonomy_mode="human_review",
            base_currency="NGN",
        ),
    )
    snapshots = iter(
        (
            {
                "progress_summary": "One document is awaiting review.",
                "readiness": {"next_actions": ["Run reconciliation for the current close run."]},
            },
            {
                "progress_summary": "Documents are approved and reconciliation is next.",
                "readiness": {"next_actions": ["Run reconciliation for the current close run."]},
            },
            {
                "progress_summary": "Documents are approved and reconciliation is blocked.",
                "readiness": {"next_actions": ["Run reconciliation for the current close run."]},
            },
        )
    )
    plans = iter(
        (
            AgentPlanningResult(
                mode="tool",
                assistant_response="I'll approve the remaining document first.",
                reasoning="One document is clearly awaiting review.",
                tool_name="review_document",
                tool_arguments={"document_id": str(uuid4()), "decision": "approved"},
            ),
            AgentPlanningResult(
                mode="tool",
                assistant_response="Next I'll run reconciliation.",
                reasoning="Reconciliation is the next requested step.",
                tool_name="run_reconciliation",
                tool_arguments={},
            ),
        )
    )
    db_session = _FakeLoopDbSession()
    chat_repo = _FakeLoopChatRepository()
    action_repo = _FakeLoopActionRepository(close_run_id=close_run_id)
    memory_updates: list[dict[str, object]] = []
    execution_count = {"count": 0}

    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._db_session = db_session
    executor._chat_repo = chat_repo
    executor._action_repo = action_repo
    executor._ensure_entity_coa_available = lambda **kwargs: None
    executor._load_thread_context = lambda **kwargs: (grounding, thread)  # type: ignore[method-assign]
    executor._snapshot_for_thread = lambda **kwargs: next(snapshots)  # type: ignore[method-assign]
    executor._plan_action = lambda **kwargs: next(plans)  # type: ignore[method-assign]
    executor._hydrate_planning_result = lambda **kwargs: kwargs["planning"]  # type: ignore[method-assign]
    executor._resolve_action = lambda **kwargs: _resolve_fake_action(  # type: ignore[method-assign]
        kwargs["planning"]
    )
    executor._build_execution_context = lambda **kwargs: SimpleNamespace()  # type: ignore[method-assign]
    executor._requires_human_approval = lambda **kwargs: False  # type: ignore[method-assign]
    executor._execute_action = lambda **kwargs: _execute_fake_loop_action_with_block(  # type: ignore[method-assign]
        kwargs["action"].tool.name,
        execution_count,
    )
    executor._handoff_thread_scope_if_needed = lambda **kwargs: (  # type: ignore[method-assign]
        grounding,
        thread,
        None,
    )
    executor._build_grounding_payload = lambda *args, **kwargs: {}  # type: ignore[method-assign]
    executor._build_trace_metadata = lambda **kwargs: {}  # type: ignore[method-assign]
    executor._update_thread_memory = lambda **kwargs: memory_updates.append(kwargs)  # type: ignore[method-assign]

    outcome = executor.send_action_message(
        thread_id=uuid4(),
        entity_id=entity_id,
        actor_user=actor_user,
        content="Approve the intake and then run reconciliation.",
        source_surface="desktop",
        trace_id="trace-partial",
    )

    assert outcome.is_read_only is False
    assert "I completed part of that request" in outcome.assistant_content
    assert "I approved invoice.pdf for this close run." in outcome.assistant_content
    assert (
        "Reconciliation is blocked by unresolved matching exceptions."
        in outcome.assistant_content
    )
    assert db_session.rollback_calls == 1
    assert db_session.commit_calls == 2
    assert memory_updates[-1]["action_status"] == "partial"


def test_send_action_message_stops_after_async_dispatch_and_marks_pending_group() -> None:
    """Async tool results should end the turn, register the pending group, and wait cleanly."""

    entity_id = uuid4()
    close_run_id = uuid4()
    continuation_group_id = uuid4()
    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    thread = SimpleNamespace(
        entity_id=entity_id,
        close_run_id=close_run_id,
        context_payload={},
    )
    grounding = SimpleNamespace(
        entity=SimpleNamespace(name="Apex Meridian Nigeria Ltd"),
        context=SimpleNamespace(
            entity_id=str(entity_id),
            entity_name="Apex Meridian Nigeria Ltd",
            close_run_id=str(close_run_id),
            period_label="Mar 2026",
            autonomy_mode="human_review",
            base_currency="NGN",
        ),
    )
    snapshots = iter(
        (
            {
                "progress_summary": "Approved documents are ready for recommendation generation.",
                "readiness": {"next_actions": ["Wait for recommendation generation to finish."]},
            },
            {
                "progress_summary": "Recommendation generation is running in the background.",
                "readiness": {"next_actions": ["Wait for recommendation generation to finish."]},
            },
        )
    )
    db_session = _FakeLoopDbSession()
    chat_repo = _FakeLoopChatRepository()
    action_repo = _FakeLoopActionRepository(close_run_id=close_run_id)
    memory_updates: list[dict[str, object]] = []

    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._db_session = db_session
    executor._chat_repo = chat_repo
    executor._action_repo = action_repo
    executor._ensure_entity_coa_available = lambda **kwargs: None
    executor._load_thread_context = lambda **kwargs: (grounding, thread)  # type: ignore[method-assign]
    executor._snapshot_for_thread = lambda **kwargs: next(snapshots)  # type: ignore[method-assign]
    executor._plan_action = lambda **kwargs: AgentPlanningResult(  # type: ignore[method-assign]
        mode="tool",
        assistant_response="I'll start recommendation generation now.",
        reasoning="The next safe step is to queue recommendations.",
        tool_name="generate_recommendations",
        tool_arguments={},
    )
    executor._hydrate_planning_result = lambda **kwargs: kwargs["planning"]  # type: ignore[method-assign]
    executor._resolve_action = lambda **kwargs: _resolve_fake_action(  # type: ignore[method-assign]
        kwargs["planning"]
    )
    executor._build_execution_context = lambda **kwargs: SimpleNamespace()  # type: ignore[method-assign]
    executor._requires_human_approval = lambda **kwargs: False  # type: ignore[method-assign]
    executor._execute_action = lambda **kwargs: {  # type: ignore[method-assign]
        "tool": "generate_recommendations",
        "queued_count": 2,
        "async_job_group": {
            "continuation_group_id": str(continuation_group_id),
            "job_count": 2,
        },
    }
    executor._handoff_thread_scope_if_needed = lambda **kwargs: (  # type: ignore[method-assign]
        grounding,
        thread,
        None,
    )
    executor._build_grounding_payload = lambda *args, **kwargs: {}  # type: ignore[method-assign]
    executor._build_trace_metadata = lambda **kwargs: {}  # type: ignore[method-assign]
    executor._update_thread_memory = lambda **kwargs: memory_updates.append(kwargs)  # type: ignore[method-assign]

    outcome = executor.send_action_message(
        thread_id=uuid4(),
        entity_id=entity_id,
        actor_user=actor_user,
        content="Start the recommendation pass and keep going when it's done.",
        source_surface="desktop",
        trace_id="trace-async",
    )

    assert outcome.is_read_only is False
    assert "I'll keep going automatically" in outcome.assistant_content
    assert db_session.commit_calls == 2
    async_turn = memory_updates[-1]["existing_payload"]["agent_async_turn"]
    assert async_turn["status"] == "pending"
    assert async_turn["continuation_group_id"] == str(continuation_group_id)


def test_resume_operator_turn_can_queue_export_after_report_generation_finishes() -> None:
    """A resumed operator turn should be able to launch the next async release step."""

    entity_id = uuid4()
    close_run_id = uuid4()
    continuation_group_id = uuid4()
    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    thread = SimpleNamespace(
        id=uuid4(),
        entity_id=entity_id,
        close_run_id=close_run_id,
        context_payload={},
    )
    grounding = SimpleNamespace(
        entity=SimpleNamespace(name="Apex Meridian Nigeria Ltd"),
        context=SimpleNamespace(
            entity_id=str(entity_id),
            entity_name="Apex Meridian Nigeria Ltd",
            close_run_id=str(close_run_id),
            period_label="Mar 2026",
            autonomy_mode="human_review",
            base_currency="NGN",
        ),
    )
    db_session = _FakeLoopDbSession()
    chat_repo = _FakeLoopChatRepository()
    action_repo = _FakeLoopActionRepository(close_run_id=close_run_id)
    memory_updates: list[dict[str, object]] = []

    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._db_session = db_session
    executor._chat_repo = chat_repo
    executor._action_repo = action_repo
    executor._ensure_entity_coa_available = lambda **kwargs: None
    executor._load_thread_context = lambda **kwargs: (grounding, thread)  # type: ignore[method-assign]
    executor._snapshot_for_thread = lambda **kwargs: {  # type: ignore[method-assign]
        "progress_summary": "Reports are complete and the close is ready for export packaging.",
        "report_runs": [{"id": str(uuid4()), "status": "completed"}],
        "exports": [],
        "readiness": {"next_actions": ["Create the export package."]},
    }
    executor._plan_action = lambda **kwargs: AgentPlanningResult(  # type: ignore[method-assign]
        mode="tool",
        assistant_response="I'll package the export now.",
        reasoning="Report generation finished, so export packaging is the next release step.",
        tool_name="generate_export",
        tool_arguments={"include_evidence_pack": True},
    )
    executor._resolve_action = lambda **kwargs: _resolve_fake_action(  # type: ignore[method-assign]
        kwargs["planning"]
    )
    executor._build_execution_context = lambda **kwargs: SimpleNamespace()  # type: ignore[method-assign]
    executor._requires_human_approval = lambda **kwargs: False  # type: ignore[method-assign]
    executor._execute_action = lambda **kwargs: {  # type: ignore[method-assign]
        "tool": "generate_export",
        "job_id": "job-export-1",
        "status": "queued",
        "async_job_group": {
            "continuation_group_id": str(continuation_group_id),
            "job_count": 1,
        },
    }
    executor._handoff_thread_scope_if_needed = lambda **kwargs: (  # type: ignore[method-assign]
        grounding,
        thread,
        None,
    )
    executor._build_grounding_payload = lambda *args, **kwargs: {}  # type: ignore[method-assign]
    executor._build_trace_metadata = lambda **kwargs: {}  # type: ignore[method-assign]
    executor._update_thread_memory = lambda **kwargs: memory_updates.append(kwargs)  # type: ignore[method-assign]

    outcome = executor.resume_operator_turn(
        thread_id=thread.id,
        entity_id=entity_id,
        actor_user=actor_user,
        objective="Finish reporting, package the export, and keep going.",
        completed_jobs=(
            SimpleNamespace(
                status=SimpleNamespace(value="completed"),
                task_name="reporting.generate_close_run_pack",
                blocking_reason=None,
                failure_reason=None,
            ),
        ),
        source_surface="desktop",
        trace_id="trace-resume-export",
    )

    assert outcome.is_read_only is False
    assert "started packaging the export" in outcome.assistant_content.lower()
    assert "keep going automatically" in outcome.assistant_content
    async_turn = memory_updates[-1]["existing_payload"]["agent_async_turn"]
    assert async_turn["status"] == "pending"
    assert async_turn["continuation_group_id"] == str(continuation_group_id)


def test_build_trace_metadata_includes_namespace_specialist_and_policy_versions() -> None:
    """Trace metadata should expose operator-domain and policy details for audit and eval."""

    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._tool_registry = SimpleNamespace(
        get_tool=lambda **kwargs: SimpleNamespace(
            namespace="reporting_and_release",
            namespace_label="Reporting and Release",
            specialist_name="Reporting Controller",
            specialist_mission=(
                "Owns supporting schedules, commentary, reporting, export packaging, "
                "evidence packs, and release records."
            ),
            intent="report_action",
            requires_human_approval=False,
        )
    )

    metadata = executor._build_trace_metadata(
        trace_id="trace-operator-1",
        mode="planner",
        tool_name="generate_reports",
        action_status="applied",
        summary="Queued the report generation run.",
    )

    assert metadata["tool"] == "generate_reports"
    assert metadata["tool_namespace"] == "reporting_and_release"
    assert metadata["specialist_name"] == "Reporting Controller"
    assert metadata["tool_intent"] == "report_action"
    assert metadata["planner_policy_version"] == "2026-04-21.operator-planner.v1"
    assert metadata["confirmation_policy_version"] == "2026-04-21.operator-confirmation.v1"
    assert metadata["eval_schema_version"] == "2026-04-21.operator-eval.v1"
    assert "namespace:reporting_and_release" in metadata["eval_tags"]


def test_build_mcp_manifest_includes_namespaces_and_operator_policy() -> None:
    """The MCP manifest should expose grouped operator domains and policy metadata."""

    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._tool_registry = SimpleNamespace(
        list_tools=lambda: (
            SimpleNamespace(
                name="generate_reports",
                namespace="reporting_and_release",
                namespace_label="Reporting and Release",
                specialist_name="Reporting Controller",
                specialist_mission=(
                    "Owns supporting schedules, commentary, reporting, export packaging, "
                    "evidence packs, and release records."
                ),
                prompt_signature="generate_reports(template_id?, generate_commentary?)",
                description="Create a report run and queue report generation.",
                intent="report_action",
                requires_human_approval=False,
                input_schema={"type": "object", "properties": {}},
            ),
        ),
        list_namespaces=lambda: (
            SimpleNamespace(
                name="reporting_and_release",
                label="Reporting and Release",
                specialist_name="Reporting Controller",
                specialist_mission=(
                    "Owns supporting schedules, commentary, reporting, export packaging, "
                    "evidence packs, and release records."
                ),
                tool_names=("generate_reports",),
            ),
        ),
    )

    manifest = executor._build_mcp_manifest()

    assert manifest["version"] == "2025-11-25"
    assert manifest["operator_policy"]["planner_policy_version"] == (
        "2026-04-21.operator-planner.v1"
    )
    assert manifest["operator_controls"]["delivery"] == "natural_language_command"
    assert manifest["namespaces"][0]["name"] == "reporting_and_release"
    assert manifest["tools"][0]["annotations"]["namespace"] == "reporting_and_release"


def test_build_operator_controls_surfaces_pending_governance_and_next_steps() -> None:
    """Workspace controls should expose portable confirm/cancel and next-step commands."""

    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    controls = executor._build_operator_controls(
        thread=SimpleNamespace(close_run_id=uuid4()),
        snapshot={
            "readiness": {
                "next_actions": [
                    "Generate the export package.",
                    "Assemble the evidence pack.",
                ]
            },
        },
        operator_memory=executor._memory_from_context_payload({}),
        pending_actions=(
            SimpleNamespace(
                payload={
                    "tool_name": "delete_workspace",
                    "tool_arguments": {
                        "workspace_id": str(uuid4()),
                    },
                }
            ),
        ),
    )

    commands = {control.command for control in controls}
    assert "confirm" in commands
    assert "cancel" in commands
    assert "Generate the export package." in commands


def test_send_action_message_surfaces_action_failure_in_thread() -> None:
    """Early execution failures should return a grounded assistant reply instead of raising."""

    entity_id = uuid4()
    close_run_id = uuid4()
    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    thread = SimpleNamespace(
        entity_id=entity_id,
        close_run_id=close_run_id,
        context_payload={},
    )
    grounding = SimpleNamespace(
        entity=SimpleNamespace(name="Apex Meridian Nigeria Ltd"),
        context=SimpleNamespace(
            entity_id=str(entity_id),
            entity_name="Apex Meridian Nigeria Ltd",
            close_run_id=str(close_run_id),
            period_label="Mar 2026",
            autonomy_mode="human_review",
            base_currency="NGN",
        ),
    )
    db_session = _FakeLoopDbSession()
    chat_repo = _FakeLoopChatRepository()
    action_repo = _FakeLoopActionRepository(close_run_id=close_run_id)
    memory_updates: list[dict[str, object]] = []

    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._db_session = db_session
    executor._chat_repo = chat_repo
    executor._action_repo = action_repo
    executor._ensure_entity_coa_available = lambda **kwargs: None
    executor._load_thread_context = lambda **kwargs: (grounding, thread)  # type: ignore[method-assign]
    executor._snapshot_for_thread = lambda **kwargs: {  # type: ignore[method-assign]
        "readiness": {
            "blockers": ["Reconciliation is blocked by unresolved matching exceptions."],
            "next_actions": ["Clear the reconciliation exceptions before retrying."],
        },
    }
    executor._plan_action = lambda **kwargs: AgentPlanningResult(  # type: ignore[method-assign]
        mode="tool",
        assistant_response="I'll run reconciliation now.",
        reasoning="The operator asked to continue reconciliation.",
        tool_name="run_reconciliation",
        tool_arguments={},
    )
    executor._hydrate_planning_result = lambda **kwargs: kwargs["planning"]  # type: ignore[method-assign]
    executor._resolve_action = lambda **kwargs: _resolve_fake_action(  # type: ignore[method-assign]
        kwargs["planning"]
    )
    executor._build_execution_context = lambda **kwargs: SimpleNamespace()  # type: ignore[method-assign]
    executor._requires_human_approval = lambda **kwargs: False  # type: ignore[method-assign]
    executor._execute_action = lambda **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        ChatActionExecutionError(
            status_code=409,
            code=ChatActionExecutionErrorCode.INVALID_ACTION_PLAN,
            message="Reconciliation is blocked by unresolved matching exceptions.",
        )
    )
    executor._build_grounding_payload = lambda *args, **kwargs: {}  # type: ignore[method-assign]
    executor._build_trace_metadata = lambda **kwargs: {}  # type: ignore[method-assign]
    executor._update_thread_memory = lambda **kwargs: memory_updates.append(kwargs)  # type: ignore[method-assign]

    outcome = executor.send_action_message(
        thread_id=uuid4(),
        entity_id=entity_id,
        actor_user=actor_user,
        content="Run reconciliation now.",
        source_surface="desktop",
        trace_id="trace-failure-surface",
    )

    assert outcome.is_read_only is True
    assert "I couldn't finish the run reconciliation step yet." in outcome.assistant_content
    assert (
        "Reconciliation is blocked by unresolved matching exceptions."
        in outcome.assistant_content
    )
    assert (
        "Next, I can clear the reconciliation exceptions before retrying."
        in outcome.assistant_content
    )
    assert db_session.rollback_calls == 1
    assert db_session.commit_calls == 1
    assert memory_updates[-1]["action_status"] == "failed"


def test_send_action_message_surfaces_access_denied_tool_failure_in_thread() -> None:
    """Access-denied tool failures should become an assistant reply with recovery guidance."""

    entity_id = uuid4()
    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    thread = SimpleNamespace(entity_id=entity_id, close_run_id=None, context_payload={})
    grounding = SimpleNamespace(
        entity=SimpleNamespace(name="Polymarket"),
        context=SimpleNamespace(
            entity_id=str(entity_id),
            entity_name="Polymarket",
            close_run_id=None,
            period_label=None,
            autonomy_mode="human_review",
            base_currency="USD",
        ),
    )
    db_session = _FakeLoopDbSession()
    chat_repo = _FakeLoopChatRepository()
    action_repo = _FakeLoopActionRepository(close_run_id=uuid4())
    memory_updates: list[dict[str, object]] = []

    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._db_session = db_session
    executor._chat_repo = chat_repo
    executor._action_repo = action_repo
    executor._ensure_entity_coa_available = lambda **kwargs: None
    executor._load_thread_context = lambda **kwargs: (grounding, thread)  # type: ignore[method-assign]
    executor._snapshot_for_thread = lambda **kwargs: {"readiness": {"next_actions": []}}  # type: ignore[method-assign]
    executor._plan_action = lambda **kwargs: AgentPlanningResult(  # type: ignore[method-assign]
        mode="tool",
        assistant_response="I'll create that close run now.",
        reasoning="The operator asked for a close run in another workspace.",
        tool_name="create_close_run",
        tool_arguments={"workspace_id": str(uuid4())},
    )
    executor._hydrate_planning_result = lambda **kwargs: kwargs["planning"]  # type: ignore[method-assign]
    executor._resolve_action = lambda **kwargs: _resolve_fake_action(  # type: ignore[method-assign]
        kwargs["planning"]
    )
    executor._build_execution_context = lambda **kwargs: SimpleNamespace()  # type: ignore[method-assign]
    executor._requires_human_approval = lambda **kwargs: False  # type: ignore[method-assign]
    executor._execute_action = lambda **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        ChatActionExecutionError(
            status_code=403,
            code=ChatActionExecutionErrorCode.ACCESS_DENIED,
            message="That workspace is not accessible to the current operator.",
        )
    )
    executor._build_grounding_payload = lambda *args, **kwargs: {}  # type: ignore[method-assign]
    executor._build_trace_metadata = lambda **kwargs: {}  # type: ignore[method-assign]
    executor._update_thread_memory = lambda **kwargs: memory_updates.append(kwargs)  # type: ignore[method-assign]

    outcome = executor.send_action_message(
        thread_id=uuid4(),
        entity_id=entity_id,
        actor_user=actor_user,
        content="Create an April close run for the workspace I mentioned.",
        source_surface="desktop",
        trace_id="trace-access-denied",
    )

    assert outcome.is_read_only is True
    assert "I couldn't access the workspace or record needed" in outcome.assistant_content
    assert "I didn't make any changes" in outcome.assistant_content
    assert "could not be completed" not in outcome.assistant_content.lower()
    assert db_session.rollback_calls == 1
    assert db_session.commit_calls == 1
    assert memory_updates[-1]["action_status"] == "failed"


def test_send_action_message_surfaces_unexpected_runtime_failure_in_thread() -> None:
    """Unexpected operator exceptions should still produce a chat-visible response."""

    entity_id = uuid4()
    close_run_id = uuid4()
    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    thread = SimpleNamespace(entity_id=entity_id, close_run_id=close_run_id, context_payload={})
    grounding = SimpleNamespace(
        entity=SimpleNamespace(name="Apex Meridian Nigeria Ltd"),
        context=SimpleNamespace(
            entity_id=str(entity_id),
            entity_name="Apex Meridian Nigeria Ltd",
            close_run_id=str(close_run_id),
            period_label="Apr 2026",
            autonomy_mode="human_review",
            base_currency="NGN",
        ),
    )
    db_session = _FakeLoopDbSession()
    chat_repo = _FakeLoopChatRepository()
    action_repo = _FakeLoopActionRepository(close_run_id=close_run_id)
    memory_updates: list[dict[str, object]] = []

    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._db_session = db_session
    executor._chat_repo = chat_repo
    executor._action_repo = action_repo
    executor._ensure_entity_coa_available = lambda **kwargs: None
    executor._load_thread_context = lambda **kwargs: (grounding, thread)  # type: ignore[method-assign]
    executor._snapshot_for_thread = lambda **kwargs: {  # type: ignore[method-assign]
        "readiness": {"next_actions": ["Review the close-run setup before retrying."]},
    }
    executor._plan_action = lambda **kwargs: AgentPlanningResult(  # type: ignore[method-assign]
        mode="tool",
        assistant_response="I'll advance the close now.",
        reasoning="The operator asked to advance the workflow.",
        tool_name="advance_close_run",
        tool_arguments={},
    )
    executor._hydrate_planning_result = lambda **kwargs: kwargs["planning"]  # type: ignore[method-assign]
    executor._resolve_action = lambda **kwargs: _resolve_fake_action(  # type: ignore[method-assign]
        kwargs["planning"]
    )
    executor._build_execution_context = lambda **kwargs: SimpleNamespace()  # type: ignore[method-assign]
    executor._requires_human_approval = lambda **kwargs: False  # type: ignore[method-assign]
    executor._execute_action = lambda **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("database timeout while loading phase state")
    )
    executor._build_grounding_payload = lambda *args, **kwargs: {}  # type: ignore[method-assign]
    executor._build_trace_metadata = lambda **kwargs: {}  # type: ignore[method-assign]
    executor._update_thread_memory = lambda **kwargs: memory_updates.append(kwargs)  # type: ignore[method-assign]

    outcome = executor.send_action_message(
        thread_id=uuid4(),
        entity_id=entity_id,
        actor_user=actor_user,
        content="Advance the close run.",
        source_surface="desktop",
        trace_id="trace-runtime-failure",
    )

    assert outcome.is_read_only is True
    assert "I hit a system error while running the advance close run step" in (
        outcome.assistant_content
    )
    assert "Unexpected RuntimeError: database timeout while loading phase state" in (
        outcome.assistant_content
    )
    assert "Next, I can review the close-run setup before retrying." in outcome.assistant_content
    assert db_session.rollback_calls == 1
    assert db_session.commit_calls == 1
    assert memory_updates[-1]["action_status"] == "failed"


def test_update_thread_memory_tracks_preferences_targets_and_recent_objectives() -> None:
    """Thread memory should retain operator preferences and recent workspace targets."""

    captured_payloads: list[dict[str, object]] = []
    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._tool_registry = SimpleNamespace(
        get_tool=lambda **kwargs: SimpleNamespace(namespace="reporting_and_release")
    )
    executor._chat_repo = SimpleNamespace(
        update_thread_context=lambda **kwargs: captured_payloads.append(kwargs["context_payload"])
    )

    executor._update_thread_memory(
        thread_id=uuid4(),
        existing_payload={
            "entity_name": "Apex Meridian Nigeria Ltd",
            "period_label": "Mar 2026",
        },
        operator_message="Keep it brief and just do it.",
        assistant_response="Queued reporting run.",
        tool_name="generate_reports",
        tool_arguments=None,
        action_status="applied",
        trace_id="trace-memory-1",
        snapshot={
            "pending_action_count": 1,
            "progress_summary": "Reporting is queued.",
            "readiness": {"next_actions": ["Generate the export package."]},
        },
    )

    payload = captured_payloads[-1]
    memory = payload["agent_memory"]
    assert memory["preferred_explanation_depth"] == "brief"
    assert memory["preferred_confirmation_style"] == "direct_when_clear"
    assert memory["recent_objectives"] == ("Keep it brief and just do it.",)
    assert memory["recent_entity_names"] == ("Apex Meridian Nigeria Ltd",)
    assert memory["recent_period_labels"] == ("Mar 2026",)
    assert memory["last_tool_namespace"] == "reporting_and_release"
    assert memory["approved_objective"] == "Keep it brief and just do it."
    assert memory["working_subtask"] == "Generate reports for the current close run"
    assert memory["pending_branch"] == "Next branch: generate the export package"


def test_update_thread_memory_tracks_last_resolved_target_for_follow_up_continuity() -> None:
    """Thread memory should retain the last concrete target for clean follow-up resolution."""

    captured_payloads: list[dict[str, object]] = []
    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._tool_registry = SimpleNamespace(
        get_tool=lambda **kwargs: SimpleNamespace(namespace="document_control")
    )
    executor._chat_repo = SimpleNamespace(
        update_thread_context=lambda **kwargs: captured_payloads.append(kwargs["context_payload"])
    )

    document_id = uuid4()
    executor._update_thread_memory(
        thread_id=uuid4(),
        existing_payload={
            "entity_name": "Apex Meridian Nigeria Ltd",
            "period_label": "Mar 2026",
        },
        operator_message="approve it",
        assistant_response="I approved the document.",
        tool_name="review_document",
        tool_arguments={"document_id": str(document_id), "decision": "approved"},
        action_status="applied",
        trace_id="trace-memory-target-1",
        snapshot={
            "pending_action_count": 0,
            "progress_summary": "Document review is moving forward.",
            "readiness": {"next_actions": ["Review the next source document."]},
            "documents": [
                {
                    "id": str(document_id),
                    "filename": "invoice-axis-haulage-2026-03.pdf",
                    "status": "needs_review",
                }
            ],
        },
    )

    payload = captured_payloads[-1]
    memory = payload["agent_memory"]
    assert memory["last_target_type"] == "document"
    assert memory["last_target_id"] == str(document_id)
    assert (
        memory["last_target_label"]
        == "the document invoice-axis-haulage-2026-03.pdf"
    )
    assert memory["working_subtask"] == "Review the document invoice-axis-haulage-2026-03.pdf"
    assert memory["approved_objective"] == "approve it"
    assert memory["pending_branch"] == "Next branch: review the next source document"
    assert payload["agent_recent_target_labels"] == (
        "the document invoice-axis-haulage-2026-03.pdf",
    )


def test_memory_from_context_payload_surfaces_active_and_last_async_workflows() -> None:
    """Derived memory should surface resumable workflow context from thread payload state."""

    continuation = new_chat_operator_continuation(
        thread_id=uuid4(),
        entity_id=uuid4(),
        actor_user_id=uuid4(),
        objective="Generate the report pack and keep going.",
        originating_tool="generate_reports",
        source_surface="desktop",
    )
    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    payload = build_pending_async_turn_payload(
        existing_payload={
            "agent_memory": {
                "preferred_explanation_depth": "detailed",
                "preferred_confirmation_style": "confirm_before_destructive",
            },
            "agent_recent_objectives": ("Finish the month-end close.",),
        },
        continuation=continuation,
        job_count=2,
        trace_id="trace-async-memory",
    )
    payload["agent_last_async_turn"] = {
        "status": "completed",
        "objective": "Run reconciliation and keep going.",
        "final_note": "Reconciliation finished cleanly.",
    }

    memory = executor._memory_from_context_payload(payload)

    assert memory.preferred_explanation_depth == "detailed"
    assert memory.preferred_confirmation_style == "confirm_before_destructive"
    assert memory.recent_objectives == ("Finish the month-end close.",)
    assert memory.active_async_status == "pending"
    assert memory.active_async_objective == "Generate the report pack and keep going."
    assert memory.active_async_originating_tool == "generate_reports"
    assert memory.active_async_retry_count == 0
    assert memory.last_async_status == "completed"
    assert memory.last_async_note == "Reconciliation finished cleanly."


def test_memory_for_thread_merges_recent_cross_thread_preferences() -> None:
    """Executor memory reads should carry preferences and recent objectives across threads."""

    current_thread_id = uuid4()
    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._chat_repo = SimpleNamespace(
        list_recent_threads_for_entity_any_scope=lambda **kwargs: (
            SimpleNamespace(
                context_payload={
                    "agent_memory": {
                        "preferred_explanation_depth": "brief",
                        "preferred_confirmation_style": "direct_when_clear",
                        "last_target_type": "document",
                        "last_target_id": "c44c4dd0-8869-4d91-b4ed-6dc7963a3bf1",
                        "last_target_label": "the document carry-forward-target.pdf",
                    },
                    "agent_recent_objectives": ("Close March quickly.",),
                    "agent_recent_entity_names": ("Apex Meridian Nigeria Ltd",),
                    "agent_recent_period_labels": ("Mar 2026",),
                    "agent_recent_target_labels": ("the document carry-forward-target.pdf",),
                }
            ),
        ),
        list_recent_threads_for_user_any_scope=lambda **kwargs: (
            SimpleNamespace(
                context_payload={
                    "agent_memory": {
                        "preferred_explanation_depth": "brief",
                        "preferred_confirmation_style": "direct_when_clear",
                        "recent_tool_names": ("generate_reports",),
                        "recent_tool_namespaces": ("reporting_and_release",),
                    }
                }
            ),
        ),
    )

    memory = executor._memory_for_thread(
        thread_id=current_thread_id,
        entity_id=uuid4(),
        actor_user_id=uuid4(),
        context_payload={"entity_name": "Apex Meridian Nigeria Ltd"},
    )

    assert memory.preferred_explanation_depth == "brief"
    assert memory.preferred_confirmation_style == "direct_when_clear"
    assert memory.recent_objectives == ("Close March quickly.",)
    assert memory.recent_entity_names == ("Apex Meridian Nigeria Ltd",)
    assert memory.recent_period_labels == ("Mar 2026",)
    assert memory.recent_target_labels == ("the document carry-forward-target.pdf",)
    assert memory.recent_tool_names == ("generate_reports",)
    assert memory.recent_tool_namespaces == ("reporting_and_release",)
    assert memory.last_target_type is None
    assert memory.last_target_id is None
    assert memory.last_target_label is None


def test_seed_context_payload_with_operator_memory_keeps_last_target_thread_local() -> None:
    """Fresh thread seeding should carry history without copying concrete action targets."""

    seeded_payload = seed_context_payload_with_operator_memory(
        context_payload={"entity_name": "Apex Meridian Nigeria Ltd"},
        recent_context_payloads=(
            {
                "agent_memory": {
                    "preferred_explanation_depth": "brief",
                    "last_target_type": "document",
                    "last_target_id": "c44c4dd0-8869-4d91-b4ed-6dc7963a3bf1",
                    "last_target_label": "the document carry-forward-target.pdf",
                },
                "agent_recent_objectives": ("Close March quickly.",),
                "agent_recent_target_labels": ("the document carry-forward-target.pdf",),
            },
        ),
    )

    memory = seeded_payload["agent_memory"]
    assert memory["preferred_explanation_depth"] == "brief"
    assert "last_target_type" not in memory
    assert "last_target_id" not in memory
    assert "last_target_label" not in memory
    assert seeded_payload["agent_recent_objectives"] == ("Close March quickly.",)
    assert seeded_payload["agent_recent_target_labels"] == (
        "the document carry-forward-target.pdf",
    )


def test_memory_from_context_payload_surfaces_recovery_guidance_for_failed_async() -> None:
    """Derived memory should surface operator-facing recovery guidance after async failures."""

    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    memory = executor._memory_from_context_payload(
        {
            "agent_last_async_turn": {
                "status": "failed",
                "objective": "Generate the export package.",
                "final_note": "The export worker failed before packaging finished.",
            }
        }
    )

    assert memory.recovery_state == "attention_required"
    assert "Generate the export package." in (memory.recovery_summary or "")
    assert memory.recovery_actions == (
        "Retry the workflow in chat after checking worker health and recent traces.",
    )


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
        entity_id: UUID,
        close_run_id: UUID | None,
        context_payload: dict[str, object],
    ):
        del thread_id
        return SimpleNamespace(
            entity_id=entity_id,
            close_run_id=close_run_id,
            context_payload=context_payload,
        )


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


class _FakeLoopDbSession:
    def __init__(self) -> None:
        self.commit_calls = 0
        self.rollback_calls = 0

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1


class _FakeLoopChatRepository:
    def __init__(self) -> None:
        self.messages: list[SimpleNamespace] = []
        self.updated_context_payload: dict[str, object] | None = None

    def create_message(self, **kwargs):
        message = SimpleNamespace(
            id=uuid4(),
            content=kwargs["content"],
            role=kwargs["role"],
            model_metadata=kwargs.get("model_metadata"),
        )
        self.messages.append(message)
        return message

    def update_thread_context(self, *, thread_id: UUID, context_payload: dict[str, object]) -> None:
        del thread_id
        self.updated_context_payload = context_payload

    def list_recent_threads_for_entity_any_scope(self, **kwargs):
        del kwargs
        return ()

    def list_recent_threads_for_user_any_scope(self, **kwargs):
        del kwargs
        return ()


class _FakeLoopActionRepository:
    def __init__(self, *, close_run_id: UUID) -> None:
        self.close_run_id = close_run_id
        self.created_records: list[ChatActionPlanRecord] = []

    def create_action_plan(self, **kwargs) -> ChatActionPlanRecord:
        record = _build_plan(
            close_run_id=kwargs["close_run_id"] or self.close_run_id,
            action_plan_id=uuid4(),
            thread_id=kwargs["thread_id"],
            entity_id=kwargs["entity_id"],
        )
        self.created_records.append(record)
        return record

    def update_action_plan_status(
        self,
        *,
        action_plan_id: UUID,
        status: str,
        applied_result: dict[str, object] | None = None,
        rejected_reason: str | None = None,
        superseded_by_id: UUID | None = None,
    ) -> ChatActionPlanRecord | None:
        del rejected_reason, superseded_by_id
        for record in self.created_records:
            if record.id != action_plan_id:
                continue
            return record.__class__(
                id=record.id,
                thread_id=record.thread_id,
                message_id=record.message_id,
                entity_id=record.entity_id,
                close_run_id=record.close_run_id,
                actor_user_id=record.actor_user_id,
                intent=record.intent,
                target_type=record.target_type,
                target_id=record.target_id,
                payload=record.payload,
                confidence=record.confidence,
                autonomy_mode=record.autonomy_mode,
                status=status,
                requires_human_approval=record.requires_human_approval,
                reasoning=record.reasoning,
                applied_result=applied_result,
                rejected_reason=record.rejected_reason,
                superseded_by_id=record.superseded_by_id,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
        return None


def _resolve_fake_action(planning: AgentPlanningResult):
    if planning.mode == "read_only" or planning.tool_name is None:
        return None
    return SimpleNamespace(
        planning=planning,
        tool=SimpleNamespace(
            name=planning.tool_name,
            namespace="close_operator",
            namespace_label="Close Operations",
            specialist_name="Close Run Operator",
            specialist_mission=(
                "Owns close-run lifecycle, phase movement, sign-off, archive, and reopen "
                "control."
            ),
            intent="workflow_action",
            requires_human_approval=False,
        ),
        target_type=None,
        target_id=None,
    )


def _build_fake_tool_registry(*tool_names: str):
    required_fields = {
        "review_document": ["document_id", "decision"],
        "ignore_document": ["document_id"],
        "approve_recommendation": ["recommendation_id"],
        "reject_recommendation": ["recommendation_id", "reason"],
        "approve_journal": ["journal_id"],
        "apply_journal": ["journal_id", "posting_target"],
        "reject_journal": ["journal_id", "reason"],
        "switch_workspace": ["workspace_id"],
        "update_workspace": ["workspace_id"],
        "delete_workspace": ["workspace_id"],
        "create_close_run": ["period_start", "period_end"],
        "approve_reconciliation": ["reconciliation_id"],
        "disposition_reconciliation_item": ["item_id", "disposition", "reason"],
        "resolve_reconciliation_anomaly": ["anomaly_id", "resolution_note"],
        "update_commentary": ["report_run_id", "section_key", "body"],
        "approve_commentary": ["report_run_id", "section_key"],
        "delete_close_run": ["close_run_id"],
    }
    tool_definitions = {
        tool_name: SimpleNamespace(
            name=tool_name,
            input_schema={"required": required_fields.get(tool_name, [])},
        )
        for tool_name in tool_names
    }
    namespaces = (
        SimpleNamespace(
            name="workspace_admin",
            label="Workspace Admin",
            specialist_name="Workspace Steward",
        ),
    )

    def get_tool(**kwargs):
        tool_name = kwargs["tool_name"]
        if tool_name not in tool_definitions:
            raise ValueError(f"Unknown tool: {tool_name}")
        return tool_definitions[tool_name]

    return SimpleNamespace(
        get_tool=get_tool,
        list_namespaces=lambda: namespaces,
    )


def _execute_fake_loop_action(tool_name: str) -> dict[str, object]:
    if tool_name == "review_document":
        return {
            "tool": "review_document",
            "document_filename": "invoice.pdf",
            "decision": "approved",
        }
    if tool_name == "generate_recommendations":
        return {
            "tool": "generate_recommendations",
            "queued_count": 1,
        }
    raise AssertionError(f"Unexpected tool for fake execution: {tool_name}")


def _execute_fake_loop_action_with_block(
    tool_name: str,
    execution_count: dict[str, int],
) -> dict[str, object]:
    execution_count["count"] += 1
    if execution_count["count"] == 1:
        return {
            "tool": "review_document",
            "document_filename": "invoice.pdf",
            "decision": "approved",
        }
    raise ChatActionExecutionError(
        status_code=409,
        code=ChatActionExecutionErrorCode.INVALID_ACTION_PLAN,
        message="Reconciliation is blocked by unresolved matching exceptions.",
    )
