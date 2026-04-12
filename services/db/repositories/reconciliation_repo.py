"""
Purpose: Persist and query reconciliation runs, items, trial balance snapshots,
and anomalies for the reconciliation service layer.
Scope: Reconciliation CRUD, item persistence, bulk disposition, trial balance
snapshot storage, anomaly recording, and reconciliation status queries.
Dependencies: SQLAlchemy ORM sessions, reconciliation and audit models.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from services.common.enums import (
    AnomalyType,
    DispositionAction,
    MatchStatus,
    ReconciliationStatus,
    ReconciliationType,
)
from services.common.types import JsonObject
from services.db.models.reconciliation import (
    Reconciliation,
    ReconciliationAnomaly,
    ReconciliationItem,
    TrialBalanceSnapshot,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class ReconciliationRecord:
    """Describe one reconciliation run as an immutable service-layer record."""

    id: UUID
    close_run_id: UUID
    reconciliation_type: ReconciliationType
    status: ReconciliationStatus
    summary: JsonObject
    blocking_reason: str | None
    approved_by_user_id: UUID | None
    created_by_user_id: UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ReconciliationItemRecord:
    """Describe one reconciliation item as an immutable service-layer record."""

    id: UUID
    reconciliation_id: UUID
    source_type: str
    source_ref: str
    match_status: MatchStatus
    amount: Decimal
    matched_to: list[dict[str, Any]]
    difference_amount: Decimal
    explanation: str | None
    requires_disposition: bool
    disposition: DispositionAction | None
    disposition_reason: str | None
    disposition_by_user_id: UUID | None
    dimensions: JsonObject
    period_date: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TrialBalanceSnapshotRecord:
    """Describe one trial balance snapshot as an immutable service-layer record."""

    id: UUID
    close_run_id: UUID
    snapshot_no: int
    total_debits: Decimal
    total_credits: Decimal
    is_balanced: bool
    account_balances: list[dict[str, Any]]
    generated_by_user_id: UUID | None
    metadata_payload: JsonObject
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReconciliationAnomalyRecord:
    """Describe one reconciliation anomaly as an immutable service-layer record."""

    id: UUID
    close_run_id: UUID
    trial_balance_snapshot_id: UUID | None
    anomaly_type: AnomalyType
    severity: str
    account_code: str | None
    description: str
    details: JsonObject
    resolved: bool
    resolved_by_user_id: UUID | None
    resolution_note: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReconciliationSummaryStats:
    """Aggregate statistics for a reconciliation run."""

    total_items: int
    matched_count: int
    partially_matched_count: int
    exception_count: int
    unmatched_count: int
    pending_disposition_count: int


class ReconciliationRepository:
    """Persist and query reconciliation records for the reconciliation service.

    This repository provides the canonical data access layer for reconciliation
    runs, items, trial balance snapshots, and anomalies. All mutations go through
    here so the service layer can remain focused on business logic.
    """

    def __init__(self, session: Session) -> None:
        """Initialize the repository with an active database session.

        Args:
            session: SQLAlchemy session for database operations.
        """
        self._session = session

    # ------------------------------------------------------------------
    # Reconciliation CRUD
    # ------------------------------------------------------------------

    def create_reconciliation(
        self,
        close_run_id: UUID,
        reconciliation_type: ReconciliationType,
        created_by_user_id: UUID | None = None,
    ) -> ReconciliationRecord:
        """Create a new reconciliation run.

        Args:
            close_run_id: The close run this reconciliation belongs to.
            reconciliation_type: The reconciliation category.
            created_by_user_id: Optional user who initiated the run.

        Returns:
            The created ReconciliationRecord.
        """
        rec = Reconciliation(
            close_run_id=close_run_id,
            reconciliation_type=reconciliation_type.value,
            status=ReconciliationStatus.DRAFT.value,
            summary={},
            created_by_user_id=created_by_user_id,
        )
        self._session.add(rec)
        self._session.flush()
        return self._to_reconciliation_record(rec)

    def get_reconciliation(self, reconciliation_id: UUID) -> ReconciliationRecord | None:
        """Fetch one reconciliation run by ID.

        Args:
            reconciliation_id: The reconciliation UUID.

        Returns:
            ReconciliationRecord or None if not found.
        """
        rec = self._session.get(Reconciliation, reconciliation_id)
        if rec is None:
            return None
        return self._to_reconciliation_record(rec)

    def get_reconciliation_for_close_run(
        self,
        reconciliation_id: UUID,
        close_run_id: UUID,
    ) -> ReconciliationRecord | None:
        """Fetch one reconciliation run scoped to a specific close run.

        This prevents cross-close-run resource access when a caller supplies
        a reconciliation_id from a different close run.

        Args:
            reconciliation_id: The reconciliation UUID.
            close_run_id: The owning close run UUID that must match.

        Returns:
            ReconciliationRecord or None if not found or close_run mismatch.
        """
        stmt = select(Reconciliation).where(
            Reconciliation.id == reconciliation_id,
            Reconciliation.close_run_id == close_run_id,
        )
        rec = self._session.scalar(stmt)
        if rec is None:
            return None
        return self._to_reconciliation_record(rec)

    def list_reconciliations(
        self,
        close_run_id: UUID,
        reconciliation_type: ReconciliationType | None = None,
    ) -> list[ReconciliationRecord]:
        """List reconciliation runs for a close run.

        Args:
            close_run_id: The close run UUID.
            reconciliation_type: Optional filter by reconciliation type.

        Returns:
            List of ReconciliationRecord in creation order.
        """
        stmt = select(Reconciliation).where(Reconciliation.close_run_id == close_run_id)
        if reconciliation_type is not None:
            stmt = stmt.where(Reconciliation.reconciliation_type == reconciliation_type.value)
        stmt = stmt.order_by(Reconciliation.created_at)
        rows = self._session.scalars(stmt).all()
        return [self._to_reconciliation_record(r) for r in rows]

    def update_reconciliation_status(
        self,
        reconciliation_id: UUID,
        status: ReconciliationStatus,
        blocking_reason: str | None = None,
        approved_by_user_id: UUID | None = None,
    ) -> ReconciliationRecord | None:
        """Update the status of a reconciliation run.

        Args:
            reconciliation_id: The reconciliation UUID.
            status: New reconciliation status.
            blocking_reason: Required when status is 'blocked'.
            approved_by_user_id: User approving the reconciliation.

        Returns:
            Updated ReconciliationRecord or None if not found.
        """
        rec = self._session.get(Reconciliation, reconciliation_id)
        if rec is None:
            return None
        rec.status = status.value
        rec.blocking_reason = blocking_reason
        if approved_by_user_id is not None:
            rec.approved_by_user_id = approved_by_user_id
        self._session.flush()
        return self._to_reconciliation_record(rec)

    def update_reconciliation_summary(
        self,
        reconciliation_id: UUID,
        summary: JsonObject,
    ) -> ReconciliationRecord | None:
        """Update the aggregated summary of a reconciliation run.

        Args:
            reconciliation_id: The reconciliation UUID.
            summary: New summary payload.

        Returns:
            Updated ReconciliationRecord or None if not found.
        """
        rec = self._session.get(Reconciliation, reconciliation_id)
        if rec is None:
            return None
        rec.summary = summary
        self._session.flush()
        return self._to_reconciliation_record(rec)

    # ------------------------------------------------------------------
    # Reconciliation items
    # ------------------------------------------------------------------

    def bulk_create_items(
        self,
        reconciliation_id: UUID,
        items: list[dict[str, Any]],
    ) -> list[ReconciliationItemRecord]:
        """Bulk create reconciliation items from match results.

        Args:
            reconciliation_id: Parent reconciliation UUID.
            items: List of item dicts from the matching engine.

        Returns:
            List of created ReconciliationItemRecord.
        """
        records: list[ReconciliationItemRecord] = []
        for item_data in items:
            item = ReconciliationItem(
                reconciliation_id=reconciliation_id,
                source_type=item_data["source_type"],
                source_ref=item_data["source_ref"],
                match_status=item_data["match_status"],
                amount=item_data["source_amount"],
                matched_to=item_data.get("matched_to", []),
                difference_amount=item_data.get("difference_amount", Decimal("0.00")),
                explanation=item_data.get("explanation"),
                requires_disposition=item_data.get("requires_disposition", False),
                dimensions=item_data.get("dimensions", {}),
                period_date=item_data.get("period_date"),
            )
            self._session.add(item)
            self._session.flush()
            records.append(self._to_item_record(item))
        return records

    def list_items(
        self,
        reconciliation_id: UUID,
        match_status: MatchStatus | None = None,
        requires_disposition: bool | None = None,
    ) -> list[ReconciliationItemRecord]:
        """List reconciliation items with optional filters.

        Args:
            reconciliation_id: Parent reconciliation UUID.
            match_status: Optional filter by match status.
            requires_disposition: Optional filter by disposition requirement.

        Returns:
            List of ReconciliationItemRecord.
        """
        stmt = select(ReconciliationItem).where(
            ReconciliationItem.reconciliation_id == reconciliation_id
        )
        if match_status is not None:
            stmt = stmt.where(ReconciliationItem.match_status == match_status.value)
        if requires_disposition is not None:
            stmt = stmt.where(ReconciliationItem.requires_disposition == requires_disposition)
        stmt = stmt.order_by(ReconciliationItem.created_at)
        rows = self._session.scalars(stmt).all()
        return [self._to_item_record(r) for r in rows]

    def disposition_item(
        self,
        item_id: UUID,
        disposition: DispositionAction,
        reason: str,
        user_id: UUID,
    ) -> ReconciliationItemRecord | None:
        """Record a reviewer disposition for a reconciliation item.

        Args:
            item_id: The item UUID.
            disposition: The reviewer's disposition choice.
            reason: Reviewer reasoning.
            user_id: User recording the disposition.

        Returns:
            Updated ReconciliationItemRecord or None if not found.
        """
        item = self._session.get(ReconciliationItem, item_id)
        if item is None:
            return None
        item.disposition = disposition.value
        item.disposition_reason = reason
        item.disposition_by_user_id = user_id
        self._session.flush()
        return self._to_item_record(item)

    def get_item_for_close_run(
        self,
        item_id: UUID,
        close_run_id: UUID,
    ) -> ReconciliationItemRecord | None:
        """Fetch one reconciliation item scoped to a specific close run.

        This prevents cross-close-run resource access when a caller supplies
        an item_id from a reconciliation belonging to a different close run.

        Args:
            item_id: The item UUID.
            close_run_id: The owning close run UUID that must match.

        Returns:
            ReconciliationItemRecord or None if not found or close_run mismatch.
        """
        stmt = (
            select(ReconciliationItem)
            .join(Reconciliation, ReconciliationItem.reconciliation_id == Reconciliation.id)
            .where(
                ReconciliationItem.id == item_id,
                Reconciliation.close_run_id == close_run_id,
            )
        )
        item = self._session.scalar(stmt)
        if item is None:
            return None
        return self._to_item_record(item)

    def compute_summary_stats(
        self,
        reconciliation_id: UUID,
    ) -> ReconciliationSummaryStats:
        """Compute aggregate statistics for a reconciliation run.

        Args:
            reconciliation_id: Parent reconciliation UUID.

        Returns:
            ReconciliationSummaryStats with item counts by status.
        """
        stmt = select(ReconciliationItem).where(
            ReconciliationItem.reconciliation_id == reconciliation_id
        )
        items = self._session.scalars(stmt).all()

        total = len(items)
        matched = sum(1 for i in items if i.match_status == MatchStatus.MATCHED.value)
        partial = sum(1 for i in items if i.match_status == MatchStatus.PARTIALLY_MATCHED.value)
        exceptions = sum(1 for i in items if i.match_status == MatchStatus.EXCEPTION.value)
        unmatched = sum(1 for i in items if i.match_status == MatchStatus.UNMATCHED.value)
        pending = sum(
            1
            for i in items
            if i.requires_disposition and i.disposition is None
        )

        return ReconciliationSummaryStats(
            total_items=total,
            matched_count=matched,
            partially_matched_count=partial,
            exception_count=exceptions,
            unmatched_count=unmatched,
            pending_disposition_count=pending,
        )

    # ------------------------------------------------------------------
    # Trial balance snapshots
    # ------------------------------------------------------------------

    def create_trial_balance_snapshot(
        self,
        close_run_id: UUID,
        total_debits: Decimal,
        total_credits: Decimal,
        is_balanced: bool,
        account_balances: list[dict[str, Any]],
        generated_by_user_id: UUID | None = None,
        metadata_payload: JsonObject | None = None,
    ) -> TrialBalanceSnapshotRecord:
        """Create a new trial balance snapshot.

        Args:
            close_run_id: The close run UUID.
            total_debits: Sum of all debit balances.
            total_credits: Sum of all credit balances.
            is_balanced: Whether debits equal credits.
            account_balances: Per-account balance records.
            generated_by_user_id: User who triggered computation.
            metadata_payload: Additional context.

        Returns:
            The created TrialBalanceSnapshotRecord.
        """
        # Compute next snapshot number
        stmt = (
            select(func.coalesce(func.max(TrialBalanceSnapshot.snapshot_no), 0))
            .where(TrialBalanceSnapshot.close_run_id == close_run_id)
        )
        max_no = self._session.scalar(stmt) or 0
        next_no = max_no + 1

        snapshot = TrialBalanceSnapshot(
            close_run_id=close_run_id,
            snapshot_no=next_no,
            total_debits=total_debits,
            total_credits=total_credits,
            is_balanced=is_balanced,
            account_balances=account_balances,
            generated_by_user_id=generated_by_user_id,
            metadata_payload=metadata_payload or {},
        )
        self._session.add(snapshot)
        self._session.flush()
        return self._to_snapshot_record(snapshot)

    def get_latest_trial_balance(
        self,
        close_run_id: UUID,
    ) -> TrialBalanceSnapshotRecord | None:
        """Fetch the most recent trial balance snapshot for a close run.

        Args:
            close_run_id: The close run UUID.

        Returns:
            Latest TrialBalanceSnapshotRecord or None if none exist.
        """
        stmt = (
            select(TrialBalanceSnapshot)
            .where(TrialBalanceSnapshot.close_run_id == close_run_id)
            .order_by(TrialBalanceSnapshot.snapshot_no.desc())
            .limit(1)
        )
        row = self._session.scalar(stmt)
        if row is None:
            return None
        return self._to_snapshot_record(row)

    # ------------------------------------------------------------------
    # Anomalies
    # ------------------------------------------------------------------

    def create_anomaly(
        self,
        close_run_id: UUID,
        anomaly_type: AnomalyType,
        severity: str,
        description: str,
        details: JsonObject,
        account_code: str | None = None,
        trial_balance_snapshot_id: UUID | None = None,
    ) -> ReconciliationAnomalyRecord:
        """Create a reconciliation anomaly record.

        Args:
            close_run_id: The close run UUID.
            anomaly_type: Category of the anomaly.
            severity: Severity level.
            description: Human-readable description.
            details: Structured anomaly details.
            account_code: Associated GL account code.
            trial_balance_snapshot_id: Related trial balance snapshot.

        Returns:
            The created ReconciliationAnomalyRecord.
        """
        anomaly = ReconciliationAnomaly(
            close_run_id=close_run_id,
            anomaly_type=anomaly_type.value,
            severity=severity,
            account_code=account_code,
            description=description,
            details=details,
            trial_balance_snapshot_id=trial_balance_snapshot_id,
        )
        self._session.add(anomaly)
        self._session.flush()
        return self._to_anomaly_record(anomaly)

    def list_anomalies(
        self,
        close_run_id: UUID,
        severity: str | None = None,
        resolved: bool | None = None,
    ) -> list[ReconciliationAnomalyRecord]:
        """List anomalies for a close run.

        Args:
            close_run_id: The close run UUID.
            severity: Optional filter by severity.
            resolved: Optional filter by resolution status.

        Returns:
            List of ReconciliationAnomalyRecord.
        """
        stmt = select(ReconciliationAnomaly).where(
            ReconciliationAnomaly.close_run_id == close_run_id
        )
        if severity is not None:
            stmt = stmt.where(ReconciliationAnomaly.severity == severity)
        if resolved is not None:
            stmt = stmt.where(ReconciliationAnomaly.resolved == resolved)
        stmt = stmt.order_by(
            ReconciliationAnomaly.severity,
            ReconciliationAnomaly.created_at,
        )
        rows = self._session.scalars(stmt).all()
        return [self._to_anomaly_record(r) for r in rows]

    def resolve_anomaly(
        self,
        anomaly_id: UUID,
        resolution_note: str,
        user_id: UUID,
    ) -> ReconciliationAnomalyRecord | None:
        """Mark an anomaly as resolved with reviewer reasoning.

        Args:
            anomaly_id: The anomaly UUID.
            resolution_note: Reviewer reasoning.
            user_id: User resolving the anomaly.

        Returns:
            Updated ReconciliationAnomalyRecord or None if not found.
        """
        anomaly = self._session.get(ReconciliationAnomaly, anomaly_id)
        if anomaly is None:
            return None
        anomaly.resolved = True
        anomaly.resolution_note = resolution_note
        anomaly.resolved_by_user_id = user_id
        self._session.flush()
        return self._to_anomaly_record(anomaly)

    def get_anomaly_for_close_run(
        self,
        anomaly_id: UUID,
        close_run_id: UUID,
    ) -> ReconciliationAnomalyRecord | None:
        """Fetch one reconciliation anomaly scoped to a specific close run.

        This prevents cross-close-run resource access when a caller supplies
        an anomaly_id from a different close run.

        Args:
            anomaly_id: The anomaly UUID.
            close_run_id: The owning close run UUID that must match.

        Returns:
            ReconciliationAnomalyRecord or None if not found or close_run mismatch.
        """
        stmt = select(ReconciliationAnomaly).where(
            ReconciliationAnomaly.id == anomaly_id,
            ReconciliationAnomaly.close_run_id == close_run_id,
        )
        anomaly = self._session.scalar(stmt)
        if anomaly is None:
            return None
        return self._to_anomaly_record(anomaly)

    # ------------------------------------------------------------------
    # Mapping helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_reconciliation_record(row: Reconciliation) -> ReconciliationRecord:
        """Map an ORM row to a service-layer record.

        Args:
            row: SQLAlchemy Reconciliation ORM instance.

        Returns:
            Immutable ReconciliationRecord.
        """
        return ReconciliationRecord(
            id=row.id,
            close_run_id=row.close_run_id,
            reconciliation_type=ReconciliationType(row.reconciliation_type),
            status=ReconciliationStatus(row.status),
            summary=row.summary,
            blocking_reason=row.blocking_reason,
            approved_by_user_id=row.approved_by_user_id,
            created_by_user_id=row.created_by_user_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _to_item_record(row: ReconciliationItem) -> ReconciliationItemRecord:
        """Map an ORM row to a service-layer item record.

        Args:
            row: SQLAlchemy ReconciliationItem ORM instance.

        Returns:
            Immutable ReconciliationItemRecord.
        """
        return ReconciliationItemRecord(
            id=row.id,
            reconciliation_id=row.reconciliation_id,
            source_type=row.source_type,
            source_ref=row.source_ref,
            match_status=MatchStatus(row.match_status),
            amount=row.amount,
            matched_to=row.matched_to,
            difference_amount=row.difference_amount,
            explanation=row.explanation,
            requires_disposition=row.requires_disposition,
            disposition=DispositionAction(row.disposition) if row.disposition else None,
            disposition_reason=row.disposition_reason,
            disposition_by_user_id=row.disposition_by_user_id,
            dimensions=row.dimensions,
            period_date=row.period_date,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _to_snapshot_record(row: TrialBalanceSnapshot) -> TrialBalanceSnapshotRecord:
        """Map an ORM row to a service-layer snapshot record.

        Args:
            row: SQLAlchemy TrialBalanceSnapshot ORM instance.

        Returns:
            Immutable TrialBalanceSnapshotRecord.
        """
        return TrialBalanceSnapshotRecord(
            id=row.id,
            close_run_id=row.close_run_id,
            snapshot_no=row.snapshot_no,
            total_debits=row.total_debits,
            total_credits=row.total_credits,
            is_balanced=row.is_balanced,
            account_balances=row.account_balances,
            generated_by_user_id=row.generated_by_user_id,
            metadata_payload=row.metadata_payload,
            created_at=row.created_at,
        )

    @staticmethod
    def _to_anomaly_record(row: ReconciliationAnomaly) -> ReconciliationAnomalyRecord:
        """Map an ORM row to a service-layer anomaly record.

        Args:
            row: SQLAlchemy ReconciliationAnomaly ORM instance.

        Returns:
            Immutable ReconciliationAnomalyRecord.
        """
        return ReconciliationAnomalyRecord(
            id=row.id,
            close_run_id=row.close_run_id,
            trial_balance_snapshot_id=row.trial_balance_snapshot_id,
            anomaly_type=AnomalyType(row.anomaly_type),
            severity=row.severity,
            account_code=row.account_code,
            description=row.description,
            details=row.details,
            resolved=row.resolved,
            resolved_by_user_id=row.resolved_by_user_id,
            resolution_note=row.resolution_note,
            created_at=row.created_at,
        )


__all__ = [
    "ReconciliationAnomalyRecord",
    "ReconciliationItemRecord",
    "ReconciliationRecord",
    "ReconciliationRepository",
    "ReconciliationSummaryStats",
    "TrialBalanceSnapshotRecord",
]
