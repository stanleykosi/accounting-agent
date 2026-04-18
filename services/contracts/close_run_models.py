"""
Purpose: Define strict API contracts for close-run lifecycle and phase-gate workflows.
Scope: Create, list, detail, transition, approval, archive, and reopen payloads
for entity-scoped close runs.
Dependencies: Pydantic contract defaults, canonical workflow enums, and shared
domain phase-state models.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator
from services.common.enums import CloseRunStatus, WorkflowPhase
from services.contracts.api_models import ContractModel
from services.contracts.domain_models import CloseRunWorkflowState
from services.contracts.ledger_models import CloseRunLedgerBindingSummary


def _normalize_currency_code(value: str) -> str:
    """Normalize a reporting currency code into uppercase three-letter form."""

    normalized = value.strip().upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise ValueError("Currency codes must be three alphabetic characters.")

    return normalized


def _normalize_optional_reason(value: str | None) -> str | None:
    """Trim optional reason fields and collapse blanks to null."""

    if value is None:
        return None

    normalized = value.strip()
    return normalized or None


class CreateCloseRunRequest(ContractModel):
    """Capture the inputs required to create one entity-period close run."""

    period_start: date = Field(description="First calendar day covered by the close run.")
    period_end: date = Field(description="Last calendar day covered by the close run.")
    reporting_currency: str | None = Field(
        default=None,
        min_length=3,
        max_length=3,
        description="Reporting currency for the period. Defaults to the entity base currency.",
    )
    allow_duplicate_period: bool = Field(
        default=False,
        description=(
            "Explicitly allow another open close run for the same entity and period when "
            "a user supplies a recovery reason."
        ),
    )
    duplicate_period_reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Required operator reason when allow_duplicate_period is true.",
    )

    @field_validator("reporting_currency")
    @classmethod
    def normalize_reporting_currency(cls, value: str | None) -> str | None:
        """Normalize the optional reporting currency before service execution."""

        if value is None:
            return None

        return _normalize_currency_code(value)

    @field_validator("duplicate_period_reason")
    @classmethod
    def normalize_duplicate_period_reason(cls, value: str | None) -> str | None:
        """Normalize duplicate-period reasons before validation."""

        return _normalize_optional_reason(value)

    @model_validator(mode="after")
    def validate_period_and_duplicate_reason(self) -> CreateCloseRunRequest:
        """Require a valid date range and a reason for explicit duplicate-period creation."""

        if self.period_end < self.period_start:
            raise ValueError("period_end must be on or after period_start.")
        if self.allow_duplicate_period and self.duplicate_period_reason is None:
            raise ValueError("duplicate_period_reason is required when duplicates are allowed.")
        if not self.allow_duplicate_period and self.duplicate_period_reason is not None:
            raise ValueError("duplicate_period_reason is only valid when duplicates are allowed.")

        return self


class TransitionCloseRunRequest(ContractModel):
    """Capture an explicit request to move into the next canonical workflow phase."""

    target_phase: WorkflowPhase = Field(
        description="Immediate next workflow phase to open after the active phase is ready.",
    )
    reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Optional operator note for audit and timeline context.",
    )

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        """Normalize optional transition reasons."""

        return _normalize_optional_reason(value)


class RewindCloseRunRequest(ContractModel):
    """Capture an explicit request to reopen an earlier canonical workflow phase."""

    target_phase: WorkflowPhase = Field(
        description="Earlier workflow phase to reopen and resume work from.",
    )
    reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Optional operator note explaining why the workflow is moving backward.",
    )

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        """Normalize optional rewind reasons."""

        return _normalize_optional_reason(value)


class CloseRunDecisionRequest(ContractModel):
    """Capture an approval, archive, or reopen decision note."""

    reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Optional operator-facing reason persisted to the close-run timeline.",
    )

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        """Normalize optional decision reasons."""

        return _normalize_optional_reason(value)


class CloseRunSummary(ContractModel):
    """Describe one close run with lifecycle metadata and calculated phase state."""

    id: str = Field(description="Stable UUID for the close run.")
    entity_id: str = Field(description="Stable UUID for the owning entity workspace.")
    period_start: date = Field(description="First calendar day covered by the close run.")
    period_end: date = Field(description="Last calendar day covered by the close run.")
    status: CloseRunStatus = Field(description="Current lifecycle status of this close run.")
    reporting_currency: str = Field(
        min_length=3,
        max_length=3,
        description="Reporting currency used for this close run.",
    )
    current_version_no: int = Field(
        ge=1,
        description="Version number for the entity-period close-run working state.",
    )
    opened_by_user_id: str = Field(description="UUID of the user who opened this run.")
    approved_by_user_id: str | None = Field(
        default=None,
        description="UUID of the user who approved this run, if signed off.",
    )
    approved_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when the run was approved, if signed off.",
    )
    archived_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when the run was archived, if archived.",
    )
    reopened_from_close_run_id: str | None = Field(
        default=None,
        description="Source close-run UUID when this row is a reopened working version.",
    )
    ledger_binding: CloseRunLedgerBindingSummary | None = Field(
        default=None,
        description="Imported ledger baseline bound to this close run, if any.",
    )
    workflow_state: CloseRunWorkflowState = Field(
        description="Calculated lifecycle and phase-gate state in canonical phase order.",
    )
    created_at: datetime = Field(description="UTC timestamp when the close run was created.")
    updated_at: datetime = Field(description="UTC timestamp when the close run was last updated.")


class CloseRunListResponse(ContractModel):
    """Return close runs for one entity in deterministic period/version order."""

    close_runs: tuple[CloseRunSummary, ...] = Field(
        default=(),
        description="Close runs the authenticated operator can access for the entity.",
    )


class CloseRunTransitionResponse(ContractModel):
    """Return the refreshed close run and the transition that just occurred."""

    close_run: CloseRunSummary = Field(description="Refreshed close-run detail.")
    completed_phase: WorkflowPhase = Field(description="Phase that was completed.")
    active_phase: WorkflowPhase = Field(description="Phase that is now active.")


class CloseRunRewindResponse(ContractModel):
    """Return the refreshed close run after reopening an earlier workflow phase."""

    close_run: CloseRunSummary = Field(description="Refreshed close-run detail.")
    previous_active_phase: WorkflowPhase = Field(
        description="Phase that was active before the rewind.",
    )
    active_phase: WorkflowPhase = Field(description="Phase that is now active again.")


class CloseRunReopenResponse(ContractModel):
    """Return the new working close-run version created by reopening a released run."""

    close_run: CloseRunSummary = Field(description="New reopened close-run working version.")
    source_close_run_id: str = Field(description="Close-run UUID that was reopened.")
    status: Literal["reopened"] = Field(description="Stable reopened response status.")


class CloseRunDeleteResponse(ContractModel):
    """Describe the destructive result of deleting one mutable close run."""

    deleted_close_run_id: str = Field(description="UUID of the deleted close run.")
    deleted_document_count: int = Field(
        ge=0,
        description="Number of source documents removed with the close run.",
    )
    deleted_recommendation_count: int = Field(
        ge=0,
        description="Number of recommendations removed with the close run.",
    )
    deleted_journal_count: int = Field(
        ge=0,
        description="Number of journals removed with the close run.",
    )
    deleted_report_run_count: int = Field(
        ge=0,
        description="Number of reporting runs removed with the close run.",
    )
    deleted_thread_count: int = Field(
        ge=0,
        description="Number of close-run-scoped chat threads removed with the close run.",
    )
    canceled_job_count: int = Field(
        ge=0,
        description="Number of active background jobs canceled before deletion.",
    )


__all__ = [
    "CloseRunDecisionRequest",
    "CloseRunDeleteResponse",
    "CloseRunListResponse",
    "CloseRunReopenResponse",
    "CloseRunRewindResponse",
    "CloseRunSummary",
    "CloseRunTransitionResponse",
    "CreateCloseRunRequest",
    "RewindCloseRunRequest",
    "TransitionCloseRunRequest",
]
