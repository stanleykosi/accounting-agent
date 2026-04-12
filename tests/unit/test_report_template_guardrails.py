"""
Purpose: Unit tests for report-template guardrail validation logic.
Scope: Mandatory section enforcement, custom-section policies, minimum-count rules,
unknown-section rejection, and guardrail-config override behavior.
Dependencies: pytest, canonical enums, and guardrail validation service.
"""

from __future__ import annotations

import pytest
from services.common.enums import ReportSectionKey
from services.reporting.guardrails import (
    CANONICAL_REQUIRED_SECTIONS,
    DEFAULT_GUARDRAIL_CONFIG,
    build_guardrail_config_from_template,
    validate_template_guardrails,
)


def _canonical_section_keys() -> list[str]:
    """Return the five mandatory section keys in canonical order."""

    return [key.value for key in ReportSectionKey]


def _canonical_is_required_map() -> dict[str, bool]:
    """Return a map marking all canonical sections as required."""

    return {key.value: True for key in ReportSectionKey}


class TestGuardrailConfigDefaults:
    """Verify default guardrail policy constants."""

    def test_canonical_required_sections_count(self) -> None:
        """The canonical set must contain exactly five mandatory sections."""

        assert len(CANONICAL_REQUIRED_SECTIONS) == 5

    def test_canonical_required_sections_contains_all_keys(self) -> None:
        """Every ReportSectionKey enum member must be in the canonical required set."""

        for key in ReportSectionKey:
            assert key in CANONICAL_REQUIRED_SECTIONS

    def test_default_config_allows_custom_sections(self) -> None:
        """The default guardrail config permits custom sections beyond the canonical five."""

        assert DEFAULT_GUARDRAIL_CONFIG.allow_custom_sections is True

    def test_default_config_minimum_count(self) -> None:
        """The default minimum section count must match the canonical section count."""

        assert DEFAULT_GUARDRAIL_CONFIG.minimum_section_count == 5


class TestValidTemplatePassesGuardrails:
    """A template with all required sections present and required must pass."""

    def test_canonical_template_is_valid(self) -> None:
        """A template containing exactly the five canonical sections passes validation."""

        section_keys = _canonical_section_keys()
        is_required_map = _canonical_is_required_map()

        result = validate_template_guardrails(
            template_id="test-template",
            section_keys=section_keys,
            section_is_required_map=is_required_map,
        )

        assert result.is_valid is True
        assert result.violations == ()

    def test_canonical_template_plus_custom_section_is_valid(self) -> None:
        """Extra custom sections are permitted when allow_custom_sections is True."""

        section_keys = [*_canonical_section_keys(), "custom_notes"]
        is_required_map = _canonical_is_required_map()
        is_required_map["custom_notes"] = False

        result = validate_template_guardrails(
            template_id="test-template",
            section_keys=section_keys,
            section_is_required_map=is_required_map,
        )

        assert result.is_valid is True
        assert result.violations == ()


class TestMissingRequiredSectionFailsGuardrails:
    """Removing any mandatory section must produce a guardrail violation."""

    @pytest.mark.parametrize(
        "removed_key",
        [key.value for key in ReportSectionKey],
    )
    def test_missing_one_required_section(self, removed_key: str) -> None:
        """Omitting one required section produces exactly one missing_required_section violation."""

        section_keys = [k for k in _canonical_section_keys() if k != removed_key]
        is_required_map = {k: True for k in section_keys}

        result = validate_template_guardrails(
            template_id="test-template",
            section_keys=section_keys,
            section_is_required_map=is_required_map,
        )

        assert result.is_valid is False
        missing_violations = [
            v for v in result.violations if v.violation_type == "missing_required_section"
        ]
        assert len(missing_violations) == 1
        assert missing_violations[0].section_key == removed_key


class TestRequiredSectionNotFlaggedFailsGuardrails:
    """A canonical section present but not marked required must fail."""

    def test_canonical_section_not_flagged_required(self) -> None:
        """A profit_and_loss section with is_required=False produces a violation."""

        section_keys = _canonical_section_keys()
        is_required_map = _canonical_is_required_map()
        is_required_map[ReportSectionKey.PROFIT_AND_LOSS.value] = False

        result = validate_template_guardrails(
            template_id="test-template",
            section_keys=section_keys,
            section_is_required_map=is_required_map,
        )

        assert result.is_valid is False
        flagged_violations = [
            v for v in result.violations if v.violation_type == "required_section_not_flagged"
        ]
        assert len(flagged_violations) == 1
        assert flagged_violations[0].section_key == ReportSectionKey.PROFIT_AND_LOSS.value


