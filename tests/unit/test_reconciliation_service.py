"""
Purpose: Verify canonical reconciliation service lifecycle behavior.
Scope: Focused coverage for trial-balance run materialization and approval rules.
Dependencies: ReconciliationService only with lightweight fake persistence.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from services.common.enums import (
    ReconciliationStatus,
    ReconciliationType,
)
from services.db.repositories.reconciliation_repo import (
    ReconciliationAnomalyRecord,
    ReconciliationRecord,
    ReconciliationSummaryStats,
    TrialBalanceSnapshotRecord,
)
from services.reconciliation.service import ReconciliationService


class _FakeReconciliationRepository:
    def __init__(self) -> None:
        self.close_run_id = uuid4()
        self.snapshot = TrialBalanceSnapshotRecord(
            id=uuid4(),
            close_run_id=self.close_run_id,
            snapshot_no=1,
            total_debits=Decimal("100.00"),
            total_credits=Decimal("100.00"),
            is_balanced=True,
            account_balances=[],
            generated_by_user_id=None,
            metadata_payload={},
            created_at=datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
        )
        self.created_reconciliations: list[ReconciliationRecord] = []
        self.updated_statuses: list[tuple[UUID, ReconciliationStatus, str | None]] = []
        self.updated_summaries: list[tuple[UUID, dict[str, object]]] = []
        self.pending_dispositions = 0
        self.unresolved_anomaly_count = 0
        self._reconciliations_by_id: dict[UUID, ReconciliationRecord] = {}

    def create_trial_balance_snapshot(self, **kwargs) -> TrialBalanceSnapshotRecord:
        self.snapshot = TrialBalanceSnapshotRecord(
            id=self.snapshot.id,
            close_run_id=kwargs["close_run_id"],
            snapshot_no=self.snapshot.snapshot_no,
            total_debits=kwargs["total_debits"],
            total_credits=kwargs["total_credits"],
            is_balanced=kwargs["is_balanced"],
            account_balances=kwargs["account_balances"],
            generated_by_user_id=kwargs.get("generated_by_user_id"),
            metadata_payload=kwargs.get("metadata_payload", {}),
            created_at=self.snapshot.created_at,
        )
        return self.snapshot

    def create_anomaly(self, **kwargs) -> ReconciliationAnomalyRecord:
        return ReconciliationAnomalyRecord(
            id=uuid4(),
            close_run_id=kwargs["close_run_id"],
            trial_balance_snapshot_id=kwargs.get("trial_balance_snapshot_id"),
            anomaly_type=kwargs["anomaly_type"],
            severity=kwargs["severity"],
            account_code=kwargs.get("account_code"),
            description=kwargs["description"],
            details=kwargs["details"],
            resolved=False,
            resolved_by_user_id=None,
            resolution_note=None,
            created_at=datetime(2026, 4, 20, 12, 5, tzinfo=UTC),
        )

    def create_reconciliation(
        self,
        *,
        close_run_id: UUID,
        reconciliation_type: ReconciliationType,
        created_by_user_id: UUID | None = None,
    ) -> ReconciliationRecord:
        record = ReconciliationRecord(
            id=uuid4(),
            close_run_id=close_run_id,
            reconciliation_type=reconciliation_type,
            status=ReconciliationStatus.DRAFT,
            summary={},
            blocking_reason=None,
            approved_by_user_id=None,
            created_by_user_id=created_by_user_id,
            created_at=datetime(2026, 4, 20, 12, 10, tzinfo=UTC),
            updated_at=datetime(2026, 4, 20, 12, 10, tzinfo=UTC),
        )
        self.created_reconciliations.append(record)
        self._reconciliations_by_id[record.id] = record
        return record

    def update_reconciliation_summary(
        self,
        reconciliation_id: UUID,
        summary: dict[str, object],
    ) -> ReconciliationRecord:
        record = self._reconciliations_by_id[reconciliation_id]
        updated = replace(record, summary=summary)
        self._reconciliations_by_id[reconciliation_id] = updated
        self.updated_summaries.append((reconciliation_id, summary))
        return updated

    def update_reconciliation_status(
        self,
        reconciliation_id: UUID,
        status: ReconciliationStatus,
        blocking_reason: str | None = None,
        approved_by_user_id: UUID | None = None,
    ) -> ReconciliationRecord:
        record = self._reconciliations_by_id[reconciliation_id]
        updated = replace(
            record,
            status=status,
            blocking_reason=blocking_reason,
            approved_by_user_id=approved_by_user_id or record.approved_by_user_id,
        )
        self._reconciliations_by_id[reconciliation_id] = updated
        self.updated_statuses.append((reconciliation_id, status, blocking_reason))
        return updated

    def get_reconciliation_for_close_run(
        self,
        *,
        reconciliation_id: UUID,
        close_run_id: UUID,
    ) -> ReconciliationRecord | None:
        record = self._reconciliations_by_id.get(reconciliation_id)
        if record is None or record.close_run_id != close_run_id:
            return None
        return record

    def compute_summary_stats(self, reconciliation_id: UUID) -> ReconciliationSummaryStats:
        del reconciliation_id
        return ReconciliationSummaryStats(
            total_items=0,
            matched_count=0,
            partially_matched_count=0,
            exception_count=0,
            unmatched_count=0,
            pending_disposition_count=self.pending_dispositions,
        )

    def count_unresolved_anomalies(
        self,
        *,
        close_run_id: UUID,
        trial_balance_snapshot_id: UUID | None = None,
    ) -> int:
        assert close_run_id == self.close_run_id
        assert trial_balance_snapshot_id == self.snapshot.id
        return self.unresolved_anomaly_count


def test_compute_trial_balance_creates_visible_trial_balance_reconciliation_run() -> None:
    """Trial-balance computation should materialize a reconciliation run for review."""

    repository = _FakeReconciliationRepository()
    service = ReconciliationService(repository=repository)

    snapshot = service.compute_trial_balance(
        close_run_id=repository.close_run_id,
        account_balances=[
            {
                "account_code": "1000",
                "account_name": "Cash",
                "account_type": "asset",
                "debit_balance": Decimal("100.00"),
                "credit_balance": Decimal("0.00"),
                "is_active": True,
            },
            {
                "account_code": "4000",
                "account_name": "Revenue",
                "account_type": "revenue",
                "debit_balance": Decimal("0.00"),
                "credit_balance": Decimal("100.00"),
                "is_active": True,
            },
        ],
        expected_account_codes={"1000", "4000"},
    )

    assert snapshot.id == repository.snapshot.id
    assert len(repository.created_reconciliations) == 1
    created_run = repository.created_reconciliations[0]
    assert created_run.reconciliation_type is ReconciliationType.TRIAL_BALANCE
    assert repository.updated_summaries[0][1]["trial_balance_snapshot_id"] == str(snapshot.id)
    assert repository.updated_statuses[0][1] is ReconciliationStatus.IN_REVIEW


def test_approve_trial_balance_blocks_when_anomalies_are_unresolved() -> None:
    """Trial-balance approval should fail fast when anomalies still need review."""

    repository = _FakeReconciliationRepository()
    service = ReconciliationService(repository=repository)
    created_run = repository.create_reconciliation(
        close_run_id=repository.close_run_id,
        reconciliation_type=ReconciliationType.TRIAL_BALANCE,
    )
    repository.update_reconciliation_summary(
        created_run.id,
        {"trial_balance_snapshot_id": str(repository.snapshot.id)},
    )
    repository.unresolved_anomaly_count = 2

    result = service.approve_reconciliation(
        reconciliation_id=created_run.id,
        close_run_id=repository.close_run_id,
        reason="Reviewed",
        user_id=uuid4(),
    )

    assert result is not None
    assert result.status is ReconciliationStatus.BLOCKED
    assert "2 trial-balance anomaly item(s)" in (result.blocking_reason or "")
