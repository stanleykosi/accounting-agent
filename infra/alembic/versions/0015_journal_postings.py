"""
Purpose: Create canonical journal posting records for Phase 2 operational posting.
Scope: Accountant-selected posting target, optional external package artifact linkage,
and durable posting audit metadata for approved journals.
Dependencies: Alembic, SQLAlchemy, PostgreSQL JSONB, and the journal/export tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision = "0015_journal_postings"
down_revision = "0014_export_distribution_records"
branch_labels = None
depends_on = None

JOURNAL_POSTING_TARGETS = ("internal_ledger", "external_erp_package")
JOURNAL_POSTING_STATUSES = ("completed", "failed")
JOURNAL_POSTING_PROVIDERS = ("generic_erp", "quickbooks_online")
JOURNAL_POSTING_ARTIFACT_TYPES = ("gl_posting_package", "quickbooks_export")


def upgrade() -> None:
    """Create the canonical journal posting table."""

    op.create_table(
        "journal_postings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("journal_entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("close_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("posting_target", sa.String(40), nullable=False),
        sa.Column("provider", sa.String(60), nullable=True),
        sa.Column(
            "status",
            sa.String(30),
            nullable=False,
            server_default=sa.text("'completed'"),
        ),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("artifact_type", sa.String(60), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "posting_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("posted_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["journal_entry_id"], ["journal_entries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["close_run_id"], ["close_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["posted_by_user_id"], ["users.id"]),
        sa.CheckConstraint(
            f"posting_target IN {JOURNAL_POSTING_TARGETS}",
            name="ck_journal_postings_journal_posting_target_valid",
        ),
        sa.CheckConstraint(
            f"status IN {JOURNAL_POSTING_STATUSES}",
            name="ck_journal_postings_journal_posting_status_valid",
        ),
        sa.CheckConstraint(
            "version_no >= 1",
            name="ck_journal_postings_journal_posting_version_no_positive",
        ),
        sa.CheckConstraint(
            "("
            "posting_target = 'internal_ledger' AND artifact_id IS NULL"
            ") OR ("
            "posting_target = 'external_erp_package' AND artifact_id IS NOT NULL"
            ")",
            name="ck_journal_postings_journal_posting_artifact_matches_target",
        ),
        sa.CheckConstraint(
            f"provider IS NULL OR provider IN {JOURNAL_POSTING_PROVIDERS}",
            name="ck_journal_postings_journal_posting_provider_valid",
        ),
        sa.CheckConstraint(
            f"artifact_type IS NULL OR artifact_type IN {JOURNAL_POSTING_ARTIFACT_TYPES}",
            name="ck_journal_postings_journal_posting_artifact_type_valid",
        ),
        sa.UniqueConstraint("journal_entry_id", name="uq_journal_postings_journal_entry_id"),
    )
    op.create_index("ix_journal_postings_close_run_id", "journal_postings", ["close_run_id"])
    op.create_index("ix_journal_postings_posting_target", "journal_postings", ["posting_target"])
    op.create_index("ix_journal_postings_status", "journal_postings", ["status"])


def downgrade() -> None:
    """Drop the journal posting table."""

    op.drop_index("ix_journal_postings_status", table_name="journal_postings")
    op.drop_index("ix_journal_postings_posting_target", table_name="journal_postings")
    op.drop_index("ix_journal_postings_close_run_id", table_name="journal_postings")
    op.drop_table("journal_postings")
