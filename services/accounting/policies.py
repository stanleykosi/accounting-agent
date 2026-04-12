"""
Purpose: Enforce deterministic policy gates for risky accounting actions.
Scope: Amount thresholds, restricted accounts, dual-approval checks, accrual/prepayment/
depreciation limits, and auto-application eligibility.
Dependencies: Python dataclasses, Decimal, canonical account/risk enums, and period helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from services.common.enums import AccountType, RiskLevel


class PolicyError(ValueError):
    """Represent an invalid policy input or configuration."""


class ApprovalLevel(StrEnum):
    """Enumerate deterministic approval levels returned by policy checks."""

    STANDARD = "standard"
    SUPERVISOR = "supervisor"
    SENIOR_MANAGEMENT = "senior_management"


@dataclass(frozen=True, slots=True)
class RiskPolicySettings:
    """Describe policy thresholds and restricted account settings."""

    low_threshold: Decimal = Decimal("1000.00")
    medium_threshold: Decimal = Decimal("10000.00")
    high_threshold: Decimal = Decimal("100000.00")
    max_auto_amount: Decimal = Decimal("50000.00")
    restricted_account_codes: frozenset[str] = frozenset({"1000", "1010", "CASH", "BANK"})
    dual_approval_account_types: frozenset[AccountType] = frozenset(
        {AccountType.EQUITY, AccountType.LIABILITY}
    )
    risky_document_types: frozenset[str] = frozenset({"contract", "journal", "loan"})
    max_accrual_future_days: int = 60
    max_accrual_past_days: int = 365
    depreciation_life_bounds_by_asset_type: dict[str, tuple[int, int]] = field(
        default_factory=lambda: {
            "building": (240, 600),
            "computer": (12, 60),
            "furniture": (60, 240),
            "office_equipment": (36, 120),
            "vehicle": (24, 84),
        }
    )

    def __post_init__(self) -> None:
        """Validate threshold ordering and period guard values."""

        if not (self.low_threshold <= self.medium_threshold <= self.high_threshold):
            raise PolicyError("Risk thresholds must be ordered low <= medium <= high.")
        if self.max_auto_amount < 0:
            raise PolicyError("Maximum automatic amount cannot be negative.")
        if self.max_accrual_future_days < 0 or self.max_accrual_past_days < 0:
            raise PolicyError("Accrual day limits cannot be negative.")


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Describe a deterministic policy decision for one accounting action."""

    risk_level: RiskLevel
    approval_level: ApprovalLevel
    requires_manual_review: bool
    can_apply_automatically: bool
    reasons: tuple[str, ...]


