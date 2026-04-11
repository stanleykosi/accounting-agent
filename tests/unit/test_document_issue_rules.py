"""
Unit tests for document issue rules and quality checks.
"""

from datetime import date, datetime
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
        # Return empty list for testing
        return ()

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
