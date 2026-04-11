"""
Purpose: Define document intake persistence for close-run source files and issues.
Scope: Uploaded document rows, versioned parser outputs, and canonical issue records
used by Collection-phase workflow controls.
Dependencies: Shared domain enums, SQLAlchemy ORM primitives, and the database base helpers.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from services.common.enums import (
    DocumentIssueSeverity,
    DocumentIssueStatus,
    DocumentSourceChannel,
    DocumentStatus,
    DocumentType,
)
from services.common.types import JsonObject
from services.db.base import Base, TimestampedModel, UUIDPrimaryKeyMixin, build_text_choice_check
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class Document(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one source or split child document attached to a close run."""

    __tablename__ = "documents"
    __table_args__ = (
        build_text_choice_check(
            column_name="document_type",
            values=DocumentType.values(),
            constraint_name="document_type_valid",
        ),
        build_text_choice_check(
            column_name="source_channel",
            values=DocumentSourceChannel.values(),
            constraint_name="source_channel_valid",
        ),
        build_text_choice_check(
            column_name="status",
            values=DocumentStatus.values(),
            constraint_name="status_valid",
        ),
        CheckConstraint("file_size_bytes >= 0", name="file_size_bytes_non_negative"),
        CheckConstraint("length(sha256_hash) = 64", name="sha256_hash_length_valid"),
        CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_end >= period_start",
            name="period_range_valid",
        ),
        CheckConstraint(
            "classification_confidence IS NULL "
            "OR (classification_confidence >= 0 AND classification_confidence <= 1)",
            name="classification_confidence_ratio_valid",
        ),
        Index("ix_documents_close_run_id", "close_run_id"),
        Index("ix_documents_sha256_hash", "sha256_hash"),
        Index("ix_documents_close_run_id_status", "close_run_id", "status"),
    )

    close_run_id: Mapped[UUID] = mapped_column(ForeignKey("close_runs.id"), nullable=False)
    parent_document_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("documents.id"),
        nullable=True,
    )
    document_type: Mapped[str] = mapped_column(String, nullable=False)
    source_channel: Mapped[str] = mapped_column(String, nullable=False)
    storage_key: Mapped[str] = mapped_column(String, nullable=False)
    original_filename: Mapped[str] = mapped_column(String, nullable=False)
    mime_type: Mapped[str] = mapped_column(String, nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    period_start: Mapped[date | None] = mapped_column(nullable=True)
    period_end: Mapped[date | None] = mapped_column(nullable=True)
    classification_confidence: Mapped[float | None] = mapped_column(
        Numeric(5, 4),
        nullable=True,
    )
    ocr_required: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default="false",
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    owner_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    last_touched_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
    )


class DocumentVersion(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist parser-version metadata and derivative references for one document version."""

    __tablename__ = "document_versions"
    __table_args__ = (
        CheckConstraint("version_no >= 1", name="version_no_positive"),
        CheckConstraint("page_count IS NULL OR page_count >= 0", name="page_count_non_negative"),
        CheckConstraint("length(checksum) = 64", name="checksum_length_valid"),
        UniqueConstraint("document_id", "version_no", name="uq_document_versions_document_version"),
        Index("ix_document_versions_document_id", "document_id"),
    )

    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    normalized_storage_key: Mapped[str | None] = mapped_column(String, nullable=True)
    ocr_text_storage_key: Mapped[str | None] = mapped_column(String, nullable=True)
    parser_name: Mapped[str] = mapped_column(String, nullable=False)
    parser_version: Mapped[str] = mapped_column(String, nullable=False)
    raw_parse_payload: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)


class DocumentIssue(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one document-level issue that can drive review routing or gate blockers."""

    __tablename__ = "document_issues"
    __table_args__ = (
        build_text_choice_check(
            column_name="severity",
            values=DocumentIssueSeverity.values(),
            constraint_name="severity_valid",
        ),
        build_text_choice_check(
            column_name="status",
            values=DocumentIssueStatus.values(),
            constraint_name="status_valid",
        ),
        CheckConstraint(
            "(status = 'open' AND resolved_by_user_id IS NULL AND resolved_at IS NULL) "
            "OR (status <> 'open' AND resolved_by_user_id IS NOT NULL AND resolved_at IS NOT NULL)",
            name="resolution_metadata_valid",
        ),
        Index("ix_document_issues_document_id", "document_id"),
        Index("ix_document_issues_status_severity", "status", "severity"),
    )

    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"), nullable=False)
    issue_type: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    details: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    assigned_to_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    resolved_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)


__all__ = ["Document", "DocumentIssue", "DocumentVersion"]
