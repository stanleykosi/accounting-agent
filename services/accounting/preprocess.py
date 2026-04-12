"""
Purpose: Provide deterministic preprocessing and normalization for accounting inputs.
Scope: Vendor, currency, date, tax, total, allocation, and period-boundary helpers using Python
Decimal only, so no arithmetic is delegated to model output.
Dependencies: Python datetime/decimal primitives and canonical document/accounting enums.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext

_MONEY_QUANTUM = Decimal("0.01")
_RATE_QUANTUM = Decimal("0.000001")
_PERCENT_DIVISOR = Decimal("100")
_ZERO = Decimal("0.00")


class PreprocessingError(ValueError):
    """Represent a deterministic preprocessing validation failure."""


@dataclass(frozen=True, slots=True)
class PeriodBoundary:
    """Describe an inclusive accounting period boundary."""

    period_start: date
    period_end: date

    def contains(self, value: date) -> bool:
        """Return whether a date belongs to this inclusive accounting period."""

        return self.period_start <= value <= self.period_end


@dataclass(frozen=True, slots=True)
class TaxComputation:
    """Describe a deterministic tax calculation result."""

    base_amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    tax_rate: Decimal


class AccountingPreprocessor:
    """Normalize extracted accounting values and perform deterministic Decimal math."""

    def normalize_currency_amount(
        self,
        amount: Decimal | int | str,
        *,
        allow_negative: bool = True,
    ) -> Decimal:
        """Normalize a currency amount to a two-decimal Decimal value."""

        try:
            if isinstance(amount, Decimal):
                value = amount
            elif isinstance(amount, int):
                value = Decimal(amount)
            elif isinstance(amount, str):
                value = Decimal(_clean_amount_text(amount))
            else:
                raise PreprocessingError(
                    f"Unsupported amount type {type(amount).__name__}. Use Decimal, int, or str."
                )
        except (InvalidOperation, ValueError) as error:
            raise PreprocessingError(f"Invalid amount value: {amount!r}.") from error

        if not allow_negative and value < 0:
            raise PreprocessingError("Amount cannot be negative for this accounting operation.")
        return self.quantize_money(value)

    def normalize_ratio(self, value: Decimal | int | str) -> Decimal:
        """Normalize a percentage or ratio into a Decimal ratio between 0 and 1."""

        raw_value = _decimal_from_value(value)
        ratio = raw_value / _PERCENT_DIVISOR if raw_value > 1 else raw_value
        if ratio < 0 or ratio > 1:
            raise PreprocessingError("Rates must be between 0 and 1, or 0 and 100 percent.")
        return ratio.quantize(_RATE_QUANTUM)

    def normalize_currency_code(self, value: str | None, *, default: str = "NGN") -> str:
        """Normalize an ISO 4217 currency code and default empty values to NGN."""

        candidate = (value or default).strip().upper()
        if len(candidate) != 3 or not candidate.isalpha():
            raise PreprocessingError("Currency codes must be three alphabetic ISO 4217 letters.")
        return candidate

    def normalize_date(self, value: date | datetime | str) -> date:
        """Normalize supported extracted date values into a Python date object."""

        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if not isinstance(value, str):
            raise PreprocessingError(
                f"Unsupported date type {type(value).__name__}. Use date, datetime, or str."
            )

        normalized = value.strip()
        for date_format in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d %b %Y"):
            try:
                return datetime.strptime(normalized, date_format).date()
            except ValueError:
                continue
        raise PreprocessingError(f"Unable to parse accounting date {value!r}.")

    def normalize_vendor_name(self, value: str | None) -> str:
        """Normalize vendor/payee names for deterministic matching."""

        if value is None:
            return ""
        normalized = re.sub(r"\s+", " ", value.strip().lower())
        suffixes = (
            " limited",
            " ltd",
            " llc",
            " inc",
            " incorporated",
            " plc",
            " company",
            " co",
        )
        for suffix in suffixes:
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)]
                break
        return normalized.strip()

    def normalize_period_boundary(self, *, period_start: date, period_end: date) -> PeriodBoundary:
        """Validate and return an inclusive accounting period boundary."""

        if period_end < period_start:
            raise PreprocessingError("Accounting period end cannot be before period start.")
        return PeriodBoundary(period_start=period_start, period_end=period_end)

    def calculate_tax_exclusive(
        self,
        *,
        base_amount: Decimal | int | str,
        tax_rate: Decimal | int | str,
    ) -> TaxComputation:
        """Calculate tax and total from a tax-exclusive base amount."""

        base = self.normalize_currency_amount(base_amount, allow_negative=False)
        rate = self.normalize_ratio(tax_rate)
        tax = self.quantize_money(base * rate)
        return TaxComputation(
            base_amount=base,
            tax_amount=tax,
            total_amount=self.quantize_money(base + tax),
            tax_rate=rate,
        )

    def calculate_tax_inclusive(
        self,
        *,
        total_amount: Decimal | int | str,
        tax_rate: Decimal | int | str,
    ) -> TaxComputation:
        """Calculate base and tax from a tax-inclusive total amount."""

        total = self.normalize_currency_amount(total_amount, allow_negative=False)
        rate = self.normalize_ratio(tax_rate)
        if rate == 0:
            return TaxComputation(
                base_amount=total,
                tax_amount=_ZERO,
                total_amount=total,
                tax_rate=rate,
            )

        with localcontext() as context:
            context.prec = 28
            base = self.quantize_money(total / (Decimal("1") + rate))
        tax = self.quantize_money(total - base)
        return TaxComputation(
            base_amount=base,
            tax_amount=tax,
            total_amount=total,
            tax_rate=rate,
        )

    def validate_total(
        self,
        *,
        subtotal: Decimal | int | str,
        tax_amount: Decimal | int | str,
        total: Decimal | int | str,
    ) -> bool:
        """Return whether subtotal plus tax equals the supplied total after money quantization."""

        normalized_subtotal = self.normalize_currency_amount(subtotal)
        normalized_tax = self.normalize_currency_amount(tax_amount)
        normalized_total = self.normalize_currency_amount(total)
        return self.quantize_money(normalized_subtotal + normalized_tax) == normalized_total

    def safe_divide(self, *, numerator: Decimal, denominator: Decimal) -> Decimal:
        """Divide two Decimal values and return zero for an explicit zero denominator."""

        if denominator == 0:
            return _ZERO
        with localcontext() as context:
            context.prec = 28
            return self.quantize_money(numerator / denominator)

    def allocate_amount(
        self,
        *,
        total: Decimal | int | str,
        ratios: tuple[Decimal, ...],
    ) -> tuple[Decimal, ...]:
        """Allocate a total amount across ratios while preserving exact cent totals."""

        normalized_total = self.normalize_currency_amount(total, allow_negative=False)
        if not ratios:
            return ()
        if any(ratio < 0 for ratio in ratios):
            raise PreprocessingError("Allocation ratios cannot be negative.")

        ratio_sum = sum(ratios, Decimal("0"))
        if ratio_sum <= 0:
            raise PreprocessingError("Allocation ratios must sum to a positive amount.")

        normalized_ratios = tuple(ratio / ratio_sum for ratio in ratios)
        total_cents = int((normalized_total * 100).to_integral_exact())
        exact_allocations = [Decimal(total_cents) * ratio for ratio in normalized_ratios]
        floor_cents = [int(allocation) for allocation in exact_allocations]
        remaining_cents = total_cents - sum(floor_cents)
        remainders = [
            (allocation - Decimal(floor_value), index)
            for index, (allocation, floor_value) in enumerate(
                zip(exact_allocations, floor_cents, strict=True)
            )
        ]

        for _, index in sorted(remainders, reverse=True)[:remaining_cents]:
            floor_cents[index] += 1

        return tuple(self.quantize_money(Decimal(cents) / 100) for cents in floor_cents)

    def quantize_money(self, value: Decimal) -> Decimal:
        """Quantize a Decimal as a two-decimal money amount using banker rounding."""

        return value.quantize(_MONEY_QUANTUM)


def get_preprocessor() -> AccountingPreprocessor:
    """Create the deterministic accounting preprocessor."""

    return AccountingPreprocessor()


def _clean_amount_text(value: str) -> str:
    """Normalize human-entered currency text into Decimal-compatible text."""

    cleaned = value.strip().replace(",", "")
    cleaned = re.sub(r"^[A-Za-z]{3}\s+", "", cleaned)
    cleaned = re.sub(r"[^\d+\-.()]", "", cleaned)
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = f"-{cleaned[1:-1]}"
    if not cleaned or cleaned in {"+", "-", ".", "+.", "-."}:
        raise PreprocessingError("Amount text did not contain a numeric value.")
    return cleaned


def _decimal_from_value(value: Decimal | int | str) -> Decimal:
    """Parse Decimal-compatible values without money quantization."""

    try:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, int):
            return Decimal(value)
        if isinstance(value, str):
            return Decimal(_clean_amount_text(value))
    except (InvalidOperation, ValueError) as error:
        raise PreprocessingError(f"Invalid decimal value: {value!r}.") from error
    raise PreprocessingError(
        f"Unsupported decimal type {type(value).__name__}. Use Decimal, int, or str."
    )


__all__ = [
    "AccountingPreprocessor",
    "PeriodBoundary",
    "PreprocessingError",
    "TaxComputation",
    "get_preprocessor",
]
