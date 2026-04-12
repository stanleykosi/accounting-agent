"""
Purpose: Define reconciliation persistence models for the accounting close run workflow.
Scope: Reconciliation headers, line items, trial balance snapshots, and anomaly records
       for all reconciliation types: bank, AR/AP ageing, intercompany, payroll control,
       fixed assets, loan amortisation, accrual tracker, budget vs actual, and trial balance.
Dependencies: SQLAlchemy ORM, canonical enums, database base helpers.

Design notes:
- Reconciliation represents one reconciliation run within a close run (e.g., one bank rec).
- ReconciliationItem represents one line or balance being matched or investigated.
- TrialBalanceSnapshot captures the computed trial balance at a point in time for audit lineage.
- ReconciliationAnomaly records anomalies detected during trial balance or reconciliation checks.
- All models attach to a close run for full audit traceability.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from services.common.enums import (
    AnomalyType,
    MatchStatus,
    ReconciliationStatus,
    ReconciliationType,
)
from services.common.types import JsonObject
from services.db.base import (
    Base,
    TimestampedModel,
    UUIDPrimaryKeyMixin,
    build_text_choice_check,
)
from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column


class Reconciliation(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one reconciliation run within a close run.

    Each reconciliation targets a specific type (bank rec, AR ageing, etc.) and
    carries a collection of ReconciliationItems representing individual lines or
    balances being matched. The reconciliation transitions through lifecycle states
    (draft -> in_review -> approved/blocked) as matching and reviewer dispositions
    are completed.
    """

    __tablename__ = "reconciliations"
    __table_args__ = (
        build_text_choice_check(
            column_name="reconciliation_type",
            values=ReconciliationType.values(),
            constraint_name="reconciliation_type_valid",
        ),
        build_text_choice_check(
            column_name="status",
            values=ReconciliationStatus.values(),
            constraint_name="reconciliation_status_valid",
        ),
        Index("ix_reconciliations_close_run_type", "close_run_id", "reconciliation_type"),
        Index("ix_reconciliations_close_run_status", "close_run_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    close_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("close_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reconciliation_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        comment="The reconciliation category (bank_reconciliation, ar_ageing, etc.).",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ReconciliationStatus.DRAFT.value,
        comment="Lifecycle state of the reconciliation run.",
    )
    summary: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Aggregated reconciliation summary (matched count, exceptions, totals).",
    )
    blocking_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Reason the reconciliation is blocked, required when status is 'blocked'.",
    )
    approved_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
        comment="User who approved this reconciliation run.",
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
        comment="User who initiated this reconciliation run.",
    )


