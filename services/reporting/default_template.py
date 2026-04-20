"""
Purpose: Define the canonical built-in report template used when no entity override exists.
Scope: Default global template metadata, required section ordering, and guardrail payloads.
Dependencies: Canonical report section enums and reporting guardrails.
"""

from __future__ import annotations

from services.common.enums import ReportSectionKey
from services.reporting.guardrails import DEFAULT_GUARDRAIL_CONFIG

DEFAULT_GLOBAL_REPORT_TEMPLATE_NAME = "Canonical Management Pack"
DEFAULT_GLOBAL_REPORT_TEMPLATE_DESCRIPTION = (
    "Built-in five-section management reporting pack used when an entity has not "
    "activated a custom report template."
)


def build_default_global_template_sections() -> tuple[dict[str, object], ...]:
    """Return the canonical ordered section payloads for the built-in report template."""

    section_order = (
        ReportSectionKey.PROFIT_AND_LOSS,
        ReportSectionKey.BALANCE_SHEET,
        ReportSectionKey.CASH_FLOW,
        ReportSectionKey.BUDGET_VARIANCE,
        ReportSectionKey.KPI_DASHBOARD,
    )
    return tuple(
        {
            "section_key": section_key.value,
            "label": section_key.label,
            "display_order": index,
            "is_required": True,
            "section_config": {},
        }
        for index, section_key in enumerate(section_order)
    )


def build_default_global_guardrail_config() -> dict[str, object]:
    """Return the canonical guardrail payload for the built-in report template."""

    return {
        "required_section_keys": [
            section_key.value for section_key in DEFAULT_GUARDRAIL_CONFIG.required_section_keys
        ],
        "allow_custom_sections": DEFAULT_GUARDRAIL_CONFIG.allow_custom_sections,
        "minimum_section_count": DEFAULT_GUARDRAIL_CONFIG.minimum_section_count,
    }


__all__ = [
    "DEFAULT_GLOBAL_REPORT_TEMPLATE_DESCRIPTION",
    "DEFAULT_GLOBAL_REPORT_TEMPLATE_NAME",
    "build_default_global_guardrail_config",
    "build_default_global_template_sections",
]
