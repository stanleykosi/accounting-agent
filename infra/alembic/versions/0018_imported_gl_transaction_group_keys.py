"""
Purpose: Persist canonical transaction grouping keys for imported general-ledger rows.
Scope: Adds one non-null transaction_group_key column, backfills existing rows with the
canonical deterministic derivation, and indexes the grouping key for batch-scoped reads.
Dependencies: Alembic, SQLAlchemy, and PostgreSQL string/hash helpers.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision = "0018_imported_gl_transaction_group_keys"
down_revision = "0017_ledger_import_baselines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add and backfill the canonical imported-GL transaction grouping column."""

    op.add_column(
        "general_ledger_import_lines",
        sa.Column("transaction_group_key", sa.String(length=40), nullable=True),
    )
    op.execute(
        """
        UPDATE general_ledger_import_lines
        SET transaction_group_key = 'glgrp_' || md5(
            posting_date::text || '|' ||
            CASE
                WHEN btrim(coalesce(external_ref, '')) <> ''
                    THEN 'external_ref|' || lower(btrim(external_ref))
                WHEN btrim(coalesce(reference, '')) <> ''
                    THEN 'reference|' || lower(btrim(reference))
                WHEN btrim(coalesce(description, '')) <> ''
                    THEN 'description|' || lower(btrim(description))
                ELSE 'line|' || line_no::text
            END
        )
        """
    )
    op.alter_column(
        "general_ledger_import_lines",
        "transaction_group_key",
        existing_type=sa.String(length=40),
        nullable=False,
    )
    op.create_index(
        "ix_gl_import_lines_batch_group",
        "general_ledger_import_lines",
        ["batch_id", "transaction_group_key"],
    )


def downgrade() -> None:
    """Remove the imported-GL transaction grouping column and supporting index."""

    op.drop_index("ix_gl_import_lines_batch_group", table_name="general_ledger_import_lines")
    op.drop_column("general_ledger_import_lines", "transaction_group_key")
