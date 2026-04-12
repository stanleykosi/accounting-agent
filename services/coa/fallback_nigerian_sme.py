"""
Purpose: Provide the canonical Nigerian SME chart-of-accounts fallback template.
Scope: Deterministic account seeds used only when no manual or QuickBooks-synced
COA exists for an entity.
Dependencies: Python dataclasses only so fallback generation stays lightweight.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FallbackCoaAccountSeed:
    """Describe one fallback account row used to seed the Nigerian SME template."""

    account_code: str
    account_name: str
    account_type: str
    parent_account_code: str | None = None
    is_postable: bool = True
    is_active: bool = True


FALLBACK_TEMPLATE_VERSION = "nigerian_sme_v1"


def build_nigerian_sme_fallback_accounts() -> tuple[FallbackCoaAccountSeed, ...]:
    """Return a deterministic fallback account list ordered by account code."""

    return (
        FallbackCoaAccountSeed("1000", "Assets", "asset", is_postable=False),
        FallbackCoaAccountSeed("1010", "Cash and Cash Equivalents", "asset", "1000"),
        FallbackCoaAccountSeed("1020", "Accounts Receivable", "asset", "1000"),
        FallbackCoaAccountSeed("1030", "Inventory", "asset", "1000"),
        FallbackCoaAccountSeed("1040", "Prepaid Expenses", "asset", "1000"),
        FallbackCoaAccountSeed("1100", "Property, Plant and Equipment", "asset", "1000"),
        FallbackCoaAccountSeed("2000", "Liabilities", "liability", is_postable=False),
        FallbackCoaAccountSeed("2010", "Accounts Payable", "liability", "2000"),
        FallbackCoaAccountSeed("2020", "Accrued Expenses", "liability", "2000"),
        FallbackCoaAccountSeed("2030", "Payroll Liabilities", "liability", "2000"),
        FallbackCoaAccountSeed("2040", "VAT Payable", "liability", "2000"),
        FallbackCoaAccountSeed("2100", "Loan Payable", "liability", "2000"),
        FallbackCoaAccountSeed("3000", "Equity", "equity", is_postable=False),
        FallbackCoaAccountSeed("3010", "Share Capital", "equity", "3000"),
        FallbackCoaAccountSeed("3020", "Retained Earnings", "equity", "3000"),
        FallbackCoaAccountSeed("4000", "Revenue", "revenue", is_postable=False),
        FallbackCoaAccountSeed("4010", "Sales Revenue", "revenue", "4000"),
        FallbackCoaAccountSeed("4020", "Service Revenue", "revenue", "4000"),
        FallbackCoaAccountSeed("5000", "Cost of Sales", "cost_of_sales", is_postable=False),
        FallbackCoaAccountSeed("5010", "Direct Materials", "cost_of_sales", "5000"),
        FallbackCoaAccountSeed("5020", "Direct Labour", "cost_of_sales", "5000"),
        FallbackCoaAccountSeed("6000", "Operating Expenses", "expense", is_postable=False),
        FallbackCoaAccountSeed("6010", "Salaries and Wages", "expense", "6000"),
        FallbackCoaAccountSeed("6020", "Rent Expense", "expense", "6000"),
        FallbackCoaAccountSeed("6030", "Utilities Expense", "expense", "6000"),
        FallbackCoaAccountSeed("6040", "Depreciation Expense", "expense", "6000"),
        FallbackCoaAccountSeed("6050", "Professional Fees", "expense", "6000"),
        FallbackCoaAccountSeed("7000", "Other Income", "other_income", is_postable=False),
        FallbackCoaAccountSeed("7010", "Interest Income", "other_income", "7000"),
        FallbackCoaAccountSeed("8000", "Other Expenses", "other_expense", is_postable=False),
        FallbackCoaAccountSeed("8010", "Bank Charges", "other_expense", "8000"),
        FallbackCoaAccountSeed("8020", "Interest Expense", "other_expense", "8000"),
    )


__all__ = [
    "FALLBACK_TEMPLATE_VERSION",
    "FallbackCoaAccountSeed",
    "build_nigerian_sme_fallback_accounts",
]
