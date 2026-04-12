"""
Purpose: Define validated Pydantic contracts for reconciliation workflows.
Scope: Request/response models for reconciliation creation, item matching, reviewer
       disposition, trial balance snapshots, anomaly investigation, and reconciliation
       status queries across all reconciliation types (bank, AR/AP ageing, intercompany,
       payroll control, fixed assets, loan amortisation, accrual tracker, budget vs actual,
       trial balance).
Dependencies: Pydantic, canonical enums, API contract base model.

Design notes:
- Every model uses extra='forbid' so that stray keys are rejected.
- Amounts are represented as strings to preserve Decimal precision across serialization.
- Match references are structured JSON objects allowing flexible counterpart linking.
- Disposition actions require explicit reasoning for audit traceability.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import Field
from services.common.enums import (
    AnomalyType,
    DispositionAction,
    MatchStatus,
    ReconciliationSourceType,
    ReconciliationStatus,
    ReconciliationType,
)
from services.contracts.api_models import ContractModel

# ---------------------------------------------------------------------------
# Reconciliation summary and list contracts
# ---------------------------------------------------------------------------


class ReconciliationSummary(ContractModel):
    """Represent one reconciliation run in API response payloads."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    id: str = Field(description="Reconciliation UUID.")
    close_run_id: str = Field(description="Owning close run UUID.")
    reconciliation_type: ReconciliationType = Field(
        description="The reconciliation category.",
    )
    status: ReconciliationStatus = Field(description="Current lifecycle status.")
    summary: dict[str, Any] = Field(
        default_factory=dict,
        description="Aggregated reconciliation summary (matched count, exceptions, totals).",
    )
    blocking_reason: str | None = Field(
        default=None,
        description="Blocking reason when status is 'blocked'.",
    )
    approved_by_user_id: str | None = Field(default=None, description="Approver user ID.")
    created_by_user_id: str | None = Field(default=None, description="Creator user ID.")
    item_count: int = Field(
        default=0,
        description="Total number of reconciliation items.",
    )
    matched_count: int = Field(
        default=0,
        description="Number of items with matched status.",
    )
    exception_count: int = Field(
        default=0,
        description="Number of items with exception or unmatched status requiring disposition.",
    )
    created_at: str = Field(description="UTC creation timestamp.")
    updated_at: str = Field(description="UTC update timestamp.")


class ReconciliationListResponse(ContractModel):
    """Return reconciliation runs for a close run."""

    reconciliations: tuple[ReconciliationSummary, ...] = Field(
        default=(),
        description="Reconciliation runs in deterministic order.",
    )


# ---------------------------------------------------------------------------
# Reconciliation creation contracts
# ---------------------------------------------------------------------------


class CreateReconciliationRequest(ContractModel):
    """Capture inputs for creating a new reconciliation run."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    close_run_id: UUID = Field(description="Close run this reconciliation belongs to.")
    reconciliation_type: ReconciliationType = Field(
        description="The reconciliation category.",
    )
    created_by_user_id: UUID | None = Field(
        default=None,
        description="User initiating the reconciliation (optional for system-generated runs).",
    )


class ReconciliationCreationResult(ContractModel):
    """Return the result after creating a reconciliation run."""

    reconciliation_id: UUID = Field(description="The UUID of the newly created reconciliation.")
    status: ReconciliationStatus = Field(description="Initial status (always 'draft').")
    reconciliation_type: ReconciliationType = Field(description="The reconciliation category.")


# ---------------------------------------------------------------------------
# Reconciliation item contracts
# ---------------------------------------------------------------------------


class ReconciliationItemMatch(ContractModel):
    """Represent one counterpart that a reconciliation item was matched to."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    source_type: ReconciliationSourceType = Field(
        description="Type of the matched counterpart.",
    )
    source_ref: str = Field(
        min_length=1,
        max_length=200,
        description="Reference to the matched counterpart.",
    )
    amount: str | None = Field(
        default=None,
        description="Counterpart amount as a decimal string, if applicable.",
    )
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Match confidence score between 0 and 1.",
    )


