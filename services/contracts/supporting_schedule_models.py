"""
Purpose: Define strict API contracts for standalone Step 6 supporting schedules.
Scope: Typed row payloads, workspace summaries, mutation requests, and review
       status transitions for fixed assets, loan amortisation, accrual tracker,
       and budget-vs-actual workpapers.
Dependencies: Pydantic contract base model and canonical supporting-schedule enums.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator
from services.common.enums import SupportingScheduleStatus, SupportingScheduleType
from services.contracts.api_models import ContractModel


class FixedAssetScheduleRowPayload(ContractModel):
    """Typed payload for one fixed-asset register row."""

    asset_id: str = Field(min_length=1, max_length=120, description="Stable asset reference.")
    asset_name: str = Field(min_length=1, max_length=300, description="Display asset name.")
    acquisition_date: str = Field(description="Acquisition date in YYYY-MM-DD format.")
    asset_account_code: str = Field(
        min_length=1,
        max_length=60,
        description="Fixed-asset ledger account code used for reconciliation.",
    )
    accumulated_depreciation_account_code: str = Field(
        min_length=1,
        max_length=60,
        description="Accumulated depreciation ledger account code.",
    )
    cost: str = Field(description="Asset cost as a decimal string.")
    accumulated_depreciation: str = Field(
        description="Accumulated depreciation as a decimal string.",
    )
    net_book_value: str | None = Field(
        default=None,
        description="Net book value as a decimal string. Derived if omitted.",
    )
    depreciation_expense: str | None = Field(
        default=None,
        description="Current-period depreciation expense as a decimal string.",
    )
    disposal_date: str | None = Field(
        default=None,
        description="Disposal date in YYYY-MM-DD format when applicable.",
    )
    notes: str | None = Field(default=None, max_length=2000, description="Optional operator note.")


class LoanAmortisationScheduleRowPayload(ContractModel):
    """Typed payload for one loan amortisation schedule row."""

    loan_id: str = Field(min_length=1, max_length=120, description="Stable loan reference.")
    lender_name: str = Field(min_length=1, max_length=300, description="Lender display name.")
    payment_no: int = Field(
        ge=1,
        description="Sequential payment number in the amortisation table.",
    )
    due_date: str = Field(description="Payment due date in YYYY-MM-DD format.")
    loan_account_code: str = Field(
        min_length=1,
        max_length=60,
        description="Principal or loan-balance ledger account code.",
    )
    interest_account_code: str = Field(
        min_length=1,
        max_length=60,
        description="Interest expense or accrued-interest ledger account code.",
    )
    principal: str = Field(description="Scheduled principal amount as a decimal string.")
    interest: str = Field(description="Scheduled interest amount as a decimal string.")
    balance: str = Field(description="Outstanding balance after this payment as a decimal string.")
    notes: str | None = Field(default=None, max_length=2000, description="Optional operator note.")


class AccrualTrackerScheduleRowPayload(ContractModel):
    """Typed payload for one accrual tracker row."""

    ref: str = Field(min_length=1, max_length=160, description="Stable accrual reference.")
    description: str = Field(min_length=1, max_length=300, description="Accrual description.")
    account_code: str = Field(
        min_length=1,
        max_length=60,
        description="Accrual ledger account code being reconciled.",
    )
    amount: str = Field(description="Expected accrual amount as a decimal string.")
    period: str = Field(description="Accounting period in YYYY-MM format.")
    reversal_date: str | None = Field(
        default=None,
        description="Expected reversal date in YYYY-MM-DD format when applicable.",
    )
    counterparty: str | None = Field(
        default=None,
        max_length=200,
        description="Counterparty or source contract reference when applicable.",
    )
    notes: str | None = Field(default=None, max_length=2000, description="Optional operator note.")


class BudgetVsActualScheduleRowPayload(ContractModel):
    """Typed payload for one budget-vs-actual workpaper row."""

    account_code: str = Field(
        min_length=1,
        max_length=60,
        description="Budget account code used for variance analysis.",
    )
    period: str = Field(description="Budget period in YYYY-MM format.")
    budget_amount: str = Field(description="Budgeted amount as a decimal string.")
    department: str | None = Field(
        default=None,
        max_length=120,
        description="Optional department dimension used for the budget line.",
    )
    cost_centre: str | None = Field(
        default=None,
        max_length=120,
        description="Optional cost-centre dimension used for the budget line.",
    )
    project: str | None = Field(
        default=None,
        max_length=120,
        description="Optional project dimension used for the budget line.",
    )
    notes: str | None = Field(default=None, max_length=2000, description="Optional operator note.")


SupportingScheduleRowPayload = (
    FixedAssetScheduleRowPayload
    | LoanAmortisationScheduleRowPayload
    | AccrualTrackerScheduleRowPayload
    | BudgetVsActualScheduleRowPayload
)


class SupportingScheduleSummary(ContractModel):
    """Describe one Step 6 supporting schedule in workspace responses."""

    id: str | None = Field(default=None, description="Schedule UUID when created.")
    close_run_id: str = Field(description="Owning close run UUID.")
    schedule_type: SupportingScheduleType = Field(description="Canonical supporting schedule type.")
    label: str = Field(description="Operator-facing schedule label.")
    status: SupportingScheduleStatus = Field(description="Current schedule lifecycle status.")
    row_count: int = Field(ge=0, description="Number of maintained rows in the schedule.")
    note: str | None = Field(default=None, description="Latest schedule note when present.")
    reviewed_by_user_id: str | None = Field(
        default=None,
        description="Reviewing user UUID when finalized.",
    )
    reviewed_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of final review.",
    )
    updated_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the latest row or header update.",
    )


class SupportingScheduleRowSummary(ContractModel):
    """Describe one persisted workpaper row in a supporting schedule."""

    id: str = Field(description="Row UUID.")
    schedule_id: str = Field(description="Parent schedule UUID.")
    schedule_type: SupportingScheduleType = Field(description="Canonical schedule type.")
    row_ref: str = Field(description="Canonical row reference.")
    line_no: int = Field(ge=1, description="Stable display order.")
    payload: dict[str, object] = Field(
        default_factory=dict,
        description="Normalized row payload exposed to the editor and agent.",
    )
    created_at: datetime = Field(description="UTC timestamp when the row was created.")
    updated_at: datetime = Field(description="UTC timestamp when the row was last updated.")


class SupportingScheduleDetail(ContractModel):
    """Describe one full supporting schedule including its rows."""

    schedule: SupportingScheduleSummary = Field(description="Schedule header state.")
    rows: tuple[SupportingScheduleRowSummary, ...] = Field(
        default=(),
        description="Rows in deterministic line order.",
    )


class SupportingScheduleWorkspaceResponse(ContractModel):
    """Return the full Step 6 supporting-schedule workspace for a close run."""

    schedules: tuple[SupportingScheduleDetail, ...] = Field(
        default=(),
        description="All canonical Step 6 schedules in deterministic order.",
    )


class UpsertSupportingScheduleRowRequest(ContractModel):
    """Create or update one schedule row using a typed payload."""

    row_id: str | None = Field(default=None, description="Existing row UUID for updates.")
    payload: SupportingScheduleRowPayload = Field(
        description="Typed schedule-row payload validated against the canonical contracts.",
    )


class SupportingScheduleRowMutationResult(ContractModel):
    """Return the current schedule after a row mutation."""

    schedule: SupportingScheduleDetail = Field(description="Schedule detail after the mutation.")


class UpdateSupportingScheduleStatusRequest(ContractModel):
    """Transition one supporting schedule between review states."""

    status: Literal["in_review", "approved", "not_applicable"] = Field(
        description="Next schedule status.",
    )
    note: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional note. Required when marking the schedule not applicable.",
    )

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


__all__ = [
    "AccrualTrackerScheduleRowPayload",
    "BudgetVsActualScheduleRowPayload",
    "FixedAssetScheduleRowPayload",
    "LoanAmortisationScheduleRowPayload",
    "SupportingScheduleDetail",
    "SupportingScheduleRowMutationResult",
    "SupportingScheduleRowPayload",
    "SupportingScheduleRowSummary",
    "SupportingScheduleSummary",
    "SupportingScheduleType",
    "SupportingScheduleWorkspaceResponse",
    "UpdateSupportingScheduleStatusRequest",
    "UpsertSupportingScheduleRowRequest",
]
