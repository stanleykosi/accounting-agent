"""
Purpose: Provide deterministic preprocessing and normalization for accounting data.
Scope: Normalization of vendors, currencies, dates, taxes, totals, and period boundaries using Python Decimal.
Dependencies: Python decimal, shared utilities, settings.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_EVEN, InvalidOperation
from typing import Any

from services.common.settings import AppSettings, get_settings


class PreprocessingError(Exception):
    """Raised when accounting data preprocessing fails."""


class AccountingPreprocessor:
    """Handle deterministic preprocessing for accounting transactions."""

    def __init__(self, settings: AppSettings | None = None):
        self.settings = settings or get_settings()
        # Configure Decimal context for accounting precision
        Decimal._context = Decimal._context.copy()
        Decimal._context.prec = 28  # High precision for intermediate calculations
        Decimal._context.rounding = ROUND_HALF_EVEN  # Banker's rounding

    def normalize_currency_amount(self, amount: Any) -> Decimal:
        """
        Normalize a currency amount to Decimal with proper precision.

        Args:
            amount: Amount as string, int, float, or Decimal

        Returns:
            Normalized Decimal amount

        Raises:
            PreprocessingError: If amount cannot be normalized
        """
        try:
            if isinstance(amount, Decimal):
                return amount
            elif isinstance(amount, (int, float)):
                # Convert float to string to avoid floating point precision issues
                return Decimal(str(amount))
            elif isinstance(amount, str):
                # Remove currency symbols, commas, and whitespace
                cleaned = re.sub(r"[^\d\-+.]", "", amount.strip())
                if not cleaned:
                    raise PreprocessingError("Empty amount after cleaning")
                return Decimal(cleaned)
            else:
                raise PreprocessingError(f"Unsupported amount type: {type(amount)}")
        except (InvalidOperation, ValueError) as e:
            raise PreprocessingError(f"Invalid amount format: {amount}") from e

    def normalize_date(self, date_input: Any) -> date:
        """
        Normalize various date formats to Python date object.

        Args:
            date_input: Date as string, datetime, or date object

        Returns:
            Normalized date object

        Raises:
            PreprocessingError: If date cannot be normalized
        """
        try:
            if isinstance(date_input, date):
                if isinstance(date_input, datetime):
                    return date_input.date()
                return date_input
            elif isinstance(date_input, str):
                # Try common date formats
                formats = [
                    "%Y-%m-%d",
                    "%d/%m/%Y",
                    "%m/%d/%Y",
                    "%d-%m-%Y",
                    "%Y/%m/%d",
                ]
                for fmt in formats:
                    try:
                        return datetime.strptime(date_input.strip(), fmt).date()
                    except ValueError:
                        continue
                raise PreprocessingError(f"Unable to parse date: {date_input}")
            else:
                raise PreprocessingError(f"Unsupported date type: {type(date_input)}")
        except Exception as e:
            raise PreprocessingError(f"Invalid date format: {date_input}") from e

    def normalize_vendor_name(self, vendor: str) -> str:
        """
        Normalize vendor name for consistent matching.

        Args:
            vendor: Vendor name string

        Returns:
            Normalized vendor name
        """
        if not vendor or not isinstance(vendor, str):
            return ""

        # Convert to lowercase, strip whitespace, and normalize internal spaces
        normalized = vendor.lower().strip()
        # Replace multiple spaces with single space
        normalized = re.sub(r"\s+", " ", normalized)
        # Remove common suffixes/prefixes that might vary
        suffixes_to_remove = [" ltd", " llc", " inc", " corp", " corporation", " company", " co"]
        for suffix in suffixes_to_remove:
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)]
                break
        return normalized.strip()

    def calculate_tax(self, amount: Decimal, tax_rate: Decimal) -> tuple[Decimal, Decimal]:
        """
        Calculate tax amount and total using deterministic decimal arithmetic.

        Args:
            amount: Base amount before tax
            tax_rate: Tax rate as decimal (e.g., 0.075 for 7.5%)

        Returns:
            Tuple of (tax_amount, total_amount)
        """
        if amount < 0:
            raise PreprocessingError("Amount cannot be negative for tax calculation")
        if tax_rate < 0 or tax_rate > 1:
            raise PreprocessingError("Tax rate must be between 0 and 1")

        # Calculate tax with proper rounding
        tax_amount = (amount * tax_rate).quantize(Decimal("0.01"))
        total_amount = amount + tax_amount
        return tax_amount, total_amount

    def calculate_tax_inclusive(self, total: Decimal, tax_rate: Decimal) -> tuple[Decimal, Decimal]:
        """
        Calculate base amount and tax from tax-inclusive total.

        Args:
            total: Total amount including tax
            tax_rate: Tax rate as decimal (e.g., 0.075 for 7.5%)

        Returns:
            Tuple of (base_amount, tax_amount)
        """
        if total < 0:
            raise PreprocessingError("Total cannot be negative")
        if tax_rate < 0 or tax_rate > 1:
            raise PreprocessingError("Tax rate must be between 0 and 1")
        if tax_rate == 0:
            return total, Decimal("0")

        # Calculate base amount: total / (1 + tax_rate)
        divisor = Decimal("1") + tax_rate
        base_amount = (total / divisor).quantize(Decimal("0.01"))
        tax_amount = total - base_amount
        return base_amount, tax_amount

    def is_same_period(
        self, date1: date, date2: date, period_start: date, period_end: date
    ) -> bool:
        """
        Check if two dates fall within the same accounting period.

        Args:
            date1: First date to check
            date2: Second date to check
            period_start: Start of accounting period
            period_end: End of accounting period

        Returns:
            True if both dates are within the period
        """
        return (period_start <= date1 <= period_end) and (period_start <= date2 <= period_end)

    def get_period_dates(self, year: int, month: int) -> tuple[date, date]:
        """
        Get start and end dates for a given month/year.

        Args:
            year: Year (e.g., 2024)
            month: Month (1-12)

        Returns:
            Tuple of (start_date, end_date)
        """
        if month < 1 or month > 12:
            raise PreprocessingError("Month must be between 1 and 12")
        if year < 1900:
            raise PreprocessingError("Year must be >= 1900")

        start_date = date(year, month, 1)
        # Calculate end date (last day of month)
        if month == 12:
            end_date = date(year + 1, 1, 1) - date.resolution
        else:
            end_date = date(year, month + 1, 1) - date.resolution
        return start_date, end_date

    def safe_divide(self, numerator: Decimal, denominator: Decimal) -> Decimal:
        """
        Safely divide two decimals, returning zero if denominator is zero.

        Args:
            numerator: Numerator value
            denominator: Denominator value

        Returns:
            Result of division or zero if denominator is zero
        """
        if denominator == 0:
            return Decimal("0")
        return (numerator / denominator).quantize(Decimal("0.01"))

    def allocate_amount(self, total: Decimal, allocations: list[Decimal]) -> list[Decimal]:
        """
        Allocate a total amount across multiple categories with proper rounding.

        Uses the largest remainder method to ensure allocations sum to total.

        Args:
            total: Total amount to allocate
            allocations: List of allocation ratios (should sum to 1)

        Returns:
            List of allocated amounts that sum exactly to total
        """
        if total < 0:
            raise PreprocessingError("Total cannot be negative for allocation")
        if not allocations:
            return []

        # Validate allocations
        alloc_sum = sum(allocations)
        if alloc_sum <= 0:
            raise PreprocessingError("Allocations must sum to a positive value")
        if abs(alloc_sum - Decimal("1")) > Decimal("0.001"):
            # Normalize if not approximately equal to 1
            allocations = [alloc / alloc_sum for alloc in allocations]

        # Calculate initial allocations
        initial_allocs = [self.safe_divide(total, alloc) for alloc in allocations]
        # Actually, we want to multiply: total * allocation_ratio
        initial_allocs = [total * alloc for alloc in allocations]

        # Calculate remainders for largest remainder method
        integer_parts = [int(alloc) for alloc in initial_allocs]
        remainders = [
            alloc - integer_part for alloc, integer_part in zip(initial_allocs, integer_parts)
        ]

        # Distribute remaining cents to largest remainders
        total_allocated = sum(integer_parts)
        remaining_cents = int((total - total_allocated) * 100)  # Convert to cents

        # Sort indices by remainder (descending) to allocate remaining cents
        sorted_indices = sorted(range(len(remainders)), key=lambda i: remainders[i], reverse=True)

        # Add one cent to allocations with largest remainders
        final_allocs = integer_parts[:]
        for i in range(min(remaining_cents, len(sorted_indices))):
            final_allocs[sorted_indices[i]] += 1

        # Convert back to decimals
        return [Decimal(str(cent)) / 100 for cent in final_allocs]


def get_preprocessor() -> AccountingPreprocessor:
    """Factory function to create an AccountingPreprocessor instance."""
    return AccountingPreprocessor()


__all__ = [
    "PreprocessingError",
    "AccountingPreprocessor",
    "get_preprocessor",
]