class ReconciliationItemSummary(ContractModel):
    """Represent one reconciliation item in API response payloads."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    id: str = Field(description="Reconciliation item UUID.")
    reconciliation_id: str = Field(description="Parent reconciliation UUID.")
    source_type: str = Field(description="Source type of the item.")
    source_ref: str = Field(description="Reference to the originating record.")
    match_status: MatchStatus = Field(description="Match outcome for this item.")
    amount: str = Field(description="Monetary amount as a decimal string.")
    difference_amount: str = Field(description="Difference from matched counterpart(s).")
    matched_to: list[ReconciliationItemMatch] = Field(
        default_factory=list,
        description="Counterparts this item was matched to.",
    )
    explanation: str | None = Field(
        default=None,
        description="Match outcome explanation.",
    )
    requires_disposition: bool = Field(
        description="Whether reviewer disposition is required.",
    )
    disposition: DispositionAction | None = Field(
        default=None,
        description="Reviewer disposition choice.",
    )
    disposition_reason: str | None = Field(
        default=None,
        description="Reviewer reasoning for the disposition.",
    )
    disposition_by_user_id: str | None = Field(default=None, description="Disposer user ID.")
    dimensions: dict[str, Any] = Field(
        default_factory=dict,
        description="Accounting dimensions if applicable.",
    )
    period_date: str | None = Field(
        default=None,
        description="Accounting period date (YYYY-MM-DD).",
    )
    created_at: str = Field(description="UTC creation timestamp.")
    updated_at: str = Field(description="UTC update timestamp.")


class ReconciliationItemListResponse(ContractModel):
    """Return reconciliation items for a reconciliation run."""

    items: tuple[ReconciliationItemSummary, ...] = Field(
        default=(),
        description="Reconciliation items in deterministic order.",
    )


# ---------------------------------------------------------------------------
# Item disposition contracts
# ---------------------------------------------------------------------------


class DispositionItemRequest(ContractModel):
    """Capture a reviewer disposition for a reconciliation item."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    disposition: DispositionAction = Field(
        description="The reviewer's disposition choice.",
    )
    reason: str = Field(
        min_length=1,
        max_length=2000,
        description="Required reasoning for the disposition decision.",
    )


class DispositionResult(ContractModel):
    """Return the result after recording a reviewer disposition."""

    item_id: UUID = Field(description="Reconciliation item UUID.")
    disposition: DispositionAction = Field(description="The recorded disposition.")
    requires_further_action: bool = Field(
        description="Whether further action is needed after this disposition.",
    )


class BulkDispositionRequest(ContractModel):
    """Capture bulk disposition for multiple reconciliation items."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    item_ids: list[UUID] = Field(
        min_length=1,
        description="List of reconciliation item UUIDs to disposition.",
    )
    disposition: DispositionAction = Field(
        description="The reviewer's disposition choice for all items.",
    )
    reason: str = Field(
        min_length=1,
        max_length=2000,
        description="Required reasoning for the bulk disposition.",
    )


# ---------------------------------------------------------------------------
# Reconciliation approval contracts
# ---------------------------------------------------------------------------


class ApproveReconciliationRequest(ContractModel):
    """Capture reviewer approval of a reconciliation run."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    reason: str = Field(
        min_length=1,
        max_length=2000,
        description="Required reviewer note for approval.",
    )


class ApproveReconciliationResult(ContractModel):
    """Return the result after approving a reconciliation run."""

    reconciliation_id: UUID = Field(description="Reconciliation UUID.")
    status: ReconciliationStatus = Field(description="Updated status (should be 'approved').")
    approved_by_user_id: str = Field(description="Approver user ID.")


# ---------------------------------------------------------------------------
# Trial balance contracts
# ---------------------------------------------------------------------------


class TrialBalanceAccountEntry(ContractModel):
    """Represent one account's balance in a trial balance snapshot."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    account_code: str = Field(
        min_length=1,
        max_length=60,
        description="GL account code.",
    )
    account_name: str = Field(
        min_length=1,
        description="GL account name.",
    )
    account_type: str = Field(description="Account type (asset, liability, etc.).")
    debit_balance: str = Field(
        default="0.00",
        description="Debit balance as a decimal string.",
    )
    credit_balance: str = Field(
        default="0.00",
        description="Credit balance as a decimal string.",
    )
    net_balance: str = Field(
        description="Net balance (debit - credit) as a decimal string.",
    )
    is_active: bool = Field(
        default=True,
        description="Whether the account is active in the chart of accounts.",
    )


class TrialBalanceSnapshotSummary(ContractModel):
    """Represent a trial balance snapshot in API responses."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    id: str = Field(description="Snapshot UUID.")
    close_run_id: str = Field(description="Owning close run UUID.")
    snapshot_no: int = Field(description="Sequential snapshot number.")
    total_debits: str = Field(description="Total debits as a decimal string.")
    total_credits: str = Field(description="Total credits as a decimal string.")
    is_balanced: bool = Field(description="Whether debits equal credits.")
    account_count: int = Field(description="Number of accounts in the snapshot.")
    generated_by_user_id: str | None = Field(default=None, description="Generator user ID.")
    created_at: str = Field(description="UTC creation timestamp.")


