"""
Purpose: Implement deterministic accounting rule evaluation for GL coding and treatment selection.
Scope: Document-type, vendor, threshold, cut-off, account-state, and dimension rules for journals,
accruals, prepayments, and depreciation inputs before any model-backed recommendation workflow.
Dependencies: Python dataclasses/Decimal, canonical enums, preprocessing, dimensions, and policies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from services.accounting.dimensions import DimensionHelper, get_dimension_helper
from services.accounting.policies import AccountingPolicyEngine, PolicyDecision, get_policy_engine
from services.accounting.preprocess import AccountingPreprocessor, PeriodBoundary, get_preprocessor
from services.common.enums import AccountType, DocumentType, RiskLevel


class RuleEngineError(ValueError):
    """Represent a deterministic rule evaluation failure."""


class AccountingTreatment(StrEnum):
    """Enumerate accounting treatments produced by deterministic rule evaluation."""

    ACCRUAL = "accrual"
    DEPRECIATION = "depreciation"
    PREPAYMENT = "prepayment"
    STANDARD_CODING = "standard_coding"


@dataclass(frozen=True, slots=True)
class ChartAccount:
    """Describe the subset of a COA account required for deterministic rule checks."""

    account_code: str
    account_name: str
    account_type: AccountType
    is_active: bool = True
    is_postable: bool = True
    dimension_defaults: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TransactionContext:
    """Describe normalized transaction context used by the rule engine."""

    amount: Decimal
    transaction_date: date
    period: PeriodBoundary
    document_type: DocumentType
    vendor_name: str | None = None
    description: str | None = None
    has_purchase_order: bool = False
    service_start: date | None = None
    service_end: date | None = None
    asset_useful_life_months: int | None = None
    asset_type: str | None = None
    existing_dimensions: dict[str, str] = field(default_factory=dict)
    requested_auto_apply: bool = False


@dataclass(frozen=True, slots=True)
class AccountingRuleEvaluation:
    """Describe a deterministic GL coding and treatment recommendation."""

    account: ChartAccount
    confidence: Decimal
    dimensions: dict[str, str]
    policy_decision: PolicyDecision
    reasons: tuple[str, ...]
    risk_level: RiskLevel
    rule_type: str
    treatment: AccountingTreatment


@dataclass(frozen=True, slots=True)
class VendorRule:
    """Describe one vendor-specific deterministic account rule."""

    normalized_vendor_name: str
    account_code: str
    confidence: Decimal = Decimal("0.90")
    dimension_overrides: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DocumentTypeRule:
    """Describe one document-type deterministic account rule."""

    document_type: DocumentType
    account_code: str
    confidence: Decimal = Decimal("0.80")
    requires_purchase_order: bool = False


@dataclass(frozen=True, slots=True)
class ThresholdRule:
    """Describe one amount-threshold deterministic account rule."""

    threshold: Decimal
    account_code_at_or_above: str
    account_code_below: str
    confidence: Decimal = Decimal("0.75")


@dataclass(frozen=True, slots=True)
class CutoffRule:
    """Describe the deterministic expense/prepayment account split around period end."""

    cutoff_day: int
    expense_account_code: str
    prepaid_account_code: str
    confidence: Decimal = Decimal("0.85")


class AccountingRuleEngine:
    """Evaluate deterministic accounting rules before bounded model reasoning is allowed."""

    def __init__(
        self,
        *,
        accounts: tuple[ChartAccount, ...],
        preprocessor: AccountingPreprocessor | None = None,
        dimension_helper: DimensionHelper | None = None,
        policy_engine: AccountingPolicyEngine | None = None,
    ) -> None:
        """Capture active COA context and deterministic helpers."""

        if not accounts:
            raise RuleEngineError("At least one COA account is required for rule evaluation.")
        self._accounts_by_code = {account.account_code: account for account in accounts}
        self._preprocessor = preprocessor or get_preprocessor()
        self._dimension_helper = dimension_helper or get_dimension_helper()
        self._policy_engine = policy_engine or get_policy_engine()
        self._vendor_rules: dict[str, VendorRule] = {}
        self._document_type_rules: dict[DocumentType, DocumentTypeRule] = {}
        self._threshold_rules: list[ThresholdRule] = []
        self._cutoff_rule: CutoffRule | None = None

    def add_vendor_rule(
        self,
        *,
        vendor_name: str,
        account_code: str,
        confidence: Decimal = Decimal("0.90"),
        dimension_overrides: dict[str, str] | None = None,
    ) -> None:
        """Add one vendor-specific GL coding rule."""

        self._require_usable_account(account_code=account_code)
        normalized_vendor = self._preprocessor.normalize_vendor_name(vendor_name)
        if not normalized_vendor:
            raise RuleEngineError("Vendor rules require a non-empty vendor name.")
        self._vendor_rules[normalized_vendor] = VendorRule(
            normalized_vendor_name=normalized_vendor,
            account_code=account_code,
            confidence=_validate_confidence(confidence),
            dimension_overrides=dimension_overrides or {},
        )

    def add_document_type_rule(
        self,
        *,
        document_type: DocumentType,
        account_code: str,
        confidence: Decimal = Decimal("0.80"),
        requires_purchase_order: bool = False,
    ) -> None:
        """Add one document-type GL coding rule."""

        self._require_usable_account(account_code=account_code)
        self._document_type_rules[document_type] = DocumentTypeRule(
            document_type=document_type,
            account_code=account_code,
            confidence=_validate_confidence(confidence),
            requires_purchase_order=requires_purchase_order,
        )

    def add_threshold_rule(
        self,
        *,
        threshold: Decimal,
        account_code_at_or_above: str,
        account_code_below: str,
        confidence: Decimal = Decimal("0.75"),
    ) -> None:
        """Add one amount-threshold GL coding rule."""

        if threshold < 0:
            raise RuleEngineError("Threshold rules cannot use negative thresholds.")
        self._require_usable_account(account_code=account_code_at_or_above)
        self._require_usable_account(account_code=account_code_below)
        self._threshold_rules.append(
            ThresholdRule(
                threshold=threshold,
                account_code_at_or_above=account_code_at_or_above,
                account_code_below=account_code_below,
                confidence=_validate_confidence(confidence),
            )
        )

    def set_cutoff_rule(
        self,
        *,
        cutoff_day: int,
        expense_account_code: str,
        prepaid_account_code: str,
        confidence: Decimal = Decimal("0.85"),
    ) -> None:
        """Set the deterministic cut-off rule used to route prepayments."""

        if cutoff_day < 1 or cutoff_day > 31:
            raise RuleEngineError("Cutoff day must be between 1 and 31.")
        self._require_usable_account(account_code=expense_account_code)
        self._require_usable_account(account_code=prepaid_account_code)
        self._cutoff_rule = CutoffRule(
            cutoff_day=cutoff_day,
            expense_account_code=expense_account_code,
            prepaid_account_code=prepaid_account_code,
            confidence=_validate_confidence(confidence),
        )

    def evaluate(self, *, context: TransactionContext) -> AccountingRuleEvaluation:
        """Evaluate all configured deterministic rules for one transaction context."""

        self._validate_context(context=context)
        selected_account, confidence, rule_type, reasons = self._select_account(context=context)
        treatment = self._select_treatment(context=context, account=selected_account)
        dimensions = self._dimension_helper.merge_dimensions(
            base_dimensions=selected_account.dimension_defaults,
            override_dimensions={
                **context.existing_dimensions,
                **_dimension_overrides_for(
                    vendor_rules=self._vendor_rules,
                    preprocessor=self._preprocessor,
                    context=context,
                ),
            },
        )
        policy_decision = self._policy_engine.evaluate_action(
            amount=context.amount,
            account_code=selected_account.account_code,
            account_type=selected_account.account_type,
            document_type=context.document_type.value,
            requested_auto_apply=context.requested_auto_apply,
        )
        return AccountingRuleEvaluation(
            account=selected_account,
            confidence=confidence,
            dimensions=dimensions,
            policy_decision=policy_decision,
            reasons=tuple(reasons),
            risk_level=policy_decision.risk_level,
            rule_type=rule_type,
            treatment=treatment,
        )

    def _select_account(
        self,
        *,
        context: TransactionContext,
    ) -> tuple[ChartAccount, Decimal, str, list[str]]:
        """Select the best deterministic account rule in priority order."""

        vendor_key = self._preprocessor.normalize_vendor_name(context.vendor_name)
        if vendor_key in self._vendor_rules:
            vendor_rule = self._vendor_rules[vendor_key]
            return (
                self._require_usable_account(account_code=vendor_rule.account_code),
                vendor_rule.confidence,
                "vendor",
                [f"Vendor rule matched {context.vendor_name}."],
            )

        if context.document_type in self._document_type_rules:
            document_type_rule = self._document_type_rules[context.document_type]
            confidence = document_type_rule.confidence
            reasons = [f"Document type rule matched {context.document_type.value}."]
            if document_type_rule.requires_purchase_order and not context.has_purchase_order:
                confidence = max(Decimal("0.01"), confidence - Decimal("0.15"))
                reasons.append("Confidence reduced because purchase-order evidence is missing.")
            return (
                self._require_usable_account(account_code=document_type_rule.account_code),
                confidence,
                "document_type",
                reasons,
            )

        for threshold_rule in sorted(
            self._threshold_rules,
            key=lambda item: item.threshold,
            reverse=True,
        ):
            account_code = (
                threshold_rule.account_code_at_or_above
                if context.amount >= threshold_rule.threshold
                else threshold_rule.account_code_below
            )
            return (
                self._require_usable_account(account_code=account_code),
                threshold_rule.confidence,
                "threshold",
                [f"Threshold rule compared {context.amount} against {threshold_rule.threshold}."],
            )

        raise RuleEngineError("No deterministic GL coding rule matched this transaction.")

    def _select_treatment(
        self,
        *,
        context: TransactionContext,
        account: ChartAccount,
    ) -> AccountingTreatment:
        """Select deterministic accrual, prepayment, depreciation, or standard treatment."""

        if context.asset_useful_life_months is not None:
            is_valid, message = self._policy_engine.validate_depreciation_life(
                useful_life_months=context.asset_useful_life_months,
                asset_type=context.asset_type,
            )
            if not is_valid:
                raise RuleEngineError(message)
            return AccountingTreatment.DEPRECIATION

        if context.service_start is not None and context.service_end is not None:
            is_valid, message = self._policy_engine.validate_accrual_period(
                service_start=context.service_start,
                service_end=context.service_end,
                accounting_period_start=context.period.period_start,
                accounting_period_end=context.period.period_end,
            )
            if not is_valid:
                raise RuleEngineError(message)
            if context.service_start > context.period.period_end:
                return AccountingTreatment.PREPAYMENT
            if context.service_end < context.period.period_start:
                return AccountingTreatment.ACCRUAL

        if (
            self._cutoff_rule is not None
            and context.transaction_date.day >= self._cutoff_rule.cutoff_day
        ):
            prepaid_account = self._require_usable_account(
                account_code=self._cutoff_rule.prepaid_account_code
            )
            if account.account_code == prepaid_account.account_code:
                return AccountingTreatment.PREPAYMENT

        return AccountingTreatment.STANDARD_CODING

    def _validate_context(self, *, context: TransactionContext) -> None:
        """Validate transaction context before rule selection."""

        if context.amount < 0:
            raise RuleEngineError("Transaction amount cannot be negative.")
        if not context.period.contains(context.transaction_date):
            raise RuleEngineError("Transaction date is outside the close-run accounting period.")

    def _require_usable_account(self, *, account_code: str) -> ChartAccount:
        """Return an active postable account or fail fast with recovery guidance."""

        account = self._accounts_by_code.get(account_code)
        if account is None:
            raise RuleEngineError(f"Account {account_code} is not in the active COA set.")
        if not account.is_active:
            raise RuleEngineError(f"Account {account_code} is inactive and cannot be suggested.")
        if not account.is_postable:
            raise RuleEngineError(f"Account {account_code} is not postable.")
        return account


def get_rule_engine(*, accounts: tuple[ChartAccount, ...]) -> AccountingRuleEngine:
    """Create a deterministic accounting rule engine for one active COA context."""

    return AccountingRuleEngine(accounts=accounts)


def _dimension_overrides_for(
    *,
    vendor_rules: dict[str, VendorRule],
    preprocessor: AccountingPreprocessor,
    context: TransactionContext,
) -> dict[str, str]:
    """Return dimension overrides from the matched vendor rule, when present."""

    vendor_key = preprocessor.normalize_vendor_name(context.vendor_name)
    rule = vendor_rules.get(vendor_key)
    return dict(rule.dimension_overrides) if rule is not None else {}


def _validate_confidence(value: Decimal) -> Decimal:
    """Validate a deterministic rule confidence value."""

    if value < 0 or value > 1:
        raise RuleEngineError("Rule confidence must be between 0 and 1.")
    return value.quantize(Decimal("0.0001"))


__all__ = [
    "AccountingRuleEngine",
    "AccountingRuleEvaluation",
    "AccountingTreatment",
    "ChartAccount",
    "CutoffRule",
    "DocumentTypeRule",
    "RuleEngineError",
    "ThresholdRule",
    "TransactionContext",
    "VendorRule",
    "get_rule_engine",
]
