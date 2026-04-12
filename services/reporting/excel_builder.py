"""
Purpose: Build accountant-ready Excel report packs for a close run.
Scope: Generate P&L, Balance Sheet, Cash Flow, Budget Variance Analysis, KPI Dashboard,
and commentary workbooks using XlsxWriter. Each workbook is structured with proper
headers, formatted tables, and commentary sheets.
Dependencies: XlsxWriter, shared enums, reporting contracts, storage repository,
close-run storage scope, and commentary data.

Design notes:
- All arithmetic uses Python Decimal for financial precision — never floats.
- Each section gets its own worksheet with consistent formatting.
- Commentary data is appended to a dedicated "Commentary" worksheet.
- The builder returns raw bytes ready for MinIO upload.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import xlsxwriter
from services.common.enums import ReportSectionKey


@dataclass(frozen=True, slots=True)
class ExcelReportInput:
    """Capture all inputs required to build an Excel report pack.

    Attributes:
        close_run_id: UUID of the close run this report belongs to.
        entity_name: Display name of the entity workspace.
        period_start: Start date of the accounting period.
        period_end: End date of the accounting period.
        currency_code: ISO 4217 currency code (default NGN).
        p_and_l: Profit and Loss section data with rows and totals.
        balance_sheet: Balance Sheet section data with assets, liabilities, equity.
        cash_flow: Cash Flow section data with operating/investing/financing flows.
        budget_variance: Budget vs Actual data with variance calculations.
        kpi_dashboard: KPI metrics and dashboard data.
        commentary: Mapping of section_key to approved commentary text.
        generated_at: Timestamp when this report pack was generated.
        generated_by: Name of the user who triggered generation, if known.
    """

    close_run_id: UUID
    entity_name: str
    period_start: date
    period_end: date
    currency_code: str
    p_and_l: dict[str, Any]
    balance_sheet: dict[str, Any]
    cash_flow: dict[str, Any]
    budget_variance: dict[str, Any]
    kpi_dashboard: dict[str, Any]
    commentary: dict[str, str] = field(default_factory=dict)
    generated_at: datetime | None = None
    generated_by: str | None = None


@dataclass(frozen=True, slots=True)
class ExcelReportResult:
    """Describe the output of one Excel report generation run.

    Attributes:
        filename: Suggested filename for the Excel report pack.
        content_type: MIME type for the generated artifact.
        payload: Raw bytes of the generated Excel file.
        section_count: Number of section worksheets created.
        has_commentary: Whether commentary was included in the pack.
    """

    filename: str
    content_type: str
    payload: bytes
    section_count: int
    has_commentary: bool


# ---------------------------------------------------------------------------
# Formatting constants
# ---------------------------------------------------------------------------

_MONEY_FORMAT = '#,##0.00'
_PERCENT_FORMAT = '0.0%'
_DATE_FORMAT = 'YYYY-MM-DD'
_HEADER_BG_COLOR = '#2F5496'
_HEADER_FONT_COLOR = '#FFFFFF'
_SUBHEADER_BG_COLOR = '#D6E4F0'
_TITLE_FONT_SIZE = 16
_SECTION_FONT_SIZE = 14
_NORMAL_FONT_SIZE = 11
_COMMENTARY_FONT_SIZE = 10


def build_excel_report_pack(input_data: ExcelReportInput) -> ExcelReportResult:
    """Generate a complete accountant-ready Excel report pack.

    Args:
        input_data: All data required to populate the report workbooks.

    Returns:
        ExcelReportResult with raw bytes ready for storage upload.
    """

    buffer = io.BytesIO()

    workbook_options: dict[str, Any] = {
        'in_memory': True,
        'default_date_format': _DATE_FORMAT,
    }

    with xlsxwriter.Workbook(buffer, workbook_options) as workbook:
        # Define reusable format objects
        formats = _create_formats(workbook)

        # Write cover sheet
        _write_cover_sheet(workbook, formats, input_data)

        section_count = 0

        # Write P&L worksheet
        if input_data.p_and_l:
            _write_profit_and_loss(workbook, formats, input_data)
            section_count += 1

        # Write Balance Sheet worksheet
        if input_data.balance_sheet:
            _write_balance_sheet(workbook, formats, input_data)
            section_count += 1

        # Write Cash Flow worksheet
        if input_data.cash_flow:
            _write_cash_flow(workbook, formats, input_data)
            section_count += 1

        # Write Budget Variance worksheet
        if input_data.budget_variance:
            _write_budget_variance(workbook, formats, input_data)
            section_count += 1

        # Write KPI Dashboard worksheet
        if input_data.kpi_dashboard:
            _write_kpi_dashboard(workbook, formats, input_data)
            section_count += 1

        # Write Commentary worksheet if commentary exists
        has_commentary = bool(input_data.commentary)
        if has_commentary:
            _write_commentary(workbook, formats, input_data)

    payload = buffer.getvalue()
    period_label = input_data.period_start.strftime('%Y%m%d')
    filename = f"{_slugify(input_data.entity_name)}_report_{period_label}.xlsx"

    return ExcelReportResult(
        filename=filename,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        payload=payload,
        section_count=section_count,
        has_commentary=has_commentary,
    )


# ---------------------------------------------------------------------------
# Format factory
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ReportFormats:
    """Hold reusable XlsxWriter format objects for consistent styling."""

    title: Any
    subtitle: Any
    section_header: Any
    header: Any
    subheader: Any
    money: Any
    money_bold: Any
    money_negative: Any
    percent: Any
    date: Any
    normal: Any
    bold: Any
    commentary: Any
    commentary_header: Any
    border: Any
    total_row: Any
    total_row_bold: Any


def _create_formats(workbook: Any) -> ReportFormats:
    """Create all reusable format objects for the workbook."""

    return ReportFormats(
        title=workbook.add_format({
            'bold': True,
            'font_size': _TITLE_FONT_SIZE,
            'font_color': _HEADER_BG_COLOR,
            'align': 'left',
            'valign': 'vcenter',
        }),
        subtitle=workbook.add_format({
            'bold': False,
            'font_size': _NORMAL_FONT_SIZE,
            'font_color': '#666666',
            'align': 'left',
            'valign': 'vcenter',
        }),
        section_header=workbook.add_format({
            'bold': True,
            'font_size': _SECTION_FONT_SIZE,
            'font_color': _HEADER_BG_COLOR,
            'bg_color': _SUBHEADER_BG_COLOR,
            'border': 1,
            'align': 'left',
            'valign': 'vcenter',
        }),
        header=workbook.add_format({
            'bold': True,
            'font_size': _NORMAL_FONT_SIZE,
            'font_color': _HEADER_FONT_COLOR,
            'bg_color': _HEADER_BG_COLOR,
            'border': 1,
            'align': 'center',
            'valign': 'vcenter',
        }),
        subheader=workbook.add_format({
            'bold': True,
            'font_size': _NORMAL_FONT_SIZE,
            'bg_color': _SUBHEADER_BG_COLOR,
            'border': 1,
            'align': 'left',
            'valign': 'vcenter',
        }),
        money=workbook.add_format({
            'num_format': _MONEY_FORMAT,
            'font_size': _NORMAL_FONT_SIZE,
            'border': 1,
            'align': 'right',
            'valign': 'vcenter',
        }),
        money_bold=workbook.add_format({
            'num_format': _MONEY_FORMAT,
            'bold': True,
            'font_size': _NORMAL_FONT_SIZE,
            'border': 1,
            'align': 'right',
            'valign': 'vcenter',
        }),
        money_negative=workbook.add_format({
            'num_format': _MONEY_FORMAT,
            'font_color': '#FF0000',
            'font_size': _NORMAL_FONT_SIZE,
            'border': 1,
            'align': 'right',
            'valign': 'vcenter',
        }),
        percent=workbook.add_format({
            'num_format': _PERCENT_FORMAT,
            'font_size': _NORMAL_FONT_SIZE,
            'border': 1,
            'align': 'right',
            'valign': 'vcenter',
        }),
        date=workbook.add_format({
            'num_format': _DATE_FORMAT,
            'font_size': _NORMAL_FONT_SIZE,
            'border': 1,
            'align': 'center',
            'valign': 'vcenter',
        }),
        normal=workbook.add_format({
            'font_size': _NORMAL_FONT_SIZE,
            'border': 1,
            'align': 'left',
            'valign': 'vcenter',
        }),
        bold=workbook.add_format({
            'bold': True,
            'font_size': _NORMAL_FONT_SIZE,
            'border': 1,
            'align': 'left',
            'valign': 'vcenter',
        }),
        commentary=workbook.add_format({
            'font_size': _COMMENTARY_FONT_SIZE,
            'text_wrap': True,
            'valign': 'top',
            'align': 'left',
        }),
        commentary_header=workbook.add_format({
            'bold': True,
            'font_size': _COMMENTARY_FONT_SIZE,
            'font_color': _HEADER_BG_COLOR,
            'bg_color': _SUBHEADER_BG_COLOR,
            'border': 1,
            'valign': 'vcenter',
        }),
        border=workbook.add_format({
            'border': 1,
            'font_size': _NORMAL_FONT_SIZE,
            'valign': 'vcenter',
        }),
        total_row=workbook.add_format({
            'top': 2,
            'bottom': 2,
            'font_size': _NORMAL_FONT_SIZE,
            'align': 'right',
            'valign': 'vcenter',
            'num_format': _MONEY_FORMAT,
        }),
        total_row_bold=workbook.add_format({
            'top': 2,
            'bottom': 2,
            'bold': True,
            'font_size': _NORMAL_FONT_SIZE,
            'align': 'right',
            'valign': 'vcenter',
            'num_format': _MONEY_FORMAT,
        }),
    )


# ---------------------------------------------------------------------------
# Cover sheet
# ---------------------------------------------------------------------------

def _write_cover_sheet(workbook: Any, formats: ReportFormats, input_data: ExcelReportInput) -> None:
    """Write a cover sheet with report metadata and generation context."""

    ws = workbook.add_worksheet('Cover')
    ws.set_column('A:A', 30)
    ws.set_column('B:B', 50)

    ws.write('A1', 'Financial Report', formats.title)
    ws.write('A3', 'Entity', formats.bold)
    ws.write('B3', input_data.entity_name, formats.normal)
    ws.write('A4', 'Period Start', formats.bold)
    ws.write('B4', input_data.period_start, formats.date)
    ws.write('A5', 'Period End', formats.bold)
    ws.write('B5', input_data.period_end, formats.date)
    ws.write('A6', 'Currency', formats.bold)
    ws.write('B6', input_data.currency_code, formats.normal)
    ws.write('A7', 'Close Run', formats.bold)
    ws.write('B7', str(input_data.close_run_id), formats.normal)

    if input_data.generated_at:
        ws.write('A8', 'Generated At', formats.bold)
        ws.write('B8', input_data.generated_at, formats.date)

    if input_data.generated_by:
        ws.write('A9', 'Generated By', formats.bold)
        ws.write('B9', input_data.generated_by, formats.normal)

    # Table of contents
    ws.write('A11', 'Table of Contents', formats.section_header)
    toc_items = [
        ('Profit and Loss', ReportSectionKey.PROFIT_AND_LOSS),
        ('Balance Sheet', ReportSectionKey.BALANCE_SHEET),
        ('Cash Flow', ReportSectionKey.CASH_FLOW),
        ('Budget Variance Analysis', ReportSectionKey.BUDGET_VARIANCE),
        ('KPI Dashboard', ReportSectionKey.KPI_DASHBOARD),
    ]
    for idx, (label, _) in enumerate(toc_items, start=12):
        ws.write(f'A{idx}', label, formats.normal)


# ---------------------------------------------------------------------------
# Profit and Loss
# ---------------------------------------------------------------------------

def _write_profit_and_loss(
    workbook: Any,
    formats: ReportFormats,
    input_data: ExcelReportInput,
) -> None:
    """Write the Profit and Loss worksheet from input data."""

    ws = workbook.add_worksheet('Profit and Loss')
    data = input_data.p_and_l

    ws.write('A1', ReportSectionKey.PROFIT_AND_LOSS.label, formats.title)

    row = 3
    ws.write(row, 0, 'Account', formats.header)
    ws.write(row, 1, 'Amount', formats.header)
    ws.set_column('A:A', 40)
    ws.set_column('B:B', 20)

    row = 4
    row = _write_section_block(ws, formats, row, 'Revenue', data.get('revenue', {}))
    row = _write_section_block(ws, formats, row, 'Cost of Sales', data.get('cost_of_sales', {}))

    # Gross profit
    gross_profit = _safe_decimal(data.get('gross_profit'))
    row += 1
    ws.write(row, 0, 'Gross Profit', formats.bold)
    ws.write(row, 1, gross_profit, formats.money_bold)
    row += 1

    row = _write_section_block(
        ws, formats, row, 'Operating Expenses',
        data.get('operating_expenses', {}),
    )

    # Net profit
    net_profit = _safe_decimal(data.get('net_profit'))
    row += 1
    ws.write(row, 0, 'Net Profit / (Loss)', formats.bold)
    ws.write(row, 1, net_profit, formats.money_bold)

    # Add commentary if present
    commentary = input_data.commentary.get(ReportSectionKey.PROFIT_AND_LOSS.value)
    if commentary:
        row += 3
        ws.write(row, 0, 'Commentary', formats.commentary_header)
        ws.write(row + 1, 0, commentary, formats.commentary)


# ---------------------------------------------------------------------------
# Balance Sheet
# ---------------------------------------------------------------------------

def _write_balance_sheet(
    workbook: Any,
    formats: ReportFormats,
    input_data: ExcelReportInput,
) -> None:
    """Write the Balance Sheet worksheet from input data."""

    ws = workbook.add_worksheet('Balance Sheet')
    data = input_data.balance_sheet

    ws.write('A1', ReportSectionKey.BALANCE_SHEET.label, formats.title)

    row = 3
    ws.write(row, 0, 'Account', formats.header)
    ws.write(row, 1, 'Amount', formats.header)
    ws.set_column('A:A', 40)
    ws.set_column('B:B', 20)

    row = 4
    row = _write_section_block(ws, formats, row, 'Assets', data.get('assets', {}))

    total_assets = _safe_decimal(data.get('total_assets'))
    row += 1
    ws.write(row, 0, 'Total Assets', formats.bold)
    ws.write(row, 1, total_assets, formats.money_bold)
    row += 1

    row = _write_section_block(ws, formats, row, 'Liabilities', data.get('liabilities', {}))

    total_liabilities = _safe_decimal(data.get('total_liabilities'))
    row += 1
    ws.write(row, 0, 'Total Liabilities', formats.bold)
    ws.write(row, 1, total_liabilities, formats.money_bold)
    row += 1

    row = _write_section_block(ws, formats, row, 'Equity', data.get('equity', {}))

    total_equity = _safe_decimal(data.get('total_equity'))
    row += 1
    ws.write(row, 0, 'Total Equity', formats.bold)
    ws.write(row, 1, total_equity, formats.money_bold)
    row += 1

    # Accounting equation check
    ws.write(row, 0, 'Assets - (Liabilities + Equity)', formats.bold)
    difference = total_assets - (total_liabilities + total_equity)
    ws.write(row, 1, difference, formats.money_bold)

    commentary = input_data.commentary.get(ReportSectionKey.BALANCE_SHEET.value)
    if commentary:
        row += 3
        ws.write(row, 0, 'Commentary', formats.commentary_header)
        ws.write(row + 1, 0, commentary, formats.commentary)


# ---------------------------------------------------------------------------
# Cash Flow
# ---------------------------------------------------------------------------

def _write_cash_flow(workbook: Any, formats: ReportFormats, input_data: ExcelReportInput) -> None:
    """Write the Cash Flow worksheet from input data."""

    ws = workbook.add_worksheet('Cash Flow')
    data = input_data.cash_flow

    ws.write('A1', ReportSectionKey.CASH_FLOW.label, formats.title)

    row = 3
    ws.write(row, 0, 'Category', formats.header)
    ws.write(row, 1, 'Amount', formats.header)
    ws.set_column('A:A', 40)
    ws.set_column('B:B', 20)

    row = 4
    row = _write_section_block(
        ws, formats, row, 'Operating Activities',
        data.get('operating_activities', {}),
    )

    net_operating = _safe_decimal(data.get('net_operating_cash_flow'))
    row += 1
    ws.write(row, 0, 'Net Cash from Operating', formats.bold)
    ws.write(row, 1, net_operating, formats.money_bold)
    row += 1

    row = _write_section_block(
        ws, formats, row, 'Investing Activities',
        data.get('investing_activities', {}),
    )

    net_investing = _safe_decimal(data.get('net_investing_cash_flow'))
    row += 1
    ws.write(row, 0, 'Net Cash from Investing', formats.bold)
    ws.write(row, 1, net_investing, formats.money_bold)
    row += 1

    row = _write_section_block(
        ws, formats, row, 'Financing Activities',
        data.get('financing_activities', {}),
    )

    net_financing = _safe_decimal(data.get('net_financing_cash_flow'))
    row += 1
    ws.write(row, 0, 'Net Cash from Financing', formats.bold)
    ws.write(row, 1, net_financing, formats.money_bold)
    row += 1

    # Net change
    net_change = net_operating + net_investing + net_financing
    ws.write(row, 0, 'Net Change in Cash', formats.bold)
    ws.write(row, 1, net_change, formats.money_bold)

    commentary = input_data.commentary.get(ReportSectionKey.CASH_FLOW.value)
    if commentary:
        row += 3
        ws.write(row, 0, 'Commentary', formats.commentary_header)
        ws.write(row + 1, 0, commentary, formats.commentary)


# ---------------------------------------------------------------------------
# Budget Variance
# ---------------------------------------------------------------------------

def _write_budget_variance(
    workbook: Any,
    formats: ReportFormats,
    input_data: ExcelReportInput,
) -> None:
    """Write the Budget Variance Analysis worksheet from input data."""

    ws = workbook.add_worksheet('Budget Variance')
    data = input_data.budget_variance

    ws.write('A1', ReportSectionKey.BUDGET_VARIANCE.label, formats.title)

    row = 3
    ws.write(row, 0, 'Item', formats.header)
    ws.write(row, 1, 'Budget', formats.header)
    ws.write(row, 2, 'Actual', formats.header)
    ws.write(row, 3, 'Variance', formats.header)
    ws.write(row, 4, 'Variance %', formats.header)
    ws.set_column('A:A', 40)
    ws.set_column('B:E', 18)

    row = 4
    items = data.get('items', [])
    for item in items:
        budget = _safe_decimal(item.get('budget', 0))
        actual = _safe_decimal(item.get('actual', 0))
        variance = actual - budget
        variance_pct = (variance / budget) if budget != 0 else Decimal('0')

        ws.write(row, 0, item.get('label', ''), formats.normal)
        ws.write(row, 1, budget, formats.money)
        ws.write(row, 2, actual, formats.money)
        ws.write(row, 3, variance, formats.money)
        ws.write(row, 4, float(variance_pct), formats.percent)
        row += 1

    commentary = input_data.commentary.get(ReportSectionKey.BUDGET_VARIANCE.value)
    if commentary:
        row += 2
        ws.write(row, 0, 'Commentary', formats.commentary_header)
        ws.write(row + 1, 0, commentary, formats.commentary)


# ---------------------------------------------------------------------------
# KPI Dashboard
# ---------------------------------------------------------------------------

def _write_kpi_dashboard(
    workbook: Any,
    formats: ReportFormats,
    input_data: ExcelReportInput,
) -> None:
    """Write the KPI Dashboard worksheet from input data."""

    ws = workbook.add_worksheet('KPI Dashboard')
    data = input_data.kpi_dashboard

    ws.write('A1', ReportSectionKey.KPI_DASHBOARD.label, formats.title)

    row = 3
    ws.write(row, 0, 'Metric', formats.header)
    ws.write(row, 1, 'Value', formats.header)
    ws.write(row, 2, 'Prior Period', formats.header)
    ws.write(row, 3, 'Change', formats.header)
    ws.set_column('A:A', 35)
    ws.set_column('B:D', 18)

    row = 4
    metrics = data.get('metrics', [])
    for metric in metrics:
        value = metric.get('value', '')
        prior = metric.get('prior_period', '')
        change = metric.get('change', '')

        # Determine format based on value type
        if isinstance(value, (int, float, Decimal)):
            value_fmt = formats.money
            prior_fmt = formats.money
            change_fmt = formats.money
        else:
            value_fmt = formats.normal
            prior_fmt = formats.normal
            change_fmt = formats.normal

        ws.write(row, 0, metric.get('label', ''), formats.normal)
        ws.write(row, 1, value, value_fmt)
        ws.write(row, 2, prior, prior_fmt)
        ws.write(row, 3, change, change_fmt)
        row += 1

    commentary = input_data.commentary.get(ReportSectionKey.KPI_DASHBOARD.value)
    if commentary:
        row += 2
        ws.write(row, 0, 'Commentary', formats.commentary_header)
        ws.write(row + 1, 0, commentary, formats.commentary)


# ---------------------------------------------------------------------------
# Commentary
# ---------------------------------------------------------------------------

def _write_commentary(workbook: Any, formats: ReportFormats, input_data: ExcelReportInput) -> None:
    """Write the Commentary worksheet with all approved commentary text."""

    ws = workbook.add_worksheet('Commentary')
    ws.set_column('A:A', 25)
    ws.set_column('B:B', 80)

    ws.write('A1', 'Management Commentary', formats.title)
    ws.write('A3', 'Section', formats.commentary_header)
    ws.write('B3', 'Commentary', formats.commentary_header)

    row = 4
    for section_key, body in sorted(input_data.commentary.items()):
        section_label = _resolve_section_label(section_key)
        ws.write(row, 0, section_label, formats.bold)
        ws.write(row, 1, body, formats.commentary)
        ws.set_row(row, None, None)  # Auto-size
        row += 1


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _write_section_block(
    ws: Any,
    formats: ReportFormats,
    start_row: int,
    section_label: str,
    items: dict[str, Any],
) -> int:
    """Write a section block of line items and return the next row index.

    Args:
        ws: XlsxWriter worksheet object.
        formats: Reusable format objects.
        start_row: Zero-based row index to start writing.
        section_label: Human-readable section header label.
        items: Mapping of account labels to monetary amounts.

    Returns:
        The next row index after the last written row.
    """

    if not items:
        return start_row

    ws.write(start_row, 0, section_label, formats.subheader)
    row = start_row + 1

    for label, amount in items.items():
        value = _safe_decimal(amount)
        ws.write(row, 0, label, formats.normal)
        fmt = formats.money_negative if value < 0 else formats.money
        ws.write(row, 1, value, fmt)
        row += 1

    return row


def _safe_decimal(value: Any) -> Decimal:
    """Convert a value to Decimal safely, returning zero on failure.

    This function ensures that all arithmetic operations use Decimal
    rather than float, preserving financial precision.

    Args:
        value: Any value that might be convertible to Decimal.

    Returns:
        Decimal representation, or Decimal('0.00') if conversion fails.
    """

    if isinstance(value, Decimal):
        return value

    try:
        return Decimal(str(value))
    except (ValueError, TypeError, ArithmeticError):
        return Decimal('0.00')


def _resolve_section_label(section_key: str) -> str:
    """Resolve a section key to its human-readable label.

    Args:
        section_key: String section identifier.

    Returns:
        Human-readable label from the canonical enum, or the raw key if unknown.
    """

    for key in ReportSectionKey:
        if key.value == section_key:
            return key.label

    return section_key


def _slugify(value: str) -> str:
    """Convert a string to a filesystem-safe slug.

    Args:
        value: Input string to slugify.

    Returns:
        Lowercase string with non-alphanumeric characters replaced by underscores.
    """

    import re
    slug = re.sub(r'[^a-zA-Z0-9]+', '_', value).strip('_').lower()
    return slug or 'report'


__all__ = [
    "ExcelReportInput",
    "ExcelReportResult",
    "build_excel_report_pack",
]
