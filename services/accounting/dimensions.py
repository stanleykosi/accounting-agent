"""
Purpose: Provide helpers for cost centre, department, and project dimensions.
Scope: Dimensional accounting helpers for GL coding and reporting.
Dependencies: Shared utilities, settings.
"""

from __future__ import annotations

from typing import Any

from services.common.settings import AppSettings, get_settings


class DimensionError(Exception):
    """Raised when dimension operations fail."""


class DimensionHelper:
    """Manage cost centre, department, and project dimensions for accounting transactions."""

    def __init__(self, settings: AppSettings | None = None):
        self.settings = settings or get_settings()

        # Load default dimensions from settings
        self._default_cost_centre = self.settings.get("default_cost_centre", "HEADQUARTERS")
        self._default_department = self.settings.get("default_department", "ADMINISTRATION")
        self._default_project = self.settings.get("default_project", "OPERATIONS")

        # Load valid dimension values from settings (if specified)
        self._valid_cost_centres = set(
            cc.strip().upper()
            for cc in self.settings.get("valid_cost_centres", "").split(",")
            if cc.strip()
        )
        self._valid_departments = set(
            dept.strip().upper()
            for dept in self.settings.get("valid_departments", "").split(",")
            if dept.strip()
        )
        self._valid_projects = set(
            proj.strip().upper()
            for proj in self.settings.get("valid_projects", "").split(",")
            if proj.strip()
        )

        # If no valid values specified, we'll accept any non-empty value
        self._restrict_to_valid_values = bool(
            self._valid_cost_centres or self._valid_departments or self._valid_projects
        )

    def normalize_dimension(self, value: str | None, dimension_type: str) -> str | None:
        """
        Normalize a dimension value (cost centre, department, project).

        Args:
            value: Dimension value to normalize
            dimension_type: Type of dimension ('cost_centre', 'department', 'project')

        Returns:
            Normalized dimension value or None if invalid
        """
        if not value or not isinstance(value, str):
            return None

        # Convert to uppercase and strip whitespace
        normalized = value.strip().upper()
        if not normalized:
            return None

        # Replace multiple spaces with single underscore
        normalized = "_".join(normalized.split())

        # Validate against allowed values if restrictions are enabled
        if self._restrict_to_valid_values:
            if dimension_type == "cost_centre" and self._valid_cost_centres:
                if normalized not in self._valid_cost_centres:
                    return None
            elif dimension_type == "department" and self._valid_departments:
                if normalized not in self._valid_departments:
                    return None
            elif dimension_type == "project" and self._valid_projects:
                if normalized not in self._valid_projects:
                    return None

        return normalized

    def get_default_dimensions(self) -> dict[str, str]:
        """
        Get default dimension values.

        Returns:
            Dictionary with default cost_centre, department, and project
        """
        return {
            "cost_centre": self._default_cost_centre,
            "department": self._default_department,
            "project": self._default_project,
        }

    def suggest_dimensions(
        self,
        vendor: str | None = None,
        document_type: str | None = None,
        amount: float | None = None,
        existing_dimensions: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """
        Suggest dimension values based on transaction context.

        Args:
            vendor: Vendor name (optional)
            document_type: Type of document (optional)
            amount: Transaction amount (optional)
            existing_dimensions: Currently assigned dimensions (optional)

        Returns:
            Dictionary with suggested dimension values
        """
        # Start with existing dimensions or defaults
        suggestions = existing_dimensions.copy() if existing_dimensions else {}
        defaults = self.get_default_dimensions()

        # Apply defaults for missing dimensions
        for dim, default_value in defaults.items():
            if dim not in suggestions or not suggestions[dim]:
                suggestions[dim] = default_value

        # Apply vendor-based suggestions
        if vendor:
            vendor_upper = vendor.upper().strip()
            # In a real implementation, this would look up vendor-specific defaults
            # For now, we'll use some simple heuristics
            if "TRANSPORT" in vendor_upper or "LOGISTICS" in vendor_upper:
                suggestions["cost_centre"] = "OPERATIONS"
                suggestions["department"] = "LOGISTICS"
            elif "IT" in vendor_upper or "TECH" in vendor_upper:
                suggestions["cost_centre"] = "TECHNOLOGY"
                suggestions["department"] = "IT"
            elif "HR" in vendor_upper or "STAFF" in vendor_upper:
                suggestions["department"] = "HUMAN_RESOURCES"

        # Apply document type suggestions
        if document_type:
            doc_upper = document_type.upper().strip()
            if "TRAVEL" in doc_upper:
                suggestions["cost_centre"] = "TRAVEL"
                suggestions["department"] = "ADMINISTRATION"
            elif "MARKETING" in doc_upper or "ADVERT" in doc_upper:
                suggestions["cost_centre"] = "MARKETING"
                suggestions["department"] = "MARKETING"
            elif "RENT" in doc_upper or "LEASE" in doc_upper:
                suggestions["cost_centre"] = "FACILITIES"
                suggestions["department"] = "OPERATIONS"

        # Apply amount-based suggestions (simplified)
        if amount is not None:
            if amount > 1000000:  # Large capital expenditures
                suggestions["cost_centre"] = "CAPITAL"
                suggestions["project"] = "CAPEX"
            elif amount < 100:  # Small expenses
                suggestions["cost_centre"] = "PETTY_CASH"

        return suggestions

    def validate_dimensions(
        self,
        dimensions: dict[str, str] | None = None,
    ) -> tuple[bool, list[str]]:
        """
        Validate dimension values.

        Args:
            dimensions: Dictionary of dimension values to validate

        Returns:
            Tuple of (is_valid, list_of_errors)
        """
        errors = []

        if not dimensions:
            dimensions = {}

        # Check each dimension type
        for dim_type in ["cost_centre", "department", "project"]:
            value = dimensions.get(dim_type)
            normalized = self.normalize_dimension(value, dim_type)

            if value is not None and normalized is None:
                errors.append(f"Invalid {dim_type}: {value}")
            elif value is None and self._get_required_for_dimension(dim_type):
                errors.append(f"{dim_type} is required")
            elif normalized is not None:
                dimensions[dim_type] = normalized  # Update with normalized value

        return len(errors) == 0, errors

    def _get_required_for_dimension(self, dimension_type: str) -> bool:
        """Check if a dimension type is required based on settings."""
        required_settings = {
            "cost_centre": self.settings.get("dimension_cost_centre_required", False),
            "department": self.settings.get("dimension_department_required", False),
            "project": self.settings.get("dimension_project_required", False),
        }
        return required_settings.get(dimension_type, False)

    def get_dimension_hierarchy(self) -> dict[str, list[str]]:
        """
        Get dimension hierarchy for roll-up reporting.

        Returns:
            Dictionary mapping parent dimensions to child dimensions
        """
        # In a real implementation, this would come from a database or configuration
        # For now, return a simple hierarchy
        return {
            "cost_centre": ["HEADQUARTERS", "REGIONAL_OFFICES", "DEPARTMENTS"],
            "department": ["CORPORATE", "OPERATIONS", "SUPPORT"],
            "project": ["OPERATIONS", "CAPEX", "OPEX"],
        }


def get_dimension_helper() -> DimensionHelper:
    """Factory function to create a DimensionHelper instance."""
    return DimensionHelper()


__all__ = [
    "DimensionError",
    "DimensionHelper",
    "get_dimension_helper",
]
