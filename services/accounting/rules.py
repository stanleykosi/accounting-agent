"""
Purpose: Implement deterministic accounting rule engine for transaction classification.
Scope: Rule evaluation for document type, vendor, thresholds, cut-off logic, and risky-action restrictions.
Dependencies: Preprocessing module, shared enums, settings.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from services.accounting.preprocess import AccountingPreprocessor, get_preprocessor
from services.common.enums import DocumentType, RiskLevel
from services.common.settings import AppSettings, get_settings


class RuleEngineError(Exception):
    """Raised when rule engine evaluation fails."""


class AccountingRuleEngine:
    """Evaluate accounting rules for transaction classification and GL coding suggestions."""

    def __init__(self, settings: AppSettings | None = None):
        self.settings = settings or get_settings()
        self.preprocessor = get_preprocessor()
        self._vendor_rules: dict[str, dict[str, Any]] = {}
        self._threshold_rules: list[dict[str, Any]] = []
        self._cutoff_rules: dict[str, Any] = {}
        self._document_type_rules: dict[DocumentType, dict[str, Any]] = {}

    def add_vendor_rule(
        self,
        vendor_name: str,
        default_account: str,
        confidence: Decimal = Decimal("0.9"),
        dimensions: dict[str, str] | None = None,
    ) -> None:
        """
        Add a vendor-specific rule for GL account suggestion.

        Args:
            vendor_name: Normalized vendor name
            default_account: Default GL account code for this vendor
            confidence: Confidence level for this rule (0-1)
            dimensions: Optional default dimensions (cost centre, department, project)
        """
        normalized_vendor = self.preprocessor.normalize_vendor_name(vendor_name)
        self._vendor_rules[normalized_vendor] = {
            "default_account": default_account,
            "confidence": confidence,
            "dimensions": dimensions or {},
        }

    def add_threshold_rule(
        self,
        amount_threshold: Decimal,
        account_above: str,
        account_below: str,
        confidence: Decimal = Decimal("0.8"),
    ) -> None:
        """
        Add a threshold-based rule for GL account suggestion.

        Args:
            amount_threshold: Transaction amount threshold
            account_above: Account to use when amount >= threshold
            account_below: Account to use when amount < threshold
            confidence: Confidence level for this rule (0-1)
        """
        self._threshold_rules.append(
            {
                "amount_threshold": amount_threshold,
                "account_above": account_above,
                "account_below": account_below,
                "confidence": confidence,
            }
        )

    def add_cutoff_rule(
        self,
        cutoff_day: int,
        expense_account: str,
        prepaid_account: str,
        confidence: Decimal = Decimal("0.85"),
    ) -> None:
        """
        Add a cut-off rule for period-end expenses.

        Args:
            cutoff_day: Day of month after which expenses are considered prepaid
            expense_account: Account for regular expenses
            prepaid_account: Account for prepaid expenses
            confidence: Confidence level for this rule (0-1)
        """
        self._cutoff_rules = {
            "cutoff_day": cutoff_day,
            "expense_account": expense_account,
            "prepaid_account": prepaid_account,
            "confidence": confidence,
        }

    def add_document_type_rule(
        self,
        doc_type: DocumentType,
        default_account: str,
        confidence: Decimal = Decimal("0.8"),
        requires_po: bool = False,
    ) -> None:
        """
        Add a document type rule for GL account suggestion.

        Args:
            doc_type: Type of document (invoice, receipt, etc.)
            default_account: Default GL account for this document type
            confidence: Confidence level for this rule (0-1)
            requires_po: Whether this document type requires a purchase order
        """
        self._document_type_rules[doc_type] = {
            "default_account": default_account,
            "confidence": confidence,
            "requires_po": requires_po,
        }

    def suggest_gl_account(
        self,
        amount: Decimal,
        date: date,
        vendor: str | None = None,
        document_type: DocumentType | None = None,
        has_po: bool = False,
    ) -> dict[str, Any]:
        """
        Suggest a GL account based on available rules.

        Args:
            amount: Transaction amount
            date: Transaction date
            vendor: Vendor name (optional)
            document_type: Type of document (optional)
            has_po: Whether a purchase order exists (optional)

        Returns:
            Dictionary with suggested account, confidence, and reasoning
        """
        # Start with lowest confidence
        best_suggestion = {
            "account": None,
            "confidence": Decimal("0"),
            "reasoning": "No matching rule found",
            "rule_type": "none",
        }

        # Check vendor rules first (highest priority)
        if vendor:
            normalized_vendor = self.preprocessor.normalize_vendor_name(vendor)
            if normalized_vendor in self._vendor_rules:
                rule = self._vendor_rules[normalized_vendor]
                if rule["confidence"] > best_suggestion["confidence"]:
                    best_suggestion = {
                        "account": rule["default_account"],
                        "confidence": rule["confidence"],
                        "reasoning": f"Vendor-specific rule for {vendor}",
                        "rule_type": "vendor",
                        "dimensions": rule["dimensions"],
                    }

        # Check document type rules
        if document_type and document_type in self._document_type_rules:
            rule = self._document_type_rules[document_type]
            # Adjust confidence based on PO requirement
            adjusted_confidence = rule["confidence"]
            if rule["requires_po"] and not has_po:
                adjusted_confidence *= Decimal(
                    "0.8"
                )  # Reduce confidence if PO required but missing
            elif not rule["requires_po"] and has_po:
                adjusted_confidence *= Decimal(
                    "0.9"
                )  # Slightly reduce if PO present but not required

            if adjusted_confidence > best_suggestion["confidence"]:
                best_suggestion = {
                    "account": rule["default_account"],
                    "confidence": adjusted_confidence,
                    "reasoning": f"Document type rule for {document_type.value}",
                    "rule_type": "document_type",
                }

        # Check threshold rules
        for rule in self._threshold_rules:
            if amount >= rule["amount_threshold"]:
                confidence = rule["confidence"]
                account = rule["account_above"]
            else:
                confidence = rule["confidence"]
                account = rule["account_below"]

            if confidence > best_suggestion["confidence"]:
                best_suggestion = {
                    "account": account,
                    "confidence": confidence,
                    "reasoning": f"Threshold rule (amount {amount} vs {rule['amount_threshold']})",
                    "rule_type": "threshold",
                }

        # Check cutoff rule (for period-end adjustments)
        if self._cutoff_rules and date.day >= self._cutoff_rules["cutoff_day"]:
            rule = self._cutoff_rules
            if rule["confidence"] > best_suggestion["confidence"]:
                best_suggestion = {
                    "account": rule["prepaid_account"],
                    "confidence": rule["confidence"],
                    "reasoning": f"Cutoff rule (day {date.day} >= {rule['cutoff_day']})",
                    "rule_type": "cutoff",
                }
        elif self._cutoff_rules:
            rule = self._cutoff_rules
            if rule["confidence"] > best_suggestion["confidence"]:
                best_suggestion = {
                    "account": rule["expense_account"],
                    "confidence": rule["confidence"],
                    "reasoning": f"Cutoff rule (day {date.day} < {rule['cutoff_day']})",
                    "rule_type": "cutoff",
                }

        return best_suggestion

    def evaluate_risk_level(
        self,
        amount: Decimal,
        account: str,
        document_type: DocumentType | None = None,
    ) -> RiskLevel:
        """
        Evaluate risk level for a transaction based on amount, account, and document type.

        Args:
            amount: Transaction amount
            account: GL account code
            document_type: Type of document (optional)

        Returns:
            Risk level enum
        """
        # High risk thresholds
        high_risk_amount = Decimal("1000000")  # 1,000,000
        medium_risk_amount = Decimal("100000")  # 100,000

        # Check amount-based risk
        if amount >= high_risk_amount:
            return RiskLevel.HIGH
        elif amount >= medium_risk_amount:
            return RiskLevel.MEDIUM

        # Check account-based risk (simplified)
        risky_accounts = {"CASH", "BANK", "CAPITAL", "DRAWINGS"}
        if account.upper() in risky_accounts:
            if amount >= Decimal("10000"):  # 10,000 for risky accounts
                return RiskLevel.HIGH
            elif amount >= Decimal("1000"):  # 1,000 for risky accounts
                return RiskLevel.MEDIUM

        # Check document type risk
        high_risk_docs = {DocumentType.JOURNAL, DocumentType.CONTRACT}
        if document_type in high_risk_docs:
            if amount >= Decimal("50000"):
                return RiskLevel.HIGH
            elif amount >= Decimal("10000"):
                return RiskLevel.MEDIUM

        return RiskLevel.LOW

    def validate_period_boundary(
        self,
        transaction_date: date,
        period_start: date,
        period_end: date,
    ) -> tuple[bool, str]:
        """
        Validate that a transaction falls within the accounting period.

        Args:
            transaction_date: Date of the transaction
            period_start: Start of accounting period
            period_end: End of accounting period

        Returns:
            Tuple of (is_valid, error_message)
        """
        if transaction_date < period_start:
            return (
                False,
                f"Transaction date {transaction_date} is before period start {period_start}",
            )
        if transaction_date > period_end:
            return False, f"Transaction date {transaction_date} is after period end {period_end}"
        return True, "Transaction date is within period"


def get_rule_engine() -> AccountingRuleEngine:
    """Factory function to create an AccountingRuleEngine instance."""
    return AccountingRuleEngine()


__all__ = [
    "RuleEngineError",
    "AccountingRuleEngine",
    "get_rule_engine",
]
