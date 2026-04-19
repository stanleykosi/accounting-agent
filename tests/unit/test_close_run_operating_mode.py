"""
Purpose: Verify canonical close-run operating-mode detection and runtime capability flags.
Scope: Focused unit coverage over imported-GL, working-ledger, trial-balance-only, and
source-documents-only operating contexts.
Dependencies: Operating-mode resolver and canonical mode enum.
"""

from __future__ import annotations

from services.close_runs.operating_mode import resolve_close_run_operating_context
from services.common.enums import CloseRunOperatingMode


def test_resolve_source_documents_only_mode_without_ledger_side_data() -> None:
    """Source-doc-only runs should not expose bank reconciliation before ledger data exists."""

    context = resolve_close_run_operating_context(
        has_general_ledger_baseline=False,
        has_trial_balance_baseline=False,
        has_working_ledger_entries=False,
        approved_bank_statement_count=1,
        effective_ledger_transaction_count=0,
    )

    assert context.mode is CloseRunOperatingMode.SOURCE_DOCUMENTS_ONLY
    assert context.bank_reconciliation_available is False
    assert context.trial_balance_review_available is False


def test_resolve_working_ledger_mode_from_posted_journals() -> None:
    """Posted journals without imported baselines should create working-ledger mode."""

    context = resolve_close_run_operating_context(
        has_general_ledger_baseline=False,
        has_trial_balance_baseline=False,
        has_working_ledger_entries=True,
        approved_bank_statement_count=1,
        effective_ledger_transaction_count=4,
    )

    assert context.mode is CloseRunOperatingMode.WORKING_LEDGER
    assert context.bank_reconciliation_available is True
    assert context.trial_balance_review_available is True
    assert context.general_ledger_export_available is True


def test_resolve_imported_general_ledger_mode_takes_precedence() -> None:
    """Imported GL baselines should dominate the mode selection even when TB data also exists."""

    context = resolve_close_run_operating_context(
        has_general_ledger_baseline=True,
        has_trial_balance_baseline=True,
        has_working_ledger_entries=False,
        approved_bank_statement_count=0,
        effective_ledger_transaction_count=22,
    )

    assert context.mode is CloseRunOperatingMode.IMPORTED_GENERAL_LEDGER
    assert context.has_trial_balance_baseline is True
    assert context.trial_balance_review_available is True


def test_resolve_trial_balance_only_mode_without_detailed_gl() -> None:
    """Trial-balance-only runs should allow control review without pretending GL detail exists."""

    context = resolve_close_run_operating_context(
        has_general_ledger_baseline=False,
        has_trial_balance_baseline=True,
        has_working_ledger_entries=False,
        approved_bank_statement_count=1,
        effective_ledger_transaction_count=0,
    )

    assert context.mode is CloseRunOperatingMode.TRIAL_BALANCE_ONLY
    assert context.bank_reconciliation_available is False
    assert context.trial_balance_review_available is True
