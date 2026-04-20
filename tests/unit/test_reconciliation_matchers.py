"""
Purpose: Unit tests for reconciliation matching helpers.
Scope: Bank reconciliation exact/fuzzy matching, AR/AP ageing, intercompany,
       payroll control, fixed assets, loan amortisation, accrual tracker,
       budget vs actual, and trial balance checks.
Dependencies: pytest, reconciliation matchers module, canonical enums.

Design notes:
- All tests use deterministic Decimal arithmetic — no LLM involvement.
- Tests cover: exact matches, fuzzy matches, unmatched items, exceptions,
  edge cases (missing amounts, date parsing failures, zero amounts).
- Trial balance tests verify debit/credit equality, unusual balances,
  missing accounts, and variance detection.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from services.common.enums import (
    AnomalyType,
    MatchStatus,
    ReconciliationSourceType,
    ReconciliationType,
)
from services.reconciliation.matchers import (
    AccrualTrackerMatcher,
    AgeingMatcher,
    BankReconciliationMatcher,
    BudgetVsActualMatcher,
    FixedAssetMatcher,
    IntercompanyMatcher,
    LoanAmortisationMatcher,
    PayrollControlMatcher,
    TrialBalanceChecker,
    _compute_amount_confidence,
    _compute_date_confidence,
    _parse_amount,
    _parse_date,
    _reference_similarity,
)

# ---------------------------------------------------------------------------
# Helper utility tests
# ---------------------------------------------------------------------------


class TestParseAmount:
    """Test amount parsing from various input types."""

    def test_parse_decimal(self) -> None:
        assert _parse_amount(Decimal("100.50")) == Decimal("100.50")

    def test_parse_string(self) -> None:
        assert _parse_amount("100.50") == Decimal("100.50")

    def test_parse_int(self) -> None:
        assert _parse_amount(100) == Decimal("100")

    def test_parse_float(self) -> None:
        assert _parse_amount(100.50) == Decimal("100.5")

    def test_parse_none(self) -> None:
        assert _parse_amount(None) is None

    def test_parse_invalid_string(self) -> None:
        assert _parse_amount("not_a_number") is None


class TestParseDate:
    """Test date parsing from various input types."""

    def test_parse_date_object(self) -> None:
        d = date(2026, 4, 12)
        assert _parse_date(d) == d

    def test_parse_iso_string(self) -> None:
        assert _parse_date("2026-04-12") == date(2026, 4, 12)

    def test_parse_none(self) -> None:
        assert _parse_date(None) is None

    def test_parse_invalid_string(self) -> None:
        assert _parse_date("not_a_date") is None


class TestAmountConfidence:
    """Test amount confidence computation."""

    def test_exact_match(self) -> None:
        assert _compute_amount_confidence(Decimal("0"), Decimal("100")) == 1.0

    def test_small_difference(self) -> None:
        confidence = _compute_amount_confidence(Decimal("0.10"), Decimal("100"))
        assert confidence >= 0.9

    def test_large_difference(self) -> None:
        confidence = _compute_amount_confidence(Decimal("50"), Decimal("100"))
        assert confidence < 0.5

    def test_zero_amount(self) -> None:
        assert _compute_amount_confidence(Decimal("0"), Decimal("0")) == 1.0
        assert _compute_amount_confidence(Decimal("1"), Decimal("0")) == 0.0


class TestDateConfidence:
    """Test date confidence computation."""

    def test_exact_match(self) -> None:
        assert _compute_date_confidence(0, 5) == 1.0

    def test_within_tolerance(self) -> None:
        confidence = _compute_date_confidence(3, 5)
        assert 0.5 <= confidence < 1.0

    def test_outside_tolerance(self) -> None:
        assert _compute_date_confidence(10, 5) == 0.0


class TestReferenceSimilarity:
    """Test reference string similarity."""

    def test_exact_match(self) -> None:
        assert _reference_similarity("INV-001", "INV-001") == 1.0

    def test_case_insensitive(self) -> None:
        assert _reference_similarity("inv-001", "INV-001") == 1.0

    def test_empty_strings(self) -> None:
        assert _reference_similarity("", "INV-001") == 0.0

    def test_similar_strings(self) -> None:
        similarity = _reference_similarity("INV-001", "INV-002")
        assert 0.7 < similarity < 1.0


# ---------------------------------------------------------------------------
# Bank reconciliation matcher tests
# ---------------------------------------------------------------------------


class TestBankReconciliationMatcher:
    """Test bank statement line to ledger transaction matching."""

    def setup_method(self) -> None:
        self.matcher = BankReconciliationMatcher()

    def test_exact_amount_reference_match(self) -> None:
        source_items = [
            {"ref": "bank_1", "amount": "1000.00", "date": "2026-04-01", "reference": "TXN-001"},
        ]
        counterparts = [
            {"ref": "ledger_1", "amount": "1000.00", "date": "2026-04-01", "reference": "TXN-001"},
        ]

        results = self.matcher.match(source_items, counterparts)
        assert len(results) == 1
        assert results[0].match_status == MatchStatus.MATCHED
        assert results[0].confidence == 1.0
        assert len(results[0].counterparts) == 1

    def test_exact_amount_date_match(self) -> None:
        source_items = [
            {"ref": "bank_1", "amount": "500.00", "date": "2026-04-01", "reference": ""},
        ]
        counterparts = [
            {"ref": "ledger_1", "amount": "500.00", "date": "2026-04-01", "reference": ""},
        ]

        results = self.matcher.match(source_items, counterparts)
        assert len(results) == 1
        assert results[0].match_status == MatchStatus.MATCHED

    def test_unmatched_bank_line(self) -> None:
        source_items = [
            {"ref": "bank_1", "amount": "999.99", "date": "2026-04-01", "reference": "NO-MATCH"},
        ]
        counterparts = [
            {"ref": "ledger_1", "amount": "100.00", "date": "2026-04-01", "reference": "OTHER"},
        ]

        results = self.matcher.match(source_items, counterparts)
        # Should have bank result + unmatched ledger
        unmatched = [r for r in results if r.match_status == MatchStatus.UNMATCHED]
        assert len(unmatched) >= 1

    def test_fuzzy_match_within_tolerance(self) -> None:
        source_items = [
            {"ref": "bank_1", "amount": "1000.00", "date": "2026-04-01", "reference": "TXN-001"},
        ]
        counterparts = [
            {"ref": "ledger_1", "amount": "1000.50", "date": "2026-04-02", "reference": "TXN-001"},
        ]

        results = self.matcher.match(source_items, counterparts)
        matched = [r for r in results if r.match_status != MatchStatus.UNMATCHED]
        assert len(matched) >= 1

    def test_missing_amount_handled(self) -> None:
        source_items = [
            {"ref": "bank_1", "amount": None, "date": "2026-04-01", "reference": ""},
        ]
        counterparts = []

        results = self.matcher.match(source_items, counterparts)
        assert len(results) == 1
        assert results[0].match_status == MatchStatus.UNMATCHED
        assert results[0].requires_disposition

    def test_unmatched_ledger_reported(self) -> None:
        source_items = []
        counterparts = [
            {"ref": "ledger_1", "amount": "200.00", "date": "2026-04-01", "reference": ""},
        ]

        results = self.matcher.match(source_items, counterparts)
        unmatched_ledger = [
            r for r in results
            if r.source_type == ReconciliationSourceType.LEDGER_TRANSACTION
            and r.match_status == MatchStatus.UNMATCHED
        ]
        assert len(unmatched_ledger) == 1

    def test_fuzzy_does_not_prematurely_mark_counterpart(self) -> None:
        """When multiple fuzzy candidates exist, only the best should be marked matched."""
        source_items = [
            {"ref": "bank_1", "amount": "1000.00", "date": "2026-04-01", "reference": "TXN-001"},
        ]
        # Two candidates: one close but imperfect, one exact match
        counterparts = [
            {"ref": "ledger_close", "amount": "1000.00", "date": "2026-04-01", "reference": "TXN-001"},
            {"ref": "ledger_fuzzy", "amount": "1010.00", "date": "2026-04-03", "reference": "TXN-002"},
        ]

        results = self.matcher.match(source_items, counterparts)
        matched = [r for r in results if r.match_status == MatchStatus.MATCHED]
        # The exact match should win
        assert len(matched) >= 1
        # The exact-match counterpart should be in matched_to
        matched_refs = {cp.source_ref for m in matched for cp in m.counterparts}
        assert "ledger_close" in matched_refs


# ---------------------------------------------------------------------------
# AR/AP ageing matcher tests
# ---------------------------------------------------------------------------


class TestAgeingMatcher:
    """Test AR/AP ageing balance matching."""

    def setup_method(self) -> None:
        self.matcher = AgeingMatcher()

    def test_exact_ageing_match(self) -> None:
        source_items = [
            {"ref": "inv_001", "amount": "1000.00", "due_date": "2026-03-01", "bucket": "31-60"},
        ]
        counterparts = [
            {"ref": "inv_001", "amount": "1000.00", "due_date": "2026-03-01", "account_code": "1100"},
        ]

        results = self.matcher.match(source_items, counterparts)
        assert len(results) == 1
        assert results[0].match_status == MatchStatus.MATCHED

    def test_unmatched_ageing_item(self) -> None:
        source_items = [
            {"ref": "inv_002", "amount": "500.00", "due_date": "2026-03-15", "bucket": "1-30"},
        ]
        counterparts = []

        results = self.matcher.match(source_items, counterparts)
        assert len(results) == 1
        assert results[0].match_status == MatchStatus.UNMATCHED

    def test_amount_difference_flagged(self) -> None:
        source_items = [
            {"ref": "inv_001", "amount": "1000.00", "due_date": "2026-03-01", "bucket": "31-60"},
        ]
        counterparts = [
            {"ref": "inv_001", "amount": "950.00", "due_date": "2026-03-01", "account_code": "1100"},
        ]

        results = self.matcher.match(source_items, counterparts)
        assert len(results) == 1
        assert results[0].match_status == MatchStatus.EXCEPTION


# ---------------------------------------------------------------------------
# Intercompany matcher tests
# ---------------------------------------------------------------------------


class TestIntercompanyMatcher:
    """Test intercompany balance matching."""

    def setup_method(self) -> None:
        self.matcher = IntercompanyMatcher()

    def test_balanced_intercompany(self) -> None:
        source_items = [
            {
                "ref": "ic_001",
                "amount": "1000.00",
                "account_code": "1200",
                "counter_entity": "entity_B",
                "entity": "entity_A",
            },
        ]
        counterparts = [
            {
                "ref": "ic_001_b",
                "amount": "-1000.00",
                "account_code": "1200",
                "counter_entity": "entity_A",
                "entity": "entity_B",
            },
        ]

        results = self.matcher.match(source_items, counterparts)
        assert len(results) == 1
        assert results[0].match_status == MatchStatus.MATCHED

    def test_unbalanced_intercompany(self) -> None:
        source_items = [
            {
                "ref": "ic_001",
                "amount": "1000.00",
                "account_code": "1200",
                "counter_entity": "entity_B",
                "entity": "entity_A",
            },
        ]
        counterparts = [
            {
                "ref": "ic_001_b",
                "amount": "-900.00",
                "account_code": "1200",
                "counter_entity": "entity_A",
                "entity": "entity_B",
            },
        ]

        results = self.matcher.match(source_items, counterparts)
        assert len(results) == 1
        assert results[0].match_status in (MatchStatus.EXCEPTION, MatchStatus.PARTIALLY_MATCHED)


# ---------------------------------------------------------------------------
# Payroll control matcher tests
# ---------------------------------------------------------------------------


class TestPayrollControlMatcher:
    """Test payroll control total matching."""

    def setup_method(self) -> None:
        self.matcher = PayrollControlMatcher()

    def test_exact_payroll_match(self) -> None:
        source_items = [
            {"ref": "gross:2026-03", "category": "gross_pay", "amount": "50000.00", "period": "2026-03"},
        ]
        counterparts = [
            {"ref": "ledger_gross", "category": "gross_pay", "amount": "50000.00", "period": "2026-03"},
        ]

        results = self.matcher.match(source_items, counterparts)
        assert len(results) == 1
        assert results[0].match_status == MatchStatus.MATCHED

    def test_missing_payroll_category(self) -> None:
        source_items = [
            {"ref": "tax:2026-03", "category": "paye_tax", "amount": "5000.00", "period": "2026-03"},
        ]
        counterparts = []

        results = self.matcher.match(source_items, counterparts)
        assert len(results) == 1
        assert results[0].match_status == MatchStatus.UNMATCHED


# ---------------------------------------------------------------------------
# Fixed asset matcher tests
# ---------------------------------------------------------------------------


class TestFixedAssetMatcher:
    """Test fixed asset register matching."""

    def setup_method(self) -> None:
        self.matcher = FixedAssetMatcher()

    def test_exact_asset_match(self) -> None:
        source_items = [
            {
                "asset_id": "ASSET-001",
                "cost": "100000.00",
                "accumulated_depreciation": "20000.00",
                "net_book_value": "80000.00",
            },
        ]
        counterparts = [
            {
                "ref": "ledger_asset",
                "asset_id": "ASSET-001",
                "cost": "100000.00",
                "accumulated_depreciation": "20000.00",
            },
        ]

        results = self.matcher.match(source_items, counterparts)
        assert len(results) == 1
        assert results[0].match_status == MatchStatus.MATCHED

    def test_asset_not_in_ledger(self) -> None:
        source_items = [
            {
                "asset_id": "ASSET-999",
                "cost": "50000.00",
                "accumulated_depreciation": "0.00",
                "net_book_value": "50000.00",
            },
        ]
        counterparts = []

        results = self.matcher.match(source_items, counterparts)
        assert len(results) == 1
        assert results[0].match_status == MatchStatus.UNMATCHED


# ---------------------------------------------------------------------------
# Loan amortisation matcher tests
# ---------------------------------------------------------------------------


class TestLoanAmortisationMatcher:
    """Test loan amortisation schedule matching."""

    def setup_method(self) -> None:
        self.matcher = LoanAmortisationMatcher()

    def test_exact_loan_payment_match(self) -> None:
        source_items = [
            {
                "payment_no": 1,
                "principal": "5000.00",
                "interest": "500.00",
                "balance": "95000.00",
            },
        ]
        counterparts = [
            {
                "ref": "loan_pmt_1",
                "payment_no": 1,
                "principal": "5000.00",
                "interest": "500.00",
                "balance": "95000.00",
            },
        ]

        results = self.matcher.match(source_items, counterparts)
        assert len(results) == 1
        assert results[0].match_status == MatchStatus.MATCHED

    def test_missing_loan_payment(self) -> None:
        source_items = [
            {
                "payment_no": 5,
                "principal": "5000.00",
                "interest": "400.00",
                "balance": "75000.00",
            },
        ]
        counterparts = []

        results = self.matcher.match(source_items, counterparts)
        assert len(results) == 1
        assert results[0].match_status == MatchStatus.UNMATCHED


# ---------------------------------------------------------------------------
# Accrual tracker matcher tests
# ---------------------------------------------------------------------------


class TestAccrualTrackerMatcher:
    """Test accrual tracking matching."""

    def setup_method(self) -> None:
        self.matcher = AccrualTrackerMatcher()

    def test_exact_accrual_match(self) -> None:
        source_items = [
            {"ref": "acc_001", "amount": "10000.00", "account_code": "2100", "period": "2026-03"},
        ]
        counterparts = [
            {"ref": "ledger_acc", "amount": "10000.00", "account_code": "2100", "period": "2026-03"},
        ]

        results = self.matcher.match(source_items, counterparts)
        assert len(results) == 1
        assert results[0].match_status == MatchStatus.MATCHED

    def test_missing_accrual(self) -> None:
        source_items = [
            {"ref": "acc_002", "amount": "5000.00", "account_code": "2200", "period": "2026-03"},
        ]
        counterparts = []

        results = self.matcher.match(source_items, counterparts)
        assert len(results) == 1
        assert results[0].match_status == MatchStatus.UNMATCHED


# ---------------------------------------------------------------------------
# Budget vs actual matcher tests
# ---------------------------------------------------------------------------


class TestBudgetVsActualMatcher:
    """Test budget vs actual variance matching."""

    def setup_method(self) -> None:
        self.matcher = BudgetVsActualMatcher()

    def test_budget_within_tolerance(self) -> None:
        source_items = [
            {"account_code": "5000", "budget_amount": "10000.00", "period": "2026-03"},
        ]
        counterparts = [
            {"account_code": "5000", "amount": "10050.00", "period": "2026-03"},
        ]

        results = self.matcher.match(source_items, counterparts)
        assert len(results) == 1
        assert results[0].match_status == MatchStatus.MATCHED

    def test_budget_variance_flagged(self) -> None:
        source_items = [
            {"account_code": "5000", "budget_amount": "10000.00", "period": "2026-03"},
        ]
        counterparts = [
            {"account_code": "5000", "amount": "15000.00", "period": "2026-03"},
        ]

        results = self.matcher.match(source_items, counterparts)
        assert len(results) == 1
        assert results[0].match_status == MatchStatus.EXCEPTION
        assert results[0].metadata["variance_pct"] == 50.0

    def test_zero_budget_with_no_actuals(self) -> None:
        """Zero budget and zero actuals should be MATCHED."""
        source_items = [
            {"account_code": "5000", "budget_amount": "0.00", "period": "2026-03"},
        ]
        counterparts = []

        results = self.matcher.match(source_items, counterparts)
        assert len(results) == 1
        assert results[0].match_status == MatchStatus.MATCHED

    def test_zero_budget_with_actuals_flagged(self) -> None:
        """Zero budget with nonzero actuals should be EXCEPTION (unbudgeted spend)."""
        source_items = [
            {"account_code": "5000", "budget_amount": "0.00", "period": "2026-03"},
        ]
        counterparts = [
            {"account_code": "5000", "amount": "500.00", "period": "2026-03"},
        ]

        results = self.matcher.match(source_items, counterparts)
        assert len(results) == 1
        assert results[0].match_status == MatchStatus.EXCEPTION
        assert results[0].metadata["variance_pct"] == 100.0
        assert results[0].confidence == 0.0

    def test_budget_dimensions_are_matched_independently(self) -> None:
        """Budget rows should stay split by department/cost centre/project dimensions."""

        source_items = [
            {
                "account_code": "6100",
                "budget_amount": "20000.00",
                "period": "2026-03",
                "department": "Ops",
                "cost_centre": "HQ",
            },
            {
                "account_code": "6100",
                "budget_amount": "10000.00",
                "period": "2026-03",
                "department": "Sales",
                "cost_centre": "HQ",
            },
        ]
        counterparts = [
            {
                "ref": "ledger:budget:6100:2026-03:Ops:HQ",
                "account_code": "6100",
                "period": "2026-03",
                "department": "Ops",
                "cost_centre": "HQ",
                "amount": "7500.00",
            },
            {
                "ref": "ledger:budget:6100:2026-03:Sales:HQ",
                "account_code": "6100",
                "period": "2026-03",
                "department": "Sales",
                "cost_centre": "HQ",
                "amount": "9900.00",
            },
        ]

        results = self.matcher.match(source_items, counterparts)

        assert len(results) == 2
        results_by_ref = {result.source_ref: result for result in results}
        ops_result = results_by_ref["budget:6100:2026-03:Ops:HQ"]
        sales_result = results_by_ref["budget:6100:2026-03:Sales:HQ"]

        assert ops_result.match_status == MatchStatus.EXCEPTION
        assert ops_result.counterparts[0].source_ref == "ledger:budget:6100:2026-03:Ops:HQ"
        assert ops_result.metadata["department"] == "Ops"
        assert ops_result.difference_amount == Decimal("-12500.00")

        assert sales_result.match_status == MatchStatus.MATCHED
        assert sales_result.counterparts[0].source_ref == "ledger:budget:6100:2026-03:Sales:HQ"
        assert sales_result.metadata["department"] == "Sales"
        assert sales_result.difference_amount == Decimal("-100.00")


# ---------------------------------------------------------------------------
# Trial balance checker tests
# ---------------------------------------------------------------------------


class TestTrialBalanceChecker:
    """Test trial balance validation checks."""

    def setup_method(self) -> None:
        self.checker = TrialBalanceChecker()

    def test_balanced_trial_balance(self) -> None:
        balances = [
            {"account_code": "1000", "account_name": "Cash", "account_type": "asset",
             "debit_balance": "10000.00", "credit_balance": "0.00", "is_active": True},
            {"account_code": "4000", "account_name": "Revenue", "account_type": "revenue",
             "debit_balance": "0.00", "credit_balance": "10000.00", "is_active": True},
        ]

        is_balanced, _debits, _credits, anomalies = self.checker.check_balance(balances)
        assert is_balanced
        assert _debits == Decimal("10000.00")
        assert _credits == Decimal("10000.00")
        assert len(anomalies) == 0

    def test_imbalanced_trial_balance(self) -> None:
        balances = [
            {"account_code": "1000", "account_name": "Cash", "account_type": "asset",
             "debit_balance": "10000.00", "credit_balance": "0.00", "is_active": True},
            {"account_code": "4000", "account_name": "Revenue", "account_type": "revenue",
             "debit_balance": "0.00", "credit_balance": "9000.00", "is_active": True},
        ]

        is_balanced, _debits, _credits, anomalies = self.checker.check_balance(balances)
        assert not is_balanced
        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == AnomalyType.DEBIT_CREDIT_IMBALANCE
        assert anomalies[0].severity == "blocking"

    def test_rounding_difference(self) -> None:
        balances = [
            {"account_code": "1000", "account_name": "Cash", "account_type": "asset",
             "debit_balance": "10000.01", "credit_balance": "0.00", "is_active": True},
            {"account_code": "4000", "account_name": "Revenue", "account_type": "revenue",
             "debit_balance": "0.00", "credit_balance": "10000.00", "is_active": True},
        ]

        is_balanced, _, _, anomalies = self.checker.check_balance(balances)
        # Within rounding tolerance (0.02), so is_balanced=True and rounding anomaly recorded
        assert is_balanced
        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == AnomalyType.ROUNDING_DIFFERENCE

    def test_unusual_balance_direction(self) -> None:
        balances = [
            {"account_code": "1000", "account_name": "Cash", "account_type": "asset",
             "debit_balance": "0.00", "credit_balance": "1000.00", "is_active": True},
        ]

        anomalies = self.checker.check_unusual_balances(balances)
        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == AnomalyType.UNUSUAL_ACCOUNT_BALANCE
        assert anomalies[0].account_code == "1000"

    def test_zero_balance_active_account(self) -> None:
        balances = [
            {"account_code": "1000", "account_name": "Cash", "account_type": "asset",
             "debit_balance": "0.00", "credit_balance": "0.00", "is_active": True},
        ]

        anomalies = self.checker.check_unusual_balances(balances)
        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == AnomalyType.ZERO_BALANCE_ACTIVE

    def test_non_postable_header_account_is_ignored_for_unusual_balance_checks(self) -> None:
        """Header/rollup accounts should not produce trial-balance direction noise."""

        balances = [
            {
                "account_code": "1000",
                "account_name": "Assets",
                "account_type": "asset",
                "debit_balance": "0.00",
                "credit_balance": "15480000.00",
                "is_active": True,
                "is_postable": False,
            },
        ]

        anomalies = self.checker.check_unusual_balances(balances)
        assert anomalies == []

    def test_missing_account_detection(self) -> None:
        balances = [
            {"account_code": "1000", "account_name": "Cash", "account_type": "asset",
             "debit_balance": "1000.00", "credit_balance": "0.00", "is_active": True},
        ]
        expected = {"1000", "2000", "3000"}

        anomalies = self.checker.check_missing_accounts(balances, expected)
        assert len(anomalies) == 2
        missing_codes = {a.account_code for a in anomalies}
        assert "2000" in missing_codes
        assert "3000" in missing_codes

    def test_variance_detection(self) -> None:
        current = [
            {"account_code": "5000", "account_name": "Expense", "account_type": "expense",
             "debit_balance": "15000.00", "credit_balance": "0.00", "is_active": True},
        ]
        prior = [
            {"account_code": "5000", "account_name": "Expense", "account_type": "expense",
             "debit_balance": "10000.00", "credit_balance": "0.00", "is_active": True},
        ]

        anomalies = self.checker.check_variance(current, prior, variance_threshold_pct=20.0)
        assert len(anomalies) == 1
        assert anomalies[0].anomaly_type == AnomalyType.UNEXPLAINED_VARIANCE
        assert anomalies[0].details["variance_pct"] == 50.0


# ---------------------------------------------------------------------------
# Matcher registry tests
# ---------------------------------------------------------------------------


class TestMatcherRegistry:
    """Test that all reconciliation types have registered matchers."""

    def test_all_types_have_matchers(self) -> None:
        from services.reconciliation.matchers import MATCHER_REGISTRY

        expected_types = [
            ReconciliationType.BANK_RECONCILIATION,
            ReconciliationType.AR_AGEING,
            ReconciliationType.AP_AGEING,
            ReconciliationType.INTERCOMPANY,
            ReconciliationType.PAYROLL_CONTROL,
            ReconciliationType.FIXED_ASSETS,
            ReconciliationType.LOAN_AMORTISATION,
            ReconciliationType.ACCRUAL_TRACKER,
            ReconciliationType.BUDGET_VS_ACTUAL,
        ]

        for rec_type in expected_types:
            assert rec_type in MATCHER_REGISTRY, f"Missing matcher for {rec_type.value}"

    def test_matcher_instantiation(self) -> None:
        from services.reconciliation.matchers import MATCHER_REGISTRY

        for rec_type, matcher_cls in MATCHER_REGISTRY.items():
            matcher = matcher_cls()
            assert hasattr(matcher, "match"), f"Matcher for {rec_type.value} missing 'match' method"
            assert hasattr(matcher, "reconciliation_type")
            # AgeingMatcher is reused for both AR and AP ageing, so it reports AR_AGEING
            if rec_type == ReconciliationType.AP_AGEING:
                assert matcher.reconciliation_type == ReconciliationType.AR_AGEING
            else:
                assert matcher.reconciliation_type == rec_type


__all__ = [
    "TestAccrualTrackerMatcher",
    "TestAgeingMatcher",
    "TestAmountConfidence",
    "TestBankReconciliationMatcher",
    "TestBudgetVsActualMatcher",
    "TestDateConfidence",
    "TestFixedAssetMatcher",
    "TestIntercompanyMatcher",
    "TestLoanAmortisationMatcher",
    "TestMatcherRegistry",
    "TestParseAmount",
    "TestParseDate",
    "TestPayrollControlMatcher",
    "TestReferenceSimilarity",
    "TestTrialBalanceChecker",
]
