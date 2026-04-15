"""
Purpose: Verify the centralized default reconciliation execution set.
Scope: Ensure callers default to types the worker can currently execute end-to-end.
Dependencies: Canonical reconciliation enums only.
"""

from __future__ import annotations

from services.common.enums import (
    DEFAULT_RECONCILIATION_EXECUTION_TYPES,
    ReconciliationType,
)


def test_default_reconciliation_execution_types_exclude_unimplemented_sources() -> None:
    """Default queued types should stay aligned with the worker's implemented source loaders."""

    assert DEFAULT_RECONCILIATION_EXECUTION_TYPES == (
        ReconciliationType.BANK_RECONCILIATION,
        ReconciliationType.FIXED_ASSETS,
        ReconciliationType.LOAN_AMORTISATION,
        ReconciliationType.ACCRUAL_TRACKER,
        ReconciliationType.BUDGET_VS_ACTUAL,
        ReconciliationType.TRIAL_BALANCE,
    )