class TestMinimumSectionCountEnforcement:
    """Templates below the minimum section count must fail."""

    def test_below_minimum_count(self) -> None:
        """Only three sections when five are required produces an insufficient count violation."""

        section_keys = [
            ReportSectionKey.PROFIT_AND_LOSS.value,
            ReportSectionKey.BALANCE_SHEET.value,
            ReportSectionKey.CASH_FLOW.value,
        ]
        is_required_map = {k: True for k in section_keys}

        result = validate_template_guardrails(
            template_id="test-template",
            section_keys=section_keys,
            section_is_required_map=is_required_map,
        )

        assert result.is_valid is False
        count_violations = [
            v for v in result.violations if v.violation_type == "insufficient_section_count"
        ]
        assert len(count_violations) == 1


class TestCustomSectionPolicy:
    """Custom section allowance controls whether non-canonical sections are permitted."""

    def test_custom_sections_disallowed_rejects_unknown(self) -> None:
        """When allow_custom_sections is False, unknown section keys produce violations."""

        section_keys = [*_canonical_section_keys(), "custom_notes"]
        is_required_map = _canonical_is_required_map()
        is_required_map["custom_notes"] = False

        guardrail_config = {
            "required_section_keys": [k.value for k in ReportSectionKey],
            "allow_custom_sections": False,
            "minimum_section_count": 5,
        }

        result = validate_template_guardrails(
            template_id="test-template",
            section_keys=section_keys,
            section_is_required_map=is_required_map,
            guardrail_config=guardrail_config,
        )

        assert result.is_valid is False
        unknown_violations = [
            v for v in result.violations if v.violation_type == "unknown_section_key"
        ]
        assert len(unknown_violations) == 1
        assert unknown_violations[0].section_key == "custom_notes"

    def test_custom_sections_allowed_passes(self) -> None:
        """When allow_custom_sections is True, unknown section keys do not produce violations."""

        section_keys = [*_canonical_section_keys(), "custom_notes"]
        is_required_map = _canonical_is_required_map()
        is_required_map["custom_notes"] = False

        guardrail_config = {
            "required_section_keys": [k.value for k in ReportSectionKey],
            "allow_custom_sections": True,
            "minimum_section_count": 5,
        }

        result = validate_template_guardrails(
            template_id="test-template",
            section_keys=section_keys,
            section_is_required_map=is_required_map,
            guardrail_config=guardrail_config,
        )

        assert result.is_valid is True
        assert result.violations == ()


class TestGuardrailConfigOverride:
    """Template-level guardrail_config overrides must affect validation behavior."""

    def test_config_parsed_from_jsonb(self) -> None:
        """build_guardrail_config_from_template correctly parses a JSONB-like dict.

        User-supplied required_section_keys are merged with the canonical set,
        never replacing it, so the result is always a superset of canonical keys.
        """

        guardrail_config = {
            "required_section_keys": [
                ReportSectionKey.PROFIT_AND_LOSS.value,
                ReportSectionKey.BALANCE_SHEET.value,
            ],
            "allow_custom_sections": False,
            "minimum_section_count": 2,
        }

        config = build_guardrail_config_from_template(guardrail_config=guardrail_config)

        # Canonical keys are always present regardless of user input.
        for key in ReportSectionKey:
            assert key in config.required_section_keys
        assert config.allow_custom_sections is False
        assert config.minimum_section_count == 2

    def test_empty_config_uses_defaults(self) -> None:
        """An empty guardrail_config dict falls back to canonical defaults."""

        config = build_guardrail_config_from_template(guardrail_config={})

        assert config.required_section_keys == CANONICAL_REQUIRED_SECTIONS
        assert config.allow_custom_sections is True
        assert config.minimum_section_count == 5

    def test_partial_config_merges_with_defaults(self) -> None:
        """A partial guardrail_config fills missing keys from defaults."""

        guardrail_config = {"allow_custom_sections": False}

        config = build_guardrail_config_from_template(guardrail_config=guardrail_config)

        assert config.required_section_keys == CANONICAL_REQUIRED_SECTIONS
        assert config.allow_custom_sections is False
        assert config.minimum_section_count == 5
