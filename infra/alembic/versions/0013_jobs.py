"""
Purpose: Create durable background-job records with checkpoints, cancellation, and
dead-letter metadata.
Scope: Persist worker lifecycle state for parsing, OCR, recommendations,
reconciliation, and reporting so jobs can be inspected and resumed safely.
Dependencies: Alembic, SQLAlchemy, PostgreSQL JSONB support, and prior entity/auth/
document schema migrations.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision = "0013_jobs"
down_revision = "0012_chat_action_plans"
branch_labels = None
depends_on = None


def _uuid_pk() -> sa.Column:
    """Build a standard UUID primary key column definition."""

    return sa.Column(
        "id",
        sa.Uuid(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _timestamps() -> tuple[sa.Column, sa.Column]:
    """Build canonical created_at / updated_at timestamp columns."""

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


def upgrade() -> None:
    """Create jobs table and indexes for checkpointed async lifecycle management."""

    op.create_table(
        "jobs",
        _uuid_pk(),
        sa.Column(
            "entity_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("entities.id"),
            nullable=True,
        ),
        sa.Column(
            "close_run_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("close_runs.id"),
            nullable=True,
        ),
        sa.Column(
            "document_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("documents.id"),
            nullable=True,
        ),
        sa.Column(
            "actor_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "canceled_by_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "resumed_from_job_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("jobs.id"),
            nullable=True,
        ),
        sa.Column("task_name", sa.String(length=120), nullable=False),
        sa.Column("queue_name", sa.String(length=60), nullable=False),
        sa.Column("routing_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "checkpoint_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "result_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.Column(
            "failure_details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("blocking_reason", sa.String(length=500), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "retry_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "max_retries",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'blocked', 'failed', 'canceled', 'completed')",
            name="jobs_status_valid",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="jobs_attempt_count_non_negative"),
        sa.CheckConstraint("retry_count >= 0", name="jobs_retry_count_non_negative"),
        sa.CheckConstraint("max_retries >= 0", name="jobs_max_retries_non_negative"),
        sa.CheckConstraint(
            "retry_count <= attempt_count",
            name="jobs_retry_count_within_attempts",
        ),
        sa.CheckConstraint(
            "attempt_count <= max_retries + 1",
            name="jobs_attempt_count_within_retry_budget",
        ),
        sa.CheckConstraint(
            "(status = 'blocked' AND blocking_reason IS NOT NULL) "
            "OR (status <> 'blocked' AND blocking_reason IS NULL)",
            name="jobs_blocking_reason_matches_status",
        ),
        sa.CheckConstraint(
            "dead_lettered_at IS NULL OR status = 'failed'",
            name="jobs_dead_letter_requires_failed_status",
        ),
        sa.CheckConstraint(
            "canceled_at IS NULL OR status = 'canceled'",
            name="jobs_canceled_timestamp_requires_canceled_status",
        ),
    )
    op.create_index("ix_jobs_entity_id_status", "jobs", ["entity_id", "status"])
    op.create_index("ix_jobs_close_run_id_status", "jobs", ["close_run_id", "status"])
    op.create_index("ix_jobs_document_id_status", "jobs", ["document_id", "status"])
    op.create_index("ix_jobs_task_name_status", "jobs", ["task_name", "status"])


def downgrade() -> None:
    """Remove jobs table and associated indexes."""

    op.drop_table("jobs")
