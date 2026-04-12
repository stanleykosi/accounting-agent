"""
Purpose: Add reconciliation tables for bank reconciliation, AR/AP ageing, intercompany,
payroll control, fixed assets, loan amortisation, accrual tracker, budget vs actual,
and trial balance reconciliation workflows.
Scope: Four tables — reconciliations, reconciliation_items, trial_balance_snapshots,
and reconciliation_anomalies — with all indexes, foreign keys, and check constraints
defined by the ORM models. Created by Step 29 (reconciliation domain models and matching
engines).
Dependencies: Alembic, SQLAlchemy, PostgreSQL JSONB, and existing close_runs/users tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision = "0009_reconciliation"
down_revision = "0008_journals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create reconciliation tables, indexes, and constraints."""

    # --- reconciliations ---
    op.create_table(
        "reconciliations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("close_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "reconciliation_type",
            sa.String(40),
            nullable=False,
            comment="The reconciliation category (bank_reconciliation, ar_ageing, etc.).",
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="draft",
            comment="Lifecycle state of the reconciliation run.",
        ),
        sa.Column(
            "summary",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
            comment="Aggregated reconciliation summary (matched count, exceptions, totals).",
        ),
        sa.Column(
            "blocking_reason",
            sa.Text,
            nullable=True,
            comment="Reason the reconciliation is blocked, required when status is 'blocked'.",
        ),
        sa.Column("approved_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
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
            ["close_run_id"],
            ["close_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
        ),
    )
    op.create_index(
        "ix_reconciliations_close_run_type",
        "reconciliations",
        ["close_run_id", "reconciliation_type"],
    )
    op.create_index(
        "ix_reconciliations_close_run_status",
        "reconciliations",
        ["close_run_id", "status"],
    )

    # --- reconciliation_items ---
    op.create_table(
        "reconciliation_items",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("reconciliation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "source_type",
            sa.String(30),
            nullable=False,
            comment="What kind of source produced this item.",
        ),
        sa.Column(
            "source_ref",
            sa.String(200),
            nullable=False,
            comment="Reference to the originating record.",
        ),
        sa.Column(
            "match_status",
            sa.String(20),
            nullable=False,
            server_default="unmatched",
            comment="Outcome of the matching process for this item.",
        ),
        sa.Column(
            "amount",
            sa.Numeric(20, 2),
            nullable=False,
            comment="Monetary amount of this reconciliation item.",
        ),
        sa.Column(
            "matched_to",
            postgresql.JSONB,
            nullable=False,
            server_default="[]",
            comment="List of counterpart references this item was matched to.",
        ),
        sa.Column(
            "difference_amount",
            sa.Numeric(20, 2),
            nullable=False,
            server_default="0.00",
            comment="Difference between this item and its matched counterpart(s).",
        ),
        sa.Column(
            "explanation",
            sa.Text,
            nullable=True,
            comment="System-generated or reviewer-provided explanation of the match outcome.",
        ),
        sa.Column(
            "requires_disposition",
            sa.Boolean,
            nullable=False,
            server_default="false",
            comment="Whether a reviewer must disposition this item before sign-off.",
        ),
        sa.Column(
            "disposition",
            sa.String(20),
            nullable=True,
            comment="Reviewer disposition choice when the item was resolved.",
        ),
        sa.Column(
            "disposition_reason",
            sa.Text,
            nullable=True,
            comment="Reviewer-provided reasoning for the disposition.",
        ),
        sa.Column("disposition_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "dimensions",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
            comment="Accounting dimensions (cost_centre, department, project) if applicable.",
        ),
        sa.Column(
            "period_date",
            sa.String(10),
            nullable=True,
            comment="Accounting period date associated with this item (YYYY-MM-DD).",
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
            ["reconciliation_id"],
            ["reconciliations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["disposition_by_user_id"],
            ["users.id"],
        ),
    )
    op.create_index(
        "ix_reconciliation_items_reconciliation",
        "reconciliation_items",
        ["reconciliation_id"],
    )
    op.create_index(
        "ix_reconciliation_items_match_status",
        "reconciliation_items",
        ["reconciliation_id", "match_status"],
    )
    op.create_index(
        "ix_reconciliation_items_source",
        "reconciliation_items",
        ["source_type", "source_ref"],
    )

    # --- trial_balance_snapshots ---
    op.create_table(
        "trial_balance_snapshots",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("close_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "snapshot_no",
            sa.Integer,
            nullable=False,
            comment="Sequential snapshot number within the close run.",
        ),
        sa.Column(
            "total_debits",
            sa.Numeric(20, 2),
            nullable=False,
            comment="Sum of all debit balances in this snapshot.",
        ),
        sa.Column(
            "total_credits",
            sa.Numeric(20, 2),
            nullable=False,
            comment="Sum of all credit balances in this snapshot.",
        ),
        sa.Column(
            "is_balanced",
            sa.Boolean,
            nullable=False,
            comment="Whether total debits equal total credits within tolerance.",
        ),
        sa.Column(
            "account_balances",
            postgresql.JSONB,
            nullable=False,
            server_default="[]",
            comment="List of per-account balance records (code, name, debit, credit, net).",
        ),
        sa.Column("generated_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "metadata_payload",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
            comment="Additional context (rule version, coa set version, generation timestamp).",
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
            ["close_run_id"],
            ["close_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["generated_by_user_id"],
            ["users.id"],
        ),
    )
    op.create_index(
        "ix_trial_balance_snapshots_close_run",
        "trial_balance_snapshots",
        ["close_run_id"],
    )
    op.create_index(
        "ix_trial_balance_snapshots_close_run_no",
        "trial_balance_snapshots",
        ["close_run_id", "snapshot_no"],
        unique=True,
    )

    # --- reconciliation_anomalies ---
    op.create_table(
        "reconciliation_anomalies",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("close_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "trial_balance_snapshot_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="The trial balance snapshot this anomaly was detected against, if applicable.",
        ),
        sa.Column(
            "anomaly_type",
            sa.String(30),
            nullable=False,
            comment="Category of the anomaly (imbalance, unusual balance, variance, etc.).",
        ),
        sa.Column(
            "severity",
            sa.String(10),
            nullable=False,
            comment="Severity level: info, warning, or blocking.",
        ),
        sa.Column(
            "account_code",
            sa.String(60),
            nullable=True,
            comment="GL account code associated with the anomaly, if applicable.",
        ),
        sa.Column(
            "description",
            sa.Text,
            nullable=False,
            comment="Human-readable description of the anomaly for reviewer investigation.",
        ),
        sa.Column(
            "details",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
            comment="Structured details (expected value, actual value, variance, threshold).",
        ),
        sa.Column(
            "resolved",
            sa.Boolean,
            nullable=False,
            server_default="false",
            comment="Whether a reviewer has investigated and resolved this anomaly.",
        ),
        sa.Column("resolved_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "resolution_note",
            sa.Text,
            nullable=True,
            comment="Reviewer-provided reasoning for resolving the anomaly.",
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
            ["close_run_id"],
            ["close_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["trial_balance_snapshot_id"],
            ["trial_balance_snapshots.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_user_id"],
            ["users.id"],
        ),
    )
    op.create_index(
        "ix_reconciliation_anomalies_close_run",
        "reconciliation_anomalies",
        ["close_run_id"],
    )
    op.create_index(
        "ix_reconciliation_anomalies_close_run_severity",
        "reconciliation_anomalies",
        ["close_run_id", "severity"],
    )
    op.create_index(
        "ix_reconciliation_anomalies_type",
        "reconciliation_anomalies",
        ["anomaly_type"],
    )


def downgrade() -> None:
    """Drop reconciliation tables and indexes."""

    # reconciliation_anomalies
    op.drop_index(
        "ix_reconciliation_anomalies_type",
        table_name="reconciliation_anomalies",
    )
    op.drop_index(
        "ix_reconciliation_anomalies_close_run_severity",
        table_name="reconciliation_anomalies",
    )
    op.drop_index(
        "ix_reconciliation_anomalies_close_run",
        table_name="reconciliation_anomalies",
    )
    op.drop_table("reconciliation_anomalies")

    # trial_balance_snapshots
    op.drop_index(
        "ix_trial_balance_snapshots_close_run_no",
        table_name="trial_balance_snapshots",
    )
    op.drop_index(
        "ix_trial_balance_snapshots_close_run",
        table_name="trial_balance_snapshots",
    )
    op.drop_table("trial_balance_snapshots")

    # reconciliation_items
    op.drop_index(
        "ix_reconciliation_items_source",
        table_name="reconciliation_items",
    )
    op.drop_index(
        "ix_reconciliation_items_match_status",
        table_name="reconciliation_items",
    )
    op.drop_index(
        "ix_reconciliation_items_reconciliation",
        table_name="reconciliation_items",
    )
    op.drop_table("reconciliation_items")

    # reconciliations
    op.drop_index(
        "ix_reconciliations_close_run_status",
        table_name="reconciliations",
    )
    op.drop_index(
        "ix_reconciliations_close_run_type",
        table_name="reconciliations",
    )
    op.drop_table("reconciliations")
