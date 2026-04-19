"""
Purpose: Define canonical reconciliation applicability rules for close runs.
Scope: Shared bank-reconciliation, trial-balance, and source-data applicability
checks used by gates, routes, and worker execution.
Dependencies: Canonical reconciliation enums only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from services.common.enums import ReconciliationType

BANK_RECONCILIATION_LEDGER_GUIDANCE = (
    "Bank reconciliation needs both the bank statement and the ledger-side cash activity. "
    "If this run only contains a bank statement, you can still review it and move on to "
    "Reporting, or upload a GL/cashbook baseline to perform true bank reconciliation."
)

NO_APPLICABLE_RECONCILIATION_WORK_MESSAGE = (
    "No applicable reconciliation work was detected for this close run right now. "
    "If you only uploaded support documents such as a bank statement without ledger-side "
    "data, you can advance to Reporting. Upload a GL/cashbook baseline or create "
    "approved/applied journals if you want full reconciliation."
)


def is_bank_reconciliation_applicable(
    *,
    approved_bank_statement_count: int,
    effective_ledger_transaction_count: int,
) -> bool:
    """Return whether bank reconciliation has both statement-side and ledger-side inputs."""

    return approved_bank_statement_count > 0 and effective_ledger_transaction_count > 0


def is_trial_balance_applicable(
    *,
    effective_ledger_transaction_count: int,
    has_trial_balance_baseline: bool,
) -> bool:
    """Return whether trial-balance computation has any ledger baseline to evaluate."""

    return has_trial_balance_baseline or effective_ledger_transaction_count > 0


def filter_runnable_reconciliation_types(
    *,
    requested_types: Sequence[ReconciliationType],
    source_data: Mapping[ReconciliationType, Mapping[str, Sequence[dict[str, Any]]]],
) -> tuple[tuple[ReconciliationType, ...], tuple[str, ...]]:
    """Return runnable reconciliation types and informational skip guidance."""

    runnable_types: list[ReconciliationType] = []
    guidance: list[str] = []

    for reconciliation_type in requested_types:
        type_data = source_data.get(reconciliation_type, {})
        source_items = tuple(type_data.get("source_items", ()))
        counterparts = tuple(type_data.get("counterparts", ()))

        if not source_items:
            continue
        if reconciliation_type is ReconciliationType.BANK_RECONCILIATION and not counterparts:
            guidance.append(BANK_RECONCILIATION_LEDGER_GUIDANCE)
            continue
        runnable_types.append(reconciliation_type)

    return tuple(runnable_types), tuple(dict.fromkeys(guidance))


__all__ = [
    "BANK_RECONCILIATION_LEDGER_GUIDANCE",
    "NO_APPLICABLE_RECONCILIATION_WORK_MESSAGE",
    "filter_runnable_reconciliation_types",
    "is_bank_reconciliation_applicable",
    "is_trial_balance_applicable",
]
