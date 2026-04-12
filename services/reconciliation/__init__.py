"""
Purpose: Reconciliation service package for the accounting close run workflow.
Scope: Bank reconciliation, AR/AP ageing, intercompany, payroll control, fixed assets,
       loan amortisation, accrual tracker, budget vs actual, and trial balance reconciliation.
Dependencies: Matching helpers (matchers), reconciliation service (service),
       and the reconciliation repository (db/repositories/reconciliation_repo).
"""

from services.reconciliation.matchers import (
    DEFAULT_MATCHING_CONFIG,
    MATCHER_REGISTRY,
    AccrualTrackerMatcher,
    AgeingMatcher,
    BankReconciliationMatcher,
    BudgetVsActualMatcher,
    FixedAssetMatcher,
    IntercompanyMatcher,
    LoanAmortisationMatcher,
    MatchCounterpart,
    MatcherProtocol,
    MatchingConfig,
    MatchResult,
    PayrollControlMatcher,
    TrialBalanceAnomaly,
    TrialBalanceChecker,
)
from services.reconciliation.service import (
    ReconciliationDispositionOutput,
    ReconciliationRunOutput,
    ReconciliationService,
)

__all__ = [
    "DEFAULT_MATCHING_CONFIG",
    "MATCHER_REGISTRY",
    "AccrualTrackerMatcher",
    "AgeingMatcher",
    "BankReconciliationMatcher",
    "BudgetVsActualMatcher",
    "FixedAssetMatcher",
    "IntercompanyMatcher",
    "LoanAmortisationMatcher",
    "MatchCounterpart",
    "MatchResult",
    "MatcherProtocol",
    "MatchingConfig",
    "PayrollControlMatcher",
    "ReconciliationDispositionOutput",
    "ReconciliationRunOutput",
    "ReconciliationService",
    "TrialBalanceAnomaly",
    "TrialBalanceChecker",
]
