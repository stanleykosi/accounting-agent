"""
Purpose: Verify focused document-review service state transitions.
Scope: Regression coverage for extraction approval and review-state updates.
Dependencies: DocumentReviewService plus strict document contracts.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from services.common.enums import AutonomyMode, DocumentSourceChannel, DocumentStatus, DocumentType
from services.contracts.document_models import DocumentSummary, ExtractedFieldSummary
from services.db.models.audit import AuditSourceSurface
from services.db.models.entity import EntityStatus
from services.db.repositories.entity_repo import EntityUserRecord

sys.modules.pop("services.documents.review_service", None)

from services.documents.review_service import DocumentReviewService  # noqa: E402


def test_review_document_reject_clears_latest_extraction_approval() -> None:
    """Rejecting a document should clear any stale approved extraction flag."""

    entity_id = uuid4()
    close_run_id = uuid4()
    document_id = uuid4()
    actor_user = EntityUserRecord(
        id=uuid4(),
        email="reviewer@example.com",
        full_name="Casey Reviewer",
    )
    document = SimpleNamespace(
        id=document_id,
        status=DocumentStatus.APPROVED.value,
        original_filename="invoice.pdf",
        last_touched_by_user_id=None,
    )
    latest_extraction = SimpleNamespace(approved_version=True)
    commit_markers: list[str] = []

    service = DocumentReviewService.__new__(DocumentReviewService)
    service._db_session = SimpleNamespace(commit=lambda: commit_markers.append("commit"))
    service._repository = SimpleNamespace(create_activity_event=lambda **kwargs: None)
    service._audit_service = SimpleNamespace(record_review_action=lambda **kwargs: None)
    service._extraction_service = SimpleNamespace(
        get_latest_extraction=lambda **kwargs: latest_extraction
    )
    service._require_document_access = lambda **kwargs: SimpleNamespace(
        entity=SimpleNamespace(
            id=entity_id,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW,
            status=EntityStatus.ACTIVE,
        ),
        close_run=SimpleNamespace(id=close_run_id),
        document=SimpleNamespace(id=document_id),
    )
    service._load_document = lambda **kwargs: document
    service._record_verification_findings = lambda **kwargs: None
    service._build_document_summary = lambda _document_id: _build_document_summary(
        document_id=document_id,
        close_run_id=close_run_id,
        status=DocumentStatus.REJECTED,
    )

    result = service.review_document(
        actor_user=actor_user,
        entity_id=entity_id,
        close_run_id=close_run_id,
        document_id=document_id,
        decision="rejected",
        reason="Duplicate source",
        verified_complete=None,
        verified_authorized=None,
        verified_period=None,
        verified_transaction_match=None,
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="trace-review-1",
    )

    assert latest_extraction.approved_version is False
    assert document.status == DocumentStatus.REJECTED.value
    assert result.decision == "rejected"
    assert commit_markers == ["commit"]


def test_review_document_approve_requires_all_verification_controls() -> None:
    """Approvals should fail fast unless the PDF verification checklist is fully confirmed."""

    entity_id = uuid4()
    close_run_id = uuid4()
    document_id = uuid4()
    actor_user = EntityUserRecord(
        id=uuid4(),
        email="reviewer@example.com",
        full_name="Casey Reviewer",
    )
    document = SimpleNamespace(
        id=document_id,
        status=DocumentStatus.NEEDS_REVIEW.value,
        original_filename="invoice.pdf",
        last_touched_by_user_id=None,
    )

    service = DocumentReviewService.__new__(DocumentReviewService)
    service._db_session = SimpleNamespace(commit=lambda: None)
    service._repository = SimpleNamespace(create_activity_event=lambda **kwargs: None)
    service._audit_service = SimpleNamespace(record_review_action=lambda **kwargs: None)
    service._extraction_service = SimpleNamespace(get_latest_extraction=lambda **kwargs: None)
    service._require_document_access = lambda **kwargs: SimpleNamespace(
        entity=SimpleNamespace(
            id=entity_id,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW,
            status=EntityStatus.ACTIVE,
        ),
        close_run=SimpleNamespace(id=close_run_id),
        document=SimpleNamespace(id=document_id),
    )
    service._load_document = lambda **kwargs: document
    service._record_verification_findings = lambda **kwargs: None
    service._build_document_summary = lambda _document_id: _build_document_summary(
        document_id=document_id,
        close_run_id=close_run_id,
        status=DocumentStatus.APPROVED,
    )

    try:
        service.review_document(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
            document_id=document_id,
            decision="approved",
            reason=None,
            verified_complete=True,
            verified_authorized=True,
            verified_period=False,
            verified_transaction_match=True,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id="trace-review-2",
        )
    except Exception as error:
        assert "requires reviewer confirmation" in str(error)
    else:
        raise AssertionError("Approval should fail when any verification control is not confirmed.")


def test_review_document_approve_clears_latest_extraction_review_flag() -> None:
    """Approving a reviewed document should clear the extraction's pending-review state."""

    entity_id = uuid4()
    close_run_id = uuid4()
    document_id = uuid4()
    actor_user = EntityUserRecord(
        id=uuid4(),
        email="reviewer@example.com",
        full_name="Casey Reviewer",
    )
    document = SimpleNamespace(
        id=document_id,
        status=DocumentStatus.NEEDS_REVIEW.value,
        original_filename="invoice.pdf",
        last_touched_by_user_id=None,
    )
    latest_extraction = SimpleNamespace(approved_version=False, needs_review=True)

    service = DocumentReviewService.__new__(DocumentReviewService)
    service._db_session = SimpleNamespace(commit=lambda: None)
    service._repository = SimpleNamespace(create_activity_event=lambda **kwargs: None)
    service._audit_service = SimpleNamespace(record_review_action=lambda **kwargs: None)
    service._extraction_service = SimpleNamespace(
        get_latest_extraction=lambda **kwargs: latest_extraction
    )
    service._require_document_access = lambda **kwargs: SimpleNamespace(
        entity=SimpleNamespace(
            id=entity_id,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW,
            status=EntityStatus.ACTIVE,
        ),
        close_run=SimpleNamespace(id=close_run_id),
        document=SimpleNamespace(id=document_id),
    )
    service._load_document = lambda **kwargs: document
    service._resolve_open_issues = lambda **kwargs: None
    service._build_document_summary = lambda _document_id: _build_document_summary(
        document_id=document_id,
        close_run_id=close_run_id,
        status=DocumentStatus.APPROVED,
    )

    result = service.review_document(
        actor_user=actor_user,
        entity_id=entity_id,
        close_run_id=close_run_id,
        document_id=document_id,
        decision="approved",
        reason="All controls verified",
        verified_complete=True,
        verified_authorized=True,
        verified_period=True,
        verified_transaction_match=True,
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="trace-review-3",
    )

    assert latest_extraction.approved_version is True
    assert latest_extraction.needs_review is False
    assert document.status == DocumentStatus.APPROVED.value
    assert result.decision == "approved"


