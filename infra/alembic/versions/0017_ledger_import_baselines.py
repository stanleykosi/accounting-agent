"""
Purpose: Create canonical imported-ledger baseline tables and close-run bindings.
Scope: Entity-scoped GL/TB import batches, their imported line payloads, and the
single binding that connects one close run to its imported baseline.
Dependencies: Alembic, SQLAlchemy, PostgreSQL JSONB, and the existing entity/auth/
close-run tables.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision = "0017_ledger_import_baselines"
down_revision = "0016_supporting_schedules"
branch_labels = None
depends_on = None

LEDGER_IMPORT_BINDING_SOURCES = ("auto", "manual")


def upgrade() -> None:
    """Create the imported-ledger batch, line, and close-run binding tables."""

    op.create_table(
        "general_ledger_import_batches",
        *_uuid_primary_key_column(),
        sa.Column(
            "entity_id",
            sa.Uuid(),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("source_format", sa.String(length=16), nullable=False),
        sa.Column("uploaded_filename", sa.String(length=255), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column(
            "imported_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "import_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        *_timestamp_columns(),
        sa.CheckConstraint("period_end >= period_start", name="period_range_valid"),
        sa.CheckConstraint("row_count >= 1", name="row_count_positive"),
    )
    op.create_index(
        "ix_general_ledger_import_batches_entity_id",
        "general_ledger_import_batches",
        ["entity_id"],
    )
    op.create_index(
        "ix_gl_import_batches_entity_period",
        "general_ledger_import_batches",
        ["entity_id", "period_start", "period_end"],
    )

    op.create_table(
        "general_ledger_import_lines",
        *_uuid_primary_key_column(),
        sa.Column(
            "batch_id",
            sa.Uuid(),
            sa.ForeignKey("general_ledger_import_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("account_code", sa.String(length=60), nullable=False),
        sa.Column("account_name", sa.String(length=255), nullable=True),
        sa.Column("reference", sa.String(length=200), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("debit_amount", sa.Numeric(20, 2), nullable=False),
        sa.Column("credit_amount", sa.Numeric(20, 2), nullable=False),
        sa.Column(
            "dimensions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("external_ref", sa.String(length=120), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "debit_amount >= 0 AND credit_amount >= 0",
            name="amounts_non_negative",
        ),
        sa.CheckConstraint(
            "(debit_amount = 0 AND credit_amount > 0) OR "
            "(credit_amount = 0 AND debit_amount > 0)",
            name="single_sided_amount",
        ),
    )
    op.create_index(
        "ix_gl_import_lines_batch_date",
        "general_ledger_import_lines",
        ["batch_id", "posting_date"],
    )
    op.create_index(
        "ix_gl_import_lines_batch_account",
        "general_ledger_import_lines",
        ["batch_id", "account_code"],
    )

    op.create_table(
        "trial_balance_import_batches",
        *_uuid_primary_key_column(),
        sa.Column(
            "entity_id",
            sa.Uuid(),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("source_format", sa.String(length=16), nullable=False),
        sa.Column("uploaded_filename", sa.String(length=255), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column(
            "imported_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "import_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        *_timestamp_columns(),
        sa.CheckConstraint("period_end >= period_start", name="period_range_valid"),
        sa.CheckConstraint("row_count >= 1", name="row_count_positive"),
    )
    op.create_index(
        "ix_trial_balance_import_batches_entity_id",
        "trial_balance_import_batches",
        ["entity_id"],
    )
    op.create_index(
        "ix_tb_import_batches_entity_period",
        "trial_balance_import_batches",
        ["entity_id", "period_start", "period_end"],
    )

    op.create_table(
        "trial_balance_import_lines",
        *_uuid_primary_key_column(),
        sa.Column(
            "batch_id",
            sa.Uuid(),
            sa.ForeignKey("trial_balance_import_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("account_code", sa.String(length=60), nullable=False),
        sa.Column("account_name", sa.String(length=255), nullable=True),
        sa.Column("account_type", sa.String(length=80), nullable=True),
        sa.Column("debit_balance", sa.Numeric(20, 2), nullable=False),
        sa.Column("credit_balance", sa.Numeric(20, 2), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "debit_balance >= 0 AND credit_balance >= 0",
            name="balances_non_negative",
        ),
        sa.CheckConstraint(
            "(debit_balance = 0 AND credit_balance >= 0) OR "
            "(credit_balance = 0 AND debit_balance >= 0)",
            name="single_sided_balance",
        ),
    )
    op.create_index(
        "ix_tb_import_lines_batch_account",
        "trial_balance_import_lines",
        ["batch_id", "account_code"],
    )

    op.create_table(
        "close_run_ledger_bindings",
        *_uuid_primary_key_column(),
        sa.Column(
            "close_run_id",
            sa.Uuid(),
            sa.ForeignKey("close_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "general_ledger_import_batch_id",
            sa.Uuid(),
            sa.ForeignKey("general_ledger_import_batches.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "trial_balance_import_batch_id",
            sa.Uuid(),
            sa.ForeignKey("trial_balance_import_batches.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "binding_source",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'auto'"),
        ),
        sa.Column(
            "bound_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        *_timestamp_columns(),
        sa.CheckConstraint(
            _in_check("binding_source", LEDGER_IMPORT_BINDING_SOURCES),
            name="binding_source_valid",
        ),
        sa.CheckConstraint(
            (
                "general_ledger_import_batch_id IS NOT NULL "
                "OR trial_balance_import_batch_id IS NOT NULL"
            ),
            name="at_least_one_import_required",
        ),
        sa.UniqueConstraint("close_run_id", name="uq_close_run_ledger_bindings_close_run_id"),
    )
    op.create_index(
        "ix_close_run_ledger_bindings_gl_batch",
        "close_run_ledger_bindings",
        ["general_ledger_import_batch_id"],
    )
    op.create_index(
        "ix_close_run_ledger_bindings_tb_batch",
        "close_run_ledger_bindings",
        ["trial_balance_import_batch_id"],
    )


def downgrade() -> None:
    """Drop the imported-ledger tables in reverse dependency order."""

    op.drop_index(
        "ix_close_run_ledger_bindings_tb_batch",
        table_name="close_run_ledger_bindings",
    )
    op.drop_index(
        "ix_close_run_ledger_bindings_gl_batch",
        table_name="close_run_ledger_bindings",
    )
    op.drop_table("close_run_ledger_bindings")

    op.drop_index(
        "ix_tb_import_lines_batch_account",
        table_name="trial_balance_import_lines",
    )
    op.drop_table("trial_balance_import_lines")

    op.drop_index(
        "ix_tb_import_batches_entity_period",
        table_name="trial_balance_import_batches",
    )
    op.drop_index(
        "ix_trial_balance_import_batches_entity_id",
        table_name="trial_balance_import_batches",
    )
    op.drop_table("trial_balance_import_batches")

    op.drop_index(
        "ix_gl_import_lines_batch_account",
        table_name="general_ledger_import_lines",
    )
    op.drop_index(
        "ix_gl_import_lines_batch_date",
        table_name="general_ledger_import_lines",
    )
    op.drop_table("general_ledger_import_lines")

    op.drop_index(
        "ix_gl_import_batches_entity_period",
        table_name="general_ledger_import_batches",
    )
    op.drop_index(
        "ix_general_ledger_import_batches_entity_id",
        table_name="general_ledger_import_batches",
    )
    op.drop_table("general_ledger_import_batches")


def _uuid_primary_key_column() -> tuple[sa.Column[Any], ...]:
    """Return the shared UUID primary key column used by migration-created tables."""

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
    """Return standard creation and update timestamp columns."""

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
