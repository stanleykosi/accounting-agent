"""
Purpose: Create report-template, report-run, and commentary tables for the reporting module.
Scope: Template versioning, mandatory section guardrails, report generation runs,
and versioned commentary state linked to close runs.
Dependencies: Alembic, SQLAlchemy, PostgreSQL JSONB and text types.
"""

from __future__ import annotations

from collections.abc import Iterable

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision = "0010_report_templates_and_runs"
down_revision = "0009_reconciliation"
branch_labels = None
depends_on = None

REPORT_TEMPLATE_SOURCES = ("global_default", "entity_custom")
REPORT_RUN_STATUSES = ("pending", "generating", "completed", "failed", "canceled")
COMMENTARY_STATUSES = ("draft", "under_review", "approved", "superseded")


def upgrade() -> None:
    """Create report template, section, run, and commentary tables."""

    op.create_table(
        "report_templates",
        _uuid_primary_key_column(),
        sa.Column(
            "entity_id",
            sa.Uuid(),
            sa.ForeignKey("entities.id"),
            nullable=True,
            index=True,
            comment=(
                "Owning entity workspace for entity-scoped templates, or NULL for global defaults."
            ),
        ),
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            comment="Template provenance: global_default or entity_custom.",
        ),
        sa.Column(
            "version_no",
            sa.Integer(),
            nullable=False,
            comment="Monotonic version number for the template lineage.",
        ),
        sa.Column(
            "name",
            sa.Text(),
            nullable=False,
            comment="Human-readable template name exposed in the UI.",
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=True,
            comment="Optional template description for operator context.",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="Whether this template is the active one for its entity or global scope.",
        ),
        sa.Column(
            "sections",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment=(
                "Ordered array of section definitions.  Each entry includes a 'key' matching "
                "ReportSectionKey, a 'label', a 'display_order', and optional 'config'."
            ),
        ),
        sa.Column(
            "guardrail_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment=(
                "Guardrail metadata: required_section_keys, allow_custom_sections, "
                "and any template-level policy overrides."
            ),
        ),
        sa.Column(
            "created_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id"),
            nullable=True,
            comment="User who created this template version, if attributable.",
        ),
        *_timestamp_columns(),
        sa.CheckConstraint(
            _in_check("source", REPORT_TEMPLATE_SOURCES),
            name="ck_report_templates_source_valid",
        ),
        sa.CheckConstraint(
            "version_no >= 1",
            name="ck_report_templates_version_no_positive",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(sections) = 'array'",
            name="ck_report_templates_sections_must_be_array",
        ),
        sa.UniqueConstraint(
            "entity_id",
            "version_no",
            name="uq_report_templates_entity_version",
        ),
        sa.Index("ix_report_templates_source", "source"),
        sa.Index("ix_report_templates_is_active", "is_active"),
        sa.Index(
            "uq_report_templates_entity_active",
            "entity_id",
            unique=True,
            postgresql_where=sa.text("is_active AND entity_id IS NOT NULL"),
        ),
    )

    op.create_table(
        "report_template_sections",
        _uuid_primary_key_column(),
        sa.Column(
            "template_id",
            sa.Uuid(),
            sa.ForeignKey("report_templates.id"),
            nullable=False,
            comment="Parent report template this section belongs to.",
        ),
        sa.Column(
            "section_key",
            sa.Text(),
            nullable=False,
            comment="Stable section identifier (canonical or custom).",
        ),
        sa.Column(
            "label",
            sa.Text(),
            nullable=False,
            comment="Human-readable section label for UI rendering.",
        ),
        sa.Column(
            "display_order",
            sa.Integer(),
            nullable=False,
            comment="Zero-based rendering order within the template.",
        ),
        sa.Column(
            "is_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
            comment="Whether the section is mandatory and protected by guardrails.",
        ),
        sa.Column(
            "section_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Optional per-section configuration (filters, formats, etc.).",
        ),
        *_timestamp_columns(),
        sa.UniqueConstraint(
            "template_id",
            "section_key",
            name="uq_report_template_sections_template_key",
        ),
        sa.Index("ix_report_template_sections_template_id", "template_id"),
        sa.Index("ix_report_template_sections_section_key", "section_key"),
    )

    op.create_table(
        "report_runs",
        _uuid_primary_key_column(),
        sa.Column(
            "close_run_id",
            sa.Uuid(),
            sa.ForeignKey("close_runs.id"),
            nullable=False,
            comment="Close run this report pack was generated for.",
        ),
        sa.Column(
            "template_id",
            sa.Uuid(),
            sa.ForeignKey("report_templates.id"),
            nullable=False,
            comment="Report template version used for this generation run.",
        ),
        sa.Column(
            "version_no",
            sa.Integer(),
            nullable=False,
            comment="Monotonic run number within the close run scope.",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            comment="Current lifecycle state of the report generation run.",
        ),
        sa.Column(
            "failure_reason",
            sa.Text(),
            nullable=True,
            comment="Structured failure description when status is 'failed'.",
        ),
        sa.Column(
            "generation_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment=(
                "Generation parameters: requested_sections, period_overrides, "
                "commentary_version, etc."
            ),
        ),
        sa.Column(
            "artifact_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment=(
                "Array of storage references for generated artifacts (Excel, PDF, evidence packs)."
            ),
        ),
        sa.Column(
            "generated_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id"),
            nullable=True,
            comment="User who triggered this report generation run.",
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="UTC timestamp when the run reached a terminal state.",
        ),
        *_timestamp_columns(),
        sa.CheckConstraint(
            _in_check("status", REPORT_RUN_STATUSES),
            name="ck_report_runs_status_valid",
        ),
        sa.CheckConstraint(
            "version_no >= 1",
            name="ck_report_runs_version_no_positive",
        ),
        sa.Index("ix_report_runs_close_run_id", "close_run_id"),
        sa.Index("ix_report_runs_template_id", "template_id"),
        sa.Index("ix_report_runs_status", "status"),
        sa.Index("ix_report_runs_close_run_version", "close_run_id", "version_no"),
    )

    op.create_table(
        "report_commentary",
        _uuid_primary_key_column(),
        sa.Column(
            "report_run_id",
            sa.Uuid(),
            sa.ForeignKey("report_runs.id"),
            nullable=False,
            comment="Parent report run this commentary belongs to.",
        ),
        sa.Column(
            "section_key",
            sa.Text(),
            nullable=False,
            comment="Report section this commentary text applies to.",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            comment="Current lifecycle state of this commentary version.",
        ),
        sa.Column(
            "body",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
            comment="Commentary text content managed by reviewers.",
        ),
        sa.Column(
            "authored_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id"),
            nullable=True,
            comment="User who last edited or approved this commentary version.",
        ),
        sa.Column(
            "superseded_by_id",
            sa.Uuid(),
            sa.ForeignKey("report_commentary.id"),
            nullable=True,
            comment="Newer commentary row that replaced this version.",
        ),
        *_timestamp_columns(),
        sa.CheckConstraint(
            _in_check("status", COMMENTARY_STATUSES),
            name="ck_report_commentary_status_valid",
        ),
        sa.Index("ix_report_commentary_report_run_id", "report_run_id"),
        sa.Index("ix_report_commentary_section_key", "section_key"),
        sa.Index(
            "ix_report_commentary_run_section_active",
            "report_run_id",
            "section_key",
            unique=False,
            postgresql_where=sa.text(
                "status IN ('draft', 'under_review', 'approved')"
            ),
        ),
    )


