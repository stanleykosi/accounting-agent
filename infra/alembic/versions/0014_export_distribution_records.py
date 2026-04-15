"""
Purpose: Create canonical artifact, export-run, and export-distribution tables.
Scope: Released artifact storage lineage, close-run export lifecycle tracking,
and management-distribution records required by review/sign-off enforcement.
Dependencies: Alembic, SQLAlchemy, PostgreSQL JSONB, and the existing reporting tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision = "0014_export_distribution_records"
down_revision = "0013_jobs"
branch_labels = None
depends_on = None

ARTIFACT_TYPES = (
    "gl_posting_package",
    "report_excel",
    "report_pdf",
    "audit_trail",
    "evidence_pack",
    "quickbooks_export",
)
EXPORT_STATUSES = ("pending", "generating", "completed", "failed", "canceled")
EXPORT_DELIVERY_CHANNELS = (
    "secure_email",
    "management_portal",
    "board_pack",
    "file_share",
)


def upgrade() -> None:
    """Create artifact, export run, and export distribution tables."""

    op.create_table(
        "artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("close_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("artifact_type", sa.String(), nullable=False),
        sa.Column("storage_key", sa.String(), nullable=False),
        sa.Column("mime_type", sa.String(), nullable=False),
        sa.Column("checksum", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["close_run_id"], ["close_runs.id"]),
        sa.ForeignKeyConstraint(["report_run_id"], ["report_runs.id"]),
        sa.CheckConstraint(
            f"artifact_type IN {ARTIFACT_TYPES}",
            name="ck_artifacts_artifact_type_valid",
        ),
        sa.CheckConstraint("version_no >= 1", name="ck_artifacts_version_no_positive"),
        sa.UniqueConstraint(
            "artifact_type",
            "idempotency_key",
            name="uq_artifacts_type_idempotency",
        ),
    )
    op.create_index("ix_artifacts_close_run_id", "artifacts", ["close_run_id"])
    op.create_index("ix_artifacts_artifact_type", "artifacts", ["artifact_type"])
    op.create_index("ix_artifacts_idempotency_key", "artifacts", ["idempotency_key"])
    op.create_index("ix_artifacts_close_run_version", "artifacts", ["close_run_id", "version_no"])

    op.create_table(
        "export_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("close_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "artifact_manifest",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("evidence_pack_key", sa.String(), nullable=True),
        sa.Column("triggered_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["close_run_id"], ["close_runs.id"]),
        sa.ForeignKeyConstraint(["triggered_by_user_id"], ["users.id"]),
        sa.CheckConstraint(
            f"status IN {EXPORT_STATUSES}",
            name="ck_export_runs_export_status_valid",
        ),
        sa.CheckConstraint("version_no >= 1", name="ck_export_runs_export_version_no_positive"),
        sa.UniqueConstraint(
            "close_run_id",
            "idempotency_key",
            name="uq_export_runs_close_run_idempotency",
        ),
    )
    op.create_index("ix_export_runs_close_run_id", "export_runs", ["close_run_id"])
    op.create_index("ix_export_runs_status", "export_runs", ["status"])
    op.create_index("ix_export_runs_idempotency_key", "export_runs", ["idempotency_key"])

    op.create_table(
        "export_distributions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("export_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("close_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("recipient_name", sa.String(), nullable=False),
        sa.Column("recipient_email", sa.String(), nullable=False),
        sa.Column("recipient_role", sa.String(), nullable=True),
        sa.Column("delivery_channel", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("distributed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("distributed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["export_run_id"], ["export_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"]),
        sa.ForeignKeyConstraint(["close_run_id"], ["close_runs.id"]),
        sa.ForeignKeyConstraint(["distributed_by_user_id"], ["users.id"]),
        sa.CheckConstraint(
            f"delivery_channel IN {EXPORT_DELIVERY_CHANNELS}",
            name="ck_export_distributions_export_distribution_delivery_channel_valid",
        ),
        sa.UniqueConstraint(
            "export_run_id",
            "recipient_email",
            "delivery_channel",
            "distributed_at",
            name="uq_export_distributions_export_recipient_channel_time",
        ),
    )
    op.create_index("ix_export_distributions_export_run_id", "export_distributions", ["export_run_id"])
    op.create_index("ix_export_distributions_distributed_at", "export_distributions", ["distributed_at"])
    op.create_index("ix_export_distributions_close_run_id", "export_distributions", ["close_run_id"])


def downgrade() -> None:
    """Drop export distribution, export run, and artifact tables."""

    op.drop_index("ix_export_distributions_close_run_id", table_name="export_distributions")
    op.drop_index("ix_export_distributions_distributed_at", table_name="export_distributions")
    op.drop_index("ix_export_distributions_export_run_id", table_name="export_distributions")
    op.drop_table("export_distributions")

    op.drop_index("ix_export_runs_idempotency_key", table_name="export_runs")
    op.drop_index("ix_export_runs_status", table_name="export_runs")
    op.drop_index("ix_export_runs_close_run_id", table_name="export_runs")
    op.drop_table("export_runs")

    op.drop_index("ix_artifacts_close_run_version", table_name="artifacts")
    op.drop_index("ix_artifacts_idempotency_key", table_name="artifacts")
    op.drop_index("ix_artifacts_artifact_type", table_name="artifacts")
    op.drop_index("ix_artifacts_close_run_id", table_name="artifacts")
    op.drop_table("artifacts")
