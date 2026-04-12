"""
Purpose: Build executive-ready PDF management report packs for a close run.
Scope: Generate PDF reports containing P&L, Balance Sheet, Cash Flow, Budget Variance
Analysis, KPI Dashboard, and commentary using WeasyPrint with HTML/CSS templates.
Dependencies: WeasyPrint, shared enums, reporting contracts, storage repository,
close-run storage scope, and commentary data.

Design notes:
- Uses WeasyPrint's HTML-to-PDF rendering for professional output.
- Templates are inline HTML with embedded CSS — no external template files required.
- All monetary values are formatted with proper locale-aware separators.
- The PDF includes a cover page, table of contents, and section breaks.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from typing import Any
from uuid import UUID

from services.common.enums import ReportSectionKey
from weasyprint import HTML


@dataclass(frozen=True, slots=True)
class PdfReportInput:
    """Capture all inputs required to build a PDF report pack.

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
class PdfReportResult:
    """Describe the output of one PDF report generation run.

    Attributes:
        filename: Suggested filename for the PDF report pack.
        content_type: MIME type for the generated artifact.
        payload: Raw bytes of the generated PDF file.
        page_count: Approximate number of pages rendered.
        has_commentary: Whether commentary was included in the pack.
    """

    filename: str
    content_type: str
    payload: bytes
    page_count: int
    has_commentary: bool


