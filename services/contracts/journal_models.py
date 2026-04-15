"""
Purpose: Define validated Pydantic contracts for journal entry creation, approval,
and apply-state routing.
Scope: Request/response models for journal drafts, line items, approval actions,
and autonomy-mode routing decisions for journal application.
Dependencies: Pydantic, canonical enums, API contract base model.

Design notes:
- Every model uses extra='forbid' so that stray keys are rejected.
- Journal lines must balance: total debits == total credits (validated at model level).
- Amounts are represented as strings to preserve Decimal precision across serialization.
- Autonomy mode determines whether an approved journal can move directly to applied
  or must wait for explicit human application.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator, model_validator
from services.common.enums import AutonomyMode, ReviewStatus
from services.contracts.api_models import ContractModel

JOURNAL_POSTING_TARGETS = ("internal_ledger", "external_erp_package")


def _normalize_posting_target(value: str) -> str:
    """Normalize one posting target and enforce the canonical options."""

    normalized = value.strip().lower()
    if normalized not in JOURNAL_POSTING_TARGETS:
        allowed = ", ".join(JOURNAL_POSTING_TARGETS)
        raise ValueError(f"posting_target must be one of: {allowed}.")
    return normalized

# ---------------------------------------------------------------------------
# Journal line contracts
# ---------------------------------------------------------------------------


class JournalLineInput(ContractModel):
    """Represent one debit or credit line for a journal entry draft."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    line_no: int = Field(ge=1, description="Sequential line number within the journal (1-based).")
    account_code: str = Field(
        min_length=1,
        max_length=60,
        description="GL account code from the active chart of accounts.",
    )
    line_type: str = Field(
        pattern=r"^(debit|credit)$",
        description="Either 'debit' or 'credit'.",
    )
    amount: str = Field(
        min_length=1,
        description="Monetary amount as a decimal string (always positive).",
    )
    description: str | None = Field(
        default=None,
        description="Optional memo or description for this line.",
    )
    dimensions: dict[str, str] = Field(
        default_factory=dict,
        description="Assigned dimensions (cost_centre, department, project).",
    )
    reference: str | None = Field(
        default=None,
        max_length=120,
        description="Optional external reference or transaction ID.",
    )

    @field_validator("amount")
    @classmethod
    def validate_amount_positive(cls, value: str) -> str:
        """Ensure the amount parses as a positive Decimal."""
        try:
            amount = Decimal(value)
        except Exception as err:
            raise ValueError(
                f"Amount must be a valid decimal string, got: {value!r}"
            ) from err
        if amount <= 0:
            raise ValueError("Journal line amounts must be strictly positive.")
        return value


