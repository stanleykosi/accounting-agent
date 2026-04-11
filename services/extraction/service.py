"""
Purpose: Service layer for extraction operations, persistence, and retrieval.
Scope: High-level extraction business logic for review, approval, and retrieval
of structured field data.
Dependencies: Extraction database models, field extractors, and evidence reference
builders.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from services.common.enums import DocumentStatus
from services.db.models.documents import Document
from services.db.models.extractions import (
    DocumentExtraction,
    DocumentLineItem,
    ExtractedField,
)
from sqlalchemy.orm import Session


class ExtractionService:
    """Service for managing document extraction operations."""

    def __init__(self, db: Session):
        """Initialize with database session."""

        self.db = db

    def get_latest_extraction(
        self,
        document_id: UUID,
    ) -> DocumentExtraction | None:
        """Retrieve the most recent extraction for a document."""

        return (
            self.db.query(DocumentExtraction)
            .filter(DocumentExtraction.document_id == document_id)
            .order_by(DocumentExtraction.version_no.desc())
            .first()
        )

    def get_extraction_fields(
        self,
        extraction_id: UUID,
    ) -> list[ExtractedField]:
        """Retrieve all fields for an extraction."""

        return (
            self.db.query(ExtractedField)
            .filter(ExtractedField.document_extraction_id == extraction_id)
            .order_by(ExtractedField.field_name)
            .all()
        )

    def get_line_items(
        self,
        extraction_id: UUID,
    ) -> list[DocumentLineItem]:
        """Retrieve all line items for an extraction."""

        return (
            self.db.query(DocumentLineItem)
            .filter(DocumentLineItem.document_extraction_id == extraction_id)
            .order_by(DocumentLineItem.line_no)
            .all()
        )

    def mark_extraction_approved(
        self,
        extraction_id: UUID,
    ) -> bool:
        """Mark an extraction as human-approved.

        Args:
            extraction_id: UUID of the extraction to approve.

        Returns:
            True if approval was successful.
        """

        extraction = (
            self.db.query(DocumentExtraction).filter(DocumentExtraction.id == extraction_id).first()
        )

        if extraction is None:
            return False

        extraction.approved_version = True

        document = self.db.query(Document).filter(Document.id == extraction.document_id).first()
        if document:
            document.status = DocumentStatus.APPROVED

        self.db.commit()
        return True

    def apply_field_correction(
        self,
        field_id: UUID,
        corrected_value: Any,
        corrected_type: str,
    ) -> bool:
        """Apply a human correction to an extracted field.

        Args:
            field_id: UUID of the field to correct.
            corrected_value: The corrected value.
            corrected_type: The type of the corrected value.

        Returns:
            True if correction was applied successfully.
        """

        field = self.db.query(ExtractedField).filter(ExtractedField.id == field_id).first()

        if field is None:
            return False

        field.field_value = corrected_value
        field.field_type = corrected_type
        field.is_human_corrected = True

        self.db.commit()
        return True

    def get_fields_below_threshold(
        self,
        extraction_id: UUID,
        threshold: float = 0.7,
    ) -> list[ExtractedField]:
        """Get fields with confidence below threshold.

        Args:
            extraction_id: UUID of the extraction to check.
            threshold: Confidence threshold (default 0.7).

        Returns:
            List of low-confidence fields.
        """

        return (
            self.db.query(ExtractedField)
            .filter(
                ExtractedField.document_extraction_id == extraction_id,
                ExtractedField.confidence < threshold,
            )
            .all()
        )


__all__ = ["ExtractionService"]
