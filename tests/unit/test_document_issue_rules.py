"""
Unit tests for document issue rules and quality checks.
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from services.documents.completeness import CompletenessCheckResult, CompletenessCheckService
from services.documents.duplicate_detection import (
    DuplicateDetectionResult,
    DuplicateDetectionService,
)
from services.documents.issues import DocumentIssueRecord, DocumentIssueService
from services.documents.period_validation import PeriodValidationResult, PeriodValidationService
from services.common.enums import (
    DocumentIssueSeverity,
    DocumentIssueStatus,
    DocumentSourceChannel,
    DocumentStatus,
    DocumentType,
)


class MockDocumentRepository:
    """Mock document repository for testing."""

    def __init__(self):
        self.documents = {}
        self.documents_with_extractions = ()

    def get_close_run_for_user(self, *, entity_id, close_run_id, user_id):
        # Mock implementation
        from services.db.repositories.document_repo import (
            DocumentCloseRunAccessRecord,
            DocumentCloseRunRecord,
            DocumentEntityRecord,
        )
        from services.common.enums import AutonomyMode, EntityStatus

        return DocumentCloseRunAccessRecord(
            close_run=DocumentCloseRunRecord(
                id=close_run_id,
                entity_id=entity_id,
                period_start=date(2024, 1, 1),
                period_end=date(2024, 1, 31),
                current_version_no=1,
            ),
            entity=DocumentEntityRecord(
                id=entity_id,
                autonomy_mode=AutonomyMode.HUMAN_REVIEW,
                status=EntityStatus.ACTIVE,
            ),
        )

    def list_documents_for_close_run(self, *, close_run_id):
        return tuple(record.document for record in self.documents_with_extractions)

    def list_documents_for_close_run_with_latest_extraction(self, *, close_run_id):
        return self.documents_with_extractions

    def commit(self):
        pass

    def rollback(self):
        pass

    def is_integrity_error(self, error):
        return False


class MockStorageRepository:
    """Mock storage repository for testing."""

    def store_source_document(
        self, *, scope, document_id, original_filename, payload, content_type, expected_sha256
    ):
        # Mock implementation
        from services.storage.repository import (
            SourceStorageMetadataProtocol,
            SourceStorageReferenceProtocol,
        )

        class MockReference:
            @property
            def object_key(self):
                return f"test-bucket/{document_id}/{original_filename}"

        class MockMetadata:
            @property
            def reference(self):
                return MockReference()

        return MockMetadata()


def _build_document_with_extraction(
    *,
    close_run_id,
    document_id,
    document_type: DocumentType,
    original_filename: str,
    sha256_hash: str,
    created_at: datetime,
    status: DocumentStatus,
    field_values: dict[str, object] | None = None,
):
    from services.db.repositories.document_repo import (
        DocumentExtractionRecord,
        DocumentRecord,
        DocumentWithExtractionRecord,
        ExtractedFieldRecord,
    )

    extraction = None
    if field_values is not None:
        extraction = DocumentExtractionRecord(
            id=document_id,
            document_id=document_id,
            version_no=1,
            schema_name=document_type.value,
            schema_version="1.0.0",
            extracted_payload={"fields": []},
            confidence_summary={},
            needs_review=False,
            approved_version=status is DocumentStatus.APPROVED,
            created_at=created_at,
            updated_at=created_at,
            fields=tuple(
                ExtractedFieldRecord(
                    id=uuid4(),
                    document_extraction_id=document_id,
                    field_name=field_name,
                    field_value=field_value,
                    field_type="string",
                    confidence=0.99,
                    evidence_ref={},
                    is_human_corrected=False,
                    created_at=created_at,
                    updated_at=created_at,
                )
                for field_name, field_value in field_values.items()
            ),
        )

    return DocumentWithExtractionRecord(
        document=DocumentRecord(
            id=document_id,
            close_run_id=close_run_id,
            parent_document_id=None,
            document_type=document_type,
            source_channel=DocumentSourceChannel.UPLOAD,
            storage_key=f"documents/source/{original_filename}",
            original_filename=original_filename,
            mime_type="application/pdf",
            file_size_bytes=1024,
            sha256_hash=sha256_hash,
            period_start=None,
            period_end=None,
            classification_confidence=None,
            ocr_required=False,
            status=status,
            owner_user_id=None,
            last_touched_by_user_id=None,
            created_at=created_at,
            updated_at=created_at,
        ),
        latest_extraction=extraction,
    )


def test_duplicate_detection_service():
    """Test duplicate detection service."""
    doc_repo = MockDocumentRepository()
    storage_repo = MockStorageRepository()

    service = DuplicateDetectionService(
        document_repo=doc_repo,
        storage_repo=storage_repo,
    )

    # Test with a hash that should not be duplicate
    result = service.check_duplicate(
        document_hash="abc123",
        close_run_id=str(uuid4()),
        entity_id=str(uuid4()),
    )

    assert isinstance(result, DuplicateDetectionResult)
    assert result.is_duplicate == False
    assert result.existing_document_id is None
    assert result.similarity_score == 0.0
    assert result.detection_method == "sha256_exact"
    print("✓ Duplicate detection service test passed")


def test_duplicate_detection_service_flags_existing_exact_match() -> None:
    """The duplicate service should flag earlier exact matches in the same close run."""

    close_run_id = uuid4()
    duplicate_row = _build_document_with_extraction(
        close_run_id=close_run_id,
        document_id=uuid4(),
        document_type=DocumentType.UNKNOWN,
        original_filename="existing.pdf",
        sha256_hash="abc123",
        created_at=datetime(2024, 1, 1),
        status=DocumentStatus.APPROVED,
    )
    doc_repo = MockDocumentRepository()
    doc_repo.documents_with_extractions = (duplicate_row,)
    service = DuplicateDetectionService(
        document_repo=doc_repo,
        storage_repo=MockStorageRepository(),
    )

    result = service.check_duplicate(
        document_hash="abc123",
        close_run_id=str(close_run_id),
        entity_id=str(uuid4()),
        current_document_id=str(uuid4()),
    )

    assert result.is_duplicate is True
    assert result.existing_document_id == str(duplicate_row.document.id)
    assert result.existing_document_filename == duplicate_row.document.original_filename
    assert result.similarity_score == 1.0
    assert result.matched_fields == ("sha256_hash",)


def test_duplicate_detection_service_flags_semantic_invoice_match() -> None:
    """Parsed invoices with different bytes but the same accounting facts should be flagged."""

    close_run_id = uuid4()
    existing_row = _build_document_with_extraction(
        close_run_id=close_run_id,
        document_id=uuid4(),
        document_type=DocumentType.INVOICE,
        original_filename="invoice-existing.pdf",
        sha256_hash="existing-hash",
        created_at=datetime(2024, 1, 1),
        status=DocumentStatus.APPROVED,
        field_values={
            "invoice_number": "INV-1001",
            "invoice_date": date(2024, 1, 15),
            "total": Decimal("1250.00"),
            "currency": "USD",
            "vendor_name": "Acme Supplies Ltd",
        },
    )
    current_row = _build_document_with_extraction(
        close_run_id=close_run_id,
        document_id=uuid4(),
        document_type=DocumentType.INVOICE,
        original_filename="invoice-scan.pdf",
        sha256_hash="current-hash",
        created_at=datetime(2024, 1, 2),
        status=DocumentStatus.NEEDS_REVIEW,
        field_values={
            "invoice_number": "INV-1001",
            "invoice_date": date(2024, 1, 15),
            "total": Decimal("1250.00"),
            "currency": "USD",
            "vendor_name": "ACME SUPPLIES LIMITED",
        },
    )
    doc_repo = MockDocumentRepository()
    doc_repo.documents_with_extractions = (existing_row, current_row)
    service = DuplicateDetectionService(
        document_repo=doc_repo,
        storage_repo=MockStorageRepository(),
    )

    result = service.check_duplicate(
        document_hash=current_row.document.sha256_hash,
        close_run_id=str(close_run_id),
        entity_id=str(uuid4()),
        current_document_id=str(current_row.document.id),
    )

    assert result.is_duplicate is True
    assert result.existing_document_id == str(existing_row.document.id)
    assert result.existing_document_filename == existing_row.document.original_filename
    assert result.detection_method == "semantic_invoice_reference"
    assert result.similarity_score >= 0.98
    assert "invoice_number" in result.matched_fields
    assert "total" in result.matched_fields
    assert "invoice_date" in result.matched_fields


def test_duplicate_detection_service_prefers_original_over_existing_duplicate_match() -> None:
    """Semantic duplicates should point at the canonical source document first."""

    close_run_id = uuid4()
    original_row = _build_document_with_extraction(
        close_run_id=close_run_id,
        document_id=uuid4(),
        document_type=DocumentType.INVOICE,
        original_filename="invoice-original.pdf",
        sha256_hash="original-hash",
        created_at=datetime(2024, 1, 1),
        status=DocumentStatus.APPROVED,
        field_values={
            "invoice_number": "INV-1001",
            "invoice_date": date(2024, 1, 15),
            "total": Decimal("1250.00"),
            "currency": "USD",
            "vendor_name": "Acme Supplies Ltd",
        },
    )
    duplicate_row = _build_document_with_extraction(
        close_run_id=close_run_id,
        document_id=uuid4(),
        document_type=DocumentType.INVOICE,
        original_filename="invoice-duplicate.pdf",
        sha256_hash="duplicate-hash",
        created_at=datetime(2024, 1, 2),
        status=DocumentStatus.DUPLICATE,
        field_values={
            "invoice_number": "INV-1001",
            "invoice_date": date(2024, 1, 15),
            "total": Decimal("1250.00"),
            "currency": "USD",
            "vendor_name": "Acme Supplies Ltd",
        },
    )
    current_row = _build_document_with_extraction(
        close_run_id=close_run_id,
        document_id=uuid4(),
        document_type=DocumentType.INVOICE,
        original_filename="invoice-current.pdf",
        sha256_hash="current-hash",
        created_at=datetime(2024, 1, 3),
        status=DocumentStatus.NEEDS_REVIEW,
        field_values={
            "invoice_number": "INV-1001",
            "invoice_date": date(2024, 1, 15),
            "total": Decimal("1250.00"),
            "currency": "USD",
            "vendor_name": "Acme Supplies Ltd",
        },
    )
    doc_repo = MockDocumentRepository()
    doc_repo.documents_with_extractions = (original_row, duplicate_row, current_row)
    service = DuplicateDetectionService(
        document_repo=doc_repo,
        storage_repo=MockStorageRepository(),
    )

    result = service.check_duplicate(
        document_hash=current_row.document.sha256_hash,
        close_run_id=str(close_run_id),
        entity_id=str(uuid4()),
        current_document_id=str(current_row.document.id),
    )

    assert result.is_duplicate is True
    assert result.existing_document_id == str(original_row.document.id)
    assert result.existing_document_filename == original_row.document.original_filename


def test_duplicate_detection_service_requires_strong_semantic_evidence() -> None:
    """Documents should not be flagged as duplicates when only weak overlapping facts exist."""

    close_run_id = uuid4()
    existing_row = _build_document_with_extraction(
        close_run_id=close_run_id,
        document_id=uuid4(),
        document_type=DocumentType.INVOICE,
        original_filename="invoice-existing.pdf",
        sha256_hash="existing-hash",
        created_at=datetime(2024, 1, 1),
        status=DocumentStatus.APPROVED,
        field_values={
            "vendor_name": "Acme Supplies Ltd",
            "currency": "USD",
        },
    )
    current_row = _build_document_with_extraction(
        close_run_id=close_run_id,
        document_id=uuid4(),
        document_type=DocumentType.INVOICE,
        original_filename="invoice-incomplete.pdf",
        sha256_hash="current-hash",
        created_at=datetime(2024, 1, 2),
        status=DocumentStatus.NEEDS_REVIEW,
        field_values={
            "vendor_name": "Acme Supplies Ltd",
            "currency": "USD",
        },
    )
    doc_repo = MockDocumentRepository()
    doc_repo.documents_with_extractions = (existing_row, current_row)
    service = DuplicateDetectionService(
        document_repo=doc_repo,
        storage_repo=MockStorageRepository(),
    )

    result = service.check_duplicate(
        document_hash=current_row.document.sha256_hash,
        close_run_id=str(close_run_id),
        entity_id=str(uuid4()),
        current_document_id=str(current_row.document.id),
    )

    assert result.is_duplicate is False


def test_period_validation_service():
    """Test period validation service."""
    doc_repo = MockDocumentRepository()
    service = PeriodValidationService(document_repo=doc_repo)

    # Test valid period overlap
    result = service.validate_period(
        document_period_start=date(2024, 1, 15),
        document_period_end=date(2024, 1, 20),
        close_run_period_start=date(2024, 1, 1),
        close_run_period_end=date(2024, 1, 31),
    )

    assert isinstance(result, PeriodValidationResult)
    assert result.is_valid == True
    assert result.document_period_start == date(2024, 1, 15)
    assert result.document_period_end == date(2024, 1, 20)
    assert result.close_run_period_start == date(2024, 1, 1)
    assert result.close_run_period_end == date(2024, 1, 31)
    print("✓ Period validation service test passed (valid overlap)")

    # Test invalid period (no overlap)
    result = service.validate_period(
        document_period_start=date(2024, 2, 1),
        document_period_end=date(2024, 2, 28),
        close_run_period_start=date(2024, 1, 1),
        close_run_period_end=date(2024, 1, 31),
    )

    assert result.is_valid == False
    print("✓ Period validation service test passed (no overlap)")

    # Test undetectable period
    result = service.validate_period(
        document_period_start=None,
        document_period_end=None,
        close_run_period_start=date(2024, 1, 1),
        close_run_period_end=date(2024, 1, 31),
    )

    assert result.is_valid == False
    print("✓ Period validation service test passed (undetectable period)")


def test_completeness_check_service():
    """Test completeness check service."""
    doc_repo = MockDocumentRepository()
    service = CompletenessCheckService(document_repo=doc_repo)

    # Test with no required types specified (should use defaults)
    result = service.check_completeness(close_run_id=str(uuid4()))

    assert isinstance(result, CompletenessCheckResult)
    # Should be incomplete since we have no documents in mock repo
    assert result.is_complete == False
    assert len(result.missing_document_types) > 0
    assert DocumentType.INVOICE in result.required_document_types
    assert DocumentType.BANK_STATEMENT in result.required_document_types
    assert DocumentType.RECEIPT in result.required_document_types
    print("✓ Completeness check service test passed (default types)")

    # Test with custom required types
    custom_types = {DocumentType.CONTRACT}
    result = service.check_completeness(
        close_run_id=str(uuid4()),
        required_document_types=custom_types,
    )

    assert result.required_document_types == custom_types
    assert result.is_complete == False  # Still no documents
    print("✓ Completeness check service test passed (custom types)")


def test_document_issue_service():
    """Test document issue service."""
    from unittest.mock import Mock
    from sqlalchemy.orm import Session

    # Mock dependencies
    db_session = Mock(spec=Session)
    doc_repo = Mock(spec=MockDocumentRepository)

    service = DocumentIssueService(
        db_session=db_session,
        document_repo=doc_repo,
    )

    # Test creating an issue
    document_id = uuid4()
    actor_user_id = uuid4()

    # Mock the audit service emit method
    service._audit_service.emit_audit_event = Mock()

    # Mock database operations
    db_session.add = Mock()
    db_session.flush = Mock()
    db_session.commit = Mock()

    # Mock helper methods
    service._get_entity_id_from_document = Mock(return_value=uuid4())
    service._get_close_run_id_from_document = Mock(return_value=uuid4())

    # Create issue
    issue_record = service.create_issue(
        document_id=document_id,
        issue_type="duplicate_document",
        severity=DocumentIssueSeverity.BLOCKING,
        details={"test": "detail"},
        actor_user_id=actor_user_id,
        source_surface="worker",  # This should be AuditSourceSurface.WORKER but we're mocking
    )

    assert isinstance(issue_record, DocumentIssueRecord)
    assert issue_record.document_id == document_id
    assert issue_record.issue_type == "duplicate_document"
    assert issue_record.severity == DocumentIssueSeverity.BLOCKING
    assert issue_record.status == DocumentIssueStatus.OPEN
    assert issue_record.details == {"test": "detail"}

    # Verify audit event was called
    service._audit_service.emit_audit_event.assert_called_once()
    print("✓ Document issue service test passed")


def run_all_tests():
    """Run all tests."""
    test_duplicate_detection_service()
    test_period_validation_service()
    test_completeness_check_service()
    test_document_issue_service()
    print("\n✅ All tests passed!")


if __name__ == "__main__":
    run_all_tests()
