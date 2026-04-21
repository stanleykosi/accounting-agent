"""
Purpose: Define strict Pydantic contracts for chat-initiated accounting
action intents, proposed edits, and approval requests.
Scope: Chat action types (proposed edit, approval request, document request,
explanation response), intent classification payloads, and the proposed-change
objects that flow through review logic based on autonomy mode.
Dependencies: Pydantic, canonical enums, and the base ContractModel.

Design notes:
- Every model uses extra='forbid' so that stray LLM keys are rejected.
- Action payloads mirror existing recommendation/journal contracts so the
  downstream approval pipeline can reuse the same validation and audit paths.
- Autonomy mode determines whether a proposed change goes to pending_review
  or can apply directly to working state.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator
from services.common.enums import (
    AutonomyMode,
    DocumentType,
    ReconciliationType,
    WorkflowPhase,
)
from services.contracts.api_models import ContractModel
from services.contracts.chat_models import AgentOperatorControl

# ---------------------------------------------------------------------------
# Chat action intent classification
# ---------------------------------------------------------------------------

CHAT_ACTION_INTENTS = (
    "proposed_edit",
    "approval_request",
    "document_request",
    "explanation",
    "workflow_action",
    "reconciliation_query",
    "report_action",
)


class ChatActionIntent(ContractModel):
    """Describe the classified intent of a user message that requests a
    workflow action rather than pure read-only analysis."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    intent: Literal[
        "proposed_edit",
        "approval_request",
        "document_request",
        "explanation",
        "workflow_action",
        "reconciliation_query",
        "report_action",
    ] = Field(
        description="The classified action intent for the user message.",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Classifier confidence for the detected intent.",
    )
    target_phase: WorkflowPhase | None = Field(
        default=None,
        description="Workflow phase the action relates to, if detectable.",
    )
    target_type: str | None = Field(
        default=None,
        description=(
            "The business object type the action targets "
            "(e.g. 'recommendation', 'journal')."
        ),
    )
    target_id: UUID | None = Field(
        default=None,
        description="Specific business object UUID when the action references one.",
    )
    requires_review: bool = Field(
        default=True,
        description="Whether the action must go through review regardless of autonomy mode.",
    )

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: float) -> float:
        """Require non-trivial confidence for action classification."""
        if value < 0.3:
            raise ValueError(
                f"Action intent confidence {value} is too low for reliable routing. "
                "Treat the message as read-only analysis instead."
            )
        return value


# ---------------------------------------------------------------------------
# Proposed edit payloads (chat-originated changes to accounting state)
# ---------------------------------------------------------------------------


class ProposedEditPayload(ContractModel):
    """Capture a structured proposed edit generated from a chat action.

    This is the canonical container for chat-originated changes to
    recommendations, journals, extracted fields, reconciliations, or report
    commentary. The payload is validated and then routed to the review
    pipeline -- it never mutates state directly.
    """

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    target_type: str = Field(
        min_length=1,
        description=(
            "The business object type being edited "
            "(e.g. 'recommendation', 'journal', 'extracted_field')."
        ),
    )
    target_id: UUID = Field(
        description="UUID of the business object being edited.",
    )
    field_path: str = Field(
        min_length=1,
        description="Dot-notation path to the field being changed.",
    )
    current_value: Any = Field(
        default=None,
        description="The value before the proposed change.",
    )
    proposed_value: Any = Field(
        description="The value the chat action proposes.",
    )
    reasoning: str = Field(
        min_length=1,
        max_length=2000,
        description="Explanation for why the change is proposed.",
    )
    evidence_refs: list[dict[str, str]] = Field(
        default_factory=list,
        description="Structured evidence links supporting the proposed change.",
    )

    @field_validator("field_path")
    @classmethod
    def normalize_field_path(cls, value: str) -> str:
        """Trim and validate the field path."""
        return value.strip()

    @model_validator(mode="after")
    def validate_change_is_nontrivial(self) -> ProposedEditPayload:
        """Reject proposed edits that don't actually change the value."""
        if self.current_value == self.proposed_value:
            raise ValueError(
                "Proposed edit must change the field value. "
                f"Current and proposed values are identical for '{self.field_path}'."
            )
        return self


