"""
Purpose: Mark the reporting package boundary for accountant-facing output generation.
Scope: Report templates, guardrails, commentary, Excel/PDF rendering,
evidence-pack generation, and artifact packaging workflows.
Dependencies: Shared service infrastructure, storage repositories,
close-run/report contracts, and canonical workflow enums.
"""

from services.reporting.commentary import (
    CommentaryGenerationInput,
    CommentaryGenerationResult,
    generate_commentary,
)
from services.reporting.excel_builder import (
    ExcelReportInput,
    ExcelReportResult,
    build_excel_report_pack,
)
from services.reporting.guardrails import (
    CANONICAL_REQUIRED_SECTIONS,
    DEFAULT_GUARDRAIL_CONFIG,
    GuardrailConfig,
    build_guardrail_config_from_template,
    validate_template_guardrails,
)
from services.reporting.pdf_builder import (
    PdfReportInput,
    PdfReportResult,
    build_pdf_report_pack,
)

__all__ = [
    "CANONICAL_REQUIRED_SECTIONS",
    "CommentaryGenerationInput",
    "CommentaryGenerationResult",
    "DEFAULT_GUARDRAIL_CONFIG",
    "ExcelReportInput",
    "ExcelReportResult",
    "GuardrailConfig",
    "PdfReportInput",
    "PdfReportResult",
    "build_excel_report_pack",
    "build_guardrail_config_from_template",
    "build_pdf_report_pack",
    "generate_commentary",
    "validate_template_guardrails",
]
