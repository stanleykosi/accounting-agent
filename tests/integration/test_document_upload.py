"""
Purpose: Verify the Step 19 document upload workflow without live MinIO or Celery.
Scope: MIME sniffing, checksum/storage metadata, document persistence, parse-task dispatch,
and list responses for close-run-attached source files.
Dependencies: Document upload service, document repository record contracts, and storage contracts.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from services.common.enums import (
    AutonomyMode,
    DocumentSourceChannel,
    DocumentStatus,
    DocumentType,
)
from services.common.types import JsonObject
from services.contracts.storage_models import (
    CloseRunStorageScope,
    ObjectStorageReference,
    SourceDocumentStorageMetadata,
    StorageBucketKind,
)
from services.db.models.audit import AuditSourceSurface
from services.db.models.entity import EntityStatus
from services.db.repositories.document_repo import (
    DocumentCloseRunAccessRecord,
    DocumentCloseRunRecord,
    DocumentEntityRecord,
    DocumentRecord,
)
from services.db.repositories.entity_repo import EntityUserRecord
from services.documents.upload_service import (
    DocumentUploadService,
    DocumentUploadServiceError,
    DocumentUploadServiceErrorCode,
    UploadDispatchReceipt,
    UploadFilePayload,
)
from services.jobs.task_names import TaskName
from services.storage.checksums import compute_sha256_bytes


def test_document_upload_persists_records_stores_originals_and_dispatches_parse_jobs() -> None:
    """Ensure a mixed PDF/CSV batch becomes stored document rows with parser jobs queued."""

    repository = InMemoryDocumentRepository()
    storage = InMemoryStorageRepository()
    dispatcher = InMemoryTaskDispatcher()
    service = DocumentUploadService(
        repository=repository,
        storage_repository=storage,
        task_dispatcher=dispatcher,
    )

    response = service.upload_documents(
        actor_user=repository.actor,
        entity_id=repository.access_record.entity.id,
        close_run_id=repository.access_record.close_run.id,
        files=(
            UploadFilePayload(
                filename="Vendor Invoice.pdf",
                payload=b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog /Font <<>> >>\n%%EOF",
                declared_content_type="application/octet-stream",
            ),
            UploadFilePayload(
                filename="bank-statement.txt",
                payload=b"date,description,amount\n2026-03-01,Opening balance,1000\n",
                declared_content_type="text/plain",
            ),
        ),
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="req-doc-upload",
    )

    assert len(response.uploaded_documents) == 2
    uploaded_pdf = response.uploaded_documents[0].document
    uploaded_csv = response.uploaded_documents[1].document

    assert uploaded_pdf.mime_type == "application/pdf"
    assert uploaded_pdf.status is DocumentStatus.UPLOADED
    assert uploaded_csv.mime_type == "text/csv"
    assert uploaded_csv.original_filename == "bank-statement.txt"
    assert uploaded_csv.sha256_hash == compute_sha256_bytes(
        b"date,description,amount\n2026-03-01,Opening balance,1000\n"
    )
    assert all(
        result.dispatch.task_name == TaskName.DOCUMENT_PARSE_AND_EXTRACT.value
        for result in response.uploaded_documents
    )
    assert len(storage.objects) == 2
    assert len(dispatcher.dispatched_kwargs) == 2
    assert repository.committed is True
    assert repository.activity_events[0]["event_type"] == "document.uploaded"

    listed = service.list_documents(
        actor_user=repository.actor,
        entity_id=repository.access_record.entity.id,
        close_run_id=repository.access_record.close_run.id,
    )
    assert tuple(document.id for document in listed.documents) == (
        uploaded_pdf.id,
        uploaded_csv.id,
    )


def test_document_upload_rejects_unsupported_content_without_partial_commit() -> None:
    """Ensure unsupported files fail fast and do not commit document rows."""

    repository = InMemoryDocumentRepository()
    service = DocumentUploadService(
        repository=repository,
        storage_repository=InMemoryStorageRepository(),
        task_dispatcher=InMemoryTaskDispatcher(),
    )

    with pytest.raises(DocumentUploadServiceError) as error:
        service.upload_documents(
            actor_user=repository.actor,
            entity_id=repository.access_record.entity.id,
            close_run_id=repository.access_record.close_run.id,
            files=(
                UploadFilePayload(
                    filename="notes.docx",
                    payload=b"not a supported accounting document",
                    declared_content_type="application/msword",
                ),
            ),
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=None,
        )

    assert error.value.status_code == 415
    assert error.value.code is DocumentUploadServiceErrorCode.UNSUPPORTED_CONTENT
    assert repository.documents == {}
    assert repository.rolled_back is True


class InMemoryDocumentRepository:
    """Provide a deterministic repository double for document upload integration tests."""

    def __init__(self) -> None:
        """Seed one accessible active entity and close run."""

        self.actor = EntityUserRecord(
            id=UUID("10000000-0000-0000-0000-000000000001"),
            email="finance@example.com",
            full_name="Finance Lead",
        )
        self.access_record = DocumentCloseRunAccessRecord(
            close_run=DocumentCloseRunRecord(
                id=UUID("20000000-0000-0000-0000-000000000001"),
                entity_id=UUID("30000000-0000-0000-0000-000000000001"),
                period_start=date(2026, 3, 1),
                period_end=date(2026, 3, 31),
                current_version_no=1,
            ),
            entity=DocumentEntityRecord(
                id=UUID("30000000-0000-0000-0000-000000000001"),
                autonomy_mode=AutonomyMode.HUMAN_REVIEW,
                status=EntityStatus.ACTIVE,
            ),
        )
        self.documents: dict[UUID, DocumentRecord] = {}
        self.activity_events: list[dict[str, object]] = []
        self.committed = False
        self.rolled_back = False

    def get_close_run_for_user(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        user_id: UUID,
    ) -> DocumentCloseRunAccessRecord | None:
        """Return seeded access only for the expected actor, entity, and close run."""

        if (
            entity_id == self.access_record.entity.id
            and close_run_id == self.access_record.close_run.id
            and user_id == self.actor.id
        ):
            return self.access_record
        return None

    def create_uploaded_document(
        self,
        *,
        document_id: UUID,
        close_run_id: UUID,
        storage_key: str,
        original_filename: str,
        mime_type: str,
        file_size_bytes: int,
        sha256_hash: str,
        ocr_required: bool,
        actor_user_id: UUID,
    ) -> DocumentRecord:
        """Persist one in-memory uploaded document row."""

        now = datetime.now(tz=UTC)
        document = DocumentRecord(
            id=document_id,
            close_run_id=close_run_id,
            parent_document_id=None,
            document_type=DocumentType.UNKNOWN,
            source_channel=DocumentSourceChannel.UPLOAD,
            storage_key=storage_key,
            original_filename=original_filename,
            mime_type=mime_type,
            file_size_bytes=file_size_bytes,
            sha256_hash=sha256_hash,
            period_start=None,
            period_end=None,
            classification_confidence=None,
            ocr_required=ocr_required,
            status=DocumentStatus.UPLOADED,
            owner_user_id=None,
            last_touched_by_user_id=actor_user_id,
            created_at=now,
            updated_at=now,
        )
        self.documents[document.id] = document
        return document

    def list_documents_for_close_run(self, *, close_run_id: UUID) -> tuple[DocumentRecord, ...]:
        """Return in-memory documents matching the close run."""

        return tuple(
            document
            for document in self.documents.values()
            if document.close_run_id == close_run_id
        )

    def create_activity_event(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        actor_user_id: UUID | None,
        event_type: str,
        source_surface: AuditSourceSurface,
        payload: JsonObject,
        trace_id: str | None,
    ) -> None:
        """Record an in-memory upload activity event."""

        self.activity_events.append(
            {
                "entity_id": entity_id,
                "close_run_id": close_run_id,
                "actor_user_id": actor_user_id,
                "event_type": event_type,
                "source_surface": source_surface,
                "payload": payload,
                "trace_id": trace_id,
            }
        )

    def commit(self) -> None:
        """Mark the in-memory unit of work as committed."""

        self.committed = True

    def rollback(self) -> None:
        """Mark the in-memory unit of work as rolled back."""

        self.rolled_back = True

    @staticmethod
    def is_integrity_error(error: Exception) -> bool:
        """The in-memory repository never emits database integrity errors."""

        return False


class InMemoryStorageRepository:
    """Provide deterministic source-object storage metadata for upload tests."""

    def __init__(self) -> None:
        """Initialize the in-memory source-object store."""

        self.objects: dict[str, bytes] = {}

    def store_source_document(
        self,
        *,
        scope: CloseRunStorageScope,
        document_id: UUID,
        original_filename: str,
        payload: bytes,
        content_type: str,
        expected_sha256: str | None = None,
    ) -> SourceDocumentStorageMetadata:
        """Store payload bytes under a deterministic object key."""

        del scope
        checksum = compute_sha256_bytes(payload)
        assert expected_sha256 in {None, checksum}
        object_key = f"documents/source/{document_id}/{original_filename}"
        self.objects[object_key] = payload
        return SourceDocumentStorageMetadata(
            reference=ObjectStorageReference(
                bucket_kind=StorageBucketKind.DOCUMENTS,
                bucket_name="documents-bucket",
                object_key=object_key,
            ),
            content_type=content_type,
            size_bytes=len(payload),
            sha256_checksum=checksum,
            etag=checksum[:32],
            version_id=None,
            document_id=document_id,
            original_filename=original_filename,
        )


class InMemoryTaskDispatcher:
    """Capture parser task dispatches without requiring Celery."""

    def __init__(self) -> None:
        """Initialize the dispatch capture list."""

        self.dispatched_kwargs: list[dict[str, object]] = []

    def dispatch_task(
        self,
        *,
        task_name: TaskName | str,
        args: tuple[object, ...] | None = None,
        kwargs: dict[str, object] | None = None,
        countdown: int | None = None,
        task_id: str | None = None,
    ) -> UploadDispatchReceipt:
        """Capture the task payload and return a deterministic receipt."""

        del args, countdown
        self.dispatched_kwargs.append(dict(kwargs or {}))
        return UploadDispatchReceipt(
            task_id=task_id or str(uuid4()),
            task_name=str(task_name),
            queue_name="documents",
            routing_key="documents.parse_and_extract",
            trace_id="trace-upload",
        )
