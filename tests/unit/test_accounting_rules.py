"""
Purpose: Verify deterministic accounting rule evaluation for GL coding, dimensions, treatments,
and policy routing.
Scope: Vendor/document/threshold precedence, account validation, prepayment treatment, and risky
action restrictions without model calls.
Dependencies: Accounting rules, policies, preprocessing, and canonical domain enums.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from services.accounting.preprocess import AccountingPreprocessor
from services.accounting.rules import (
    AccountingRuleEngine,
    AccountingTreatment,
    ChartAccount,
    RuleEngineError,
    TransactionContext,
)
from services.common.enums import AccountType, DocumentType, RiskLevel


def test_vendor_rule_wins_and_assigns_dimensions() -> None:
    """Ensure vendor-specific rules take priority and merge default dimensions."""

    engine = AccountingRuleEngine(accounts=_accounts())
    engine.add_vendor_rule(
        vendor_name="Acme Logistics Ltd",
        account_code="6010",
        confidence=Decimal("0.93"),
        dimension_overrides={"department": "logistics"},
    )
    engine.add_document_type_rule(document_type=DocumentType.INVOICE, account_code="6020")

    evaluation = engine.evaluate(
        context=TransactionContext(
            amount=Decimal("12500.00"),
            document_type=DocumentType.INVOICE,
            period=AccountingPreprocessor().normalize_period_boundary(
                period_start=date(2026, 3, 1),
                period_end=date(2026, 3, 31),
            ),
            transaction_date=date(2026, 3, 14),
            vendor_name="ACME Logistics Limited",
        )
    )

    assert evaluation.account.account_code == "6010"
    assert evaluation.rule_type == "vendor"
    assert evaluation.confidence == Decimal("0.9300")
    assert evaluation.dimensions["department"] == "LOGISTICS"
    assert evaluation.policy_decision.requires_manual_review is True
    assert evaluation.risk_level is RiskLevel.MEDIUM


def test_inactive_or_nonpostable_account_cannot_be_suggested() -> None:
    """Ensure the engine fails fast if a rule targets an unusable COA account."""

    engine = AccountingRuleEngine(accounts=_accounts())
    with pytest.raises(RuleEngineError, match="inactive"):
        engine.add_document_type_rule(
            document_type=DocumentType.RECEIPT,
            account_code="9999",
        )

    with pytest.raises(RuleEngineError, match="not postable"):
        engine.add_threshold_rule(
            threshold=Decimal("100.00"),
            account_code_at_or_above="6000",
            account_code_below="6020",
        )


def test_prepayment_treatment_uses_service_period_and_policy_limits() -> None:
    """Ensure future service periods are classified as prepayments deterministically."""

    engine = AccountingRuleEngine(accounts=_accounts())
    engine.add_document_type_rule(document_type=DocumentType.INVOICE, account_code="1040")

    evaluation = engine.evaluate(
        context=TransactionContext(
            amount=Decimal("5000.00"),
            document_type=DocumentType.INVOICE,
            period=AccountingPreprocessor().normalize_period_boundary(
                period_start=date(2026, 3, 1),
                period_end=date(2026, 3, 31),
            ),
            service_start=date(2026, 4, 1),
            service_end=date(2026, 4, 30),
            transaction_date=date(2026, 3, 28),
            vendor_name="RentCo",
        )
    )

    assert evaluation.account.account_code == "1040"
    assert evaluation.treatment is AccountingTreatment.PREPAYMENT
    assert evaluation.policy_decision.can_apply_automatically is False


def _accounts() -> tuple[ChartAccount, ...]:
    """Return a compact active COA context for accounting rule tests."""

    return (
        ChartAccount(
            account_code="1040",
            account_name="Prepaid Expenses",
            account_type=AccountType.ASSET,
            dimension_defaults={"cost_centre": "facilities"},
        ),
        ChartAccount(
            account_code="6000",
            account_name="Operating Expenses",
            account_type=AccountType.EXPENSE,
            is_postable=False,
        ),
        ChartAccount(
            account_code="6010",
            account_name="Logistics Expense",
            account_type=AccountType.EXPENSE,
            dimension_defaults={"cost_centre": "operations"},
        ),
        ChartAccount(
            account_code="6020",
            account_name="Rent Expense",
            account_type=AccountType.EXPENSE,
        ),
        ChartAccount(
            account_code="9999",
            account_name="Inactive Suspense",
            account_type=AccountType.EXPENSE,
            is_active=False,
        ),
    )