def test_correct_extracted_field_recomputes_derived_review_state() -> None:
    """Field corrections should return the document to review and refresh derived state."""

    class _DbSessionDouble:
        def __init__(self, *, field, extraction) -> None:
            self._objects = {
                field.id: field,
                extraction.id: extraction,
            }
            self.commit_markers: list[str] = []

        def get(self, _model, key):
            return self._objects.get(key)

        def commit(self) -> None:
            self.commit_markers.append("commit")

    entity_id = uuid4()
    close_run_id = uuid4()
    extraction_id = uuid4()
    field_id = uuid4()
    actor_user = EntityUserRecord(
        id=uuid4(),
        email="reviewer@example.com",
        full_name="Casey Reviewer",
    )
    now = datetime.now(tz=UTC)
    field = SimpleNamespace(
        id=field_id,
        document_extraction_id=extraction_id,
        field_name="invoice_date",
        field_value="2026-03-31",
        field_type="date",
        confidence=0.62,
        evidence_ref={"snippet": "Invoice date 2026-03-31"},
        is_human_corrected=False,
        created_at=now,
        updated_at=now,
    )
    extraction = SimpleNamespace(
        id=extraction_id,
        document_id=uuid4(),
        approved_version=True,
        needs_review=False,
        extracted_payload={},
    )
    document = SimpleNamespace(
        id=extraction.document_id,
        status=DocumentStatus.APPROVED.value,
        original_filename="invoice.pdf",
        last_touched_by_user_id=None,
    )
    db_session = _DbSessionDouble(field=field, extraction=extraction)
    refreshed_calls: list[dict[str, object]] = []

    service = DocumentReviewService.__new__(DocumentReviewService)
    service._db_session = db_session
    service._repository = SimpleNamespace(create_activity_event=lambda **kwargs: None)
    service._audit_service = SimpleNamespace(record_review_action=lambda **kwargs: None)
    service._extraction_service = SimpleNamespace(apply_field_correction=lambda **kwargs: True)
    service._require_document_access = lambda **kwargs: SimpleNamespace(
        entity=SimpleNamespace(
            id=entity_id,
            autonomy_mode=AutonomyMode.REDUCED_INTERRUPTION,
            status=EntityStatus.ACTIVE,
        ),
        close_run=SimpleNamespace(
            id=close_run_id,
            period_start=datetime(2026, 3, 1, tzinfo=UTC).date(),
            period_end=datetime(2026, 3, 31, tzinfo=UTC).date(),
        ),
        document=SimpleNamespace(id=extraction.document_id),
    )
    service._load_document = lambda **kwargs: document
    service._refresh_document_derived_state = lambda **kwargs: refreshed_calls.append(kwargs)
    service._build_document_summary = lambda _document_id: _build_document_summary(
        document_id=document.id,
        close_run_id=close_run_id,
        status=DocumentStatus.NEEDS_REVIEW,
    )
    service._build_field_summary = lambda refreshed_field: _build_field_summary(refreshed_field)

    result = service.correct_extracted_field(
        actor_user=actor_user,
        entity_id=entity_id,
        close_run_id=close_run_id,
        field_id=field_id,
        corrected_value="2026-03-15",
        corrected_type="date",
        reason="Corrected the invoice date",
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="trace-review-4",
    )

    assert extraction.approved_version is False
    assert extraction.needs_review is True
    assert extraction.extracted_payload["auto_review"]["auto_approved"] is False
    assert document.status == DocumentStatus.NEEDS_REVIEW.value
    assert document.last_touched_by_user_id == actor_user.id
    assert len(refreshed_calls) == 1
    assert refreshed_calls[0]["document"] is document
    assert refreshed_calls[0]["actor_user_id"] == actor_user.id
    assert db_session.commit_markers == ["commit"]
    assert result.document.status == DocumentStatus.NEEDS_REVIEW


