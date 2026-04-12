"""
Purpose: Define canonical report-template and report-run persistence models.
Scope: Versioned report templates with mandatory section guardrails, entity-scoped
template customization, report-run snapshots, commentary state, and artifact lineage.
Dependencies: SQLAlchemy ORM primitives, PostgreSQL JSONB support, shared
DB base helpers, and canonical enums from services.common.enums.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from services.common.types import JsonObject
from services.db.base import Base, TimestampedModel, UUIDPrimaryKeyMixin, build_text_choice_check
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ReportTemplateSource(StrEnum):
    """Enumerate the canonical sources a report template can originate from."""

    GLOBAL_DEFAULT = "global_default"
    ENTITY_CUSTOM = "entity_custom"


class ReportRunStatus(StrEnum):
    """Enumerate the lifecycle states of a report generation run."""

    PENDING = "pending"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class CommentaryStatus(StrEnum):
    """Enumerate the lifecycle states of report commentary text."""

    DRAFT = "draft"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    SUPERSEDED = "superseded"


# ---------------------------------------------------------------------------
# SQL check constraint helpers
# ---------------------------------------------------------------------------

_TEMPLATE_SOURCES = tuple(source.value for source in ReportTemplateSource)
_RUN_STATUSES = tuple(status.value for status in ReportRunStatus)
_COMMENTARY_STATUSES = tuple(status.value for status in CommentaryStatus)


# ---------------------------------------------------------------------------
# Report Template
# ---------------------------------------------------------------------------

class ReportTemplate(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one versioned report template attached to an entity or the global scope.

    A report template defines which sections appear in generated reports, their
    display order, and the guardrail metadata that prevents required sections
    from being removed by customization.  Each template is immutable after
    creation; edits produce a new template row with an incremented version number.
    """

    __tablename__ = "report_templates"
    __table_args__ = (
        build_text_choice_check(
            column_name="source",
            values=_TEMPLATE_SOURCES,
            constraint_name="source_valid",
        ),
        CheckConstraint("version_no >= 1", name="version_no_positive"),
        CheckConstraint(
            "jsonb_typeof(sections) = 'array'",
            name="sections_must_be_array",
        ),
        # One template per entity + version; global defaults have NULL entity_id.
        UniqueConstraint(
            "entity_id",
            "version_no",
            name="uq_report_templates_entity_version",
        ),
        Index("ix_report_templates_entity_id", "entity_id"),
        Index("ix_report_templates_source", "source"),
        Index("ix_report_templates_is_active", "is_active"),
        # Partial index for the single active template per entity.
        Index(
            "uq_report_templates_entity_active",
            "entity_id",
            unique=True,
            postgresql_where=text("is_active AND entity_id IS NOT NULL"),
        ),
    )

    entity_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("entities.id"),
        nullable=True,
        index=True,
        comment=(
            "Owning entity workspace for entity-scoped templates, or NULL for global defaults."
        ),
    )
    source: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="Template provenance: global_default or entity_custom.",
    )
    version_no: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Monotonic version number for the template lineage.",
    )
    name: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="Human-readable template name exposed in the UI.",
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Optional template description for operator context.",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="Whether this template is the active one for its entity or global scope.",
    )
    sections: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
        comment=(
            "Ordered array of section definitions.  Each entry includes a 'key' matching "
            "ReportSectionKey, a 'label', a 'display_order', and optional 'config'."
        ),
    )
    guardrail_config: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment=(
            "Guardrail metadata: required_section_keys, allow_custom_sections, "
            "and any template-level policy overrides."
        ),
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
        comment="User who created this template version, if attributable.",
    )


# ---------------------------------------------------------------------------
# Report Template Section (denormalized in JSONB but indexed separately)
# ---------------------------------------------------------------------------

