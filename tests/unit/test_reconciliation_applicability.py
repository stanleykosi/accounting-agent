"""
Purpose: Verify shared reconciliation applicability helpers.
Scope: Bank-reconciliation, trial-balance, and worker-side runnable-type filtering only.
Dependencies: Canonical reconciliation enums and applicability helpers.
"""

from __future__ import annotations

from services.common.enums import ReconciliationType
from services.reconciliation.applicability import (
    BANK_RECONCILIATION_LEDGER_GUIDANCE,
    filter_runnable_reconciliation_types,
    is_bank_reconciliation_applicable,
    is_trial_balance_applicable,
)


def test_bank_reconciliation_is_not_applicable_without_ledger_side_input() -> None:
    """A bank statement alone should not make bank reconciliation applicable."""

    assert not is_bank_reconciliation_applicable(
        approved_bank_statement_count=1,
        effective_ledger_transaction_count=0,
    )


def test_trial_balance_is_not_applicable_without_any_ledger_baseline() -> None:
    """Trial balance should stay out of scope when no ledger-side baseline exists."""

    assert not is_trial_balance_applicable(
        effective_ledger_transaction_count=0,
        has_trial_balance_baseline=False,
    )


def test_filter_runnable_reconciliation_types_skips_bank_reconciliation_without_counterparts(
) -> None:
    """The worker should skip bank reconciliation when statement lines have no ledger side."""

    runnable_types, guidance = filter_runnable_reconciliation_types(
        requested_types=(
            ReconciliationType.BANK_RECONCILIATION,
            ReconciliationType.TRIAL_BALANCE,
        ),
        source_data={
            ReconciliationType.BANK_RECONCILIATION: {
                "source_items": [{"ref": "bank:1", "amount": "12000.00"}],
                "counterparts": [],
            },
            ReconciliationType.TRIAL_BALANCE: {
                "source_items": [{"account_code": "1010"}],
                "counterparts": [],
            },
        },
    )

    assert runnable_types == (ReconciliationType.TRIAL_BALANCE,)
    assert guidance == (BANK_RECONCILIATION_LEDGER_GUIDANCE,)
