"""
Purpose: Create canonical standalone Step 6 supporting-schedule tables.
Scope: Supporting-schedule headers and row payloads for fixed assets, loan
amortisation, accrual tracker, and budget-vs-actual workpapers.
Dependencies: Alembic, SQLAlchemy, PostgreSQL JSONB, and the close-run/user tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision = "0016_supporting_schedules"
down_revision = "0015_journal_postings"
branch_labels = None
depends_on = None

SUPPORTING_SCHEDULE_TYPES = (
    "fixed_assets",
    "loan_amortisation",
    "accrual_tracker",
    "budget_vs_actual",
)
SUPPORTING_SCHEDULE_STATUSES = ("draft", "in_review", "approved", "not_applicable")


def upgrade() -> None:
    """Create the canonical Step 6 supporting-schedule tables."""

    op.create_table(
        "supporting_schedules",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("close_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schedule_type", sa.String(length=40), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("reviewed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["close_run_id"], ["close_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"]),
        sa.CheckConstraint(
            f"schedule_type IN {SUPPORTING_SCHEDULE_TYPES}",
            name="ck_supporting_schedules_supporting_schedule_type_valid",
        ),
        sa.CheckConstraint(
            f"status IN {SUPPORTING_SCHEDULE_STATUSES}",
            name="ck_supporting_schedules_supporting_schedule_status_valid",
        ),
        sa.UniqueConstraint(
            "close_run_id",
            "schedule_type",
            name="uq_supporting_schedules_close_run_type",
        ),
    )
    op.create_index(
        "ix_supporting_schedules_close_run_id",
        "supporting_schedules",
        ["close_run_id"],
    )
    op.create_index(
        "ix_supporting_schedules_close_run_status",
        "supporting_schedules",
        ["close_run_id", "status"],
    )

    op.create_table(
        "supporting_schedule_rows",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("supporting_schedule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("row_ref", sa.String(length=200), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
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
            ["supporting_schedule_id"],
            ["supporting_schedules.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "supporting_schedule_id",
            "row_ref",
            name="uq_supporting_schedule_rows_schedule_row_ref",
        ),
        sa.UniqueConstraint(
            "supporting_schedule_id",
            "line_no",
            name="uq_supporting_schedule_rows_schedule_line_no",
        ),
    )
    op.create_index(
        "ix_supporting_schedule_rows_schedule_id",
        "supporting_schedule_rows",
        ["supporting_schedule_id"],
    )


def downgrade() -> None:
    """Drop the standalone Step 6 supporting-schedule tables."""

    op.drop_index(
        "ix_supporting_schedule_rows_schedule_id",
        table_name="supporting_schedule_rows",
    )
    op.drop_table("supporting_schedule_rows")
    op.drop_index(
        "ix_supporting_schedules_close_run_status",
        table_name="supporting_schedules",
    )
    op.drop_index(
        "ix_supporting_schedules_close_run_id",
        table_name="supporting_schedules",
    )
    op.drop_table("supporting_schedules")
