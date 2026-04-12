"""
Purpose: Implement risky-action restrictions and policy checks for accounting operations.
Scope: Policy evaluation for preventing risky actions based on amounts, accounts, and contexts.
Dependencies: Preprocessing module, shared enums, settings.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from services.accounting.preprocess import AccountingPreprocessor, get_preprocessor
from services.common.enums import RiskLevel, AccountType
from services.common.settings import AppSettings, get_settings


class PolicyError(Exception):
    """Raised when policy validation fails."""


class AccountingPolicyEngine:
    """Evaluate accounting policies to restrict risky actions."""

    def __init__(self, settings: AppSettings | None = None):
        self.settings = settings or get_settings()
        self.preprocessor = get_preprocessor()

        # Load policy settings from configuration
        self._risk_thresholds = {
            RiskLevel.LOW: Decimal(str(self.settings.get("policy_low_risk_threshold", "1000"))),
            RiskLevel.MEDIUM: Decimal(
                str(self.settings.get("policy_medium_risk_threshold", "10000"))
            ),
            RiskLevel.HIGH: Decimal(str(self.settings.get("policy_high_risk_threshold", "100000"))),
        }

        # Restricted accounts that require additional approval
        self._restricted_accounts = set(
            acc.strip().upper()
            for acc in self.settings.get(
                "policy_restricted_accounts", "CASH,BANK,CAPITAL,DRAWINGS"
            ).split(",")
            if acc.strip()
        )

        # Require dual approval for these account types
        self._dual_approval_account_types = set(
            acc_type.strip().upper()
            for acc_type in self.settings.get(
                "policy_dual_approval_account_types", "EQUITY,LIABILITY"
            ).split(",")
            if acc_type.strip()
        )

        # Maximum transaction amounts by risk level (above these require manual review)
        self._max_amounts = {
            RiskLevel.LOW: Decimal(str(self.settings.get("policy_max_low_risk_amount", "50000"))),
            RiskLevel.MEDIUM: Decimal(
                str(self.settings.get("policy_max_medium_risk_amount", "200000"))
            ),
            RiskLevel.HIGH: Decimal(
                str(self.settings.get("policy_max_high_risk_amount", "1000000"))
            ),
        }

    def check_transaction_risk(
        self,
        amount: Decimal,
        account: str,
        account_type: AccountType | None = None,
        document_type: str | None = None,
    ) -> dict[str, Any]:
        """
        Check if a transaction violates any policies and determine required actions.

        Args:
            amount: Transaction amount
            account: GL account code
            account_type: Type of account (optional)
            document_type: Type of document (optional)

        Returns:
            Dictionary with policy evaluation results
        """
        # Normalize inputs
        normalized_account = account.upper().strip() if account else ""

        # Determine risk level based on amount and account
        risk_level = self._assess_risk_level(
            amount, normalized_account, account_type, document_type
        )

        # Check if account is restricted
        is_restricted = normalized_account in self._restricted_accounts

        # Check if dual approval is required
        requires_dual_approval = self._requires_dual_approval(account_type, normalized_account)

        # Check amount limits
        max_allowed = self._max_amounts.get(risk_level, Decimal("0"))
        exceeds_limit = amount > max_allowed if max_allowed > 0 else False

        # Determine if manual review is required
        requires_manual_review = (
            is_restricted or requires_dual_approval or exceeds_limit or risk_level == RiskLevel.HIGH
        )

        # Determine required approval level
        approval_level = self._determine_approval_level(
            risk_level, is_restricted, requires_dual_approval, exceeds_limit
        )

        # Generate policy explanation
        explanation = self._generate_policy_explanation(
            amount,
            normalized_account,
            risk_level,
            is_restricted,
            requires_dual_approval,
            exceeds_limit,
        )

        return {
            "amount": amount,
            "account": normalized_account,
            "risk_level": risk_level.value,
            "is_restricted": is_restricted,
            "requires_dual_approval": requires_dual_approval,
            "exceeds_limit": exceeds_limit,
            "max_allowed": max_allowed,
            "requires_manual_review": requires_manual_review,
            "approval_level": approval_level,
            "explanation": explanation,
            "can_proceed_automatically": not requires_manual_review
            and risk_level != RiskLevel.HIGH,
        }

    def _assess_risk_level(
        self,
        amount: Decimal,
        account: str,
        account_type: AccountType | None = None,
        document_type: str | None = None,
    ) -> RiskLevel:
        """Assess risk level based on amount, account, and context."""
        # Start with amount-based risk
        if amount >= self._risk_thresholds[RiskLevel.HIGH]:
            risk_level = RiskLevel.HIGH
        elif amount >= self._risk_thresholds[RiskLevel.MEDIUM]:
            risk_level = RiskLevel.MEDIUM
        else:
            risk_level = RiskLevel.LOW

        # Increase risk for certain account types
        high_risk_account_types = {AccountType.ASSET, AccountType.LIABILITY}
        if (
            account_type in high_risk_account_types
            and amount >= self._risk_thresholds[RiskLevel.MEDIUM]
        ):
            risk_level = RiskLevel.HIGH
        elif (
            account_type in high_risk_account_types
            and amount >= self._risk_thresholds[RiskLevel.LOW]
        ):
            if risk_level == RiskLevel.LOW:
                risk_level = RiskLevel.MEDIUM

        # Increase risk for transaction types that are inherently risky
        high_risk_doc_types = {"JOURNAL_ENTRY", "CONTRACT", "LOAN"}
        if document_type and document_type.upper() in high_risk_doc_types:
            if amount >= self._risk_thresholds[RiskLevel.LOW]:
                if risk_level == RiskLevel.LOW:
                    risk_level = RiskLevel.MEDIUM
                elif risk_level == RiskLevel.MEDIUM:
                    risk_level = RiskLevel.HIGH

        return risk_level

    def _requires_dual_approval(
        self,
        account_type: AccountType | None = None,
        account: str = "",
    ) -> bool:
        """Check if transaction requires dual approval based on account."""
        # Check by account type
        if account_type:
            if account_type.value.upper() in self._dual_approval_account_types:
                return True

        # Check by specific account patterns
        dual_approval_patterns = self.settings.get("policy_dual_approval_patterns", "").split(",")
        for pattern in dual_approval_patterns:
            pattern = pattern.strip().upper()
            if pattern and pattern in account:
                return True

        return False

    def _determine_approval_level(
        self,
        risk_level: RiskLevel,
        is_restricted: bool,
        requires_dual_approval: bool,
        exceeds_limit: bool,
    ) -> str:
        """Determine the required approval level for a transaction."""
        if is_restricted or exceeds_limit or risk_level == RiskLevel.HIGH:
            return "senior_management"
        elif requires_dual_approval or risk_level == RiskLevel.MEDIUM:
            return "supervisor"
        else:
            return "standard"

    def _generate_policy_explanation(
        self,
        amount: Decimal,
        account: str,
        risk_level: RiskLevel,
        is_restricted: bool,
        requires_dual_approval: bool,
        exceeds_limit: bool,
    ) -> str:
        """Generate human-readable explanation of policy evaluation."""
        parts = []

        parts.append(f"Amount: {amount}")
        parts.append(f"Account: {account}")
        parts.append(f"Risk level: {risk_level.value}")

        if is_restricted:
            parts.append("Account is restricted (requires special approval)")

        if requires_dual_approval:
            parts.append("Transaction requires dual approval")

        if exceeds_limit:
            max_allowed = self._max_amounts.get(risk_level, Decimal("0"))
            parts.append(f"Amount exceeds {risk_level.value} risk limit of {max_allowed}")

        if risk_level == RiskLevel.HIGH:
            parts.append("High risk transaction - requires manual review")
        elif risk_level == RiskLevel.MEDIUM and not requires_dual_approval and not is_restricted:
            parts.append("Medium risk transaction - standard approval required")
        else:
            parts.append("Low risk transaction - can proceed with standard approval")

        return "; ".join(parts)

    def validate_accrual_period(
        self,
        start_date: date,
        end_date: date,
        accounting_period_start: date,
        accounting_period_end: date,
    ) -> tuple[bool, str]:
        """
        Validate that an accrual period is reasonable and within policy.

        Args:
            start_date: Accrual start date
            end_date: Accrual end date
            accounting_period_start: Accounting period start
            accounting_period_end: Accounting period end

        Returns:
            Tuple of (is_valid, explanation)
        """
        # Check that dates are in order
        if start_date > end_date:
            return False, "Accrual start date cannot be after end date"

        # Check that accrual doesn't extend too far beyond accounting period
        # Allow accruals to span at most one future period
        from datetime import timedelta

        max_future_extension = timedelta(days=60)  # Approximately 2 months

        if end_date > accounting_period_end + max_future_extension:
            return (
                False,
                f"Accrual extends too far beyond accounting period (max {max_future_extension.days} days allowed)",
            )

        # Check that accrual doesn't start too far in the past
        max_past_extension = timedelta(days=365)  # 1 year
        if start_date < accounting_period_start - max_past_extension:
            return (
                False,
                f"Accrual starts too far before accounting period (max {max_past_extension.days} days allowed)",
            )

        return True, "Accrual period is within policy limits"

    def validate_depreciation_life(
        self,
        asset_life_months: int,
        asset_type: str | None = None,
    ) -> tuple[bool, str]:
        """
        Validate that depreciation life is reasonable according to policy.

        Args:
            asset_life_months: Asset useful life in months
            asset_type: Type of asset (optional)

        Returns:
            Tuple of (is_valid, explanation)
        """
        # Reasonable bounds for asset life
        min_life_months = 6  # 6 months minimum
        max_life_months = 600  # 50 years maximum

        if asset_life_months < min_life_months:
            return (
                False,
                f"Asset life too short: {asset_life_months} months (minimum {min_life_months})",
            )

        if asset_life_months > max_life_months:
            return (
                False,
                f"Asset life too long: {asset_life_months} months (maximum {max_life_months})",
            )

        # Check for common asset types
        if asset_type:
            asset_limits = {
                "COMPUTER": (12, 60),  # 1-5 years
                "VEHICLE": (24, 84),  # 2-7 years
                "OFFICE_EQUIPMENT": (36, 120),  # 3-10 years
                "FURNITURE": (60, 240),  # 5-20 years
                "BUILDING": (240, 600),  # 20-50 years
            }

            asset_type_upper = asset_type.upper()
            if asset_type_upper in asset_limits:
                min_life, max_life = asset_limits[asset_type_upper]
                if asset_life_months < min_life:
                    return (
                        False,
                        f"{asset_type} life too short: {asset_life_months} months (minimum {min_life} for this asset type)",
                    )
                if asset_life_months > max_life:
                    return (
                        False,
                        f"{asset_type} life too long: {asset_life_months} months (maximum {max_life} for this asset type)",
                    )

        return True, f"Asset life of {asset_life_months} months is within policy limits"


def get_policy_engine() -> AccountingPolicyEngine:
    """Factory function to create an AccountingPolicyEngine instance."""
    return AccountingPolicyEngine()


__all__ = [
    "PolicyError",
    "AccountingPolicyEngine",
    "get_policy_engine",
]