class ProposedJournalEdit(ContractModel):
    """Capture a chat-originated edit to a journal entry draft."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    journal_id: UUID = Field(description="Journal entry being edited.")
    description_change: dict[str, str] | None = Field(
        default=None,
        description="{'from': old, 'to': new} for the journal description.",
    )
    line_changes: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of line-level change objects with line_no, field, from, to.",
    )
    reasoning: str = Field(
        min_length=1,
        max_length=2000,
        description="Why the journal edit is proposed.",
    )

    @model_validator(mode="after")
    def validate_at_least_one_change(self) -> ProposedJournalEdit:
        """Require at least one change."""
        if not self.description_change and not self.line_changes:
            raise ValueError(
                "A proposed journal edit must include at least one description or line change."
            )
        return self


# ---------------------------------------------------------------------------
# Approval requests generated from chat
# ---------------------------------------------------------------------------


class ChatApprovalRequest(ContractModel):
    """Capture an approval request generated by chat for a reviewable object."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    target_type: Literal["recommendation", "journal", "reconciliation", "report"] = Field(
        description="The type of object being submitted for approval.",
    )
    target_id: UUID = Field(
        description="UUID of the object to approve.",
    )
    reason: str | None = Field(
        default=None,
        max_length=500,
        description="Optional reviewer note attached to the approval request.",
    )
    requested_action: Literal["approve", "reject", "request_info"] = Field(
        description="The approval action the chat assistant is requesting.",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence that the approval request is appropriate.",
    )


# ---------------------------------------------------------------------------
# Document request actions (chat asking for missing documents)
# ---------------------------------------------------------------------------


class ChatDocumentRequest(ContractModel):
    """Capture a request for missing documents generated by chat analysis."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    close_run_id: UUID = Field(
        description="Close run that needs the documents.",
    )
    document_types: list[DocumentType] = Field(
        min_length=1,
        description="Types of documents the chat assistant is requesting.",
    )
    reason: str = Field(
        min_length=1,
        max_length=1000,
        description="Why these documents are needed to proceed.",
    )
    blocking: bool = Field(
        default=False,
        description="Whether missing these documents blocks workflow progression.",
    )


# ---------------------------------------------------------------------------
# Reconciliation and report query actions
# ---------------------------------------------------------------------------


class ChatReconciliationAction(ContractModel):
    """Capture a reconciliation-related chat action."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    reconciliation_type: ReconciliationType = Field(
        description="The reconciliation type the chat action relates to.",
    )
    action: Literal["query", "resolve_exception", "request_disposition"] = Field(
        description="The reconciliation action the chat is performing.",
    )
    item_refs: list[UUID] = Field(
        default_factory=list,
        description="Specific reconciliation item IDs if the action targets items.",
    )
    reasoning: str | None = Field(
        default=None,
        max_length=2000,
        description="Explanation of the reconciliation action.",
    )


