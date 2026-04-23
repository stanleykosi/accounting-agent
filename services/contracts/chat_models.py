"""
Purpose: Define strict API contracts for chat threads, messages, and
grounded finance copilot interactions.
Scope: Thread creation/list/read, message history, and send-message
request/response payloads for read-only analysis flows.
Dependencies: Pydantic contract defaults, shared numeric helpers, and
the canonical enum definitions for chat message types.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator
from services.contracts.api_models import ContractModel

CHAT_MESSAGE_ROLES = ("user", "assistant", "system")
CHAT_MESSAGE_TYPES = ("analysis", "workflow", "action", "warning")


class GroundingContext(ContractModel):
    """Describe the entity, close run, and period context that grounds a chat thread."""

    entity_id: str = Field(
        min_length=1,
        description="UUID of the entity workspace that owns this conversation.",
    )
    entity_name: str = Field(
        min_length=1,
        description="Display name of the entity workspace.",
    )
    close_run_id: str | None = Field(
        default=None,
        description="UUID of the close run scoping this thread, if period-specific.",
    )
    period_label: str | None = Field(
        default=None,
        description="Human-readable period label (e.g. 'Jan 2025') when a close run is present.",
    )
    autonomy_mode: str = Field(
        min_length=1,
        description="Current autonomy mode for the entity (human_review or reduced_interruption).",
    )
    base_currency: str = Field(
        min_length=3,
        max_length=3,
        description="Entity base currency used for formatting context.",
    )


class ChatThreadSummary(ContractModel):
    """Describe one chat thread for list and overview surfaces."""

    id: str = Field(description="Stable UUID for the chat thread.")
    entity_id: str = Field(description="Entity workspace that owns this thread.")
    close_run_id: str | None = Field(
        default=None,
        description="Close run scoping this thread, if period-specific.",
    )
    title: str | None = Field(
        default=None,
        description="Thread title shown in the conversation list.",
    )
    grounding: GroundingContext = Field(
        description="Entity and close run context that grounds this thread.",
    )
    message_count: int = Field(
        ge=0,
        description="Total number of messages in the thread.",
    )
    last_message_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the most recent message in the thread.",
    )
    created_at: datetime = Field(description="UTC timestamp when the thread was created.")
    updated_at: datetime = Field(description="UTC timestamp when the thread was last updated.")


class ChatMessageRecord(ContractModel):
    """Describe one message persisted inside a chat thread."""

    id: str = Field(description="Stable UUID for the chat message.")
    thread_id: str = Field(description="Parent chat thread that this message belongs to.")
    message_order: int = Field(
        ge=1,
        description=(
            "Canonical per-thread message sequence used for deterministic "
            "conversation ordering."
        ),
    )
    role: Literal["user", "assistant", "system"] = Field(
        description="Message originator: user, assistant, or system.",
    )
    content: str = Field(
        min_length=1,
        description="Message text content (Markdown for assistant messages).",
    )
    message_type: Literal["analysis", "workflow", "action", "warning"] = Field(
        default="analysis",
        description="Intent classification used for UI rendering.",
    )
    linked_action_id: str | None = Field(
        default=None,
        description="Optional reference to a recommendation discussed in this message.",
    )
    grounding_payload: dict[str, object] = Field(
        default_factory=dict,
        description="Evidence snapshot attached to assistant messages.",
    )
    model_metadata: dict[str, object] | None = Field(
        default=None,
        description="Model name, token usage, and latency for assistant messages.",
    )
    created_at: datetime = Field(description="UTC timestamp when the message was created.")


class CreateChatThreadRequest(ContractModel):
    """Capture the inputs required to create a new grounded chat thread."""

    entity_id: str = Field(
        min_length=1,
        description="Entity workspace UUID that will own this conversation.",
    )
    close_run_id: str | None = Field(
        default=None,
        description="Optional close run UUID to scope the thread to a specific period.",
    )
    title: str | None = Field(
        default=None,
        max_length=300,
        description="Optional user-provided or auto-derived thread title.",
    )

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        """Trim thread titles and collapse blank values to null."""

        if value is None:
            return None

        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_title_if_close_run_scoped(self) -> CreateChatThreadRequest:
        """Require a non-blank title when the thread is scoped to a close run."""

        if self.close_run_id is not None and (
            self.title is None or not self.title.strip()
        ):
            message = "Provide a title when creating a close-run-scoped thread."
            raise ValueError(message)

        return self


class CreateGlobalChatThreadRequest(ContractModel):
    """Capture optional inputs for a workspace-wide global assistant thread."""

    title: str | None = Field(
        default=None,
        max_length=300,
        description="Optional user-provided or auto-derived thread title.",
    )

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        """Trim thread titles and collapse blank values to null."""

        if value is None:
            return None

        normalized = value.strip()
        return normalized or None


class SendChatMessageRequest(ContractModel):
    """Capture a user message to be sent to an existing chat thread."""

    content: str = Field(
        min_length=1,
        max_length=10_000,
        description="User question or instruction text.",
    )

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        """Reject whitespace-only user messages before they reach the service layer."""

        normalized = value.strip()
        if not normalized:
            message = "Message content cannot be blank."
            raise ValueError(message)

        return normalized


class ChatThreadWithMessages(ContractModel):
    """Return a full chat thread with its message history for detail views."""

    thread: ChatThreadSummary = Field(description="Thread summary with grounding context.")
    messages: tuple[ChatMessageRecord, ...] = Field(
        default=(),
        description="Messages ordered chronologically (oldest first).",
    )


class ChatMessageResponse(ContractModel):
    """Return the assistant response generated for a user message."""

    message: ChatMessageRecord = Field(
        description="The newly created assistant message with grounding payload.",
    )
    user_message: ChatMessageRecord | None = Field(
        default=None,
        description="Echo of the persisted user message when sent in the same response.",
    )


class ChatThreadListResponse(ContractModel):
    """Return the threads available for an entity or close run."""

    threads: tuple[ChatThreadSummary, ...] = Field(
        default=(),
        description="Threads ordered newest-first for the specified scope.",
    )


class ChatThreadDeleteResponse(ContractModel):
    """Return the canonical result after deleting one chat thread."""

    deleted_thread_id: str = Field(description="Stable UUID of the deleted thread.")
    deleted_thread_title: str | None = Field(
        default=None,
        description="Human-readable title of the deleted thread, when present.",
    )
    deleted_message_count: int = Field(
        ge=0,
        description="Number of persisted messages deleted with the thread.",
    )


class AgentMemorySummary(ContractModel):
    """Describe the persisted working memory for one agent-scoped chat thread."""

    last_operator_message: str | None = Field(
        default=None,
        description="Most recent operator instruction retained in thread memory.",
    )
    last_assistant_response: str | None = Field(
        default=None,
        description="Most recent assistant response summary retained in thread memory.",
    )
    last_tool_name: str | None = Field(
        default=None,
        description="Most recent deterministic tool used by the agent, if any.",
    )
    last_tool_namespace: str | None = Field(
        default=None,
        description="Most recent operator namespace used by the agent, if any.",
    )
    last_action_status: str | None = Field(
        default=None,
        description="Outcome state of the last recorded agent action.",
    )
    last_trace_id: str | None = Field(
        default=None,
        description="Trace identifier linked to the most recent agent turn.",
    )
    preferred_explanation_depth: str = Field(
        default="balanced",
        description="Preferred answer depth inferred from recent operator instructions.",
    )
    preferred_confirmation_style: str = Field(
        default="confirm_high_risk",
        description="Preferred action-confirmation style inferred from operator instructions.",
    )
    pending_action_count: int = Field(
        default=0,
        ge=0,
        description="Number of staged approvals currently pending in the thread.",
    )
    progress_summary: str | None = Field(
        default=None,
        description="Latest compact progress narrative for the close run.",
    )
    recent_tool_names: tuple[str, ...] = Field(
        default=(),
        description="Recently used tool names retained in compact thread memory.",
    )
    recent_tool_namespaces: tuple[str, ...] = Field(
        default=(),
        description="Recently used operator namespaces retained in compact thread memory.",
    )
    recent_objectives: tuple[str, ...] = Field(
        default=(),
        description="Recent operator objectives retained for conversational continuity.",
    )
    recent_entity_names: tuple[str, ...] = Field(
        default=(),
        description="Recent workspace names retained to help the agent target the right scope.",
    )
    recent_period_labels: tuple[str, ...] = Field(
        default=(),
        description="Recent period labels retained for conversational targeting.",
    )
    recent_target_labels: tuple[str, ...] = Field(
        default=(),
        description="Recent concrete target labels retained for follow-up continuity.",
    )
    last_target_type: str | None = Field(
        default=None,
        description="Most recent concrete target type retained for follow-up resolution.",
    )
    last_target_id: str | None = Field(
        default=None,
        description="Most recent concrete target identifier retained for follow-up resolution.",
    )
    last_target_label: str | None = Field(
        default=None,
        description="Most recent concrete target label retained for follow-up resolution.",
    )
    working_subtask: str | None = Field(
        default=None,
        description="Compact current subtask summary retained for conversational continuity.",
    )
    approved_objective: str | None = Field(
        default=None,
        description=(
            "Most recent operator objective that the agent actively committed "
            "to carry out."
        ),
    )
    pending_branch: str | None = Field(
        default=None,
        description="Compact next branch or hold state retained for long-turn continuity.",
    )
    active_async_status: str | None = Field(
        default=None,
        description="Status of the currently active async workflow owned by this thread.",
    )
    active_async_objective: str | None = Field(
        default=None,
        description="Objective currently waiting on background work, when present.",
    )
    active_async_originating_tool: str | None = Field(
        default=None,
        description="Originating tool for the active async workflow, when present.",
    )
    active_async_retry_count: int = Field(
        default=0,
        ge=0,
        description="Number of async recovery retries attempted for the active workflow.",
    )
    active_async_last_failure: str | None = Field(
        default=None,
        description="Most recent recovery failure note attached to the active async workflow.",
    )
    last_async_status: str | None = Field(
        default=None,
        description="Final status of the most recently completed or superseded async workflow.",
    )
    last_async_objective: str | None = Field(
        default=None,
        description="Objective of the most recent async workflow retained for resume guidance.",
    )
    last_async_note: str | None = Field(
        default=None,
        description="Final note retained from the most recent async workflow outcome.",
    )
    recovery_state: str | None = Field(
        default=None,
        description="Operator-facing recovery state derived from active or recent async workflows.",
    )
    recovery_summary: str | None = Field(
        default=None,
        description=(
            "Compact recovery guidance shown when async work needs monitoring or "
            "intervention."
        ),
    )
    recovery_actions: tuple[str, ...] = Field(
        default=(),
        description="Suggested operator recovery actions derived from async workflow state.",
    )
    updated_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when the thread memory was last refreshed.",
    )


class AgentCoaAccountSummary(ContractModel):
    """Describe one active COA account surfaced into the agent workspace."""

    account_code: str = Field(min_length=1, description="Canonical account code.")
    account_name: str = Field(min_length=1, description="Display account name.")
    account_type: str = Field(min_length=1, description="Normalized account type label.")
    is_active: bool = Field(description="Whether the account is active in the current COA set.")
    is_postable: bool = Field(description="Whether the account is eligible for journal posting.")


class AgentCoaSummary(ContractModel):
    """Describe the active chart-of-accounts state available to the agent."""

    is_available: bool = Field(
        default=False,
        description="Whether an active chart of accounts is currently available.",
    )
    status: str = Field(
        default="missing",
        description="Readiness label for the COA state: missing, fallback, or active.",
    )
    source: str | None = Field(
        default=None,
        description="Source of the active COA set when available.",
    )
    version_no: int | None = Field(
        default=None,
        ge=1,
        description="Active COA version number when available.",
    )
    account_count: int = Field(
        default=0,
        ge=0,
        description="Number of active accounts in the current COA set.",
    )
    postable_account_count: int = Field(
        default=0,
        ge=0,
        description="Number of postable active accounts in the current COA set.",
    )
    requires_operator_upload: bool = Field(
        default=False,
        description="Whether the operator should upload or sync a production COA.",
    )
    activated_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when the active COA set became effective.",
    )
    summary: str | None = Field(
        default=None,
        description="Compact narrative of the active COA readiness state.",
    )
    accounts: tuple[AgentCoaAccountSummary, ...] = Field(
        default=(),
        description="Active COA accounts exposed to the planner for grounded reasoning.",
    )


class AgentRunPhaseState(ContractModel):
    """Describe one workflow phase shown in the agent readiness timeline."""

    phase: str = Field(min_length=1, description="Stable workflow phase key.")
    label: str = Field(min_length=1, description="Operator-facing workflow phase label.")
    status: str = Field(min_length=1, description="Current state of the workflow phase.")
    blocking_reason: str | None = Field(
        default=None,
        description="Reason the phase is blocked, when applicable.",
    )
    completed_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when the phase completed, when applicable.",
    )


class AgentRunReadiness(ContractModel):
    """Describe the run-level readiness state exposed to the chat workbench."""

    has_close_run: bool = Field(
        default=False,
        description="Whether this chat thread is currently scoped to a close run.",
    )
    status: str = Field(
        default="not_scoped",
        description="Overall readiness state: ready, attention_required, blocked, or not_scoped.",
    )
    blockers: tuple[str, ...] = Field(
        default=(),
        description="Issues that should be resolved before the next major workflow step.",
    )
    warnings: tuple[str, ...] = Field(
        default=(),
        description="Advisory notices that should remain visible to the operator.",
    )
    next_actions: tuple[str, ...] = Field(
        default=(),
        description="Suggested next operator or agent actions based on current state.",
    )
    document_count: int = Field(
        default=0,
        ge=0,
        description="Total documents currently attached to the close run.",
    )
    has_source_documents: bool = Field(
        default=False,
        description="Whether the close run has any uploaded source documents.",
    )
    parsed_document_count: int = Field(
        default=0,
        ge=0,
        description="Number of documents that have reached parsed or later states.",
    )
    phase_states: tuple[AgentRunPhaseState, ...] = Field(
        default=(),
        description="Ordered workflow phases with current status for timeline rendering.",
    )


class AgentToolManifestItem(ContractModel):
    """Describe one registered agent tool surfaced to UI and external runtimes."""

    name: str = Field(description="Stable registered tool name.")
    namespace: str = Field(description="Operator namespace that owns this tool.")
    namespace_label: str = Field(description="Human-readable label for the tool namespace.")
    specialist_name: str = Field(
        description="Internal specialist persona that owns this tool namespace."
    )
    specialist_mission: str = Field(
        description="Compact mission statement for the internal specialist domain."
    )
    prompt_signature: str = Field(description="Prompt-facing function signature.")
    description: str = Field(description="Operator-facing summary of what the tool does.")
    intent: str = Field(description="Intent bucket used for routing and review.")
    requires_human_approval: bool = Field(
        description="Whether tool execution stages for human approval by policy.",
    )
    input_schema: dict[str, object] = Field(
        default_factory=dict,
        description="Portable tool input schema for external runtimes and manifests.",
    )


class AgentTraceRecord(ContractModel):
    """Describe one recent trace event from an agent message or system action."""

    message_id: str = Field(description="Chat message UUID that emitted this trace.")
    created_at: datetime = Field(description="UTC timestamp when the trace was recorded.")
    mode: str | None = Field(default=None, description="Planner or system execution mode.")
    tool_name: str | None = Field(default=None, description="Deterministic tool name when present.")
    tool_namespace: str | None = Field(
        default=None,
        description="Operator namespace that owned the tool decision, when present.",
    )
    specialist_name: str | None = Field(
        default=None,
        description="Internal specialist persona associated with the tool, when present.",
    )
    tool_intent: str | None = Field(
        default=None,
        description="Intent bucket associated with the tool, when present.",
    )
    trace_id: str | None = Field(default=None, description="Request trace identifier when present.")
    planner_policy_version: str | None = Field(
        default=None,
        description="Planner policy version applied to this turn when recorded.",
    )
    confirmation_policy_version: str | None = Field(
        default=None,
        description="Confirmation policy version applied to this turn when recorded.",
    )
    action_status: str | None = Field(
        default=None,
        description="Action status linked to the trace.",
    )
    summary: str | None = Field(default=None, description="Compact summary of what happened.")
    eval_tags: tuple[str, ...] = Field(
        default=(),
        description="Compact evaluation tags attached to the trace for analytics and QA.",
    )


class AgentOperatorControl(ContractModel):
    """Describe one channel-portable operator action suggestion."""

    id: str = Field(description="Stable control identifier for analytics and deduplication.")
    label: str = Field(description="Short operator-facing label for the control.")
    command: str = Field(
        description="Canonical natural-language command that can be sent back to the agent.",
    )
    kind: str = Field(
        description="Control type such as next_step, recovery, governed_action, or status_check.",
    )
    scope: str = Field(description="Control scope such as global, entity, or close_run.")
    description: str | None = Field(
        default=None,
        description="Optional longer explanation of what the control will do.",
    )
    requires_confirmation: bool = Field(
        default=False,
        description="Whether using this control will still lead into governed confirmation.",
    )
    enabled: bool = Field(
        default=True,
        description="Whether the control is currently actionable.",
    )
    disabled_reason: str | None = Field(
        default=None,
        description="Reason the control is unavailable when enabled is false.",
    )


class ChatThreadWorkspaceResponse(ContractModel):
    """Return the agent workspace context surfaced for one chat thread."""

    thread_id: str = Field(description="Chat thread UUID.")
    grounding: GroundingContext = Field(description="Grounding context for this thread.")
    progress_summary: str | None = Field(
        default=None,
        description="Current close-run progress summary visible to the agent.",
    )
    coa: AgentCoaSummary = Field(description="Active chart-of-accounts state visible to the agent.")
    readiness: AgentRunReadiness = Field(
        description="Run readiness, workflow phases, and next-step guidance for the workbench.",
    )
    memory: AgentMemorySummary = Field(description="Persisted working memory for the thread.")
    tools: tuple[AgentToolManifestItem, ...] = Field(
        default=(),
        description="Registered deterministic tools available to the agent.",
    )
    recent_traces: tuple[AgentTraceRecord, ...] = Field(
        default=(),
        description="Recent trace events emitted by the agent in this thread.",
    )
    operator_controls: tuple[AgentOperatorControl, ...] = Field(
        default=(),
        description=(
            "Channel-portable suggested commands and governed-action controls that "
            "non-web clients can surface directly."
        ),
    )
    mcp_manifest: dict[str, object] = Field(
        default_factory=dict,
        description="Portable MCP-style tool manifest for external agent integrations.",
    )


__all__ = [
    "CHAT_MESSAGE_ROLES",
    "CHAT_MESSAGE_TYPES",
    "AgentCoaAccountSummary",
    "AgentCoaSummary",
    "AgentMemorySummary",
    "AgentOperatorControl",
    "AgentRunPhaseState",
    "AgentRunReadiness",
    "AgentToolManifestItem",
    "AgentTraceRecord",
    "ChatMessageRecord",
    "ChatMessageResponse",
    "ChatThreadDeleteResponse",
    "ChatThreadListResponse",
    "ChatThreadSummary",
    "ChatThreadWithMessages",
    "ChatThreadWorkspaceResponse",
    "CreateChatThreadRequest",
    "GroundingContext",
    "SendChatMessageRequest",
]
