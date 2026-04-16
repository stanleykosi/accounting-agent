"""
Purpose: Orchestrate reconciliation workflows across all reconciliation types.
Scope: Bank reconciliation, AR/AP ageing, intercompany, payroll control, fixed assets,
       loan amortisation, accrual tracker, budget vs actual, and trial balance. The
       service dispatches to the appropriate matcher, persists results, runs trial
       balance checks, and manages reconciliation lifecycle state transitions.
Dependencies: Matching helpers, reconciliation repository, audit service, COA repository,
       journal and recommendation repositories, and canonical enums.

Design notes:
- The service is the single entry point for all reconciliation operations.
- Matching is delegated to type-specific matchers from the matchers module.
- All state changes go through the repository layer.
- Trial balance checks run after reconciliation matching to validate overall integrity.
- Anomalies detected during checks are persisted for reviewer investigation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID

from services.common.enums import (
    DispositionAction,
    MatchStatus,
    ReconciliationStatus,
    ReconciliationType,
)
from services.common.types import JsonObject
from services.db.repositories.reconciliation_repo import (
    ReconciliationAnomalyRecord,
    ReconciliationItemRecord,
    ReconciliationRecord,
    ReconciliationRepository,
    ReconciliationSummaryStats,
    TrialBalanceSnapshotRecord,
)
from services.reconciliation.matchers import (
    DEFAULT_MATCHING_CONFIG,
    MATCHER_REGISTRY,
    MatchingConfig,
    MatchResult,
    TrialBalanceChecker,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReconciliationRunOutput:
    """Capture the full output of a reconciliation run execution.

    Attributes:
        reconciliations: Created or updated reconciliation records.
        all_items: All reconciliation item records across all runs.
        trial_balance: Trial balance snapshot record, if computed.
        anomalies: Detected anomaly records.
        total_items: Total number of items created.
        matched_items: Number of items matched successfully.
        exception_items: Number of items requiring disposition.
        unmatched_items: Number of items with no counterpart.
    """

    reconciliations: list[ReconciliationRecord]
    all_items: list[ReconciliationItemRecord]
    trial_balance: TrialBalanceSnapshotRecord | None
    anomalies: list[ReconciliationAnomalyRecord]
    total_items: int
    matched_items: int
    exception_items: int
    unmatched_items: int


@dataclass(frozen=True, slots=True)
class ReconciliationDispositionOutput:
    """Capture the output of a bulk item disposition operation.

    Attributes:
        disposed_items: Item records that were dispositioned.
        failed_item_ids: Item IDs that could not be dispositioned.
    """

    disposed_items: list[ReconciliationItemRecord]
    failed_item_ids: list[UUID]


class ReconciliationService:
    """Orchestrate reconciliation matching, trial balance checks, and lifecycle management.

    This service provides the canonical interface for:
    - Running reconciliation matching across one or more reconciliation types.
    - Recording reviewer dispositions for unmatched/exception items.
    - Approving reconciliation runs after all dispositions are recorded.
    - Computing and validating trial balance snapshots.
    - Recording and resolving reconciliation anomalies.
    """

    def __init__(
        self,
        repository: ReconciliationRepository,
        matching_config: MatchingConfig | None = None,
    ) -> None:
        """Initialize the reconciliation service.

        Args:
            repository: Data access layer for reconciliation records.
            matching_config: Optional matching configuration overrides.
        """
        self._repo = repository
        self._config = matching_config or DEFAULT_MATCHING_CONFIG
        self._tb_checker = TrialBalanceChecker()

    # ------------------------------------------------------------------
    # Reconciliation run execution
    # ------------------------------------------------------------------

    def run_reconciliation(
        self,
        close_run_id: UUID,
        reconciliation_types: list[ReconciliationType],
        source_data: dict[ReconciliationType, dict[str, list[dict[str, Any]]]],
        created_by_user_id: UUID | None = None,
        matching_config: MatchingConfig | None = None,
        progress_guard: Callable[[], None] | None = None,
    ) -> ReconciliationRunOutput:
        """Run reconciliation matching for one or more reconciliation types.

        This is the primary entry point for reconciliation execution. It:
        1. Creates or retrieves reconciliation runs for each type.
        2. Dispatches to the appropriate matcher for each type.
        3. Persists match results as reconciliation items.
        4. Updates reconciliation summary statistics.

        Args:
            close_run_id: The close run to run reconciliation for.
            reconciliation_types: Which reconciliation types to execute.
            source_data: Per-type source data with 'source_items' and 'counterparts' keys.
            created_by_user_id: Optional user initiating the run.
            matching_config: Optional per-run matching configuration.

        Returns:
            ReconciliationRunOutput with all results.
        """
        effective_config = matching_config or self._config
        all_reconciliations: list[ReconciliationRecord] = []
        all_items: list[ReconciliationItemRecord] = []
        total_matched = 0
        total_exceptions = 0
        total_unmatched = 0

        for rec_type in reconciliation_types:
            type_data = source_data.get(rec_type)
            if type_data is None:
                logger.warning(
                    "No source data provided for reconciliation type %s, skipping.",
                    rec_type.value,
                )
                continue

            source_items = type_data.get("source_items", [])
            counterparts = type_data.get("counterparts", [])

            if not source_items:
                logger.info(
                    "No source items for reconciliation type %s in close run %s.",
                    rec_type.value,
                    close_run_id,
                )
                continue

            # Create reconciliation run
            if progress_guard is not None:
                progress_guard()
            rec_record = self._repo.create_reconciliation(
                close_run_id=close_run_id,
                reconciliation_type=rec_type,
                created_by_user_id=created_by_user_id,
            )

            # Get the appropriate matcher
            matcher_cls = MATCHER_REGISTRY.get(rec_type)
            if matcher_cls is None:
                logger.error("No matcher registered for reconciliation type %s.", rec_type.value)
                continue

            matcher = matcher_cls()

            # Run matching
            match_results = matcher.match(
                source_items=source_items,
                counterparts=counterparts,
                config=effective_config,
            )

            # Prepare items for persistence
            item_dicts = self._match_results_to_item_dicts(match_results)

            # Persist items
            if progress_guard is not None:
                progress_guard()
            item_records = self._repo.bulk_create_items(
                reconciliation_id=rec_record.id,
                items=item_dicts,
            )
            all_items.extend(item_records)

            # Compute summary stats
            stats = self._repo.compute_summary_stats(rec_record.id)

            # Update reconciliation summary
            summary: JsonObject = {
                "total_items": stats.total_items,
                "matched_count": stats.matched_count,
                "partially_matched_count": stats.partially_matched_count,
                "exception_count": stats.exception_count,
                "unmatched_count": stats.unmatched_count,
                "pending_disposition_count": stats.pending_disposition_count,
            }
            self._repo.update_reconciliation_summary(rec_record.id, summary)

            # Transition to in_review if there are items
            if stats.total_items > 0:
                self._repo.update_reconciliation_status(
                    rec_record.id,
                    ReconciliationStatus.IN_REVIEW,
                )

            # Refresh the record after updates
            refreshed_record = self._repo.get_reconciliation(rec_record.id)
            if refreshed_record is not None:
                all_reconciliations.append(refreshed_record)

            total_matched += stats.matched_count + stats.partially_matched_count
            total_exceptions += stats.exception_count
            total_unmatched += stats.unmatched_count

        return ReconciliationRunOutput(
            reconciliations=all_reconciliations,
            all_items=all_items,
            trial_balance=None,  # Computed separately
            anomalies=[],
            total_items=len(all_items),
            matched_items=total_matched,
            exception_items=total_exceptions,
            unmatched_items=total_unmatched,
        )

    # ------------------------------------------------------------------
    # Trial balance
    # ------------------------------------------------------------------

    def compute_trial_balance(
        self,
        close_run_id: UUID,
        account_balances: list[dict[str, Any]],
        expected_account_codes: set[str] | None = None,
        prior_balances: list[dict[str, Any]] | None = None,
        variance_threshold_pct: float = 20.0,
        generated_by_user_id: UUID | None = None,
        progress_guard: Callable[[], None] | None = None,
    ) -> TrialBalanceSnapshotRecord:
        """Compute and validate a trial balance snapshot for a close run.

        This method:
        1. Validates debit-equals-credit equality.
        2. Detects unusual balance directions.
        3. Detects zero-balance active accounts.
        4. Detects missing expected accounts.
        5. Detects unexplained MoM variances.
        6. Persists the snapshot and all anomalies.

        Args:
            close_run_id: The close run UUID.
            account_balances: Per-account balance dicts with keys:
                account_code, account_name, account_type, debit_balance, credit_balance, is_active.
            expected_account_codes: Optional set of account codes that should appear.
            prior_balances: Optional prior period balances for variance analysis.
            variance_threshold_pct: MoM variance percentage threshold.
            generated_by_user_id: User triggering the computation.

        Returns:
            TrialBalanceSnapshotRecord with all detected anomalies recorded.
        """
        # Run balance check
        is_balanced, total_debits, total_credits, anomalies = (
            self._tb_checker.check_balance(account_balances)
        )

        # Run unusual balance check
        anomalies.extend(self._tb_checker.check_unusual_balances(account_balances))

        # Run missing account check
        if expected_account_codes:
            anomalies.extend(
                self._tb_checker.check_missing_accounts(account_balances, expected_account_codes)
            )

        # Run variance check
        if prior_balances:
            anomalies.extend(
                self._tb_checker.check_variance(
                    account_balances, prior_balances, variance_threshold_pct
                )
            )

        # Persist trial balance snapshot
        if progress_guard is not None:
            progress_guard()
        snapshot = self._repo.create_trial_balance_snapshot(
            close_run_id=close_run_id,
            total_debits=total_debits,
            total_credits=total_credits,
            is_balanced=is_balanced,
            account_balances=account_balances,
            generated_by_user_id=generated_by_user_id,
            metadata_payload={
                "expected_account_count": (
                    len(expected_account_codes) if expected_account_codes else 0
                ),
                "variance_threshold_pct": variance_threshold_pct,
                "prior_balances_provided": prior_balances is not None,
            },
        )

        # Persist anomalies
        anomaly_records: list[ReconciliationAnomalyRecord] = []
        for anomaly in anomalies:
            if progress_guard is not None:
                progress_guard()
            record = self._repo.create_anomaly(
                close_run_id=close_run_id,
                anomaly_type=anomaly.anomaly_type,
                severity=anomaly.severity,
                account_code=anomaly.account_code,
                description=anomaly.description,
                details=anomaly.details,
                trial_balance_snapshot_id=snapshot.id,
            )
            anomaly_records.append(record)

        return snapshot

    # ------------------------------------------------------------------
    # Disposition and approval
    # ------------------------------------------------------------------

    def disposition_item(
        self,
        item_id: UUID,
        close_run_id: UUID,
        disposition: DispositionAction,
        reason: str,
        user_id: UUID,
    ) -> ReconciliationItemRecord | None:
        """Record a reviewer disposition for a reconciliation item.

        Args:
            item_id: The reconciliation item UUID.
            close_run_id: The owning close run UUID for access scoping.
            disposition: The disposition action enum.
            reason: Reviewer reasoning.
            user_id: User recording the disposition.

        Returns:
            Updated ReconciliationItemRecord or None if not found or close_run mismatch.
        """
        item = self._repo.get_item_for_close_run(
            item_id=item_id,
            close_run_id=close_run_id,
        )
        if item is None:
            return None
        return self._repo.disposition_item(
            item_id=item_id,
            disposition=disposition,
            reason=reason,
            user_id=user_id,
        )

    def bulk_disposition_items(
        self,
        item_ids: list[UUID],
        close_run_id: UUID,
        disposition: DispositionAction,
        reason: str,
        user_id: UUID,
    ) -> ReconciliationDispositionOutput:
        """Record bulk dispositions for multiple reconciliation items.

        Args:
            item_ids: List of item UUIDs to disposition.
            close_run_id: The owning close run UUID for access scoping.
            disposition: The disposition action enum.
            reason: Reviewer reasoning.
            user_id: User recording the dispositions.

        Returns:
            ReconciliationDispositionOutput with success and failure lists.
        """
        disposed: list[ReconciliationItemRecord] = []
        failed: list[UUID] = []

        for item_id in item_ids:
            # Verify item belongs to the claimed close run
            item = self._repo.get_item_for_close_run(
                item_id=item_id,
                close_run_id=close_run_id,
            )
            if item is None:
                failed.append(item_id)
                continue
            result = self._repo.disposition_item(
                item_id=item_id,
                disposition=disposition,
                reason=reason,
                user_id=user_id,
            )
            if result is not None:
                disposed.append(result)
            else:
                failed.append(item_id)

        return ReconciliationDispositionOutput(
            disposed_items=disposed,
            failed_item_ids=failed,
        )

    def approve_reconciliation(
        self,
        reconciliation_id: UUID,
        close_run_id: UUID,
        reason: str,
        user_id: UUID,
    ) -> ReconciliationRecord | None:
        """Approve a reconciliation run after all required dispositions are recorded.

        Args:
            reconciliation_id: The reconciliation UUID.
            close_run_id: The owning close run UUID for access scoping.
            reason: Approver reasoning.
            user_id: User approving the reconciliation.

        Returns:
            Updated ReconciliationRecord or None if not found or close_run mismatch.
        """
        rec = self._repo.get_reconciliation_for_close_run(
            reconciliation_id=reconciliation_id,
            close_run_id=close_run_id,
        )
        if rec is None:
            return None

        # Check for pending dispositions
        stats = self._repo.compute_summary_stats(reconciliation_id)
        if stats.pending_disposition_count > 0:
            # Update to blocked with reason
            return self._repo.update_reconciliation_status(
                reconciliation_id,
                ReconciliationStatus.BLOCKED,
                blocking_reason=(
                    f"{stats.pending_disposition_count} items still require "
                    f"reviewer disposition before approval."
                ),
            )

        # All dispositions recorded — approve
        return self._repo.update_reconciliation_status(
            reconciliation_id,
            ReconciliationStatus.APPROVED,
            approved_by_user_id=user_id,
        )

    # ------------------------------------------------------------------
    # Anomaly management
    # ------------------------------------------------------------------

    def resolve_anomaly(
        self,
        anomaly_id: UUID,
        close_run_id: UUID,
        resolution_note: str,
        user_id: UUID,
    ) -> ReconciliationAnomalyRecord | None:
        """Mark a reconciliation anomaly as resolved.

        Args:
            anomaly_id: The anomaly UUID.
            close_run_id: The owning close run UUID for access scoping.
            resolution_note: Reviewer reasoning.
            user_id: User resolving the anomaly.

        Returns:
            Updated ReconciliationAnomalyRecord or None if not found or close_run mismatch.
        """
        anomaly = self._repo.get_anomaly_for_close_run(
            anomaly_id=anomaly_id,
            close_run_id=close_run_id,
        )
        if anomaly is None:
            return None
        return self._repo.resolve_anomaly(
            anomaly_id=anomaly_id,
            resolution_note=resolution_note,
            user_id=user_id,
        )

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def list_reconciliations(
        self,
        close_run_id: UUID,
        reconciliation_type: ReconciliationType | None = None,
    ) -> list[ReconciliationRecord]:
        """List reconciliation runs for a close run.

        Args:
            close_run_id: The close run UUID.
            reconciliation_type: Optional filter by type.

        Returns:
            List of ReconciliationRecord.
        """
        return self._repo.list_reconciliations(
            close_run_id=close_run_id,
            reconciliation_type=reconciliation_type,
        )

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
        return self._repo.list_items(
            reconciliation_id=reconciliation_id,
            match_status=match_status,
            requires_disposition=requires_disposition,
        )

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
        return self._repo.list_anomalies(
            close_run_id=close_run_id,
            severity=severity,
            resolved=resolved,
        )

    def get_latest_trial_balance(
        self,
        close_run_id: UUID,
    ) -> TrialBalanceSnapshotRecord | None:
        """Fetch the most recent trial balance snapshot for a close run.

        Args:
            close_run_id: The close run UUID.

        Returns:
            Latest TrialBalanceSnapshotRecord or None.
        """
        return self._repo.get_latest_trial_balance(close_run_id=close_run_id)

    def get_summary_stats(
        self,
        reconciliation_id: UUID,
    ) -> ReconciliationSummaryStats:
        """Compute aggregate statistics for a reconciliation run.

        Args:
            reconciliation_id: Parent reconciliation UUID.

        Returns:
            ReconciliationSummaryStats.
        """
        return self._repo.compute_summary_stats(reconciliation_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _match_results_to_item_dicts(
        results: list[MatchResult],
    ) -> list[dict[str, Any]]:
        """Convert matcher MatchResult objects into item dicts for persistence.

        Args:
            results: List of MatchResult from the matching engine.

        Returns:
            List of dicts suitable for bulk_create_items.
        """
        item_dicts: list[dict[str, Any]] = []
        for result in results:
            matched_to = [
                {
                    "source_type": cp.source_type.value,
                    "source_ref": cp.source_ref,
                    "amount": str(cp.amount) if cp.amount is not None else None,
                    "confidence": cp.confidence,
                    "match_reason": cp.match_reason,
                }
                for cp in result.counterparts
            ]

            item_dicts.append(
                {
                    "source_type": result.source_type,
                    "source_ref": result.source_ref,
                    "match_status": result.match_status.value,
                    "source_amount": result.source_amount,
                    "matched_to": matched_to,
                    "difference_amount": result.difference_amount,
                    "explanation": result.explanation,
                    "requires_disposition": result.requires_disposition,
                    "dimensions": result.metadata,
                    "period_date": None,
                }
            )
        return item_dicts


__all__ = [
    "ReconciliationDispositionOutput",
    "ReconciliationRunOutput",
    "ReconciliationService",
]