class ChatReportAction(ContractModel):
    """Capture a report-related chat action (regeneration, commentary edit, etc.)."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    action: Literal[
        "request_regeneration",
        "edit_commentary",
        "approve_commentary",
        "request_evidence_pack",
    ] = Field(
        description="The report action the chat is performing.",
    )
    target_sections: list[str] | None = Field(
        default=None,
        description="Report section keys affected, or null for full report.",
    )
    commentary_change: dict[str, str] | None = Field(
        default=None,
        description="{'section': key, 'from': old, 'to': new} for commentary edits.",
    )
    reasoning: str | None = Field(
        default=None,
        max_length=2000,
        description="Explanation of the report action.",
    )


# ---------------------------------------------------------------------------
# Unified chat action execution plan
# ---------------------------------------------------------------------------


class ChatActionExecutionPlan(ContractModel):
    """Capture the full execution plan for a chat-detected action intent.

    This is the central object that ties together intent classification,
    target identification, proposed changes, and autonomy-mode routing.
    The service layer persists this plan as a proposed change when the
    intent represents a state-changing request.
    """

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    thread_id: UUID = Field(description="Chat thread where the action originated.")
    message_id: UUID | None = Field(
        default=None,
        description="Message that triggered the action (set after persistence).",
    )
    intent: ChatActionIntent = Field(description="Classified action intent.")
    autonomy_mode: AutonomyMode = Field(
        description="Autonomy mode in effect when the action was detected.",
    )
    proposed_edit: ProposedEditPayload | None = Field(
        default=None,
        description="Structured proposed edit when intent is 'proposed_edit'.",
    )
    approval_request: ChatApprovalRequest | None = Field(
        default=None,
        description="Approval request when intent is 'approval_request'.",
    )
    document_request: ChatDocumentRequest | None = Field(
        default=None,
        description="Document request when intent is 'document_request'.",
    )
    reconciliation_action: ChatReconciliationAction | None = Field(
        default=None,
        description="Reconciliation action when intent is 'reconciliation_query'.",
    )
    report_action: ChatReportAction | None = Field(
        default=None,
        description="Report action when intent is 'report_action'.",
    )
    reasoning: str = Field(
        min_length=1,
        max_length=3000,
        description="Narrative explanation of the action plan.",
    )
    requires_human_approval: bool = Field(
        default=True,
        description="Whether the action requires explicit human approval.",
    )
    status: Literal["pending", "approved", "rejected", "superseded", "applied"] = Field(
        default="pending",
        description="Review lifecycle state for this action plan.",
    )


# ---------------------------------------------------------------------------
# API request/response contracts for chat action endpoints
# ---------------------------------------------------------------------------


class SendChatActionRequest(ContractModel):
    """Input for sending a user message that may contain action intents."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    content: str = Field(
        min_length=1,
        max_length=10_000,
        description="User message text that may contain action intent.",
    )
    force_action_mode: bool = Field(
        default=False,
        description="When true, skip read-only analysis and attempt action routing.",
    )

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        """Reject whitespace-only messages."""
        normalized = value.strip()
        if not normalized:
            raise ValueError("Message content cannot be blank.")
        return normalized


class ChatActionResponse(ContractModel):
    """Return the assistant response that may include an action execution plan."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    message_id: str = Field(description="Assistant message UUID.")
    content: str = Field(description="Assistant response text.")
    action_plan: ChatActionSummary | None = Field(
        default=None,
        description="Action summary when the message contained action intent. "
        "Null when the response is pure read-only analysis.",
    )
    is_read_only: bool = Field(
        default=True,
        description="True when the response is pure analysis with no state changes.",
    )
    thread_entity_id: str = Field(
        description="Entity workspace UUID that now anchors the thread after this turn.",
    )
    thread_close_run_id: str | None = Field(
        default=None,
        description="Close run UUID anchoring the thread after this turn, if any.",
    )
    operator_controls: tuple[AgentOperatorControl, ...] = Field(
        default=(),
        description=(
            "Channel-portable suggested commands and governed-action controls returned "
            "with the assistant reply."
        ),
    )


class ApproveChatActionRequest(ContractModel):
    """Approve a pending chat-originated action plan."""

    reason: str | None = Field(
        default=None,
        max_length=500,
        description="Optional reviewer note for the audit trail.",
    )


class RejectChatActionRequest(ContractModel):
    """Reject a pending chat-originated action plan."""

    reason: str = Field(
        min_length=1,
        max_length=500,
        description="Required reason for rejecting the chat action.",
    )


class ChatActionSummary(ContractModel):
    """Summarize a chat action plan for list and badge views."""

    id: str = Field(description="Action plan UUID.")
    thread_id: str = Field(description="Source chat thread.")
    intent: str = Field(description="Classified action intent.")
    target_type: str | None = Field(default=None, description="Target business object type.")
    target_id: str | None = Field(default=None, description="Target business object UUID.")
    status: str = Field(description="Current review status.")
    requires_human_approval: bool = Field(description="Whether human approval is needed.")
    created_at: str = Field(description="UTC creation timestamp.")


__all__ = [
    "CHAT_ACTION_INTENTS",
    "ApproveChatActionRequest",
    "ChatActionExecutionPlan",
    "ChatActionIntent",
    "ChatActionResponse",
    "ChatActionSummary",
    "ChatApprovalRequest",
    "ChatDocumentRequest",
    "ChatReconciliationAction",
    "ChatReportAction",
    "ProposedEditPayload",
    "ProposedJournalEdit",
    "RejectChatActionRequest",
    "SendChatActionRequest",
]
