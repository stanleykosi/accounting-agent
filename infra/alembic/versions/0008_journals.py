"""
Purpose: Add journal entries and journal lines tables for the accounting engine.
Scope: Journal entries generated from approved recommendations with balanced debit/credit
lines, review lifecycle tracking, and approval lineage. Also adds autonomy_mode column
to recommendations to preserve the routing mode at creation time.
Dependencies: Alembic, SQLAlchemy, PostgreSQL JSONB, and existing entities/close_runs/
users/recommendations tables. Created by Step 28 (journal drafting + approval routing).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision = "0008_journals"
down_revision = "0007_recommendations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create journal tables and add autonomy_mode to recommendations."""

    # --- recommendations: add autonomy_mode column ---
    op.add_column(
        "recommendations",
        sa.Column(
            "autonomy_mode",
            sa.String(30),
            nullable=True,
            comment="Autonomy mode active when the recommendation was created.",
        ),
    )

    # --- journal_entries ---
    op.create_table(
        "journal_entries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("close_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recommendation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "journal_number",
            sa.String(60),
            nullable=False,
            unique=True,
            comment="Human-readable journal identifier (e.g., 'JE-2026-00001').",
        ),
        sa.Column(
            "posting_date",
            sa.Date,
            nullable=False,
            comment="Accounting date for the journal posting.",
        ),
        sa.Column(
            "status",
            sa.String(30),
            nullable=False,
            server_default="draft",
            comment="Review lifecycle state of the journal entry.",
        ),
        sa.Column(
            "description",
            sa.Text,
            nullable=False,
            comment="Narrative description of the journal entry purpose.",
        ),
        sa.Column(
            "total_debits",
            sa.Numeric(20, 2),
            nullable=False,
            comment="Sum of all debit line amounts. Must equal total_credits.",
        ),
        sa.Column(
            "total_credits",
            sa.Numeric(20, 2),
            nullable=False,
            comment="Sum of all credit line amounts. Must equal total_debits.",
        ),
        sa.Column(
            "line_count",
            sa.Integer,
            nullable=False,
            comment="Number of journal lines attached to this entry.",
        ),
        sa.Column(
            "source_surface",
            sa.String(30),
            nullable=False,
            server_default="system",
            comment="Surface that created the journal (system, desktop, cli, chat).",
        ),
        sa.Column(
            "autonomy_mode",
            sa.String(30),
            nullable=True,
            comment="Autonomy mode active when the journal was created.",
        ),
        sa.Column(
            "reasoning_summary",
            sa.Text,
            nullable=True,
            comment="Explanation of why this journal was generated.",
        ),
        sa.Column(
            "metadata_payload",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
            comment="Additional structured metadata (rule version, prompt version, etc.).",
        ),
        sa.Column("approved_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("applied_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("superseded_by_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entities.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["close_run_id"],
            ["close_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recommendation_id"],
            ["recommendations.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["applied_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_id"],
            ["journal_entries.id"],
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "total_debits = total_credits",
            name="journal_debits_equal_credits",
        ),
        sa.CheckConstraint(
            "line_count >= 2",
            name="journal_minimum_lines",
        ),
    )
    op.create_index(
        "ix_journal_entries_close_run_status",
        "journal_entries",
        ["close_run_id", "status"],
    )
    op.create_index(
        "ix_journal_entries_recommendation",
        "journal_entries",
        ["recommendation_id"],
    )
    op.create_index(
        "ix_journal_entries_entity_period",
        "journal_entries",
        ["entity_id", "posting_date"],
    )

    # --- journal_lines ---
    op.create_table(
        "journal_lines",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("journal_entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "line_no",
            sa.Integer,
            nullable=False,
            comment="Sequential line number within the journal entry (1-based).",
        ),
        sa.Column(
            "account_code",
            sa.String(60),
            nullable=False,
            comment="GL account code from the active chart of accounts.",
        ),
        sa.Column(
            "line_type",
            sa.String(10),
            nullable=False,
            comment="Either 'debit' or 'credit'.",
        ),
        sa.Column(
            "amount",
            sa.Numeric(20, 2),
            nullable=False,
            comment="Monetary amount for this line (always positive).",
        ),
        sa.Column(
            "description",
            sa.Text,
            nullable=True,
            comment="Optional memo or description for this specific line.",
        ),
        sa.Column(
            "dimensions",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
            comment="Assigned dimensions (cost_centre, department, project).",
        ),
        sa.Column(
            "reference",
            sa.String(120),
            nullable=True,
            comment="Optional external reference or transaction ID.",
        ),
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
        sa.ForeignKeyConstraint(
            ["journal_entry_id"],
            ["journal_entries.id"],
            ondelete="CASCADE",
            name="fk_journal_lines_journal_entry_id",
        ),
        sa.CheckConstraint(
            "amount > 0",
            name="journal_line_amount_positive",
        ),
        sa.CheckConstraint(
            "line_no >= 1",
            name="journal_line_no_positive",
        ),
    )
    op.create_index(
        "ix_journal_lines_journal_entry",
        "journal_lines",
        ["journal_entry_id"],
    )
    op.create_index(
        "ix_journal_lines_account_code",
        "journal_lines",
        ["account_code"],
    )


def downgrade() -> None:
    """Drop the journal_lines and journal_entries tables and remove autonomy_mode."""

    # journal_lines
    op.drop_index("ix_journal_lines_account_code", table_name="journal_lines")
    op.drop_index("ix_journal_lines_journal_entry", table_name="journal_lines")
    op.drop_table("journal_lines")

    # journal_entries
    op.drop_index("ix_journal_entries_entity_period", table_name="journal_entries")
    op.drop_index("ix_journal_entries_recommendation", table_name="journal_entries")
    op.drop_index("ix_journal_entries_close_run_status", table_name="journal_entries")
    op.drop_table("journal_entries")

    # recommendations: remove autonomy_mode
    op.drop_column("recommendations", "autonomy_mode")
