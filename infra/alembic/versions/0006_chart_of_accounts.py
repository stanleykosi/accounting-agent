"""
Purpose: Add canonical chart-of-accounts persistence for upload, fallback, and mapping workflows.
Scope: Versioned COA sets, COA account rows, and reusable mapping rules with strict
activation/version constraints.
Dependencies: Alembic, SQLAlchemy, PostgreSQL JSONB, and existing entity/auth tables.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision = "0006_chart_of_accounts"
down_revision = "0005_document_extractions"
branch_labels = None
depends_on = None

COA_SET_SOURCES = ("manual_upload", "quickbooks_sync", "fallback_nigerian_sme")


def upgrade() -> None:
    """Create chart-of-accounts tables and indexes required by Step 24."""

    op.create_table(
        "coa_sets",
        *_uuid_primary_key_column(),
        sa.Column("entity_id", sa.Uuid(), sa.ForeignKey("entities.id"), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "import_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(_in_check("source", COA_SET_SOURCES), name="ck_coa_sets_source_valid"),
        sa.CheckConstraint("version_no >= 1", name="ck_coa_sets_version_no_positive"),
        sa.UniqueConstraint("entity_id", "version_no", name="uq_coa_sets_entity_version"),
    )
    op.create_index("ix_coa_sets_entity_id_source", "coa_sets", ["entity_id", "source"])
    op.create_index("ix_coa_sets_entity_id_version_no", "coa_sets", ["entity_id", "version_no"])
    op.create_index(
        "uq_coa_sets_entity_active",
        "coa_sets",
        ["entity_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "coa_accounts",
        *_uuid_primary_key_column(),
        sa.Column("coa_set_id", sa.Uuid(), sa.ForeignKey("coa_sets.id"), nullable=False),
        sa.Column("account_code", sa.Text(), nullable=False),
        sa.Column("account_name", sa.Text(), nullable=False),
        sa.Column("account_type", sa.Text(), nullable=False),
        sa.Column("parent_account_id", sa.Uuid(), sa.ForeignKey("coa_accounts.id"), nullable=True),
        sa.Column("is_postable", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("external_ref", sa.Text(), nullable=True),
        sa.Column(
            "dimension_defaults",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        *_timestamp_columns(),
        sa.UniqueConstraint("coa_set_id", "account_code", name="uq_coa_accounts_set_code"),
    )
    op.create_index(
        "ix_coa_accounts_coa_set_id_account_type",
        "coa_accounts",
        ["coa_set_id", "account_type"],
    )
    op.create_index(
        "ix_coa_accounts_coa_set_id_account_code",
        "coa_accounts",
        ["coa_set_id", "account_code"],
    )

    op.create_table(
        "coa_mapping_rules",
        *_uuid_primary_key_column(),
        sa.Column("entity_id", sa.Uuid(), sa.ForeignKey("entities.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column(
            "match_conditions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("target_account_id", sa.Uuid(), sa.ForeignKey("coa_accounts.id"), nullable=False),
        sa.Column(
            "target_dimensions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_from_override",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_timestamp_columns(),
        sa.CheckConstraint("priority >= 0", name="ck_coa_mapping_rules_priority_non_negative"),
    )
    op.create_index(
        "ix_coa_mapping_rules_entity_priority",
        "coa_mapping_rules",
        ["entity_id", "priority"],
    )
    op.create_index(
        "ix_coa_mapping_rules_entity_active",
        "coa_mapping_rules",
        ["entity_id", "is_active"],
    )


def downgrade() -> None:
    """Drop chart-of-accounts tables and indexes in reverse dependency order."""

    op.drop_index("ix_coa_mapping_rules_entity_active", table_name="coa_mapping_rules")
    op.drop_index("ix_coa_mapping_rules_entity_priority", table_name="coa_mapping_rules")
    op.drop_table("coa_mapping_rules")

    op.drop_index("ix_coa_accounts_coa_set_id_account_code", table_name="coa_accounts")
    op.drop_index("ix_coa_accounts_coa_set_id_account_type", table_name="coa_accounts")
    op.drop_table("coa_accounts")

    op.drop_index("uq_coa_sets_entity_active", table_name="coa_sets")
    op.drop_index("ix_coa_sets_entity_id_version_no", table_name="coa_sets")
    op.drop_index("ix_coa_sets_entity_id_source", table_name="coa_sets")
    op.drop_table("coa_sets")


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
