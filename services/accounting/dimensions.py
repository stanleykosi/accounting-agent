"""
Purpose: Provide deterministic helpers for cost centre, department, and project dimensions.
Scope: Dimension normalization, validation, defaulting, and context-based assignment for GL coding.
Dependencies: Python dataclasses only; callers provide any entity-specific catalog values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


class DimensionError(ValueError):
    """Represent an invalid accounting dimension value or catalog configuration."""


class DimensionType(StrEnum):
    """Enumerate dimensions currently supported by the accounting engine."""

    COST_CENTRE = "cost_centre"
    DEPARTMENT = "department"
    PROJECT = "project"


@dataclass(frozen=True, slots=True)
class DimensionDefaults:
    """Describe the default dimensions applied when no stronger rule exists."""

    cost_centre: str = "HEADQUARTERS"
    department: str = "ADMINISTRATION"
    project: str = "OPERATIONS"

    def as_dict(self) -> dict[str, str]:
        """Return the defaults in canonical dimension-key format."""

        return {
            DimensionType.COST_CENTRE.value: self.cost_centre,
            DimensionType.DEPARTMENT.value: self.department,
            DimensionType.PROJECT.value: self.project,
        }


@dataclass(frozen=True, slots=True)
class DimensionCatalog:
    """Describe optional allowed values and required flags for dimensions."""

    allowed_values: dict[str, frozenset[str]] = field(default_factory=dict)
    required_dimensions: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class DimensionValidationResult:
    """Describe the result of validating assigned dimensions."""

    is_valid: bool
    normalized_dimensions: dict[str, str]
    errors: tuple[str, ...]


class DimensionHelper:
    """Normalize, validate, and suggest dimension assignments deterministically."""

    def __init__(
        self,
        *,
        defaults: DimensionDefaults | None = None,
        catalog: DimensionCatalog | None = None,
    ) -> None:
        """Capture entity-specific defaults and optional allowed-value catalogs."""

        self._defaults = defaults or DimensionDefaults()
        self._catalog = catalog or DimensionCatalog()

    def normalize_dimension(self, value: str | None, dimension_type: str) -> str | None:
        """Normalize one dimension value and reject values outside the catalog when configured."""

        normalized_type = self._normalize_dimension_type(dimension_type)
        if value is None:
            return None
        normalized_value = "_".join(value.strip().upper().split())
        if not normalized_value:
            return None

        allowed_values = self._catalog.allowed_values.get(normalized_type)
        if allowed_values and normalized_value not in allowed_values:
            raise DimensionError(
                f"{normalized_type} value {normalized_value} is not in the configured catalog."
            )
        return normalized_value

    def get_default_dimensions(self) -> dict[str, str]:
        """Return normalized default dimension values."""

        return {
            key: self.normalize_dimension(value, key) or value
            for key, value in self._defaults.as_dict().items()
        }

    def merge_dimensions(
        self,
        *,
        base_dimensions: dict[str, str] | None = None,
        override_dimensions: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Merge defaults, base dimensions, and overrides into normalized canonical dimensions."""

        merged = self.get_default_dimensions()
        for source in (base_dimensions or {}, override_dimensions or {}):
            for dimension_type, value in source.items():
                normalized_type = self._normalize_dimension_type(dimension_type)
                normalized_value = self.normalize_dimension(value, normalized_type)
                if normalized_value is not None:
                    merged[normalized_type] = normalized_value
        return merged

    def suggest_dimensions(
        self,
        *,
        vendor: str | None = None,
        document_type: str | None = None,
        amount: Decimal | None = None,
        existing_dimensions: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Suggest deterministic dimensions from simple vendor, document, and amount signals."""

        suggestions = self.merge_dimensions(base_dimensions=existing_dimensions)
        vendor_text = (vendor or "").upper()
        document_text = (document_type or "").upper()

        if any(token in vendor_text for token in ("TRANSPORT", "LOGISTICS")):
            suggestions[DimensionType.COST_CENTRE.value] = "OPERATIONS"
            suggestions[DimensionType.DEPARTMENT.value] = "LOGISTICS"
        elif any(token in vendor_text for token in ("TECH", "SOFTWARE", "IT")):
            suggestions[DimensionType.COST_CENTRE.value] = "TECHNOLOGY"
            suggestions[DimensionType.DEPARTMENT.value] = "IT"
        elif any(token in vendor_text for token in ("HR", "STAFF", "PAYROLL")):
            suggestions[DimensionType.DEPARTMENT.value] = "HUMAN_RESOURCES"

        if any(token in document_text for token in ("TRAVEL", "TRANSPORT")):
            suggestions[DimensionType.COST_CENTRE.value] = "TRAVEL"
        elif any(token in document_text for token in ("MARKETING", "ADVERT")):
            suggestions[DimensionType.COST_CENTRE.value] = "MARKETING"
            suggestions[DimensionType.DEPARTMENT.value] = "MARKETING"
        elif any(token in document_text for token in ("RENT", "LEASE")):
            suggestions[DimensionType.COST_CENTRE.value] = "FACILITIES"

        if amount is not None and amount >= Decimal("1000000"):
            suggestions[DimensionType.PROJECT.value] = "CAPEX"
        return self.validate_dimensions(suggestions).normalized_dimensions

    def validate_dimensions(
        self,
        dimensions: dict[str, str] | None,
    ) -> DimensionValidationResult:
        """Validate a dimension dictionary and return normalized values plus all errors."""

        normalized_dimensions: dict[str, str] = {}
        errors: list[str] = []
        for dimension_type in DimensionType:
            source_value = (dimensions or {}).get(dimension_type.value)
            if source_value is None:
                if dimension_type.value in self._catalog.required_dimensions:
                    errors.append(f"{dimension_type.value} is required.")
                continue
            try:
                normalized_value = self.normalize_dimension(source_value, dimension_type.value)
            except DimensionError as error:
                errors.append(str(error))
                continue
            if normalized_value is not None:
                normalized_dimensions[dimension_type.value] = normalized_value

        return DimensionValidationResult(
            is_valid=not errors,
            normalized_dimensions=normalized_dimensions,
            errors=tuple(errors),
        )

    def _normalize_dimension_type(self, value: str) -> str:
        """Normalize and validate a dimension type key."""

        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized not in {dimension.value for dimension in DimensionType}:
            raise DimensionError(f"Unsupported dimension type {value!r}.")
        return normalized


def get_dimension_helper() -> DimensionHelper:
    """Create the deterministic dimension helper with default local-demo settings."""

    return DimensionHelper()


__all__ = [
    "DimensionCatalog",
    "DimensionDefaults",
    "DimensionError",
    "DimensionHelper",
    "DimensionType",
    "DimensionValidationResult",
    "get_dimension_helper",
]
