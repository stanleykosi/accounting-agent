"""
Purpose: Replay realistic accountant transcripts through the operator decision
layer and score whether the runtime answers directly, acts through tools, or
asks one clarification.
Scope: Transcript-style eval coverage for workspace status, workspace
switching, ambiguous follow-ups, referential continuity, and next-step
grounding.
Dependencies: ChatActionExecutor hydration and memory helpers only.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from uuid import uuid4

import pytest
from services.agents.models import AgentPlanningResult
from services.chat.action_execution import ChatActionExecutor


@dataclass(frozen=True, slots=True)
class _TranscriptTurnCase:
    """Describe one single-turn transcript eval expectation."""

    label: str
    operator_content: str
    planning: AgentPlanningResult
    snapshot: dict[str, object]
    tool_names: tuple[str, ...]
    expected_mode: str
    expected_tool_name: str | None
    expected_response_fragments: tuple[str, ...] = ()
    expected_tool_arguments: dict[str, str] | None = None
    context_payload: dict[str, object] | None = None


@pytest.mark.parametrize(
    "case",
    (
        _TranscriptTurnCase(
            label="current_workspace_status_reads_directly",
            operator_content="Which workspace are you currently on?",
            planning=AgentPlanningResult(
                mode="tool",
                assistant_response="I'll switch if needed.",
                reasoning="The operator asked about workspace scope.",
                tool_name="workspace_admin",
                tool_arguments={},
            ),
            snapshot={
                "workspace": {
                    "id": str(uuid4()),
                    "name": "Apex Meridian Nigeria Ltd",
                }
            },
            tool_names=("switch_workspace", "update_workspace", "delete_workspace"),
            expected_mode="read_only",
            expected_tool_name=None,
            expected_response_fragments=("Apex Meridian Nigeria Ltd",),
        ),
        _TranscriptTurnCase(
            label="close_blocker_question_reads_directly",
            operator_content="What is blocking this close right now?",
            planning=AgentPlanningResult(
                mode="tool",
                assistant_response="I'll check the blockers.",
                reasoning="The operator asked for blocker status.",
                tool_name="run_reconciliation",
                tool_arguments={},
            ),
            snapshot={
                "close_run_id": str(uuid4()),
                "readiness": {
                    "blockers": [
                        "Collection is blocked by no approved source documents yet."
                    ],
                    "next_actions": ["Approve the remaining source document."],
                },
            },
            tool_names=("run_reconciliation",),
            expected_mode="read_only",
            expected_tool_name=None,
            expected_response_fragments=(
                "Collection is blocked by no approved source documents yet.",
                "approve the remaining source document",
            ),
        ),
        _TranscriptTurnCase(
            label="ambiguous_approve_it_clarifies",
            operator_content="Approve it.",
            planning=AgentPlanningResult(
                mode="tool",
                assistant_response="I'll approve that now.",
                reasoning="The operator used a referential follow-up.",
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
            tool_names=("review_document", "approve_recommendation", "approve_journal"),
            expected_mode="read_only",
            expected_tool_name=None,
            expected_response_fragments=(
                "Which one do you want?",
                "document",
                "recommendation",
            ),
        ),
        _TranscriptTurnCase(
            label="remembered_document_target_stays_actionable",
            operator_content="Approve it.",
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
                        "id": "4d8f6f5d-2d8f-4b2f-a5d9-a9f2aaf3c003",
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
            },
            tool_names=("review_document", "approve_recommendation", "approve_journal"),
            expected_mode="tool",
            expected_tool_name="review_document",
            expected_tool_arguments={
                "document_id": "4d8f6f5d-2d8f-4b2f-a5d9-a9f2aaf3c003",
                "decision": "approved",
            },
            context_payload={
                "agent_memory": {
                    "last_target_type": "document",
                    "last_target_id": "4d8f6f5d-2d8f-4b2f-a5d9-a9f2aaf3c003",
                    "last_target_label": (
                        "the document invoice-axis-haulage-2026-03.pdf"
                    ),
                }
            },
        ),
        _TranscriptTurnCase(
            label="switch_workspace_repairs_namespace_leak",
            operator_content="Switch back to Polymarket Workspace.",
            planning=AgentPlanningResult(
                mode="tool",
                assistant_response="I'll move this conversation there.",
                reasoning="The operator asked to move the chat to another workspace.",
                tool_name="workspace_admin",
                tool_arguments={},
            ),
            snapshot={
                "workspace": {
                    "id": str(uuid4()),
                    "name": "Apex Meridian Distribution Limited",
                },
                "accessible_workspaces": [
                    {
                        "id": "a6dcecd4-7233-4ba7-870f-b9f5bcb61c4a",
                        "name": "Polymarket Workspace",
                    },
                    {
                        "id": str(uuid4()),
                        "name": "Apex Meridian Distribution Limited",
                    },
                ],
            },
            tool_names=("switch_workspace", "create_workspace", "update_workspace"),
            expected_mode="tool",
            expected_tool_name="switch_workspace",
            expected_tool_arguments={"workspace_id": "a6dcecd4-7233-4ba7-870f-b9f5bcb61c4a"},
        ),
    ),
    ids=lambda case: case.label,
)
def test_operator_transcript_eval_single_turn_decision_modes(
    case: _TranscriptTurnCase,
) -> None:
    """Transcript turns should deterministically choose answer, act, or clarify."""

    executor = _new_transcript_executor(*case.tool_names)

    hydrated = executor._hydrate_planning_result(
        planning=case.planning,
        snapshot=case.snapshot,
        operator_content=case.operator_content,
        operator_memory=executor._memory_from_context_payload(case.context_payload or {}),
    )

    assert hydrated.mode == case.expected_mode
    assert hydrated.tool_name == case.expected_tool_name
    for fragment in case.expected_response_fragments:
        assert fragment in hydrated.assistant_response
    for key, expected_value in (case.expected_tool_arguments or {}).items():
        assert hydrated.tool_arguments[key] == expected_value


def test_operator_transcript_eval_replays_workspace_switch_status_and_switch_back() -> None:
    """A real workspace transcript should move naturally between act and answer turns."""

    apex_workspace_id = "ef4b1d20-8d47-4518-9d66-7d78e9770b67"
    polymarket_workspace_id = "b5c224b5-52b8-4f01-af68-911271d82d4f"
    executor = _new_transcript_executor(
        "switch_workspace",
        "create_workspace",
        "update_workspace",
        "delete_workspace",
    )
    memory_payload: dict[str, object] = {"entity_name": "Polymarket Workspace"}

    accessible_workspaces = [
        {
            "id": polymarket_workspace_id,
            "name": "Polymarket Workspace",
        },
        {
            "id": apex_workspace_id,
            "name": "Apex Meridian Distribution Limited",
        },
    ]

    first_turn = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll switch us there now.",
            reasoning="The operator named one accessible workspace.",
            tool_name="switch_workspace",
            tool_arguments={},
        ),
        snapshot={
            "workspace": {
                "id": polymarket_workspace_id,
                "name": "Polymarket Workspace",
            },
            "accessible_workspaces": accessible_workspaces,
        },
        operator_content="Switch this chat to Apex Meridian Distribution Limited workspace.",
        operator_memory=executor._memory_from_context_payload(memory_payload),
    )

    assert first_turn.mode == "tool"
    assert first_turn.tool_name == "switch_workspace"
    assert first_turn.tool_arguments["workspace_id"] == apex_workspace_id

    memory_payload = _advance_transcript_memory(
        executor,
        existing_payload={
            **memory_payload,
            "entity_name": "Apex Meridian Distribution Limited",
        },
        operator_message="Switch this chat to Apex Meridian Distribution Limited workspace.",
        assistant_response=(
            "I switched this conversation to the Apex Meridian Distribution "
            "Limited workspace."
        ),
        tool_name="switch_workspace",
        tool_arguments={"workspace_id": apex_workspace_id},
        snapshot={
            "workspace": {
                "id": apex_workspace_id,
                "name": "Apex Meridian Distribution Limited",
            },
            "accessible_workspaces": accessible_workspaces,
            "progress_summary": (
                "Workspace scope is now anchored to Apex Meridian Distribution "
                "Limited."
            ),
            "readiness": {
                "next_actions": ["Start a new close run or open an existing one."],
            },
        },
    )

    second_turn = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll check that.",
            reasoning="The operator asked for the current workspace.",
            tool_name="workspace_admin",
            tool_arguments={},
        ),
        snapshot={
            "workspace": {
                "id": apex_workspace_id,
                "name": "Apex Meridian Distribution Limited",
            },
            "accessible_workspaces": accessible_workspaces,
        },
        operator_content="Which workspace are you currently on?",
        operator_memory=executor._memory_from_context_payload(memory_payload),
    )

    assert second_turn.mode == "read_only"
    assert second_turn.tool_name is None
    assert "Apex Meridian Distribution Limited" in second_turn.assistant_response

    third_turn = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll move this conversation back now.",
            reasoning="The operator wants to switch back to the previous workspace.",
            tool_name="workspace_admin",
            tool_arguments={},
        ),
        snapshot={
            "workspace": {
                "id": apex_workspace_id,
                "name": "Apex Meridian Distribution Limited",
            },
            "accessible_workspaces": accessible_workspaces,
        },
        operator_content="Switch back to Polymarket Workspace.",
        operator_memory=executor._memory_from_context_payload(memory_payload),
    )

    assert third_turn.mode == "tool"
    assert third_turn.tool_name == "switch_workspace"
    assert third_turn.tool_arguments["workspace_id"] == polymarket_workspace_id


def test_operator_transcript_eval_replays_document_focus_follow_up_and_next_step() -> None:
    """A grounded transcript should preserve the focused document and then
    answer what comes next.
    """

    document_id = "c9ef8a47-dfef-499e-bdaf-0854ccf29395"
    executor = _new_transcript_executor(
        "review_document",
        "approve_recommendation",
        "approve_journal",
        "generate_recommendations",
    )
    memory_payload: dict[str, object] = {
        "entity_name": "Apex Meridian Nigeria Ltd",
        "period_label": "Mar 2026",
    }

    focus_turn = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll take care of that now.",
            reasoning="The operator named one pending source document.",
            tool_name="review_document",
            tool_arguments={},
        ),
        snapshot={
            "documents": [
                {
                    "id": document_id,
                    "filename": "invoice-axis-haulage-2026-03.pdf",
                    "status": "needs_review",
                    "document_type": "invoice",
                }
            ],
            "recommendations": [],
            "journals": [],
        },
        operator_content="Approve the remaining haulage invoice.",
        operator_memory=executor._memory_from_context_payload(memory_payload),
    )

    assert focus_turn.mode == "tool"
    assert focus_turn.tool_name == "review_document"
    assert focus_turn.tool_arguments["document_id"] == document_id
    assert focus_turn.tool_arguments["decision"] == "approved"

    memory_payload = _advance_transcript_memory(
        executor,
        existing_payload=memory_payload,
        operator_message="Approve the remaining haulage invoice.",
        assistant_response="I approved the document invoice-axis-haulage-2026-03.pdf.",
        tool_name="review_document",
        tool_arguments={
            "document_id": document_id,
            "decision": "approved",
        },
        snapshot={
            "documents": [
                {
                    "id": document_id,
                    "filename": "invoice-axis-haulage-2026-03.pdf",
                    "status": "needs_review",
                }
            ],
            "progress_summary": "The remaining haulage invoice is now approved.",
            "readiness": {
                "next_actions": ["Generate recommendations for the approved documents."],
            },
        },
    )

    follow_up_turn = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll approve that now.",
            reasoning="The operator is following up on the same focused item.",
            tool_name="review_document",
            tool_arguments={},
        ),
        snapshot={
            "documents": [
                {
                    "id": document_id,
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
        operator_content="Approve it.",
        operator_memory=executor._memory_from_context_payload(memory_payload),
    )

    assert follow_up_turn.mode == "tool"
    assert follow_up_turn.tool_name == "review_document"
    assert follow_up_turn.tool_arguments["document_id"] == document_id
    assert follow_up_turn.tool_arguments["decision"] == "approved"

    next_step_turn = executor._hydrate_planning_result(
        planning=AgentPlanningResult(
            mode="tool",
            assistant_response="I'll queue the recommendation pass.",
            reasoning="The next requested step would be recommendation generation.",
            tool_name="generate_recommendations",
            tool_arguments={},
        ),
        snapshot={
            "close_run_id": str(uuid4()),
            "progress_summary": "Documents are approved and recommendation generation is next.",
            "readiness": {
                "next_actions": ["Generate recommendations for the approved documents."],
            },
        },
        operator_content="What should we do next?",
        operator_memory=executor._memory_from_context_payload(memory_payload),
    )

    assert next_step_turn.mode == "read_only"
    assert next_step_turn.tool_name is None
    assert "generate recommendations for the approved documents" in (
        next_step_turn.assistant_response
    )


def _new_transcript_executor(*tool_names: str) -> ChatActionExecutor:
    """Return a chat executor wired with the minimal registry needed for transcript evals."""

    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._tool_registry = _build_transcript_tool_registry(*tool_names)
    return executor


def _advance_transcript_memory(
    executor: ChatActionExecutor,
    *,
    existing_payload: dict[str, object],
    operator_message: str,
    assistant_response: str,
    tool_name: str,
    tool_arguments: dict[str, object],
    snapshot: dict[str, object],
) -> dict[str, object]:
    """Persist one synthetic transcript step into thread memory and return the new payload."""

    captured_payloads: list[dict[str, object]] = []
    executor._chat_repo = SimpleNamespace(
        update_thread_context=lambda **kwargs: captured_payloads.append(
            kwargs["context_payload"]
        )
    )
    executor._update_thread_memory(
        thread_id=uuid4(),
        existing_payload=existing_payload,
        operator_message=operator_message,
        assistant_response=assistant_response,
        tool_name=tool_name,
        tool_arguments=tool_arguments,
        action_status="applied",
        trace_id="trace-transcript-eval",
        snapshot=snapshot,
    )
    return captured_payloads[-1]


def _build_transcript_tool_registry(*tool_names: str):
    """Return a lightweight registry that supports transcript hydration and memory writes."""

    tool_definitions = {
        tool_name: SimpleNamespace(
            name=tool_name,
            namespace=_tool_namespace_for_name(tool_name),
        )
        for tool_name in tool_names
    }
    namespace_order = (
        "workspace_admin",
        "close_operator",
        "document_control",
        "treatment_and_journals",
        "reconciliation_control",
        "reporting_and_release",
    )
    namespace_labels = {
        "workspace_admin": "Workspace Admin",
        "close_operator": "Close Operations",
        "document_control": "Document Control",
        "treatment_and_journals": "Treatment and Journals",
        "reconciliation_control": "Reconciliation Control",
        "reporting_and_release": "Reporting and Release",
    }
    specialist_names = {
        "workspace_admin": "Workspace Steward",
        "close_operator": "Close Run Operator",
        "document_control": "Document Controller",
        "treatment_and_journals": "Journal Specialist",
        "reconciliation_control": "Reconciliation Analyst",
        "reporting_and_release": "Reporting Controller",
    }
    namespaces = tuple(
        SimpleNamespace(
            name=namespace_name,
            label=namespace_labels[namespace_name],
            specialist_name=specialist_names[namespace_name],
        )
        for namespace_name in namespace_order
    )

    def get_tool(*, tool_name: str):
        definition = tool_definitions.get(tool_name)
        if definition is None:
            raise ValueError(f"Unknown tool: {tool_name}")
        return definition

    return SimpleNamespace(
        get_tool=get_tool,
        list_namespaces=lambda: namespaces,
    )


def _tool_namespace_for_name(tool_name: str) -> str:
    """Return the canonical namespace used by transcript-only fake tool definitions."""

    if tool_name in {
        "switch_workspace",
        "create_workspace",
        "update_workspace",
        "delete_workspace",
    }:
        return "workspace_admin"
    if tool_name in {"review_document", "ignore_document"}:
        return "document_control"
    if tool_name in {
        "approve_recommendation",
        "reject_recommendation",
        "approve_journal",
        "reject_journal",
        "apply_journal",
        "generate_recommendations",
    }:
        return "treatment_and_journals"
    if tool_name in {"run_reconciliation", "approve_reconciliation"}:
        return "reconciliation_control"
    if tool_name in {"generate_reports", "generate_export", "assemble_evidence_pack"}:
        return "reporting_and_release"
    return "close_operator"
