"""
Purpose: Create the baseline relational schema for auth, entities, close runs, and audit roots.
Scope: PostgreSQL extensions plus the minimum durable tables required by later workflow features.
Dependencies: Alembic, SQLAlchemy, and PostgreSQL-specific column types.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision = "0001_baseline_auth_and_close_runs"
down_revision = None
branch_labels = None
depends_on = None

USER_STATUSES = ("active", "disabled")
ENTITY_STATUSES = ("active", "archived")
AUTONOMY_MODES = ("human_review", "reduced_interruption")
CLOSE_RUN_STATUSES = ("draft", "in_review", "approved", "exported", "archived", "reopened")
WORKFLOW_PHASES = ("collection", "processing", "reconciliation", "reporting", "review_signoff")
PHASE_STATUSES = ("not_started", "in_progress", "blocked", "ready", "completed")
AUDIT_SOURCE_SURFACES = ("desktop", "cli", "system", "worker", "integration")
DEFAULT_ENTITY_CONFIDENCE_THRESHOLDS_SQL = (
    "jsonb_build_object("
    "'classification', 0.85, "
    "'coding', 0.85, "
    "'reconciliation', 0.9, "
    "'posting', 0.95"
    ")"
)


def upgrade() -> None:
    """Create the baseline tables and indexes required for Step 8."""

    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.create_table(
        "users",
        *_uuid_primary_key_column(),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(_in_check("status", USER_STATUSES), name="ck_users_status_valid"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "entities",
        *_uuid_primary_key_column(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("legal_name", sa.Text(), nullable=True),
        sa.Column(
            "base_currency",
            sa.CHAR(length=3),
            nullable=False,
            server_default=sa.text("'NGN'"),
        ),
        sa.Column(
            "country_code",
            sa.CHAR(length=2),
            nullable=False,
            server_default=sa.text("'NG'"),
        ),
        sa.Column(
            "timezone",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'Africa/Lagos'"),
        ),
        sa.Column("accounting_standard", sa.Text(), nullable=True),
        sa.Column(
            "autonomy_mode",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'human_review'"),
        ),
        sa.Column(
            "default_confidence_thresholds",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text(DEFAULT_ENTITY_CONFIDENCE_THRESHOLDS_SQL),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        *_timestamp_columns(),
        sa.CheckConstraint(
            _in_check("autonomy_mode", AUTONOMY_MODES),
            name="ck_entities_autonomy_mode_valid",
        ),
        sa.CheckConstraint(_in_check("status", ENTITY_STATUSES), name="ck_entities_status_valid"),
    )

    op.create_table(
        "sessions",
        *_uuid_primary_key_column(),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("session_token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        *_timestamp_columns(),
        sa.UniqueConstraint("session_token_hash", name="uq_sessions_session_token_hash"),
    )

    op.create_table(
        "api_tokens",
        *_uuid_primary_key_column(),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "scope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        sa.UniqueConstraint("token_hash", name="uq_api_tokens_token_hash"),
    )

    op.create_table(
        "entity_memberships",
        *_uuid_primary_key_column(),
        sa.Column(
            "entity_id",
            sa.Uuid(),
            sa.ForeignKey("entities.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "is_default_actor",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        *_timestamp_columns(),
        sa.UniqueConstraint("entity_id", "user_id", name="uq_entity_memberships_entity_user"),
    )

    op.create_table(
        "close_runs",
        *_uuid_primary_key_column(),
        sa.Column(
            "entity_id",
            sa.Uuid(),
            sa.ForeignKey("entities.id"),
            nullable=False,
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reporting_currency", sa.CHAR(length=3), nullable=False),
        sa.Column("current_version_no", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "opened_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "approved_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "reopened_from_close_run_id",
            sa.Uuid(),
            sa.ForeignKey("close_runs.id"),
            nullable=True,
        ),
        *_timestamp_columns(),
        sa.CheckConstraint(
            _in_check("status", CLOSE_RUN_STATUSES),
            name="ck_close_runs_status_valid",
        ),
        sa.CheckConstraint("period_end >= period_start", name="ck_close_runs_period_range_valid"),
        sa.CheckConstraint(
            "current_version_no >= 1",
            name="ck_close_runs_current_version_no_positive",
        ),
        sa.UniqueConstraint(
            "entity_id",
            "period_start",
            "period_end",
            "current_version_no",
            name="uq_close_runs_entity_period_version",
        ),
    )

    op.create_table(
        "close_run_phase_states",
        *_uuid_primary_key_column(),
        sa.Column(
            "close_run_id",
            sa.Uuid(),
            sa.ForeignKey("close_runs.id"),
            nullable=False,
        ),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("blocking_reason", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            _in_check("phase", WORKFLOW_PHASES),
            name="ck_close_run_phase_states_phase_valid",
        ),
        sa.CheckConstraint(
            _in_check("status", PHASE_STATUSES),
            name="ck_close_run_phase_states_status_valid",
        ),
        sa.CheckConstraint(
            "(status = 'blocked' AND blocking_reason IS NOT NULL) "
            "OR (status <> 'blocked' AND blocking_reason IS NULL)",
            name="ck_close_run_phase_states_blocking_reason_valid",
        ),
        sa.UniqueConstraint(
            "close_run_id",
            "phase",
            name="uq_close_run_phase_states_close_run_phase",
        ),
    )

    op.create_table(
        "review_actions",
        *_uuid_primary_key_column(),
        sa.Column(
            "close_run_id",
            sa.Uuid(),
            sa.ForeignKey("close_runs.id"),
            nullable=False,
        ),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column(
            "actor_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("autonomy_mode", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("before_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            _in_check("autonomy_mode", AUTONOMY_MODES),
            name="ck_review_actions_autonomy_mode_valid",
        ),
    )

    op.create_table(
        "audit_events",
        *_uuid_primary_key_column(),
        sa.Column(
            "entity_id",
            sa.Uuid(),
            sa.ForeignKey("entities.id"),
            nullable=False,
        ),
        sa.Column(
            "close_run_id",
            sa.Uuid(),
            sa.ForeignKey("close_runs.id"),
            nullable=True,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "actor_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("source_surface", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            _in_check("source_surface", AUDIT_SOURCE_SURFACES),
            name="ck_audit_events_source_surface_valid",
        ),
    )

    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"])
    op.create_index(
        "ix_api_tokens_active_user_id",
        "api_tokens",
        ["user_id"],
        unique=False,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index("ix_entities_name", "entities", ["name"])
    op.create_index("ix_entities_status", "entities", ["status"])
    op.create_index("ix_close_runs_entity_id_status", "close_runs", ["entity_id", "status"])
    op.create_index(
        "ix_close_runs_entity_id_period_start_period_end",
        "close_runs",
        ["entity_id", "period_start", "period_end"],
    )
    op.create_index(
        "ix_review_actions_close_run_id_target_type_target_id",
        "review_actions",
        ["close_run_id", "target_type", "target_id"],
    )
    op.create_index(
        "ix_audit_events_entity_id_created_at",
        "audit_events",
        ["entity_id", "created_at"],
    )
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])
    op.create_index("ix_audit_events_trace_id", "audit_events", ["trace_id"])


def downgrade() -> None:
    """Drop the baseline tables and indexes in reverse dependency order."""

    op.drop_index("ix_audit_events_trace_id", table_name="audit_events")
    op.drop_index("ix_audit_events_event_type", table_name="audit_events")
    op.drop_index("ix_audit_events_entity_id_created_at", table_name="audit_events")
    op.drop_index(
        "ix_review_actions_close_run_id_target_type_target_id",
        table_name="review_actions",
    )
    op.drop_index(
        "ix_close_runs_entity_id_period_start_period_end",
        table_name="close_runs",
    )
    op.drop_index("ix_close_runs_entity_id_status", table_name="close_runs")
    op.drop_index("ix_entities_status", table_name="entities")
    op.drop_index("ix_entities_name", table_name="entities")
    op.drop_index("ix_api_tokens_active_user_id", table_name="api_tokens")
    op.drop_index("ix_api_tokens_user_id", table_name="api_tokens")
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_index("ix_sessions_user_id", table_name="sessions")

    op.drop_table("audit_events")
    op.drop_table("review_actions")
    op.drop_table("close_run_phase_states")
    op.drop_table("close_runs")
    op.drop_table("entity_memberships")
    op.drop_table("api_tokens")
    op.drop_table("sessions")
    op.drop_table("entities")
    op.drop_table("users")


def _uuid_primary_key_column() -> tuple[sa.Column[Any], ...]:
    """Return the shared UUID primary key column used by all baseline tables."""

    return (
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
    )


def _timestamp_columns() -> tuple[sa.Column[Any], sa.Column[Any]]:
    """Return the shared created/updated timestamp columns for baseline tables."""

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