def downgrade() -> None:
    """Drop report template, section, run, and commentary tables."""

    op.drop_index(
        "ix_report_commentary_run_section_active",
        table_name="report_commentary",
    )
    op.drop_index("ix_report_commentary_section_key", table_name="report_commentary")
    op.drop_index("ix_report_commentary_report_run_id", table_name="report_commentary")
    op.drop_table("report_commentary")

    op.drop_index("ix_report_runs_close_run_version", table_name="report_runs")
    op.drop_index("ix_report_runs_status", table_name="report_runs")
    op.drop_index("ix_report_runs_template_id", table_name="report_runs")
    op.drop_index("ix_report_runs_close_run_id", table_name="report_runs")
    op.drop_table("report_runs")

    op.drop_index(
        "ix_report_template_sections_section_key",
        table_name="report_template_sections",
    )
    op.drop_index(
        "ix_report_template_sections_template_id",
        table_name="report_template_sections",
    )
    op.drop_table("report_template_sections")

    op.drop_index("ix_report_templates_is_active", table_name="report_templates")
    op.drop_index("ix_report_templates_source", table_name="report_templates")
    op.drop_index("ix_report_templates_entity_id", table_name="report_templates")
    op.drop_table("report_templates")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _uuid_primary_key_column() -> tuple[sa.Column[object], ...]:
    """Return the shared UUID primary key column used by all reporting tables."""

    return (
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
    )


def _timestamp_columns() -> tuple[sa.Column[object], sa.Column[object]]:
    """Return the shared created/updated timestamp columns for reporting tables."""

    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def _in_check(column_name: str, values: Iterable[str]) -> str:
    """Build static SQL for a text-column membership check constraint."""

    joined_values = ", ".join(_quote_sql_literal(value) for value in values)
    return f"{column_name} IN ({joined_values})"


def _quote_sql_literal(value: str) -> str:
    """Quote a trusted string literal for static migration SQL."""

    escaped_value = value.replace("'", "''")
    return f"'{escaped_value}'"