class ReportTemplateSection(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one section definition row attached to a specific template version.

    While section definitions live inside the template's sections JSONB column,
    this table provides a relational anchor for FK references, auditing, and
    explicit per-section customization tracking.

    The section_key column intentionally has no check constraint so that custom
    sections beyond the canonical five can be stored when a template's
    guardrail policy permits them.
    """

    __tablename__ = "report_template_sections"
    __table_args__ = (
        UniqueConstraint(
            "template_id",
            "section_key",
            name="uq_report_template_sections_template_key",
        ),
        Index("ix_report_template_sections_template_id", "template_id"),
        Index("ix_report_template_sections_section_key", "section_key"),
    )

    template_id: Mapped[UUID] = mapped_column(
        ForeignKey("report_templates.id"),
        nullable=False,
        comment="Parent report template this section belongs to.",
    )
    section_key: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="Stable section identifier matching ReportSectionKey.",
    )
    label: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="Human-readable section label for UI rendering.",
    )
    display_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Zero-based rendering order within the template.",
    )
    is_required: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        comment="Whether the section is mandatory and protected by guardrails.",
    )
    section_config: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="Optional per-section configuration (filters, formats, etc.).",
    )


# ---------------------------------------------------------------------------
# Report Run
# ---------------------------------------------------------------------------

class ReportRun(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one report-generation run scoped to a close run and template.

    A report run captures the template version, generation parameters, status,
    and output artifact references at a point in time.  Multiple report runs
    can exist for one close run, enabling regeneration of selected sections or
    periods without replacing previously released artifacts.
    """

    __tablename__ = "report_runs"
    __table_args__ = (
        build_text_choice_check(
            column_name="status",
            values=_RUN_STATUSES,
            constraint_name="status_valid",
        ),
        CheckConstraint("version_no >= 1", name="version_no_positive"),
        Index("ix_report_runs_close_run_id", "close_run_id"),
        Index("ix_report_runs_template_id", "template_id"),
        Index("ix_report_runs_status", "status"),
        Index("ix_report_runs_close_run_version", "close_run_id", "version_no"),
    )

    close_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("close_runs.id"),
        nullable=False,
        comment="Close run this report pack was generated for.",
    )
    template_id: Mapped[UUID] = mapped_column(
        ForeignKey("report_templates.id"),
        nullable=False,
        comment="Report template version used for this generation run.",
    )
    version_no: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Monotonic run number within the close run scope.",
    )
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="Current lifecycle state of the report generation run.",
    )
    failure_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Structured failure description when status is 'failed'.",
    )
    generation_config: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment=(
            "Generation parameters: requested_sections, period_overrides, commentary_version, etc."
        ),
    )
    artifact_refs: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
        comment=(
            "Array of storage references for generated artifacts (Excel, PDF, evidence packs)."
        ),
    )
    generated_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
        comment="User who triggered this report generation run.",
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="UTC timestamp when the run reached a terminal state.",
    )


# ---------------------------------------------------------------------------
# Report Commentary
# ---------------------------------------------------------------------------

class ReportCommentary(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one version of management commentary text for a report section.

    Commentary is tracked independently per report run and section so that
    approved commentary can be preserved across regenerations and reviewers
    can edit commentary without re-generating the entire report pack.
    """

    __tablename__ = "report_commentary"
    __table_args__ = (
        build_text_choice_check(
            column_name="status",
            values=_COMMENTARY_STATUSES,
            constraint_name="status_valid",
        ),
        Index("ix_report_commentary_report_run_id", "report_run_id"),
        Index("ix_report_commentary_section_key", "section_key"),
        Index(
            "ix_report_commentary_run_section_active",
            "report_run_id",
            "section_key",
            unique=False,
            postgresql_where=text("status IN ('draft', 'under_review', 'approved')"),
        ),
    )

    report_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("report_runs.id"),
        nullable=False,
        comment="Parent report run this commentary belongs to.",
    )
    section_key: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="Report section this commentary text applies to.",
    )
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="Current lifecycle state of this commentary version.",
    )
    body: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        server_default=text("''"),
        comment="Commentary text content managed by reviewers.",
    )
    authored_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
        comment="User who last edited or approved this commentary version.",
    )
    superseded_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("report_commentary.id"),
        nullable=True,
        comment="Newer commentary row that replaced this version.",
    )


__all__ = [
    "CommentaryStatus",
    "ReportCommentary",
    "ReportRun",
    "ReportRunStatus",
    "ReportTemplate",
    "ReportTemplateSection",
    "ReportTemplateSource",
]
