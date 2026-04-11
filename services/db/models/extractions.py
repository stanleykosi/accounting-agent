"""
Purpose: Define extraction persistence models for document extractions,
extracted fields, and line items.
Scope: SQLAlchemy models for extraction payloads, field-level evidence,
and confidence summaries used by downstream review and accounting.
Dependencies: Shared domain enums, SQLAlchemy ORM primitives, and the database
base helpers from Step 8.
"""

from __future__ import annotations

from uuid import UUID

from services.common.types import JsonObject, JsonValue
from services.db.base import Base, TimestampedModel, UUIDPrimaryKeyMixin
from sqlalchemy import (
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


class DocumentExtraction(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one extraction result for a document version."""

    __tablename__ = "document_extractions"
    __table_args__ = (
        CheckConstraint("version_no >= 1", name="extraction_version_no_positive"),
        UniqueConstraint(
            "document_id",
            "version_no",
            name="uq_document_extractions_document_version",
        ),
        Index("ix_document_extractions_document_id", "document_id"),
    )

    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_name: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Name of the extraction schema used.",
    )
    schema_version: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="Version of the extraction schema.",
    )
    extracted_payload: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        comment="Full extraction payload as JSON.",
    )
    confidence_summary: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        comment="Aggregate confidence metrics.",
    )
    needs_review: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default="false",
        comment="True if any field failed confidence threshold.",
    )
    approved_version: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default="false",
        comment="True if a reviewer approved this extraction version.",
    )


class ExtractedField(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one field-level extraction with evidence and confidence."""

    __tablename__ = "extracted_fields"
    __table_args__ = (
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="extracted_field_confidence_valid",
        ),
        Index(
            "ix_extracted_fields_document_extraction_id_field_name",
            "document_extraction_id",
            "field_name",
        ),
    )

    document_extraction_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_extractions.id"),
        nullable=False,
    )
    field_name: Mapped[str] = mapped_column(String(100), nullable=False)
    field_value: Mapped[JsonValue | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Typed field value as JSON.",
    )
    field_type: Mapped[str] = mapped_column(String(20), nullable=False)
    confidence: Mapped[float] = mapped_column(
        Numeric(5, 4),
        nullable=False,
        comment="Confidence score 0-1 for this field.",
    )
    evidence_ref: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        comment="Source location reference for this field.",
    )
    is_human_corrected: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default="false",
        comment="True if a human corrected this field after extraction.",
    )


class DocumentLineItem(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one line item from a document table or structured section."""

    __tablename__ = "document_line_items"
    __table_args__ = (
        CheckConstraint("line_no >= 1", name="line_item_no_positive"),
        UniqueConstraint(
            "document_extraction_id",
            "line_no",
            name="uq_document_line_items_extraction_line",
        ),
        Index(
            "ix_document_line_items_document_extraction_id",
            "document_extraction_id",
        ),
    )

    document_extraction_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_extractions.id"),
        nullable=False,
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    quantity: Mapped[float | None] = mapped_column(
        Numeric(18, 6),
        nullable=True,
    )
    unit_price: Mapped[float | None] = mapped_column(
        Numeric(18, 6),
        nullable=True,
    )
    amount: Mapped[float | None] = mapped_column(
        Numeric(18, 2),
        nullable=True,
    )
    tax_amount: Mapped[float | None] = mapped_column(
        Numeric(18, 2),
        nullable=True,
    )
    dimensions: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Cost centre, department, project tags.",
    )
    evidence_ref: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        comment="Source location reference for this line item.",
    )


__all__ = [
    "DocumentExtraction",
    "DocumentLineItem",
    "ExtractedField",
]