class ReconciliationItem(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one line or balance within a reconciliation run.

    Each item represents one thing being matched (a bank statement line, a ledger
    balance, an intercompany account, etc.) with its match outcome, counterpart
    references, and reviewer disposition. Items with match_status 'unmatched' or
    'exception' and requires_disposition=True must receive a reviewer disposition
    before the close run can be signed off.
    """

    __tablename__ = "reconciliation_items"
    __table_args__ = (
        build_text_choice_check(
            column_name="match_status",
            values=MatchStatus.values(),
            constraint_name="reconciliation_item_match_status_valid",
        ),
        Index("ix_reconciliation_items_reconciliation", "reconciliation_id"),
        Index(
            "ix_reconciliation_items_match_status",
            "reconciliation_id",
            "match_status",
        ),
        Index("ix_reconciliation_items_source", "source_type", "source_ref"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    reconciliation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("reconciliations.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        comment="What kind of source produced this item (bank_statement_line, ledger_transaction, etc.).",
    )
    source_ref: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="Reference to the originating record (statement line ID, journal number, etc.).",
    )
    match_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=MatchStatus.UNMATCHED.value,
        comment="Outcome of the matching process for this item.",
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(20, 2),
        nullable=False,
        comment="Monetary amount of this reconciliation item.",
    )
    matched_to: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="List of counterpart references this item was matched to.",
    )
    difference_amount: Mapped[Decimal] = mapped_column(
        Numeric(20, 2),
        nullable=False,
        default=Decimal("0.00"),
        comment="Difference between this item and its matched counterpart(s).",
    )
    explanation: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="System-generated or reviewer-provided explanation of the match outcome.",
    )
    requires_disposition: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        comment="Whether a reviewer must disposition this item before sign-off.",
    )
    disposition: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="Reviewer disposition choice when the item was resolved.",
    )
    disposition_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Reviewer-provided reasoning for the disposition.",
    )
    disposition_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
        comment="User who recorded the disposition for this item.",
    )
    dimensions: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Accounting dimensions (cost_centre, department, project) if applicable.",
    )
    period_date: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="Accounting period date associated with this item (YYYY-MM-DD).",
    )


class TrialBalanceSnapshot(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one computed trial balance snapshot for a close run.

    Trial balance snapshots capture the debit/credit totals per account at a
    specific point in time, enabling variance analysis, anomaly detection, and
    audit lineage. Multiple snapshots may exist for one close run if the trial
    balance is recomputed after adjustments.
    """

    __tablename__ = "trial_balance_snapshots"
    __table_args__ = (
        CheckConstraint(
            "snapshot_no >= 1",
            name="trial_balance_snapshot_no_positive",
        ),
        Index("ix_trial_balance_snapshots_close_run", "close_run_id"),
        Index(
            "ix_trial_balance_snapshots_close_run_no",
            "close_run_id",
            "snapshot_no",
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    close_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("close_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_no: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Sequential snapshot number within the close run.",
    )
    total_debits: Mapped[Decimal] = mapped_column(
        Numeric(20, 2),
        nullable=False,
        comment="Sum of all debit balances in this snapshot.",
    )
    total_credits: Mapped[Decimal] = mapped_column(
        Numeric(20, 2),
        nullable=False,
        comment="Sum of all credit balances in this snapshot.",
    )
    is_balanced: Mapped[bool] = mapped_column(
        nullable=False,
        comment="Whether total debits equal total credits within tolerance.",
    )
    account_balances: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="List of per-account balance records (code, name, debit, credit, net).",
    )
    generated_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
        comment="User who triggered this trial balance computation.",
    )
    metadata_payload: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Additional context (rule version, coa set version, generation timestamp).",
    )


class ReconciliationAnomaly(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one anomaly detected during trial balance or reconciliation checks.

    Anomalies flag unusual balances, imbalances, unexplained variances, missing
    accounts, or rounding differences. They carry severity indicators and require
    reviewer investigation before close run sign-off.
    """

    __tablename__ = "reconciliation_anomalies"
    __table_args__ = (
        build_text_choice_check(
            column_name="anomaly_type",
            values=AnomalyType.values(),
            constraint_name="reconciliation_anomaly_type_valid",
        ),
        CheckConstraint(
            "severity IN ('info', 'warning', 'blocking')",
            name="reconciliation_anomaly_severity_valid",
        ),
        Index("ix_reconciliation_anomalies_close_run", "close_run_id"),
        Index(
            "ix_reconciliation_anomalies_close_run_severity",
            "close_run_id",
            "severity",
        ),
        Index("ix_reconciliation_anomalies_type", "anomaly_type"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    close_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("close_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trial_balance_snapshot_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("trial_balance_snapshots.id", ondelete="SET NULL"),
        nullable=True,
        comment="The trial balance snapshot this anomaly was detected against, if applicable.",
    )
    anomaly_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        comment="Category of the anomaly (imbalance, unusual balance, variance, etc.).",
    )
    severity: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        comment="Severity level: info, warning, or blocking.",
    )
    account_code: Mapped[str | None] = mapped_column(
        String(60),
        nullable=True,
        comment="GL account code associated with the anomaly, if applicable.",
    )
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Human-readable description of the anomaly for reviewer investigation.",
    )
    details: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Structured details (expected value, actual value, variance, threshold).",
    )
    resolved: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        comment="Whether a reviewer has investigated and resolved this anomaly.",
    )
    resolved_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
        comment="User who resolved this anomaly.",
    )
    resolution_note: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Reviewer-provided reasoning for resolving the anomaly.",
    )


__all__ = [
    "Reconciliation",
    "ReconciliationAnomaly",
    "ReconciliationItem",
    "TrialBalanceSnapshot",
]
