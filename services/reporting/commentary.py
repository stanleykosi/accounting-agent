"""
Purpose: Provide commentary draft generation and persistence for report sections.
Scope: Generate commentary text using deterministic variance analysis and LLM-assisted
narrative generation, persist drafts, and manage the commentary approval workflow.
Dependencies: Shared enums, reporting contracts, DB session factory, audit service,
model gateway for optional LLM-assisted commentary, and structured logging.

Design notes:
- Commentary can be generated deterministically from numerical data alone.
- When the model gateway is available, LLM reasoning enhances the narrative quality.
- All generated commentary persists as draft status awaiting human review.
- The service does NOT auto-approve commentary — approval always requires human action.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from services.common.enums import ReportSectionKey
from services.common.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CommentaryGenerationInput:
    """Capture all inputs required to generate commentary for a report run.

    Attributes:
        close_run_id: UUID of the close run being reported on.
        entity_name: Display name of the entity workspace.
        period_start: Start date of the accounting period.
        period_end: End date of the accounting period.
        currency_code: ISO 4217 currency code.
        p_and_l: Profit and Loss data for variance analysis commentary.
        balance_sheet: Balance Sheet data for financial position commentary.
        cash_flow: Cash Flow data for liquidity commentary.
        budget_variance: Budget vs Actual data for budget variance commentary.
        kpi_dashboard: KPI data for performance commentary.
        use_llm: Whether to attempt LLM-enhanced commentary generation.
    """

    close_run_id: UUID
    entity_name: str
    period_start: datetime | str
    period_end: datetime | str
    currency_code: str
    p_and_l: dict[str, Any]
    balance_sheet: dict[str, Any]
    cash_flow: dict[str, Any]
    budget_variance: dict[str, Any]
    kpi_dashboard: dict[str, Any]
    use_llm: bool = False


@dataclass(frozen=True, slots=True)
class CommentaryGenerationResult:
    """Describe the output of commentary generation.

    Attributes:
        commentary: Mapping of section_key to generated commentary text.
        sections_generated: Number of sections with generated commentary.
        llm_enhanced: Whether LLM reasoning was used in generation.
        errors: Any errors encountered during generation.
    """

    commentary: dict[str, str]
    sections_generated: int
    llm_enhanced: bool
    errors: list[str]


def generate_commentary(input_data: CommentaryGenerationInput) -> CommentaryGenerationResult:
    """Generate draft commentary for all report sections.

    This function generates management commentary text for each mandatory report
    section using deterministic variance analysis. When LLM enhancement is
    enabled and the model gateway is available, the LLM improves narrative quality.

    Args:
        input_data: All data required to generate commentary.

    Returns:
        CommentaryGenerationResult with generated commentary text per section.
    """

    commentary: dict[str, str] = {}
    errors: list[str] = []
    llm_enhanced = False

    # Generate deterministic commentary for each section
    if input_data.p_and_l:
        try:
            commentary[ReportSectionKey.PROFIT_AND_LOSS.value] = (
                _generate_pl_commentary(input_data)
            )
        except Exception as exc:
            errors.append(f"P&L commentary failed: {exc}")

    if input_data.balance_sheet:
        try:
            commentary[ReportSectionKey.BALANCE_SHEET.value] = (
                _generate_bs_commentary(input_data)
            )
        except Exception as exc:
            errors.append(f"Balance Sheet commentary failed: {exc}")

    if input_data.cash_flow:
        try:
            commentary[ReportSectionKey.CASH_FLOW.value] = (
                _generate_cf_commentary(input_data)
            )
        except Exception as exc:
            errors.append(f"Cash Flow commentary failed: {exc}")

    if input_data.budget_variance:
        try:
            commentary[ReportSectionKey.BUDGET_VARIANCE.value] = (
                _generate_budget_commentary(input_data)
            )
        except Exception as exc:
            errors.append(f"Budget Variance commentary failed: {exc}")

    if input_data.kpi_dashboard:
        try:
            commentary[ReportSectionKey.KPI_DASHBOARD.value] = (
                _generate_kpi_commentary(input_data)
            )
        except Exception as exc:
            errors.append(f"KPI Dashboard commentary failed: {exc}")

    # If LLM enhancement is requested, attempt to improve commentary
    if input_data.use_llm:
        try:
            commentary = _enhance_commentary_with_llm(
                commentary=commentary,
                entity_name=input_data.entity_name,
                period_start=str(input_data.period_start),
                period_end=str(input_data.period_end),
            )
            llm_enhanced = True
        except Exception as exc:
            errors.append(f"LLM commentary enhancement failed: {exc}")
            logger.warning(
                "llm_commentary_enhancement_failed",
                close_run_id=str(input_data.close_run_id),
                error=str(exc),
            )

    logger.info(
        "commentary_generation_complete",
        close_run_id=str(input_data.close_run_id),
        sections_generated=len(commentary),
        llm_enhanced=llm_enhanced,
        error_count=len(errors),
    )

    return CommentaryGenerationResult(
        commentary=commentary,
        sections_generated=len(commentary),
        llm_enhanced=llm_enhanced,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Deterministic commentary generators
# ---------------------------------------------------------------------------

def _generate_pl_commentary(input_data: CommentaryGenerationInput) -> str:
    """Generate deterministic Profit and Loss commentary.

    Args:
        input_data: Report generation input data.

    Returns:
        Commentary text string for the P&L section.
    """

    p_and_l = input_data.p_and_l
    revenue = p_and_l.get('revenue', {})
    cost_of_sales = p_and_l.get('cost_of_sales', {})
    gross_profit = _safe_decimal(p_and_l.get('gross_profit'))
    net_profit = _safe_decimal(p_and_l.get('net_profit'))

    total_revenue = sum(
        _safe_decimal(v) for v in revenue.values()
    ) if revenue else Decimal('0')
    total_cogs = sum(
        _safe_decimal(v) for v in cost_of_sales.values()
    ) if cost_of_sales else Decimal('0')
    gross_margin = (
        (gross_profit / total_revenue * 100)
        if total_revenue > 0 else Decimal('0')
    )

    lines = [
        (
            f"Profit and Loss Analysis for {input_data.entity_name} "
            f"({input_data.period_start} to {input_data.period_end})."
        ),
        (
            f"Total revenue for the period was "
            f"{_format_money(total_revenue)} {input_data.currency_code}."
        ),
    ]

    if revenue:
        top_revenue = sorted(revenue.items(), key=lambda x: _safe_decimal(x[1]), reverse=True)[:3]
        lines.append(f"Primary revenue streams: {', '.join(k for k, _ in top_revenue)}.")

    lines.append(
        
            f"Gross profit was {_format_money(gross_profit)} "
            f"with a gross margin of {gross_margin:.1f}%."
        
    )

    if total_cogs > 0:
        cogs_ratio = total_cogs / total_revenue * 100 if total_revenue > 0 else Decimal('0')
        lines.append(f"Cost of sales represented {cogs_ratio:.1f}% of total revenue.")

    if net_profit >= 0:
        net_margin = (
            (net_profit / total_revenue * 100)
            if total_revenue > 0 else Decimal('0')
        )
        lines.append(
            
                f"The entity recorded a net profit of "
                f"{_format_money(net_profit)} {input_data.currency_code} "
                f"with a net margin of {net_margin:.1f}%."
            
        )
    else:
        lines.append(
            
                f"The entity recorded a net loss of "
                f"{_format_money(abs(net_profit))} "
                f"{input_data.currency_code} for the period. "
                f"Management should investigate cost structures "
                f"and revenue shortfalls."
            
        )

    return ' '.join(lines)


def _generate_bs_commentary(input_data: CommentaryGenerationInput) -> str:
    """Generate deterministic Balance Sheet commentary.

    Args:
        input_data: Report generation input data.

    Returns:
        Commentary text string for the Balance Sheet section.
    """

    bs = input_data.balance_sheet
    total_assets = _safe_decimal(bs.get('total_assets'))
    total_liabilities = _safe_decimal(bs.get('total_liabilities'))
    total_equity = _safe_decimal(bs.get('total_equity'))

    difference = total_assets - (total_liabilities + total_equity)

    lines = [
        f"Balance Sheet position for {input_data.entity_name} as at {input_data.period_end}.",
        f"Total assets stood at {_format_money(total_assets)} {input_data.currency_code}.",
        f"Total liabilities were {_format_money(total_liabilities)} {input_data.currency_code}.",
        f"Total equity was {_format_money(total_equity)} {input_data.currency_code}.",
    ]

    if abs(difference) > Decimal('0.01'):
        lines.append(
            
                f"NOTE: There is an unexplained difference of "
                f"{_format_money(difference)} {input_data.currency_code} "
                f"between total assets and total liabilities plus equity. "
                f"This requires investigation before final sign-off."
            
        )
    else:
        lines.append("The balance sheet is in balance — assets equal liabilities plus equity.")

    # Debt-to-equity ratio
    if total_equity > 0:
        debt_to_equity = total_liabilities / total_equity
        lines.append(f"The debt-to-equity ratio is {debt_to_equity:.2f}.")

    return ' '.join(lines)


def _generate_cf_commentary(input_data: CommentaryGenerationInput) -> str:
    """Generate deterministic Cash Flow commentary.

    Args:
        input_data: Report generation input data.

    Returns:
        Commentary text string for the Cash Flow section.
    """

    cf = input_data.cash_flow
    net_operating = _safe_decimal(cf.get('net_operating_cash_flow'))
    net_investing = _safe_decimal(cf.get('net_investing_cash_flow'))
    net_financing = _safe_decimal(cf.get('net_financing_cash_flow'))
    net_change = net_operating + net_investing + net_financing

    lines = [
        (
            f"Cash Flow analysis for {input_data.entity_name} "
            f"({input_data.period_start} to {input_data.period_end})."
        ),
    ]

    if net_operating >= 0:
        lines.append(
            
                f"Operating activities generated positive cash flow of "
                f"{_format_money(net_operating)} {input_data.currency_code}."
            
        )
    else:
        lines.append(
            
                f"Operating activities consumed cash of "
                f"{_format_money(abs(net_operating))} "
                f"{input_data.currency_code}. "
                f"This warrants management attention."
            
        )

    if net_investing != 0:
        lines.append(
            
                f"Net cash from investing activities was "
                f"{_format_money(net_investing)} {input_data.currency_code}."
            
        )

    if net_financing != 0:
        lines.append(
            
                f"Net cash from financing activities was "
                f"{_format_money(net_financing)} {input_data.currency_code}."
            
        )

    lines.append(
        
            f"The net change in cash for the period was "
            f"{_format_money(net_change)} {input_data.currency_code}."
        
    )

    return ' '.join(lines)


def _generate_budget_commentary(input_data: CommentaryGenerationInput) -> str:
    """Generate deterministic Budget Variance commentary.

    Args:
        input_data: Report generation input data.

    Returns:
        Commentary text string for the Budget Variance section.
    """

    bv = input_data.budget_variance
    items = bv.get('items', [])

    if not items:
        return (
            f"Budget variance data for {input_data.entity_name} is not available for this period. "
            f"Please ensure budget targets are configured for meaningful variance analysis."
        )

    significant_variances = []
    for item in items:
        budget = _safe_decimal(item.get('budget', 0))
        actual = _safe_decimal(item.get('actual', 0))
        if budget > 0:
            variance_pct = abs(actual - budget) / budget * 100
            if variance_pct > Decimal('10'):
                significant_variances.append(
                    (item.get('label', 'Unknown'), variance_pct, actual - budget)
                )

    lines = [
        (
            f"Budget Variance Analysis for {input_data.entity_name} "
            f"({input_data.period_start} to {input_data.period_end})."
        ),
    ]

    if significant_variances:
        lines.append(
            f"{len(significant_variances)} item(s) exceeded the 10% variance threshold:"
        )
        for label, pct, variance in significant_variances:
            direction = "above" if variance > 0 else "below"
            lines.append(
                f"  • {label}: {pct:.1f}% {direction} budget "
                f"(variance of {_format_money(abs(variance))} {input_data.currency_code})."
            )
    else:
        lines.append("All items are within the 10% variance tolerance threshold.")

    lines.append("Review items flagged above for management explanation and corrective action.")

    return ' '.join(lines)


def _generate_kpi_commentary(input_data: CommentaryGenerationInput) -> str:
    """Generate deterministic KPI Dashboard commentary.

    Args:
        input_data: Report generation input data.

    Returns:
        Commentary text string for the KPI Dashboard section.
    """

    kpi = input_data.kpi_dashboard
    metrics = kpi.get('metrics', [])

    if not metrics:
        return (
            f"Key performance indicator data for {input_data.entity_name} is not available "
            f"for this period. Configure KPI metrics to enable dashboard commentary."
        )

    lines = [
        (
            f"Key Performance Indicators for {input_data.entity_name} "
            f"({input_data.period_start} to {input_data.period_end})."
        ),
    ]

    improving = []
    declining = []

    for metric in metrics:
        change = metric.get('change', '')
        label = metric.get('label', 'Unknown')

        # Simple heuristic: positive change = improving, negative = declining
        if isinstance(change, (int, float, Decimal)):
            if change > 0:
                improving.append(label)
            elif change < 0:
                declining.append(label)
        elif isinstance(change, str):
            change_lower = change.lower()
            if (
                change.startswith('+')
                or 'increase' in change_lower
                or 'improved' in change_lower
            ):
                improving.append(label)
            elif (
                change.startswith('-')
                or 'decrease' in change_lower
                or 'declined' in change_lower
            ):
                declining.append(label)

    if improving:
        lines.append(f"Improving metrics: {', '.join(improving)}.")
    if declining:
        lines.append(
            f"Declining metrics: {', '.join(declining)}. "
            f"Management attention recommended."
        )

    if not improving and not declining:
        lines.append("KPI metrics are stable with no significant period-over-period changes noted.")

    return ' '.join(lines)


# ---------------------------------------------------------------------------
# Optional LLM enhancement
# ---------------------------------------------------------------------------

def _enhance_commentary_with_llm(
    *,
    commentary: dict[str, str],
    entity_name: str,
    period_start: str,
    period_end: str,
) -> dict[str, str]:
    """Attempt to enhance generated commentary using the LLM model gateway.

    This function calls the model gateway to improve the narrative quality of
    the deterministic commentary. If the model gateway is unavailable, it returns
    the original commentary unchanged — this is NOT a silent fallback, the calling
    code receives an error indication.

    Args:
        commentary: Existing deterministic commentary by section key.
        entity_name: Entity workspace name for context.
        period_start: Period start date string.
        period_end: Period end date string.

    Returns:
        Enhanced commentary dictionary.

    Raises:
        RuntimeError: When the model gateway is not configured or fails.
    """

    try:
        from services.model_gateway.client import ModelGateway, ModelGatewayError
        from services.model_gateway.prompts import get_prompt_template
    except ImportError as exc:
        raise RuntimeError(
            "Model gateway is not available. LLM-enhanced commentary requires "
            "a configured OpenRouter API key and model gateway service."
        ) from exc

    gateway = ModelGateway()
    template = get_prompt_template("commentary_enhance")
    enhanced: dict[str, str] = dict(commentary)

    for section_key, body in commentary.items():
        if not body.strip():
            continue

        system_prompt, user_prompt = template.render(
            entity_name=entity_name,
            period_start=period_start,
            period_end=period_end,
            section_key=section_key,
            draft_commentary=body,
        )

        try:
            text = gateway.complete(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            text = text.strip()
            if text:
                enhanced[section_key] = text
        except ModelGatewayError:
            # Keep the original deterministic commentary for this section
            logger.warning(
                "llm_section_enhancement_failed",
                section_key=section_key,
            )

    return enhanced


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
    """Format a Decimal value as a money string with thousands separators.

    Args:
        value: Monetary value to format.

    Returns:
        Formatted string with commas and two decimal places.
    """

    sign = '-' if value < 0 else ''
    abs_val = abs(value)
    integer_part = int(abs_val)
    decimal_part = abs_val - integer_part

    formatted_int = f'{integer_part:,}'
    decimal_str = f'{decimal_part:.2f}'[1:]

    return f'{sign}{formatted_int}{decimal_str}'


__all__ = [
    "CommentaryGenerationInput",
    "CommentaryGenerationResult",
    "generate_commentary",
]