class TrialBalanceDetailResponse(ContractModel):
    """Return a full trial balance with per-account detail."""

    snapshot: TrialBalanceSnapshotSummary = Field(
        description="Snapshot metadata.",
    )
    accounts: list[TrialBalanceAccountEntry] = Field(
        description="Per-account balance entries.",
    )


# ---------------------------------------------------------------------------
# Reconciliation anomaly contracts
# ---------------------------------------------------------------------------


class ReconciliationAnomalySummary(ContractModel):
    """Represent one anomaly in API response payloads."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    id: str = Field(description="Anomaly UUID.")
    close_run_id: str = Field(description="Owning close run UUID.")
    anomaly_type: AnomalyType = Field(description="Category of the anomaly.")
    severity: str = Field(description="Severity: info, warning, or blocking.")
    account_code: str | None = Field(default=None, description="Associated GL account code.")
    description: str = Field(description="Human-readable anomaly description.")
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured anomaly details.",
    )
    resolved: bool = Field(description="Whether the anomaly was resolved.")
    resolved_by_user_id: str | None = Field(default=None, description="Resolver user ID.")
    created_at: str = Field(description="UTC creation timestamp.")


class ReconciliationAnomalyListResponse(ContractModel):
    """Return anomalies for a close run."""

    anomalies: tuple[ReconciliationAnomalySummary, ...] = Field(
        default=(),
        description="Anomalies in deterministic order.",
    )


class ResolveAnomalyRequest(ContractModel):
    """Capture reviewer resolution of a reconciliation anomaly."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    resolution_note: str = Field(
        min_length=1,
        max_length=2000,
        description="Required reviewer reasoning for resolving the anomaly.",
    )


# ---------------------------------------------------------------------------
# Matching configuration contracts
# ---------------------------------------------------------------------------


class MatchingConfig(ContractModel):
    """Capture matching thresholds and tolerances for reconciliation."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    exact_amount_tolerance: str = Field(
        default="0.00",
        description="Exact match amount tolerance as a decimal string.",
    )
    fuzzy_amount_tolerance_pct: float = Field(
        default=1.0,
        ge=0.0,
        le=100.0,
        description="Fuzzy match tolerance as a percentage.",
    )
    date_tolerance_days: int = Field(
        default=5,
        ge=0,
        description="Maximum days between matched transaction dates.",
    )
    reference_match_strict: bool = Field(
        default=True,
        description="Whether reference matching requires exact string match.",
    )


# ---------------------------------------------------------------------------
# Reconciliation run execution contracts
# ---------------------------------------------------------------------------


class RunReconciliationRequest(ContractModel):
    """Capture inputs for running reconciliation matching on a close run."""

    model_config = ContractModel.model_config.copy() | {"frozen": False}

    close_run_id: UUID = Field(description="Close run to run reconciliation for.")
    reconciliation_types: list[ReconciliationType] = Field(
        min_length=1,
        description="Which reconciliation types to run.",
    )
    matching_config: MatchingConfig | None = Field(
        default=None,
        description="Optional custom matching configuration.",
    )


class ReconciliationRunResult(ContractModel):
    """Return the result after running reconciliation matching."""

    close_run_id: UUID = Field(description="Close run UUID.")
    reconciliations: list[ReconciliationSummary] = Field(
        description="Created or updated reconciliation runs.",
    )
    total_items: int = Field(description="Total reconciliation items created.")
    matched_items: int = Field(description="Items matched successfully.")
    exception_items: int = Field(description="Items requiring reviewer disposition.")
    unmatched_items: int = Field(description="Items with no counterpart found.")


__all__ = [
    "ApproveReconciliationRequest",
    "ApproveReconciliationResult",
    "BulkDispositionRequest",
    "CreateReconciliationRequest",
    "DispositionItemRequest",
    "DispositionResult",
    "MatchingConfig",
    "ReconciliationAnomalyListResponse",
    "ReconciliationAnomalySummary",
    "ReconciliationCreationResult",
    "ReconciliationItemListResponse",
    "ReconciliationItemMatch",
    "ReconciliationItemSummary",
    "ReconciliationListResponse",
    "ReconciliationRunResult",
    "ReconciliationSummary",
    "ResolveAnomalyRequest",
    "RunReconciliationRequest",
    "TrialBalanceAccountEntry",
    "TrialBalanceDetailResponse",
    "TrialBalanceSnapshotSummary",
]
