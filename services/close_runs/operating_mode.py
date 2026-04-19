"""
Purpose: Resolve the canonical close-run operating mode from available ledger baselines and
working-ledger activity.
Scope: Determine whether a run is source-documents-only, working-ledger, imported-GL, or
trial-balance-only, and expose the runtime capabilities that flow from that posture.
Dependencies: Canonical close-run operating-mode enum only.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.common.enums import CloseRunOperatingMode


@dataclass(frozen=True, slots=True)
class CloseRunOperatingContext:
    """Describe the current ledger/control posture for one close run."""

    mode: CloseRunOperatingMode
    has_general_ledger_baseline: bool
    has_trial_balance_baseline: bool
    has_working_ledger_entries: bool
    bank_reconciliation_available: bool
    trial_balance_review_available: bool
    journal_posting_available: bool
    general_ledger_export_available: bool
    description: str


def resolve_close_run_operating_context(
    *,
    has_general_ledger_baseline: bool,
    has_trial_balance_baseline: bool,
    has_working_ledger_entries: bool,
    approved_bank_statement_count: int,
    effective_ledger_transaction_count: int,
) -> CloseRunOperatingContext:
    """Return the canonical operating mode and runtime capabilities for one close run."""

    if has_general_ledger_baseline:
        mode = CloseRunOperatingMode.IMPORTED_GENERAL_LEDGER
        description = (
            "An imported general-ledger baseline is bound to this close run. Reconciliation uses "
            "the imported books plus any approved or applied close-run journal adjustments."
        )
    elif has_trial_balance_baseline:
        mode = CloseRunOperatingMode.TRIAL_BALANCE_ONLY
        description = (
            "A trial-balance baseline is bound to this close run, but no imported detailed GL is "
            "available. Trial-balance control work can proceed, while detailed bank "
            "reconciliation still depends on posted working-ledger transactions."
        )
    elif has_working_ledger_entries:
        mode = CloseRunOperatingMode.WORKING_LEDGER
        description = (
            "This close run is operating from approved or applied close-run journals in the "
            "platform working ledger. Reconciliation and exports use those posted entries as the "
            "ledger-side source of truth."
        )
    else:
        mode = CloseRunOperatingMode.SOURCE_DOCUMENTS_ONLY
        description = (
            "This close run currently has source documents only. Document review, processing, and "
            "reporting can continue, but bank reconciliation remains not applicable until "
            "ledger-side data exists."
        )

    bank_reconciliation_available = (
        approved_bank_statement_count > 0 and effective_ledger_transaction_count > 0
    )
    trial_balance_review_available = (
        has_trial_balance_baseline or effective_ledger_transaction_count > 0
    )
    journal_posting_available = True
    general_ledger_export_available = effective_ledger_transaction_count > 0

    return CloseRunOperatingContext(
        mode=mode,
        has_general_ledger_baseline=has_general_ledger_baseline,
        has_trial_balance_baseline=has_trial_balance_baseline,
        has_working_ledger_entries=has_working_ledger_entries,
        bank_reconciliation_available=bank_reconciliation_available,
        trial_balance_review_available=trial_balance_review_available,
        journal_posting_available=journal_posting_available,
        general_ledger_export_available=general_ledger_export_available,
        description=description,
    )


__all__ = [
    "CloseRunOperatingContext",
    "resolve_close_run_operating_context",
]