def build_pdf_report_pack(input_data: PdfReportInput) -> PdfReportResult:
    """Generate a complete executive-ready PDF report pack.

    Args:
        input_data: All data required to populate the report.

    Returns:
        PdfReportResult with raw bytes ready for storage upload.
    """

    html_content = _render_full_report_html(input_data)
    pdf_bytes = BytesIO()

    HTML(string=html_content).write_pdf(pdf_bytes)
    payload = pdf_bytes.getvalue()

    period_label = input_data.period_start.strftime('%Y%m%d')
    filename = f"{_slugify(input_data.entity_name)}_report_{period_label}.pdf"

    # Approximate page count from payload size (rough heuristic)
    page_count = max(1, len(payload) // 15000)

    return PdfReportResult(
        filename=filename,
        content_type='application/pdf',
        payload=payload,
        page_count=page_count,
        has_commentary=bool(input_data.commentary),
    )


# ---------------------------------------------------------------------------
# HTML template rendering
# ---------------------------------------------------------------------------

def _render_full_report_html(input_data: PdfReportInput) -> str:
    """Render the complete HTML report with all sections and commentary.

    Args:
        input_data: All data required to populate the report.

    Returns:
        Complete HTML document string with embedded CSS.
    """

    sections_html = ""

    if input_data.p_and_l:
        sections_html += _render_profit_and_loss_html(input_data)

    if input_data.balance_sheet:
        sections_html += _render_balance_sheet_html(input_data)

    if input_data.cash_flow:
        sections_html += _render_cash_flow_html(input_data)

    if input_data.budget_variance:
        sections_html += _render_budget_variance_html(input_data)

    if input_data.kpi_dashboard:
        sections_html += _render_kpi_dashboard_html(input_data)

    if input_data.commentary:
        sections_html += _render_commentary_html(input_data)

    period_start_str = input_data.period_start.strftime('%B %d, %Y')
    period_end_str = input_data.period_end.strftime('%B %d, %Y')
    generated_str = (
        input_data.generated_at.strftime('%B %d, %Y at %H:%M')
        if input_data.generated_at
        else 'N/A'
    )
    gen_by = (
        f'<br>Generated by: {html.escape(input_data.generated_by)}'
        if input_data.generated_by
        else ''
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Financial Report — {html.escape(input_data.entity_name)}</title>
<style>
    @page {{
        size: A4;
        margin: 2.5cm 2cm 2.5cm 2cm;
        @bottom-right {{
            content: "Page " counter(page) " of " counter(pages);
            font-size: 9pt;
            color: #888;
        }}
    }}
    @page cover {{
        size: A4;
        margin: 0;
    }}
    body {{
        font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
        font-size: 11pt;
        line-height: 1.5;
        color: #1a1a1a;
    }}
    .cover {{
        page: cover;
        display: flex;
        flex-direction: column;
        justify-content: center;
        align-items: center;
        height: 100vh;
        background: linear-gradient(135deg, #2F5496 0%, #1a365d 100%);
        color: white;
        text-align: center;
        padding: 4rem;
    }}
    .cover h1 {{
        font-size: 2.5rem;
        margin-bottom: 0.5rem;
        font-weight: 700;
    }}
    .cover .entity {{
        font-size: 1.5rem;
        font-weight: 300;
        margin-bottom: 2rem;
        opacity: 0.9;
    }}
    .cover .period {{
        font-size: 1.1rem;
        opacity: 0.8;
    }}
    .cover .meta {{
        margin-top: 3rem;
        font-size: 0.9rem;
        opacity: 0.7;
    }}
    .section {{
        page-break-inside: avoid;
        margin-bottom: 2rem;
    }}
    .section-title {{
        font-size: 1.4rem;
        font-weight: 700;
        color: #2F5496;
        border-bottom: 2px solid #2F5496;
        padding-bottom: 0.4rem;
        margin-bottom: 1rem;
    }}
    .section-subtitle {{
        font-size: 1rem;
        font-weight: 600;
        color: #4a5568;
        margin-top: 1rem;
        margin-bottom: 0.5rem;
    }}
    table {{
        width: 100%;
        border-collapse: collapse;
        margin-bottom: 1rem;
        font-size: 10pt;
    }}
    th {{
        background-color: #2F5496;
        color: white;
        text-align: left;
        padding: 0.5rem 0.75rem;
        font-weight: 600;
    }}
    td {{
        padding: 0.4rem 0.75rem;
        border-bottom: 1px solid #e2e8f0;
    }}
    tr:nth-child(even) {{
        background-color: #f7fafc;
    }}
    .text-right {{
        text-align: right;
    }}
    .total-row {{
        font-weight: 700;
        border-top: 2px solid #2F5496;
        background-color: #edf2f7 !important;
    }}
    .negative {{
        color: #e53e3e;
    }}
    .commentary {{
        background-color: #f7fafc;
        border-left: 4px solid #2F5496;
        padding: 1rem 1.25rem;
        margin-top: 1rem;
        font-size: 10pt;
        color: #4a5568;
        line-height: 1.6;
    }}
    .commentary-label {{
        font-weight: 600;
        color: #2F5496;
        margin-bottom: 0.5rem;
    }}
    .toc {{
        page-break-after: always;
    }}
    .toc h2 {{
        font-size: 1.4rem;
        color: #2F5496;
        margin-bottom: 1rem;
    }}
    .toc ul {{
        list-style: none;
        padding: 0;
    }}
    .toc li {{
        padding: 0.5rem 0;
        border-bottom: 1px dotted #cbd5e0;
        font-size: 11pt;
    }}
    .kpi-metric {{
        display: inline-block;
        width: 48%;
        margin: 1%;
        padding: 1rem;
        background: #f7fafc;
        border-radius: 4px;
        vertical-align: top;
    }}
    .kpi-label {{
        font-size: 9pt;
        color: #718096;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }}
    .kpi-value {{
        font-size: 1.5rem;
        font-weight: 700;
        color: #2F5496;
    }}
    .kpi-change {{
        font-size: 9pt;
        color: #718096;
    }}
</style>
</head>
<body>

<!-- Cover Page -->
<div class="cover">
    <h1>Financial Report</h1>
    <div class="entity">{html.escape(input_data.entity_name)}</div>
    <div class="period">{period_start_str} — {period_end_str}</div>
    <div class="meta">
        Currency: {html.escape(input_data.currency_code)}<br>
        Generated: {generated_str}
        {gen_by}
    </div>
</div>

<!-- Table of Contents -->
<div class="toc">
    <h2>Table of Contents</h2>
    <ul>
        {f'<li>{ReportSectionKey.PROFIT_AND_LOSS.label}</li>' if input_data.p_and_l else ''}
        {f'<li>{ReportSectionKey.BALANCE_SHEET.label}</li>' if input_data.balance_sheet else ''}
        {f'<li>{ReportSectionKey.CASH_FLOW.label}</li>' if input_data.cash_flow else ''}
        {f'<li>{ReportSectionKey.BUDGET_VARIANCE.label}</li>' if input_data.budget_variance else ''}
        {f'<li>{ReportSectionKey.KPI_DASHBOARD.label}</li>' if input_data.kpi_dashboard else ''}
        {'<li>Management Commentary</li>' if input_data.commentary else ''}
    </ul>
</div>

{sections_html}

</body>
</html>"""


def _render_profit_and_loss_html(input_data: PdfReportInput) -> str:
    """Render the P&L section as HTML."""

    data = input_data.p_and_l
    rows_html = ""

    for category, items in [
        ('Revenue', data.get('revenue', {})),
        ('Cost of Sales', data.get('cost_of_sales', {})),
    ]:
        if items:
            rows_html += f'<tr><td colspan="2" class="section-subtitle">{category}</td></tr>'
            for label, amount in items.items():
                val = _safe_decimal(amount)
                cls = 'negative' if val < 0 else ''
                rows_html += (
                    f'<tr>'
                    f'<td>{html.escape(label)}</td>'
                    f'<td class="text-right {cls}">{_format_money(val)}</td>'
                    f'</tr>'
                )

    gross_profit = _safe_decimal(data.get('gross_profit'))
    net_profit = _safe_decimal(data.get('net_profit'))
    gp_cls = 'negative' if gross_profit < 0 else ''
    np_cls = 'negative' if net_profit < 0 else ''

    rows_html += (
        f'<tr class="total-row">'
        f'<td>Gross Profit</td>'
        f'<td class="text-right {gp_cls}">{_format_money(gross_profit)}</td>'
        f'</tr>'
    )

    if data.get('operating_expenses'):
        rows_html += '<tr><td colspan="2" class="section-subtitle">Operating Expenses</td></tr>'
        for label, amount in data['operating_expenses'].items():
            val = _safe_decimal(amount)
            cls = 'negative' if val < 0 else ''
            rows_html += (
                f'<tr>'
                f'<td>{html.escape(label)}</td>'
                f'<td class="text-right {cls}">{_format_money(val)}</td>'
                f'</tr>'
            )

    rows_html += (
        f'<tr class="total-row">'
        f'<td>Net Profit / (Loss)</td>'
        f'<td class="text-right {np_cls}">{_format_money(net_profit)}</td>'
        f'</tr>'
    )

    commentary_html = _render_commentary_block_html(
        input_data.commentary.get(ReportSectionKey.PROFIT_AND_LOSS.value)
    )

    return f"""
<div class="section">
    <h2 class="section-title">{ReportSectionKey.PROFIT_AND_LOSS.label}</h2>
    <table>
        <thead><tr><th>Account</th><th class="text-right">Amount</th></tr></thead>
        <tbody>{rows_html}</tbody>
    </table>
    {commentary_html}
</div>"""


def _render_balance_sheet_html(input_data: PdfReportInput) -> str:
    """Render the Balance Sheet section as HTML."""

    data = input_data.balance_sheet
    rows_html = ""

    for category in ('assets', 'liabilities', 'equity'):
        items = data.get(category, {})
        if items:
            label = category.capitalize()
            rows_html += f'<tr><td colspan="2" class="section-subtitle">{label}</td></tr>'
            for acct, amount in items.items():
                val = _safe_decimal(amount)
                rows_html += (
                    f'<tr>'
                    f'<td>{html.escape(acct)}</td>'
                    f'<td class="text-right">{_format_money(val)}</td>'
                    f'</tr>'
                )

    total_assets = _safe_decimal(data.get('total_assets'))
    total_liabilities = _safe_decimal(data.get('total_liabilities'))
    total_equity = _safe_decimal(data.get('total_equity'))
    difference = total_assets - (total_liabilities + total_equity)

    rows_html += (
        f'<tr class="total-row">'
        f'<td>Total Assets</td>'
        f'<td class="text-right">{_format_money(total_assets)}</td>'
        f'</tr>'
        f'<tr class="total-row">'
        f'<td>Total Liabilities</td>'
        f'<td class="text-right">{_format_money(total_liabilities)}</td>'
        f'</tr>'
        f'<tr class="total-row">'
        f'<td>Total Equity</td>'
        f'<td class="text-right">{_format_money(total_equity)}</td>'
        f'</tr>'
        f'<tr class="total-row">'
        f'<td>Assets - (Liabilities + Equity)</td>'
        f'<td class="text-right">{_format_money(difference)}</td>'
        f'</tr>'
    )

    commentary_html = _render_commentary_block_html(
        input_data.commentary.get(ReportSectionKey.BALANCE_SHEET.value)
    )

    return f"""
<div class="section">
    <h2 class="section-title">{ReportSectionKey.BALANCE_SHEET.label}</h2>
    <table>
        <thead><tr><th>Account</th><th class="text-right">Amount</th></tr></thead>
        <tbody>{rows_html}</tbody>
    </table>
    {commentary_html}
</div>"""


def _render_cash_flow_html(input_data: PdfReportInput) -> str:
    """Render the Cash Flow section as HTML."""

    data = input_data.cash_flow
    rows_html = ""

    for category in (
        ('operating_activities', 'Operating Activities'),
        ('investing_activities', 'Investing Activities'),
        ('financing_activities', 'Financing Activities'),
    ):
        items = data.get(category[0], {})
        if items:
            rows_html += f'<tr><td colspan="2" class="section-subtitle">{category[1]}</td></tr>'
            for label, amount in items.items():
                val = _safe_decimal(amount)
                rows_html += (
                    f'<tr>'
                    f'<td>{html.escape(label)}</td>'
                    f'<td class="text-right">{_format_money(val)}</td>'
                    f'</tr>'
                )

    net_operating = _safe_decimal(data.get('net_operating_cash_flow'))
    net_investing = _safe_decimal(data.get('net_investing_cash_flow'))
    net_financing = _safe_decimal(data.get('net_financing_cash_flow'))
    net_change = net_operating + net_investing + net_financing

    rows_html += (
        f'<tr class="total-row">'
        f'<td>Net Cash from Operating</td>'
        f'<td class="text-right">{_format_money(net_operating)}</td>'
        f'</tr>'
        f'<tr class="total-row">'
        f'<td>Net Cash from Investing</td>'
        f'<td class="text-right">{_format_money(net_investing)}</td>'
        f'</tr>'
        f'<tr class="total-row">'
        f'<td>Net Cash from Financing</td>'
        f'<td class="text-right">{_format_money(net_financing)}</td>'
        f'</tr>'
        f'<tr class="total-row">'
        f'<td>Net Change in Cash</td>'
        f'<td class="text-right">{_format_money(net_change)}</td>'
        f'</tr>'
    )

    commentary_html = _render_commentary_block_html(
        input_data.commentary.get(ReportSectionKey.CASH_FLOW.value)
    )

    return f"""
<div class="section">
    <h2 class="section-title">{ReportSectionKey.CASH_FLOW.label}</h2>
    <table>
        <thead><tr><th>Category</th><th class="text-right">Amount</th></tr></thead>
        <tbody>{rows_html}</tbody>
    </table>
    {commentary_html}
</div>"""


def _render_budget_variance_html(input_data: PdfReportInput) -> str:
    """Render the Budget Variance section as HTML."""

    data = input_data.budget_variance
    items = data.get('items', [])
    rows_html = ""

    for item in items:
        budget = _safe_decimal(item.get('budget', 0))
        actual = _safe_decimal(item.get('actual', 0))
        variance = actual - budget
        variance_pct = (variance / budget * 100) if budget != 0 else Decimal('0')
        var_cls = 'negative' if variance < 0 else ''

        rows_html += (
            f'<tr>'
            f'<td>{html.escape(item.get("label", ""))}</td>'
            f'<td class="text-right">{_format_money(budget)}</td>'
            f'<td class="text-right">{_format_money(actual)}</td>'
            f'<td class="text-right {var_cls}">{_format_money(variance)}</td>'
            f'<td class="text-right {var_cls}">{variance_pct:.1f}%</td>'
            f'</tr>'
        )

    commentary_html = _render_commentary_block_html(
        input_data.commentary.get(ReportSectionKey.BUDGET_VARIANCE.value)
    )

    return f"""
<div class="section">
    <h2 class="section-title">{ReportSectionKey.BUDGET_VARIANCE.label}</h2>
    <table>
        <thead>
            <tr>
                <th>Item</th>
                <th class="text-right">Budget</th>
                <th class="text-right">Actual</th>
                <th class="text-right">Variance</th>
                <th class="text-right">Variance %</th>
            </tr>
        </thead>
        <tbody>{rows_html}</tbody>
    </table>
    {commentary_html}
</div>"""


def _render_kpi_dashboard_html(input_data: PdfReportInput) -> str:
    """Render the KPI Dashboard section as HTML."""

    data = input_data.kpi_dashboard
    metrics = data.get('metrics', [])
    cards_html = ""

    for metric in metrics:
        label = html.escape(metric.get('label', ''))
        value = metric.get('value', '—')
        prior = metric.get('prior_period', '—')
        change = metric.get('change', '—')

        if isinstance(value, (int, float, Decimal)):
            value = _format_money(_safe_decimal(value))

        cards_html += f"""
        <div class="kpi-metric">
            <div class="kpi-label">{label}</div>
            <div class="kpi-value">{value}</div>
            <div class="kpi-change">Prior: {prior} | Change: {change}</div>
        </div>"""

    commentary_html = _render_commentary_block_html(
        input_data.commentary.get(ReportSectionKey.KPI_DASHBOARD.value)
    )

    return f"""
<div class="section">
    <h2 class="section-title">{ReportSectionKey.KPI_DASHBOARD.label}</h2>
    <div>{cards_html}</div>
    {commentary_html}
</div>"""


def _render_commentary_html(input_data: PdfReportInput) -> str:
    """Render the Management Commentary section as HTML."""

    blocks_html = ""
    for section_key, body in sorted(input_data.commentary.items()):
        section_label = _resolve_section_label(section_key)
        blocks_html += f"""
        <div class="commentary">
            <div class="commentary-label">{section_label}</div>
            <p>{html.escape(body)}</p>
        </div>"""

    return f"""
<div class="section">
    <h2 class="section-title">Management Commentary</h2>
    {blocks_html}
</div>"""


def _render_commentary_block_html(commentary: str | None) -> str:
    """Render a single commentary block as HTML, or return empty string."""

    if not commentary:
        return ""

    return f"""
    <div class="commentary">
        <div class="commentary-label">Management Commentary</div>
        <p>{html.escape(commentary)}</p>
    </div>"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_decimal(value: Any) -> Decimal:
    """Convert a value to Decimal safely, returning zero on failure.

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


def _format_money(value: Decimal) -> str:
    """Format a Decimal value as a locale-aware money string.

    Args:
        value: Monetary value to format.

    Returns:
        Formatted string with thousands separators and two decimal places.
    """

    # Format with thousands separator and 2 decimal places
    sign = '-' if value < 0 else ''
    abs_val = abs(value)
    integer_part = int(abs_val)
    decimal_part = abs_val - integer_part

    # Format integer part with commas
    formatted_int = f'{integer_part:,}'

    # Format decimal part to 2 places
    decimal_str = f'{decimal_part:.2f}'[1:]  # Get the .XX part

    return f'{sign}{formatted_int}{decimal_str}'


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
    "PdfReportInput",
    "PdfReportResult",
    "build_pdf_report_pack",
]
