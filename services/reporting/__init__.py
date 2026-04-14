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
from services.reporting.evidence_pack import (
    EvidencePackInput,
    EvidencePackResult,
    build_evidence_pack,
    upload_evidence_pack,
)
from services.reporting.exports import (
    ExportManifestBuilder,
    ExportManifestResult,
    build_export_manifest,
)
from services.reporting.guardrails import (
    CANONICAL_REQUIRED_SECTIONS,
    DEFAULT_GUARDRAIL_CONFIG,
    GuardrailConfig,
    build_guardrail_config_from_template,
    validate_template_guardrails,
)

__all__ = [
    "CANONICAL_REQUIRED_SECTIONS",
    "CommentaryGenerationInput",
    "CommentaryGenerationResult",
    "DEFAULT_GUARDRAIL_CONFIG",
    "EvidencePackInput",
    "EvidencePackResult",
    "ExcelReportInput",
    "ExcelReportResult",
    "ExportManifestBuilder",
    "ExportManifestResult",
    "GuardrailConfig",
    "build_evidence_pack",
    "build_excel_report_pack",
    "build_export_manifest",
    "build_guardrail_config_from_template",
    "generate_commentary",
    "upload_evidence_pack",
    "validate_template_guardrails",
]
