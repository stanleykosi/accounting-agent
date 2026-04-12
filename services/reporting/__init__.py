"""
Purpose: Mark the reporting package boundary for accountant-facing output generation.
Scope: Report templates, guardrails, commentary, Excel/PDF rendering,
evidence-pack generation, and artifact packaging workflows.
Dependencies: Shared service infrastructure, storage repositories,
close-run/report contracts, and canonical workflow enums.
"""

from services.reporting.guardrails import (
    CANONICAL_REQUIRED_SECTIONS,
    DEFAULT_GUARDRAIL_CONFIG,
    GuardrailConfig,
    build_guardrail_config_from_template,
    validate_template_guardrails,
)

__all__ = [
    "CANONICAL_REQUIRED_SECTIONS",
    "DEFAULT_GUARDRAIL_CONFIG",
    "GuardrailConfig",
    "build_guardrail_config_from_template",
    "validate_template_guardrails",
]
