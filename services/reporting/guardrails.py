"""
Purpose: Enforce mandatory section guardrails on report templates.
Scope: Validation logic that prevents custom templates or entity overrides
from removing required 5-phase workflow report sections.
Dependencies: Canonical enums, contract models, and shared type definitions.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.common.enums import ReportSectionKey
from services.common.types import JsonObject
from services.contracts.report_models import GuardrailValidationResponse, GuardrailViolation


@dataclass(frozen=True, slots=True)
class GuardrailConfig:
    """Define the guardrail policy applied to a report template.

    The config determines which sections are mandatory, whether custom sections
    beyond the canonical five are permitted, and what minimum section count
    the template must maintain.
    """

    required_section_keys: frozenset[ReportSectionKey]
    allow_custom_sections: bool = False
    minimum_section_count: int = 5


# The canonical set of mandatory sections derived from the 5-phase workflow
# reporting requirements.  These sections must appear in every valid template.
CANONICAL_REQUIRED_SECTIONS: frozenset[ReportSectionKey] = frozenset(
    {
        ReportSectionKey.PROFIT_AND_LOSS,
        ReportSectionKey.BALANCE_SHEET,
        ReportSectionKey.CASH_FLOW,
        ReportSectionKey.BUDGET_VARIANCE,
        ReportSectionKey.KPI_DASHBOARD,
    }
)

DEFAULT_GUARDRAIL_CONFIG = GuardrailConfig(
    required_section_keys=CANONICAL_REQUIRED_SECTIONS,
    allow_custom_sections=True,
    minimum_section_count=5,
)


def build_guardrail_config_from_template(
    *,
    guardrail_config: JsonObject,
) -> GuardrailConfig:
    """Parse a template's guardrail_config JSONB into a validated GuardrailConfig.

    Missing keys fall back to the canonical defaults so that older templates
    without explicit guardrail metadata still enforce the mandatory sections.

    Crucially, any user-supplied required_section_keys are merged with the
    canonical required set.  Custom templates must never be able to remove
    mandatory reporting sections from the guardrail policy.
    """

    required_keys_raw = guardrail_config.get("required_section_keys")
    if isinstance(required_keys_raw, list):
        user_required: frozenset[ReportSectionKey] = frozenset(
            _resolve_section_key(key) for key in required_keys_raw if isinstance(key, str)
        )
    else:
        user_required = frozenset()

    # Merge user-supplied keys with the canonical required set.  This prevents
    # entity-level overrides from removing mandatory workflow sections.
    required_keys = CANONICAL_REQUIRED_SECTIONS | user_required

    allow_custom = bool(guardrail_config.get("allow_custom_sections", True))
    minimum_count_raw = guardrail_config.get("minimum_section_count", 5)
    minimum_count = (
        int(minimum_count_raw)
        if isinstance(minimum_count_raw, (int, str, float))
        else 5
    )

    return GuardrailConfig(
        required_section_keys=required_keys,
        allow_custom_sections=allow_custom,
        minimum_section_count=minimum_count,
    )


def validate_template_guardrails(
    *,
    template_id: str,
    section_keys: list[str],
    section_is_required_map: dict[str, bool],
    guardrail_config: JsonObject | None = None,
) -> GuardrailValidationResponse:
    """Validate a template's sections against the guardrail policy.

    Args:
        template_id: UUID string of the template being validated.
        section_keys: Ordered list of section keys defined in the template.
        section_is_required_map: Mapping from section key to whether it is marked required.
        guardrail_config: Optional template-specific guardrail overrides.

    Returns:
        GuardrailValidationResponse with is_valid flag and any violations found.
    """

    config = (
        build_guardrail_config_from_template(guardrail_config=guardrail_config)
        if guardrail_config
        else DEFAULT_GUARDRAIL_CONFIG
    )

    violations: list[GuardrailViolation] = []
    section_key_set = set(section_keys)
    canonical_key_set = {key.value for key in ReportSectionKey}

    # Check 1: All required sections must be present.
    for required_key in config.required_section_keys:
        if required_key.value not in section_key_set:
            violations.append(
                GuardrailViolation(
                    violation_type="missing_required_section",
                    section_key=required_key.value,
                    message=(
                        f"Required section '{required_key.value}' is missing. "
                        "Template guardrails prevent removal of mandatory reporting sections."
                    ),
                )
            )

    # Check 2: Required sections must actually be marked as required.
    for section_key in section_keys:
        if (
            section_key in {k.value for k in config.required_section_keys}
            and not section_is_required_map.get(section_key, False)
        ):
            violations.append(
                GuardrailViolation(
                    violation_type="required_section_not_flagged",
                    section_key=section_key,
                    message=(
                        f"Section '{section_key}' is a mandatory workflow section "
                        "but is not marked as required in the template definition."
                    ),
                )
            )

    # Check 3: Minimum section count.
    if len(section_keys) < config.minimum_section_count:
        violations.append(
            GuardrailViolation(
                violation_type="insufficient_section_count",
                section_key=None,
                message=(
                    f"Template has {len(section_keys)} sections but the guardrail policy "
                    f"requires at least {config.minimum_section_count}."
                ),
            )
        )

    # Check 4: Unknown section keys that are not canonical (only if custom sections disallowed).
    if not config.allow_custom_sections:
        for section_key in section_keys:
            if section_key not in canonical_key_set:
                violations.append(
                    GuardrailViolation(
                        violation_type="unknown_section_key",
                        section_key=section_key,
                        message=(
                            f"Section key '{section_key}' is not a recognized canonical section. "
                            "Custom sections are not permitted by this template's guardrail policy."
                        ),
                    )
                )

    return GuardrailValidationResponse(
        template_id=template_id,
        is_valid=len(violations) == 0,
        violations=tuple(violations),
    )


def _resolve_section_key(value: str) -> ReportSectionKey:
    """Resolve a string section key into the canonical enum or fail fast."""

    for key in ReportSectionKey:
        if key.value == value:
            return key

    raise ValueError(f"Unsupported report section key value: {value}")


__all__ = [
    "CANONICAL_REQUIRED_SECTIONS",
    "DEFAULT_GUARDRAIL_CONFIG",
    "GuardrailConfig",
    "build_guardrail_config_from_template",
    "validate_template_guardrails",
]
