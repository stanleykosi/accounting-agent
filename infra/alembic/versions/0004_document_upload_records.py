"""
Purpose: Add canonical document upload persistence for close-run Collection workflows.
Scope: Source document rows, parser-version metadata, document issues, constraints,
and search/status indexes required by the primary upload path.
Dependencies: Alembic, SQLAlchemy, PostgreSQL JSONB, and existing auth/close-run tables.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision = "0004_document_upload_records"
down_revision = "0003_ownership_targets"
branch_labels = None
depends_on = None

DOCUMENT_TYPES = (
    "unknown",
    "invoice",
    "bank_statement",
    "payslip",
    "receipt",
    "contract",
)
DOCUMENT_SOURCE_CHANNELS = ("upload", "api_import", "manual_entry")
DOCUMENT_STATUSES = (
    "uploaded",
    "processing",
    "parsed",
    "needs_review",
    "approved",
    "rejected",
    "failed",
    "duplicate",
    "blocked",
)
DOCUMENT_ISSUE_SEVERITIES = ("info", "warning", "blocking")
DOCUMENT_ISSUE_STATUSES = ("open", "resolved", "dismissed")


def upgrade() -> None:
    """Create document upload tables and indexes used by Step 19."""

    op.create_table(
        "documents",
        *_uuid_primary_key_column(),
        sa.Column("close_run_id", sa.Uuid(), sa.ForeignKey("close_runs.id"), nullable=False),
        sa.Column("parent_document_id", sa.Uuid(), sa.ForeignKey("documents.id"), nullable=True),
        sa.Column("document_type", sa.Text(), nullable=False, server_default=sa.text("'unknown'")),
        sa.Column("source_channel", sa.Text(), nullable=False, server_default=sa.text("'upload'")),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256_hash", sa.String(length=64), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("classification_confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("ocr_required", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'uploaded'")),
        sa.Column("owner_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "last_touched_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        *_timestamp_columns(),
        sa.CheckConstraint(
            _in_check("document_type", DOCUMENT_TYPES),
            name="ck_documents_document_type_valid",
        ),
        sa.CheckConstraint(
            _in_check("source_channel", DOCUMENT_SOURCE_CHANNELS),
            name="ck_documents_source_channel_valid",
        ),
        sa.CheckConstraint(
            _in_check("status", DOCUMENT_STATUSES),
            name="ck_documents_status_valid",
        ),
        sa.CheckConstraint(
            "file_size_bytes >= 0",
            name="ck_documents_file_size_bytes_non_negative",
        ),
        sa.CheckConstraint(
            "length(sha256_hash) = 64",
            name="ck_documents_sha256_hash_length_valid",
        ),
        sa.CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_end >= period_start",
            name="ck_documents_period_range_valid",
        ),
        sa.CheckConstraint(
            "classification_confidence IS NULL "
            "OR (classification_confidence >= 0 AND classification_confidence <= 1)",
            name="ck_documents_classification_confidence_ratio_valid",
        ),
    )
    op.create_index("ix_documents_close_run_id", "documents", ["close_run_id"])
    op.create_index("ix_documents_sha256_hash", "documents", ["sha256_hash"])
    op.create_index("ix_documents_close_run_id_status", "documents", ["close_run_id", "status"])
    op.create_index(
        "ix_documents_original_filename_tsv",
        "documents",
        [sa.text("to_tsvector('simple', original_filename)")],
        postgresql_using="gin",
    )

    op.create_table(
        "document_versions",
        *_uuid_primary_key_column(),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("normalized_storage_key", sa.Text(), nullable=True),
        sa.Column("ocr_text_storage_key", sa.Text(), nullable=True),
        sa.Column("parser_name", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column(
            "raw_parse_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint("version_no >= 1", name="ck_document_versions_version_no_positive"),
        sa.CheckConstraint(
            "page_count IS NULL OR page_count >= 0",
            name="ck_document_versions_page_count_non_negative",
        ),
        sa.CheckConstraint(
            "length(checksum) = 64",
            name="ck_document_versions_checksum_length_valid",
        ),
        sa.UniqueConstraint(
            "document_id",
            "version_no",
            name="uq_document_versions_document_version",
        ),
    )
    op.create_index("ix_document_versions_document_id", "document_versions", ["document_id"])

    op.create_table(
        "document_issues",
        *_uuid_primary_key_column(),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("issue_type", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("assigned_to_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("resolved_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            _in_check("severity", DOCUMENT_ISSUE_SEVERITIES),
            name="ck_document_issues_severity_valid",
        ),
        sa.CheckConstraint(
            _in_check("status", DOCUMENT_ISSUE_STATUSES),
            name="ck_document_issues_status_valid",
        ),
        sa.CheckConstraint(
            "(status = 'open' AND resolved_by_user_id IS NULL AND resolved_at IS NULL) "
            "OR (status <> 'open' AND resolved_by_user_id IS NOT NULL AND resolved_at IS NOT NULL)",
            name="ck_document_issues_resolution_metadata_valid",
        ),
    )
    op.create_index("ix_document_issues_document_id", "document_issues", ["document_id"])
    op.create_index(
        "ix_document_issues_status_severity",
        "document_issues",
        ["status", "severity"],
    )


def downgrade() -> None:
    """Drop document upload tables and indexes in reverse dependency order."""

    op.drop_index("ix_document_issues_status_severity", table_name="document_issues")
    op.drop_index("ix_document_issues_document_id", table_name="document_issues")
    op.drop_table("document_issues")
    op.drop_index("ix_document_versions_document_id", table_name="document_versions")
    op.drop_table("document_versions")
    op.drop_index("ix_documents_original_filename_tsv", table_name="documents")
    op.drop_index("ix_documents_close_run_id_status", table_name="documents")
    op.drop_index("ix_documents_sha256_hash", table_name="documents")
    op.drop_index("ix_documents_close_run_id", table_name="documents")
    op.drop_table("documents")


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
