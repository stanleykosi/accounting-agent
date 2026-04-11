"""
Purpose: Integration tests for extraction persistence and retrieval.
Scope: Tests for extraction service operations, field persistence, and
retrieval workflows.
Dependencies: Extraction service, database models, and test fixtures.
"""

from decimal import Decimal
from uuid import uuid4

import pytest
from services.common.enums import DocumentSourceChannel, DocumentStatus, DocumentType
from services.db.base import Base
from services.db.models.documents import Document
from services.db.models.extractions import (
    DocumentExtraction,
    DocumentLineItem,
    ExtractedField,
)
from services.extraction.service import ExtractionService
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(
    _type_: JSONB,
    _compiler: object,
    **_compiler_kwargs: object,
) -> str:
    """Let extraction persistence tests run against in-memory SQLite."""

    return "JSON"


@pytest.fixture()
def db_session():
    """Provide a lightweight database session for extraction persistence tests."""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    tables = [
        Document.__table__,
        DocumentExtraction.__table__,
        ExtractedField.__table__,
        DocumentLineItem.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as session:
        yield session


@pytest.mark.integration
class TestExtractionServicePersistence:
    """Tests for extraction service database operations."""

    def test_get_latest_extraction_returns_none_for_new_document(
        self,
        db_session,
    ):
        """Service returns None when no extraction exists."""
        service = ExtractionService(db_session)
        doc_id = uuid4()

        result = service.get_latest_extraction(doc_id)

        assert result is None

    def test_get_extraction_fields_returns_empty_for_new_extraction(
        self,
        db_session,
    ):
        """Service returns empty list when no fields exist."""
        service = ExtractionService(db_session)
        extraction_id = uuid4()

        result = service.get_extraction_fields(extraction_id)

        assert result == []

    def test_get_line_items_returns_empty_for_new_extraction(
        self,
        db_session,
    ):
        """Service returns empty list when no line items exist."""
        service = ExtractionService(db_session)
        extraction_id = uuid4()

        result = service.get_line_items(extraction_id)

        assert result == []

    def test_mark_extraction_approves_document(
        self,
        db_session,
    ):
        """Approval updates both extraction and document status."""
        doc_id = uuid4()
        extraction_id = uuid4()

        doc = Document(
            id=doc_id,
            close_run_id=uuid4(),
            document_type=DocumentType.INVOICE,
            source_channel=DocumentSourceChannel.UPLOAD,
            storage_key="test/doc.pdf",
            original_filename="test.pdf",
            mime_type="application/pdf",
            file_size_bytes=1024,
            sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            status=DocumentStatus.NEEDS_REVIEW,
        )
        db_session.add(doc)

        extraction = DocumentExtraction(
            id=extraction_id,
            document_id=doc_id,
            version_no=1,
            schema_name="invoice",
            schema_version="1.0.0",
            extracted_payload={"fields": []},
            confidence_summary={
                "overall_confidence": 0.85,
                "field_count": 10,
                "low_confidence_fields": 0,
                "missing_fields": 0,
            },
            needs_review=False,
        )
        db_session.add(extraction)
        db_session.commit()

        service = ExtractionService(db_session)
        result = service.mark_extraction_approved(extraction_id)

        assert result is True
        db_session.refresh(extraction)
        db_session.refresh(doc)
        assert extraction.approved_version is True
        assert doc.status == DocumentStatus.APPROVED

    def test_apply_field_correction(
        self,
        db_session,
    ):
        """Field correction updates value and marks as corrected."""
        doc_id = uuid4()
        extraction_id = uuid4()

        doc = Document(
            id=doc_id,
            close_run_id=uuid4(),
            document_type=DocumentType.INVOICE,
            source_channel=DocumentSourceChannel.UPLOAD,
            storage_key="test/doc.pdf",
            original_filename="test.pdf",
            mime_type="application/pdf",
            file_size_bytes=1024,
            sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            status=DocumentStatus.PARSED,
        )
        extraction = DocumentExtraction(
            id=extraction_id,
            document_id=doc_id,
            version_no=1,
            schema_name="invoice",
            schema_version="1.0.0",
            extracted_payload={"fields": []},
            confidence_summary={
                "overall_confidence": 0.75,
                "field_count": 5,
                "low_confidence_fields": 1,
                "missing_fields": 0,
            },
            needs_review=True,
        )
        field = ExtractedField(
            id=uuid4(),
            document_extraction_id=extraction_id,
            field_name="total",
            field_value="10000.00",
            field_type="decimal",
            confidence=0.65,
            evidence_ref={"snippet": "Total: 10000.00"},
            is_human_corrected=False,
        )

        db_session.add_all([doc, extraction, field])
        db_session.commit()

        service = ExtractionService(db_session)
        result = service.apply_field_correction(
            field.id,
            corrected_value="12500.00",
            corrected_type="decimal",
        )

        assert result is True
        db_session.refresh(field)
        assert field.field_value == "12500.00"
        assert field.field_type == "decimal"
        assert field.is_human_corrected is True

    def test_get_fields_below_threshold(
        self,
        db_session,
    ):
        """Service returns fields with confidence below threshold."""
        doc_id = uuid4()
        extraction_id = uuid4()

        doc = Document(
            id=doc_id,
            close_run_id=uuid4(),
            document_type=DocumentType.INVOICE,
            source_channel=DocumentSourceChannel.UPLOAD,
            storage_key="test/doc.pdf",
            original_filename="test.pdf",
            mime_type="application/pdf",
            file_size_bytes=1024,
            sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            status=DocumentStatus.PARSED,
        )
        extraction = DocumentExtraction(
            id=extraction_id,
            document_id=doc_id,
            version_no=1,
            schema_name="invoice",
            schema_version="1.0.0",
            extracted_payload={"fields": []},
            confidence_summary={
                "overall_confidence": 0.60,
                "field_count": 3,
                "low_confidence_fields": 2,
                "missing_fields": 0,
            },
            needs_review=True,
        )

        low_confidence = ExtractedField(
            id=uuid4(),
            document_extraction_id=extraction_id,
            field_name="vendor_address",
            field_value="123 Main St",
            field_type="string",
            confidence=0.45,
            evidence_ref={},
            is_human_corrected=False,
        )
        high_confidence = ExtractedField(
            id=uuid4(),
            document_extraction_id=extraction_id,
            field_name="invoice_number",
            field_value="INV-001",
            field_type="string",
            confidence=0.92,
            evidence_ref={},
            is_human_corrected=False,
        )
        another_low = ExtractedField(
            id=uuid4(),
            document_extraction_id=extraction_id,
            field_name="due_date",
            field_value="2024-12-31",
            field_type="date",
            confidence=0.55,
            evidence_ref={},
            is_human_corrected=False,
        )

        db_session.add_all([doc, extraction, low_confidence, high_confidence, another_low])
        db_session.commit()

        service = ExtractionService(db_session)
        low_fields = service.get_fields_below_threshold(extraction_id, threshold=0.7)

        assert len(low_fields) == 2
        field_names = {f.field_name for f in low_fields}
        assert "vendor_address" in field_names
        assert "invoice_number" not in field_names
        assert "due_date" in field_names


@pytest.mark.integration
class TestExtractionModelsPersistence:
    """Tests for extraction database model persistence."""

    def test_document_extraction_insert(
        self,
        db_session,
    ):
        """DocumentExtraction model persists correctly."""
        doc_id = uuid4()
        extraction_id = uuid4()

        extraction = DocumentExtraction(
            id=extraction_id,
            document_id=doc_id,
            version_no=1,
            schema_name="invoice",
            schema_version="1.0.0",
            extracted_payload={
                "vendor_name": "Test Vendor",
                "total": "50000.00",
            },
            confidence_summary={
                "overall_confidence": 0.88,
                "field_count": 12,
                "low_confidence_fields": 1,
                "missing_fields": 0,
            },
            needs_review=False,
        )
        db_session.add(extraction)
        db_session.commit()

        retrieved = (
            db_session.query(DocumentExtraction)
            .filter(DocumentExtraction.id == extraction_id)
            .first()
        )
        assert retrieved is not None
        assert retrieved.schema_name == "invoice"
        assert retrieved.confidence_summary["overall_confidence"] == 0.88

    def test_extracted_field_insert(
        self,
        db_session,
    ):
        """ExtractedField model persists correctly."""
        extraction_id = uuid4()
        field_id = uuid4()

        field = ExtractedField(
            id=field_id,
            document_extraction_id=extraction_id,
            field_name="total",
            field_value="50000.00",
            field_type="decimal",
            confidence=0.85,
            evidence_ref={"page": 1, "cell": "E10", "snippet": "Total: 50000.00"},
            is_human_corrected=False,
        )
        db_session.add(field)
        db_session.commit()

        retrieved = db_session.query(ExtractedField).filter(ExtractedField.id == field_id).first()
        assert retrieved is not None
        assert retrieved.field_name == "total"
        assert retrieved.confidence == Decimal("0.8500")

    def test_document_line_item_insert(
        self,
        db_session,
    ):
        """DocumentLineItem model persists correctly."""
        extraction_id = uuid4()
        line_id = uuid4()

        line_item = DocumentLineItem(
            id=line_id,
            document_extraction_id=extraction_id,
            line_no=1,
            description="Consulting services",
            quantity=10.0,
            unit_price=5000.0,
            amount=50000.0,
            tax_amount=5000.0,
            dimensions={"department": "professional_services"},
            evidence_ref={"page": 2, "row": 5},
        )
        db_session.add(line_item)
        db_session.commit()

        retrieved = (
            db_session.query(DocumentLineItem).filter(DocumentLineItem.id == line_id).first()
        )
        assert retrieved is not None
        assert retrieved.description == "Consulting services"
        assert retrieved.quantity == 10.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
