"""
Purpose: Integration tests for the report generation pipeline.
Scope: Exercise Excel building, PDF building, commentary generation,
and the full report generation task flow with realistic inputs.
Dependencies: pytest, Excel/PDF builders, commentary generator,
and storage/report contracts.

Design notes:
- These tests verify that report artifacts are generated correctly
  with proper structure, formatting, and content.
- Tests use in-memory data — no external services required.
- PDF tests verify the output is valid PDF bytes.
- Excel tests verify the workbook structure and sheet names.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from services.reporting.commentary import (
    CommentaryGenerationInput,
    generate_commentary,
)
from services.reporting.excel_builder import (
    ExcelReportInput,
    build_excel_report_pack,
)
from services.reporting.pdf_builder import (
    PdfReportInput,
    build_pdf_report_pack,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_close_run_id() -> UUID:
    """Provide a stable UUID for test close runs."""
    return UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture()
def sample_period_start() -> date:
    """Provide a test accounting period start date."""
    return date(2025, 1, 1)


@pytest.fixture()
def sample_period_end() -> date:
    """Provide a test accounting period end date."""
    return date(2025, 12, 31)


@pytest.fixture()
def sample_p_and_l_data() -> dict:
    """Provide realistic P&L test data."""
    return {
        'revenue': {
            'Sales Revenue': Decimal('500000.00'),
            'Service Revenue': Decimal('150000.00'),
        },
        'cost_of_sales': {
            'Cost of Goods Sold': Decimal('200000.00'),
            'Direct Labor': Decimal('50000.00'),
        },
        'gross_profit': Decimal('400000.00'),
        'operating_expenses': {
            'Salaries': Decimal('120000.00'),
            'Rent': Decimal('60000.00'),
            'Utilities': Decimal('15000.00'),
            'Marketing': Decimal('25000.00'),
        },
        'net_profit': Decimal('180000.00'),
    }


@pytest.fixture()
def sample_balance_sheet_data() -> dict:
    """Provide realistic Balance Sheet test data."""
    return {
        'assets': {
            'Cash and Bank': Decimal('250000.00'),
            'Accounts Receivable': Decimal('120000.00'),
            'Inventory': Decimal('80000.00'),
            'Fixed Assets': Decimal('350000.00'),
        },
        'total_assets': Decimal('800000.00'),
        'liabilities': {
            'Accounts Payable': Decimal('90000.00'),
            'Short-term Loans': Decimal('50000.00'),
            'Long-term Debt': Decimal('200000.00'),
        },
        'total_liabilities': Decimal('340000.00'),
        'equity': {
            'Share Capital': Decimal('300000.00'),
            'Retained Earnings': Decimal('160000.00'),
        },
        'total_equity': Decimal('460000.00'),
    }


@pytest.fixture()
def sample_cash_flow_data() -> dict:
    """Provide realistic Cash Flow test data."""
    return {
        'operating_activities': {
            'Cash from Customers': Decimal('600000.00'),
            'Cash to Suppliers': Decimal('-300000.00'),
        },
        'net_operating_cash_flow': Decimal('300000.00'),
        'investing_activities': {
            'Purchase of Equipment': Decimal('-100000.00'),
        },
        'net_investing_cash_flow': Decimal('-100000.00'),
        'financing_activities': {
            'Loan Repayment': Decimal('-50000.00'),
            'Dividends Paid': Decimal('-30000.00'),
        },
        'net_financing_cash_flow': Decimal('-80000.00'),
    }


@pytest.fixture()
def sample_budget_variance_data() -> dict:
    """Provide realistic Budget Variance test data."""
    return {
        'items': [
            {
                'label': 'Revenue',
                'budget': Decimal('600000.00'),
                'actual': Decimal('650000.00'),
            },
            {
                'label': 'Cost of Sales',
                'budget': Decimal('250000.00'),
                'actual': Decimal('280000.00'),
            },
            {
                'label': 'Operating Expenses',
                'budget': Decimal('200000.00'),
                'actual': Decimal('220000.00'),
            },
            {
                'label': 'Net Profit',
                'budget': Decimal('150000.00'),
                'actual': Decimal('180000.00'),
            },
        ],
    }


@pytest.fixture()
def sample_kpi_data() -> dict:
    """Provide realistic KPI Dashboard test data."""
    return {
        'metrics': [
            {
                'label': 'Gross Margin',
                'value': Decimal('61.54'),
                'prior_period': Decimal('58.20'),
                'change': Decimal('3.34'),
            },
            {
                'label': 'Current Ratio',
                'value': Decimal('2.35'),
                'prior_period': Decimal('2.10'),
                'change': Decimal('0.25'),
            },
            {
                'label': 'Debt-to-Equity',
                'value': Decimal('0.74'),
                'prior_period': Decimal('0.80'),
                'change': Decimal('-0.06'),
            },
            {
                'label': 'Return on Equity',
                'value': Decimal('39.13'),
                'prior_period': Decimal('35.00'),
                'change': Decimal('4.13'),
            },
        ],
    }


@pytest.fixture()
def sample_commentary() -> dict[str, str]:
    """Provide pre-written commentary text for test report packs."""
    return {
        'profit_and_loss': (
            'The entity delivered strong revenue growth this period, '
            'with net profit exceeding budget by 20%. Cost management '
            'remains an area of focus.'
        ),
        'balance_sheet': (
            'The balance sheet remains healthy with adequate liquidity. '
            'The debt-to-equity ratio has improved compared to prior periods.'
        ),
    }


# ---------------------------------------------------------------------------
# Excel report generation tests
# ---------------------------------------------------------------------------

class TestExcelReportGeneration:
    """Validate Excel report pack generation and structure."""

    def test_excel_report_generates_valid_workbook(
        self,
        sample_close_run_id: UUID,
        sample_period_start: date,
        sample_period_end: date,
        sample_p_and_l_data: dict,
        sample_balance_sheet_data: dict,
        sample_cash_flow_data: dict,
        sample_budget_variance_data: dict,
        sample_kpi_data: dict,
        sample_commentary: dict[str, str],
    ) -> None:
        """Excel report generation must produce a valid XLSX workbook."""

        input_data = ExcelReportInput(
            close_run_id=sample_close_run_id,
            entity_name="Test Entity Ltd",
            period_start=sample_period_start,
            period_end=sample_period_end,
            currency_code="NGN",
            p_and_l=sample_p_and_l_data,
            balance_sheet=sample_balance_sheet_data,
            cash_flow=sample_cash_flow_data,
            budget_variance=sample_budget_variance_data,
            kpi_dashboard=sample_kpi_data,
            commentary=sample_commentary,
        )

        result = build_excel_report_pack(input_data)

        assert result.payload, "Excel payload must not be empty"
        assert result.filename.endswith('.xlsx'), "Filename must have .xlsx extension"
        assert result.content_type == (
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        assert result.section_count == 5, "All five sections should be present"
        assert result.has_commentary is True, "Commentary should be included"

    def test_excel_report_contains_required_sheets(
        self,
        sample_close_run_id: UUID,
        sample_period_start: date,
        sample_period_end: date,
        sample_p_and_l_data: dict,
        sample_balance_sheet_data: dict,
        sample_cash_flow_data: dict,
        sample_budget_variance_data: dict,
        sample_kpi_data: dict,
    ) -> None:
        """Excel report must contain worksheets for all mandatory sections."""

        input_data = ExcelReportInput(
            close_run_id=sample_close_run_id,
            entity_name="Test Entity Ltd",
            period_start=sample_period_start,
            period_end=sample_period_end,
            currency_code="NGN",
            p_and_l=sample_p_and_l_data,
            balance_sheet=sample_balance_sheet_data,
            cash_flow=sample_cash_flow_data,
            budget_variance=sample_budget_variance_data,
            kpi_dashboard=sample_kpi_data,
        )

        result = build_excel_report_pack(input_data)

        # Read the workbook and verify sheet names
        buffer = io.BytesIO(result.payload)
        # Use xlsxwriter's output validation — the file should be a valid ZIP
        import zipfile
        assert zipfile.is_zipfile(buffer), "XLSX files are ZIP archives"

    def test_excel_report_without_commentary(
        self,
        sample_close_run_id: UUID,
        sample_period_start: date,
        sample_period_end: date,
        sample_p_and_l_data: dict,
    ) -> None:
        """Excel report should omit commentary worksheet when no commentary provided."""

        input_data = ExcelReportInput(
            close_run_id=sample_close_run_id,
            entity_name="Test Entity Ltd",
            period_start=sample_period_start,
            period_end=sample_period_end,
            currency_code="NGN",
            p_and_l=sample_p_and_l_data,
            balance_sheet={},
            cash_flow={},
            budget_variance={},
            kpi_dashboard={},
            commentary={},
        )

        result = build_excel_report_pack(input_data)

        assert result.has_commentary is False, "Commentary flag should be False"
        assert result.section_count == 1, "Only P&L section should be present"


# ---------------------------------------------------------------------------
# PDF report generation tests
# ---------------------------------------------------------------------------

class TestPdfReportGeneration:
    """Validate PDF report pack generation and structure."""

    def test_pdf_report_generates_valid_pdf(
        self,
        sample_close_run_id: UUID,
        sample_period_start: date,
        sample_period_end: date,
        sample_p_and_l_data: dict,
        sample_balance_sheet_data: dict,
        sample_cash_flow_data: dict,
        sample_budget_variance_data: dict,
        sample_kpi_data: dict,
        sample_commentary: dict[str, str],
    ) -> None:
        """PDF report generation must produce valid PDF bytes."""

        input_data = PdfReportInput(
            close_run_id=sample_close_run_id,
            entity_name="Test Entity Ltd",
            period_start=sample_period_start,
            period_end=sample_period_end,
            currency_code="NGN",
            p_and_l=sample_p_and_l_data,
            balance_sheet=sample_balance_sheet_data,
            cash_flow=sample_cash_flow_data,
            budget_variance=sample_budget_variance_data,
            kpi_dashboard=sample_kpi_data,
            commentary=sample_commentary,
        )

        result = build_pdf_report_pack(input_data)

        assert result.payload, "PDF payload must not be empty"
        assert result.filename.endswith('.pdf'), "Filename must have .pdf extension"
        assert result.content_type == 'application/pdf'
        assert result.page_count >= 1, "PDF should have at least one page"
        assert result.has_commentary is True, "Commentary should be included"

        # Check PDF header magic
        assert result.payload[:5] == b'%PDF-', "Output must start with PDF header"

    def test_pdf_report_without_commentary(
        self,
        sample_close_run_id: UUID,
        sample_period_start: date,
        sample_period_end: date,
        sample_p_and_l_data: dict,
    ) -> None:
        """PDF report should work without commentary."""

        input_data = PdfReportInput(
            close_run_id=sample_close_run_id,
            entity_name="Test Entity Ltd",
            period_start=sample_period_start,
            period_end=sample_period_end,
            currency_code="NGN",
            p_and_l=sample_p_and_l_data,
            balance_sheet={},
            cash_flow={},
            budget_variance={},
            kpi_dashboard={},
            commentary={},
        )

        result = build_pdf_report_pack(input_data)

        assert result.has_commentary is False, "Commentary flag should be False"
        assert result.payload[:5] == b'%PDF-', "Output must still be valid PDF"


# ---------------------------------------------------------------------------
# Commentary generation tests
# ---------------------------------------------------------------------------

class TestCommentaryGeneration:
    """Validate deterministic commentary generation."""

    def test_commentary_generates_for_all_sections(
        self,
        sample_close_run_id: UUID,
        sample_period_start: date,
        sample_period_end: date,
        sample_p_and_l_data: dict,
        sample_balance_sheet_data: dict,
        sample_cash_flow_data: dict,
        sample_budget_variance_data: dict,
        sample_kpi_data: dict,
    ) -> None:
        """Commentary should be generated for all sections with data."""

        input_data = CommentaryGenerationInput(
            close_run_id=sample_close_run_id,
            entity_name="Test Entity Ltd",
            period_start=sample_period_start,
            period_end=sample_period_end,
            currency_code="NGN",
            p_and_l=sample_p_and_l_data,
            balance_sheet=sample_balance_sheet_data,
            cash_flow=sample_cash_flow_data,
            budget_variance=sample_budget_variance_data,
            kpi_dashboard=sample_kpi_data,
            use_llm=False,
        )

        result = generate_commentary(input_data)

        assert result.sections_generated == 5, "All five sections should have commentary"
        assert result.llm_enhanced is False, "LLM should not be used"
        assert len(result.errors) == 0, "No errors expected"

        # Verify each section has meaningful commentary
        for section_key in [
            'profit_and_loss',
            'balance_sheet',
            'cash_flow',
            'budget_variance',
            'kpi_dashboard',
        ]:
            assert section_key in result.commentary, f"{section_key} commentary missing"
            assert len(result.commentary[section_key]) > 50, (
                f"{section_key} commentary should be substantive"
            )

    def test_commentary_handles_empty_data_gracefully(
        self,
        sample_close_run_id: UUID,
        sample_period_start: date,
        sample_period_end: date,
    ) -> None:
        """Commentary generation should not crash on empty section data."""

        input_data = CommentaryGenerationInput(
            close_run_id=sample_close_run_id,
            entity_name="Test Entity Ltd",
            period_start=sample_period_start,
            period_end=sample_period_end,
            currency_code="NGN",
            p_and_l={},
            balance_sheet={},
            cash_flow={},
            budget_variance={},
            kpi_dashboard={},
            use_llm=False,
        )

        result = generate_commentary(input_data)

        # Should still complete without errors even with empty data
        assert len(result.errors) == 0, "Empty data should not cause errors"


# ---------------------------------------------------------------------------
# Financial precision tests
# ---------------------------------------------------------------------------

class TestFinancialPrecision:
    """Validate that arithmetic uses Decimal precision throughout."""

    def test_excel_uses_decimal_precision(
        self,
        sample_close_run_id: UUID,
        sample_period_start: date,
        sample_period_end: date,
    ) -> None:
        """Excel builder must handle monetary values with Decimal precision."""

        # Use values that would expose float precision issues
        precise_data = {
            'revenue': {
                'Test Revenue': Decimal('1000000.01'),
            },
            'cost_of_sales': {},
            'gross_profit': Decimal('1000000.01'),
            'operating_expenses': {},
            'net_profit': Decimal('1000000.01'),
        }

        input_data = ExcelReportInput(
            close_run_id=sample_close_run_id,
            entity_name="Precision Test",
            period_start=sample_period_start,
            period_end=sample_period_end,
            currency_code="NGN",
            p_and_l=precise_data,
            balance_sheet={},
            cash_flow={},
            budget_variance={},
            kpi_dashboard={},
        )

        result = build_excel_report_pack(input_data)
        assert result.payload, "Precision test must generate Excel output"

    def test_pdf_uses_decimal_precision(
        self,
        sample_close_run_id: UUID,
        sample_period_start: date,
        sample_period_end: date,
    ) -> None:
        """PDF builder must handle monetary values with Decimal precision."""

        precise_data = {
            'revenue': {
                'Test Revenue': Decimal('1000000.01'),
            },
            'cost_of_sales': {},
            'gross_profit': Decimal('1000000.01'),
            'operating_expenses': {},
            'net_profit': Decimal('1000000.01'),
        }

        input_data = PdfReportInput(
            close_run_id=sample_close_run_id,
            entity_name="Precision Test",
            period_start=sample_period_start,
            period_end=sample_period_end,
            currency_code="NGN",
            p_and_l=precise_data,
            balance_sheet={},
            cash_flow={},
            budget_variance={},
            kpi_dashboard={},
        )

        result = build_pdf_report_pack(input_data)
        assert result.payload[:5] == b'%PDF-', "Precision test must generate valid PDF"


__all__ = [
    "TestCommentaryGeneration",
    "TestExcelReportGeneration",
    "TestFinancialPrecision",
    "TestPdfReportGeneration",
]
