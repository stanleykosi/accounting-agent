"""
Purpose: Define strict Pydantic API contracts for report-template and commentary workflows.
Scope: Template creation, activation, listing, section definitions, report-run summaries,
commentary payloads, and guardrail validation responses.
Dependencies: Pydantic contract defaults, canonical enums, and shared API model base.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator, model_validator
from services.contracts.api_models import ContractModel


def _normalize_required_text(value: str, *, field_name: str) -> str:
    """Trim required text values and reject blank strings."""

    normalized = value.strip()
    if normalized:
        return normalized

    raise ValueError(f"{field_name} cannot be blank.")


def _normalize_optional_text(value: str | None) -> str | None:
    """Trim optional strings and collapse blank values to null."""

    if value is None:
        return None

    normalized = value.strip()
    return normalized or None


# ---------------------------------------------------------------------------
# Section definitions
# ---------------------------------------------------------------------------

class ReportSectionDefinition(ContractModel):
    """Define one report section with guardrail metadata and display config."""

    section_key: str = Field(
        min_length=1,
        max_length=80,
        description="Stable section identifier (e.g. profit_and_loss, balance_sheet).",
    )
    label: str = Field(
        min_length=1,
        max_length=200,
        description="Human-readable section label rendered in reports and the UI.",
    )
    display_order: int = Field(
        ge=0,
        description="Zero-based rendering order within the report template.",
    )
    is_required: bool = Field(
        default=True,
        description="Whether this section is mandatory and protected by template guardrails.",
    )
    section_config: dict[str, object] = Field(
        default_factory=dict,
        description="Optional per-section configuration such as filters or format overrides.",
    )

    @field_validator("section_key")
    @classmethod
    def normalize_section_key(cls, value: str) -> str:
        """Normalize section keys to lowercase snake_case."""

        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("section_key cannot be blank.")
        return normalized

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str) -> str:
        """Normalize required section labels."""

        return _normalize_required_text(value, field_name="label")


# ---------------------------------------------------------------------------
# Template contracts
# ---------------------------------------------------------------------------

class ReportTemplateSummary(ContractModel):
    """Describe one versioned report template for list and detail views."""

    id: str = Field(description="Stable UUID for the report template.")
    entity_id: str | None = Field(
        default=None,
        description="Owning entity workspace UUID, or null for global default templates.",
    )
    source: str = Field(
        min_length=1,
        description="Template provenance: global_default or entity_custom.",
    )
    version_no: int = Field(
        ge=1,
        description="Monotonic version number within the template lineage.",
    )
    name: str = Field(min_length=1, max_length=240, description="Template display name.")
    description: str | None = Field(
        default=None,
        description="Optional template description for operator context.",
    )
    is_active: bool = Field(description="Whether this template is the active one for its scope.")
    section_count: int = Field(
        ge=0,
        description="Number of section definitions attached to this template version.",
    )
    has_required_sections: bool = Field(
        description="Whether all mandatory workflow sections are present.",
    )
    created_by_user_id: str | None = Field(
        default=None,
        description="UUID of the user who created this template version.",
    )
    created_at: datetime = Field(description="UTC timestamp when the template was created.")
    updated_at: datetime = Field(description="UTC timestamp when the template was last updated.")


class ReportTemplateDetail(ReportTemplateSummary):
    """Extend the summary with full section definitions and guardrail config."""

    sections: tuple[ReportSectionDefinition, ...] = Field(
        default=(),
        description="Ordered section definitions persisted with this template version.",
    )
    guardrail_config: dict[str, object] = Field(
        default_factory=dict,
        description="Guardrail metadata for this template.",
    )


class CreateReportTemplateRequest(ContractModel):
    """Capture the inputs required to create a new entity-scoped report template."""

    name: str = Field(min_length=1, max_length=240, description="New template display name.")
    description: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional template description.",
    )
    sections: tuple[ReportSectionDefinition, ...] = Field(
        min_length=1,
        description="Ordered section definitions for the new template.",
    )
    guardrail_config: dict[str, object] = Field(
        default_factory=dict,
        description="Optional guardrail policy overrides for this template.",
    )
    activate_immediately: bool = Field(
        default=True,
        description="Whether to activate this template upon creation.",
    )

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        """Normalize required template names."""

        return _normalize_required_text(value, field_name="name")

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        """Normalize optional template descriptions."""

        return _normalize_optional_text(value)

    @model_validator(mode="after")
    def validate_sections(self) -> CreateReportTemplateRequest:
        """Ensure section display_order values are unique and sequential from zero."""

        orders = [section.display_order for section in self.sections]
        if len(orders) != len(set(orders)):
            raise ValueError("Section display_order values must be unique.")

        keys = [section.section_key for section in self.sections]
        if len(keys) != len(set(keys)):
            raise ValueError("Section section_key values must be unique within a template.")

        return self


class ActivateReportTemplateRequest(ContractModel):
    """Capture an operator reason when switching the active report template."""

    reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Optional reason persisted in the activity timeline for the activation.",
    )

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        """Normalize optional activation reasons."""

        return _normalize_optional_text(value)


class ReportTemplateListResponse(ContractModel):
    """Return all templates for one entity workspace in version order."""

    entity_id: str = Field(description="Owning entity workspace UUID.")
    templates: tuple[ReportTemplateSummary, ...] = Field(
        default=(),
        description="Report template versions for the entity, newest first.",
    )
    active_template_id: str | None = Field(
        default=None,
        description="UUID of the currently active template, if one exists.",
    )


# ---------------------------------------------------------------------------
# Report run contracts
# ---------------------------------------------------------------------------

class ReportRunSummary(ContractModel):
    """Describe one report-generation run for a close run."""

    id: str = Field(description="Stable UUID for the report run.")
    close_run_id: str = Field(description="Close run this report pack was generated for.")
    template_id: str = Field(description="Report template version used for generation.")
    version_no: int = Field(ge=1, description="Monotonic run number within the close run.")
    status: str = Field(min_length=1, description="Current lifecycle state of the report run.")
    failure_reason: str | None = Field(
        default=None,
        description="Structured failure description when status is 'failed'.",
    )
    generated_by_user_id: str | None = Field(
        default=None,
        description="UUID of the user who triggered this report generation.",
    )
    completed_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when the run reached a terminal state.",
    )
    created_at: datetime = Field(description="UTC timestamp when the report run was created.")
    updated_at: datetime = Field(description="UTC timestamp when the report run was last updated.")


class ReportRunDetail(ReportRunSummary):
    """Extend the report run summary with commentary and artifact references."""

    artifact_refs: list[dict[str, object]] = Field(
        default_factory=list,
        description="Storage references for generated artifacts (Excel, PDF, evidence packs).",
    )
    commentary: tuple[CommentarySummary, ...] = Field(
        default=(),
        description="Commentary versions attached to this report run.",
    )


class ReportRunListResponse(ContractModel):
    """Return report runs for one close run in newest-first order."""

    close_run_id: str = Field(description="Close run UUID the report runs belong to.")
    report_runs: tuple[ReportRunSummary, ...] = Field(
        default=(),
        description="Report runs for the close run, newest first.",
    )


# ---------------------------------------------------------------------------
# Commentary contracts
# ---------------------------------------------------------------------------

class CommentarySummary(ContractModel):
    """Describe one version of management commentary for a report section."""

    id: str = Field(description="Stable UUID for the commentary row.")
    report_run_id: str = Field(description="Parent report run UUID.")
    section_key: str = Field(description="Report section this commentary applies to.")
    status: str = Field(min_length=1, description="Current lifecycle state of this commentary.")
    body: str = Field(description="Commentary text content.")
    authored_by_user_id: str | None = Field(
        default=None,
        description="UUID of the user who last edited or approved this commentary.",
    )
    created_at: datetime = Field(description="UTC timestamp when the commentary was created.")
    updated_at: datetime = Field(description="UTC timestamp when the commentary was last updated.")


class UpdateCommentaryRequest(ContractModel):
    """Capture commentary text edits for one report section."""

    body: str = Field(
        max_length=50000,
        description="Updated commentary text content for the section.",
    )

    @field_validator("body")
    @classmethod
    def normalize_body(cls, value: str) -> str:
        """Trim commentary body text."""

        return value.strip()


class ApproveCommentaryRequest(ContractModel):
    """Capture an explicit commentary approval with optional edit."""

    body: str | None = Field(
        default=None,
        max_length=50000,
        description="Optional final commentary text applied during approval.",
    )
    reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Optional approval reason persisted to the activity timeline.",
    )

    @field_validator("body")
    @classmethod
    def normalize_body(cls, value: str | None) -> str | None:
        """Trim optional commentary body edits."""

        if value is None:
            return None
        return value.strip()

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        """Normalize optional approval reasons."""

        return _normalize_optional_text(value)


# ---------------------------------------------------------------------------
# Guardrail validation contracts
# ---------------------------------------------------------------------------

class GuardrailViolation(ContractModel):
    """Describe one template guardrail validation failure."""

    violation_type: str = Field(
        min_length=1,
        description="Stable violation category: missing_required_section, etc.",
    )
    section_key: str | None = Field(
        default=None,
        description="Section key involved in the violation, if applicable.",
    )
    message: str = Field(
        min_length=1,
        description="Operator-facing explanation of the guardrail violation.",
    )


class GuardrailValidationResponse(ContractModel):
    """Return the result of validating a template against guardrail rules."""

    template_id: str = Field(description="Template UUID that was validated.")
    is_valid: bool = Field(description="Whether the template passes all guardrail checks.")
    violations: tuple[GuardrailViolation, ...] = Field(
        default=(),
        description="Guardrail violations found, empty when the template is valid.",
    )


__all__ = [
    "ActivateReportTemplateRequest",
    "ApproveCommentaryRequest",
    "CommentarySummary",
    "CreateReportTemplateRequest",
    "GuardrailValidationResponse",
    "GuardrailViolation",
    "ReportRunDetail",
    "ReportRunListResponse",
    "ReportRunSummary",
    "ReportSectionDefinition",
    "ReportTemplateDetail",
    "ReportTemplateListResponse",
    "ReportTemplateSummary",
    "UpdateCommentaryRequest",
]
