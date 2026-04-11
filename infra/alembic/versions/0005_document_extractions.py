"""
Purpose: Add canonical extraction persistence tables for parsed documents.
Scope: Document extraction versions, field-level evidence, and line item records.
Dependencies: Alembic, SQLAlchemy, PostgreSQL JSONB, and document upload tables.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision = "0005_document_extractions"
down_revision = "0004_document_upload_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create extraction persistence tables and lookup indexes."""

    op.create_table(
        "document_extractions",
        *_uuid_primary_key_column(),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("schema_name", sa.String(length=50), nullable=False),
        sa.Column("schema_version", sa.String(length=20), nullable=False),
        sa.Column(
            "extracted_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "confidence_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "approved_version",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "version_no >= 1",
            name="ck_document_extractions_extraction_version_no_positive",
        ),
        sa.UniqueConstraint(
            "document_id",
            "version_no",
            name="uq_document_extractions_document_version",
        ),
    )
    op.create_index(
        "ix_document_extractions_document_id",
        "document_extractions",
        ["document_id"],
    )

    op.create_table(
        "extracted_fields",
        *_uuid_primary_key_column(),
        sa.Column(
            "document_extraction_id",
            sa.Uuid(),
            sa.ForeignKey("document_extractions.id"),
            nullable=False,
        ),
        sa.Column("field_name", sa.String(length=100), nullable=False),
        sa.Column("field_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("field_type", sa.String(length=20), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("evidence_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "is_human_corrected",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_extracted_fields_extracted_field_confidence_valid",
        ),
    )
    op.create_index(
        "ix_extracted_fields_document_extraction_id_field_name",
        "extracted_fields",
        ["document_extraction_id", "field_name"],
    )

    op.create_table(
        "document_line_items",
        *_uuid_primary_key_column(),
        sa.Column(
            "document_extraction_id",
            sa.Uuid(),
            sa.ForeignKey("document_extractions.id"),
            nullable=False,
        ),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=True),
        sa.Column("unit_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("tax_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column(
            "dimensions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("evidence_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint("line_no >= 1", name="ck_document_line_items_line_item_no_positive"),
        sa.UniqueConstraint(
            "document_extraction_id",
            "line_no",
            name="uq_document_line_items_extraction_line",
        ),
    )
    op.create_index(
        "ix_document_line_items_document_extraction_id",
        "document_line_items",
        ["document_extraction_id"],
    )


def downgrade() -> None:
    """Drop extraction persistence tables in reverse dependency order."""

    op.drop_index(
        "ix_document_line_items_document_extraction_id",
        table_name="document_line_items",
    )
    op.drop_table("document_line_items")
    op.drop_index(
        "ix_extracted_fields_document_extraction_id_field_name",
        table_name="extracted_fields",
    )
    op.drop_table("extracted_fields")
    op.drop_index("ix_document_extractions_document_id", table_name="document_extractions")
    op.drop_table("document_extractions")


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