def _build_document_summary(
    *,
    document_id,
    close_run_id,
    status: DocumentStatus,
) -> DocumentSummary:
    """Build a minimal valid document summary for review-service unit tests."""

    now = datetime.now(tz=UTC)
    return DocumentSummary(
        id=str(document_id),
        close_run_id=str(close_run_id),
        parent_document_id=None,
        document_type=DocumentType.INVOICE,
        source_channel=DocumentSourceChannel.UPLOAD,
        storage_key="documents/invoice.pdf",
        original_filename="invoice.pdf",
        mime_type="application/pdf",
        file_size_bytes=1024,
        sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        period_start=None,
        period_end=None,
        classification_confidence=0.91,
        ocr_required=False,
        status=status,
        owner_user_id=None,
        last_touched_by_user_id=None,
        latest_extraction=None,
        open_issues=(),
        created_at=now,
        updated_at=now,
    )


def _build_field_summary(field) -> ExtractedFieldSummary:
    """Build a minimal valid extracted-field summary for review-service unit tests."""

    return ExtractedFieldSummary(
        id=str(field.id),
        field_name=field.field_name,
        field_value=field.field_value,
        field_type=field.field_type,
        confidence=field.confidence,
        evidence_ref=field.evidence_ref,
        is_human_corrected=field.is_human_corrected,
        created_at=field.created_at,
        updated_at=field.updated_at,
    )