class JournalDraftInput(ContractModel):
    """Capture the inputs needed to create a balanced journal entry draft."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    close_run_id: UUID = Field(description="Close run this journal belongs to.")
    entity_id: UUID = Field(description="Entity workspace owning the journal.")
    recommendation_id: UUID | None = Field(
        default=None,
        description="Source recommendation UUID, if this journal was generated from one.",
    )
    posting_date: date = Field(description="Accounting date for the journal posting.")
    description: str = Field(
        min_length=1,
        max_length=2000,
        description="Narrative description of the journal entry purpose.",
    )
    lines: list[JournalLineInput] = Field(
        min_length=2,
        description="Journal lines that must balance (debits == credits).",
    )
    reasoning_summary: str | None = Field(
        default=None,
        max_length=5000,
        description="Explanation of why this journal was generated.",
    )
    metadata_payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional structured metadata (rule version, prompt version, etc.).",
    )
    source_surface: str = Field(
        default="system",
        min_length=1,
        description="Surface that created the journal (system, desktop, cli, chat).",
    )

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        """Trim and validate the journal description."""
        return value.strip()

    @model_validator(mode="after")
    def validate_balance(self) -> JournalDraftInput:
        """Ensure total debits equal total credits within cent-level tolerance."""
        total_debits = Decimal("0.00")
        total_credits = Decimal("0.00")

        for line in self.lines:
            amount = Decimal(line.amount)
            if line.line_type == "debit":
                total_debits += amount
            else:
                total_credits += amount

        if total_debits != total_credits:
            raise ValueError(
                f"Journal lines must balance. "
                f"Total debits: {total_debits}, Total credits: {total_credits}."
            )

        # Check for duplicate line numbers
        line_numbers = [line.line_no for line in self.lines]
        if len(line_numbers) != len(set(line_numbers)):
            raise ValueError("Journal line numbers must be unique.")

        return self

    @property
    def total_debits(self) -> Decimal:
        """Return the total debit amount from all lines."""
        return sum(
            (Decimal(line.amount) for line in self.lines if line.line_type == "debit"),
            Decimal("0.00"),
        )

    @property
    def total_credits(self) -> Decimal:
        """Return the total credit amount from all lines."""
        return sum(
            (Decimal(line.amount) for line in self.lines if line.line_type == "credit"),
            Decimal("0.00"),
        )


# ---------------------------------------------------------------------------
# Journal creation result
# ---------------------------------------------------------------------------


class JournalDraftResult(ContractModel):
    """Return the result after creating a journal entry draft."""

    journal_id: UUID = Field(description="The UUID of the newly created journal entry.")
    journal_number: str = Field(description="Human-readable journal identifier.")
    status: ReviewStatus = Field(description="Initial review status of the journal entry.")
    total_debits: str = Field(description="Total debit amount as a decimal string.")
    total_credits: str = Field(description="Total credit amount as a decimal string.")
    line_count: int = Field(description="Number of journal lines.")


# ---------------------------------------------------------------------------
# Approval and apply actions
# ---------------------------------------------------------------------------


class ApproveJournalRequest(ContractModel):
    """Capture a human approval decision for a journal entry."""

    reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Optional reviewer note for audit and timeline context.",
    )


class ApplyJournalRequest(ContractModel):
    """Capture a request to post an approved journal through the chosen target."""

    posting_target: str = Field(
        min_length=1,
        description=(
            "Canonical posting target: internal_ledger writes to the platform working ledger, "
            "while external_erp_package generates an accountant-managed ERP import package."
        ),
    )

    reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Optional operator note for audit and timeline context.",
    )

    @field_validator("posting_target")
    @classmethod
    def normalize_posting_target(cls, value: str) -> str:
        """Normalize and validate the requested posting target."""

        return _normalize_posting_target(value)


class RejectJournalRequest(ContractModel):
    """Capture a rejection decision for a journal entry."""

    reason: str = Field(
        min_length=1,
        max_length=500,
        description="Required reason for rejecting a journal entry.",
    )


class EditJournalRequest(ContractModel):
    """Capture edits to a journal entry before it is applied."""

    description: str | None = Field(
        default=None,
        min_length=1,
        max_length=2000,
        description="Updated journal description.",
    )
    lines: list[JournalLineInput] | None = Field(
        default=None,
        min_length=2,
        description="Updated journal lines that must still balance.",
    )
    reason: str = Field(
        min_length=1,
        max_length=500,
        description="Required reason for the journal edit.",
    )


# ---------------------------------------------------------------------------
# Journal summary for API responses
# ---------------------------------------------------------------------------


class JournalLineSummary(ContractModel):
    """Represent one journal line in an API response."""

    id: str = Field(description="Journal line UUID.")
    line_no: int = Field(description="Sequential line number.")
    account_code: str = Field(description="GL account code.")
    line_type: str = Field(description="Debit or credit.")
    amount: str = Field(description="Line amount as a decimal string.")
    description: str | None = Field(default=None, description="Line memo.")
    dimensions: dict[str, str] = Field(description="Assigned dimensions.")
    reference: str | None = Field(default=None, description="External reference.")


class JournalPostingSummary(ContractModel):
    """Describe the applied posting outcome for a journal entry."""

    id: str = Field(description="Journal posting UUID.")
    posting_target: str = Field(description="Selected posting target for the journal.")
    provider: str | None = Field(
        default=None,
        description="Resolved provider or package format when applicable.",
    )
    status: str = Field(description="Posting lifecycle state.")
    artifact_id: str | None = Field(
        default=None,
        description="Generated artifact UUID when an external package was created.",
    )
    artifact_type: str | None = Field(
        default=None,
        description="Artifact type released for the posting, when applicable.",
    )
    artifact_filename: str | None = Field(
        default=None,
        description="Download filename for the posting package, when applicable.",
    )
    artifact_storage_key: str | None = Field(
        default=None,
        description="Storage key for the posting package artifact, when applicable.",
    )
    note: str | None = Field(
        default=None,
        description="Optional operator note recorded at posting time.",
    )
    posted_by_user_id: str | None = Field(
        default=None,
        description="User who executed the posting action.",
    )
    posted_at: str = Field(description="UTC timestamp when the posting completed.")
    posting_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured posting metadata for UI and audit rendering.",
    )


class JournalSummary(ContractModel):
    """Represent a journal entry in API response payloads."""

    id: str = Field(description="Journal entry UUID.")
    entity_id: str = Field(description="Owning entity UUID.")
    close_run_id: str = Field(description="Owning close run UUID.")
    recommendation_id: str | None = Field(
        default=None,
        description="Source recommendation, if any.",
    )
    journal_number: str = Field(description="Human-readable journal identifier.")
    posting_date: date = Field(description="Accounting posting date.")
    status: ReviewStatus = Field(description="Current review status.")
    description: str = Field(description="Journal narrative description.")
    total_debits: str = Field(description="Total debit amount.")
    total_credits: str = Field(description="Total credit amount.")
    line_count: int = Field(description="Number of lines.")
    source_surface: str = Field(description="Creation surface.")
    autonomy_mode: str | None = Field(default=None, description="Autonomy mode at creation.")
    reasoning_summary: str | None = Field(default=None, description="Why this journal was created.")
    approved_by_user_id: str | None = Field(default=None, description="Approver user ID.")
    applied_by_user_id: str | None = Field(default=None, description="Applier user ID.")
    postings: list[JournalPostingSummary] = Field(
        default_factory=list,
        description="Posting outcomes recorded for this journal entry.",
    )
    lines: list[JournalLineSummary] = Field(description="Journal line items.")
    created_at: str = Field(description="UTC creation timestamp.")
    updated_at: str = Field(description="UTC update timestamp.")


class JournalListResponse(ContractModel):
    """Return journal entries for a close run or recommendation."""

    journals: tuple[JournalSummary, ...] = Field(
        default=(),
        description="Journal entries in deterministic order.",
    )


class JournalActionResponse(ContractModel):
    """Return the refreshed journal after an approval, rejection, or apply action."""

    journal: JournalSummary = Field(description="Updated journal entry.")
    action: str = Field(description="The action that was performed.")
    autonomy_mode: AutonomyMode | None = Field(
        default=None,
        description="Autonomy mode in effect when the action was taken.",
    )


# ---------------------------------------------------------------------------
# Autonomy routing helpers
# ---------------------------------------------------------------------------


class AutonomyRoutingResult(ContractModel):
    """Describe how a journal should be routed based on autonomy mode and policy."""

    target_status: ReviewStatus = Field(
        description="The review status the journal should transition to.",
    )
    requires_human_approval: bool = Field(
        description="Whether explicit human approval is required before applying.",
    )
    can_apply_automatically: bool = Field(
        description="Whether the journal can be auto-applied after approval.",
    )
    reason: str = Field(
        description="Explanation of the routing decision.",
    )


__all__ = [
    "JOURNAL_POSTING_TARGETS",
    "ApplyJournalRequest",
    "ApproveJournalRequest",
    "AutonomyRoutingResult",
    "EditJournalRequest",
    "JournalActionResponse",
    "JournalDraftInput",
    "JournalDraftResult",
    "JournalLineInput",
    "JournalLineSummary",
    "JournalListResponse",
    "JournalPostingSummary",
    "JournalSummary",
    "RejectJournalRequest",
]