class AccountingPolicyEngine:
    """Evaluate risky-action restrictions before recommendations can update working state."""

    def __init__(self, *, settings: RiskPolicySettings | None = None) -> None:
        """Capture immutable policy settings for deterministic evaluation."""

        self._settings = settings or RiskPolicySettings()

    def evaluate_action(
        self,
        *,
        amount: Decimal,
        account_code: str,
        account_type: AccountType,
        document_type: str | None,
        requested_auto_apply: bool,
    ) -> PolicyDecision:
        """Evaluate one proposed accounting action against risk and auto-apply policy gates."""

        normalized_account = account_code.strip().upper()
        if not normalized_account:
            raise PolicyError("Account code is required for policy evaluation.")
        if amount < 0:
            raise PolicyError("Policy evaluation amount cannot be negative.")

        reasons: list[str] = []
        risk_level = self.assess_risk_level(
            amount=amount,
            account_code=normalized_account,
            account_type=account_type,
            document_type=document_type,
        )
        if normalized_account in self._settings.restricted_account_codes:
            reasons.append(f"Account {normalized_account} is restricted.")
        if account_type in self._settings.dual_approval_account_types:
            reasons.append(f"{account_type.label} accounts require dual approval.")
        if amount > self._settings.max_auto_amount:
            reasons.append(f"Amount {amount} exceeds the automatic-apply limit.")
        if risk_level is RiskLevel.HIGH:
            reasons.append("High-risk accounting actions require manual review.")

        requires_manual_review = bool(reasons) or risk_level is not RiskLevel.LOW
        can_apply_automatically = requested_auto_apply and not requires_manual_review
        return PolicyDecision(
            risk_level=risk_level,
            approval_level=_approval_level_for(
                risk_level=risk_level,
                requires_manual_review=requires_manual_review,
            ),
            requires_manual_review=requires_manual_review,
            can_apply_automatically=can_apply_automatically,
            reasons=tuple(reasons) or ("No policy restrictions were triggered.",),
        )

    def assess_risk_level(
        self,
        *,
        amount: Decimal,
        account_code: str,
        account_type: AccountType,
        document_type: str | None,
    ) -> RiskLevel:
        """Assess deterministic risk from amount, account family, and document context."""

        if amount >= self._settings.high_threshold:
            risk_level = RiskLevel.HIGH
        elif amount >= self._settings.medium_threshold:
            risk_level = RiskLevel.MEDIUM
        else:
            risk_level = RiskLevel.LOW

        if account_type in {AccountType.ASSET, AccountType.LIABILITY, AccountType.EQUITY}:
            risk_level = _raise_at_least(risk_level=risk_level, floor=RiskLevel.MEDIUM)

        if document_type is not None and document_type.strip().lower() in (
            self._settings.risky_document_types
        ):
            risk_level = _raise_at_least(risk_level=risk_level, floor=RiskLevel.MEDIUM)

        if account_code.strip().upper() in self._settings.restricted_account_codes:
            risk_level = _raise_at_least(risk_level=risk_level, floor=RiskLevel.MEDIUM)

        return risk_level

    def validate_accrual_period(
        self,
        *,
        service_start: date,
        service_end: date,
        accounting_period_start: date,
        accounting_period_end: date,
    ) -> tuple[bool, str]:
        """Validate whether an accrual service period fits policy limits."""

        if service_end < service_start:
            return False, "Accrual service end cannot be before service start."
        max_future_date = accounting_period_end + timedelta(
            days=self._settings.max_accrual_future_days
        )
        if service_end > max_future_date:
            return False, "Accrual service period extends too far beyond the accounting period."
        min_past_date = accounting_period_start - timedelta(
            days=self._settings.max_accrual_past_days
        )
        if service_start < min_past_date:
            return False, "Accrual service period starts too far before the accounting period."
        return True, "Accrual service period is within policy limits."

    def validate_depreciation_life(
        self,
        *,
        useful_life_months: int,
        asset_type: str | None = None,
    ) -> tuple[bool, str]:
        """Validate useful life assumptions for depreciation workflows."""

        if useful_life_months <= 0:
            return False, "Useful life must be a positive number of months."
        normalized_asset_type = (asset_type or "").strip().lower().replace(" ", "_")
        minimum, maximum = self._settings.depreciation_life_bounds_by_asset_type.get(
            normalized_asset_type,
            (6, 600),
        )
        if useful_life_months < minimum:
            return False, f"Useful life is below the {minimum}-month policy minimum."
        if useful_life_months > maximum:
            return False, f"Useful life is above the {maximum}-month policy maximum."
        return True, "Useful life is within depreciation policy limits."


def get_policy_engine() -> AccountingPolicyEngine:
    """Create the deterministic accounting policy engine."""

    return AccountingPolicyEngine()


def _approval_level_for(
    *,
    risk_level: RiskLevel,
    requires_manual_review: bool,
) -> ApprovalLevel:
    """Resolve the required approval level from risk and review requirements."""

    if risk_level is RiskLevel.HIGH:
        return ApprovalLevel.SENIOR_MANAGEMENT
    if requires_manual_review or risk_level is RiskLevel.MEDIUM:
        return ApprovalLevel.SUPERVISOR
    return ApprovalLevel.STANDARD


def _raise_at_least(*, risk_level: RiskLevel, floor: RiskLevel) -> RiskLevel:
    """Raise a risk level to a minimum floor without lowering already-higher risk."""

    order = {
        RiskLevel.LOW: 1,
        RiskLevel.MEDIUM: 2,
        RiskLevel.HIGH: 3,
    }
    return floor if order[risk_level] < order[floor] else risk_level


__all__ = [
    "AccountingPolicyEngine",
    "ApprovalLevel",
    "PolicyDecision",
    "PolicyError",
    "RiskPolicySettings",
    "get_policy_engine",
]
