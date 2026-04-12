"""
Purpose: Verify that accounting preprocessing uses Decimal-safe arithmetic for financial math.
Scope: Currency normalization, tax calculations, exact cent allocation, total validation, and the
absence of model-gateway dependencies in deterministic math modules.
Dependencies: Accounting preprocessing helpers and Python Decimal.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from services.accounting.preprocess import AccountingPreprocessor


def test_currency_math_uses_decimal_not_float_artifacts() -> None:
    """Ensure string-normalized currency math avoids binary floating-point artifacts."""

    preprocessor = AccountingPreprocessor()
    first = preprocessor.normalize_currency_amount("0.10")
    second = preprocessor.normalize_currency_amount("0.20")

    assert first + second == Decimal("0.30")
    assert preprocessor.validate_total(
        subtotal="0.10",
        tax_amount="0.20",
        total="0.30",
    )


def test_tax_inclusive_and_exclusive_calculations_are_quantized() -> None:
    """Ensure tax calculations use repeatable Decimal quantization."""

    preprocessor = AccountingPreprocessor()
    exclusive = preprocessor.calculate_tax_exclusive(
        base_amount="1000.00",
        tax_rate="7.5",
    )
    inclusive = preprocessor.calculate_tax_inclusive(
        total_amount="1075.00",
        tax_rate="0.075",
    )

    assert exclusive.tax_amount == Decimal("75.00")
    assert exclusive.total_amount == Decimal("1075.00")
    assert inclusive.base_amount == Decimal("1000.00")
    assert inclusive.tax_amount == Decimal("75.00")


def test_allocate_amount_preserves_exact_cent_total() -> None:
    """Ensure allocations distribute cents exactly using deterministic remainder ordering."""

    preprocessor = AccountingPreprocessor()
    allocations = preprocessor.allocate_amount(
        total="100.00",
        ratios=(Decimal("1"), Decimal("1"), Decimal("1")),
    )

    assert allocations == (Decimal("33.33"), Decimal("33.33"), Decimal("33.34"))
    assert sum(allocations, Decimal("0")) == Decimal("100.00")


def test_deterministic_accounting_modules_do_not_import_model_gateway() -> None:
    """Ensure deterministic math/rule modules remain isolated from model-backed reasoning."""

    accounting_files = (
        Path("services/accounting/preprocess.py"),
        Path("services/accounting/rules.py"),
        Path("services/accounting/policies.py"),
        Path("services/accounting/dimensions.py"),
    )

    for accounting_file in accounting_files:
        source = accounting_file.read_text(encoding="utf-8")
        assert "model_gateway" not in source
        assert "openrouter" not in source.lower()
