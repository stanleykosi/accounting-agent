"""
Purpose: Verify the Step 19 document upload workflow without live MinIO or Celery.
Scope: MIME sniffing, checksum/storage metadata, document persistence, parse-task dispatch,
and list responses for close-run-attached source files.
Dependencies: Document upload service, document repository record contracts, and storage contracts.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from services.common.enums import (
    AutonomyMode,
    DocumentSourceChannel,
    DocumentStatus,
    DocumentType,
    JobStatus,
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
    DocumentDeletionPlan,
    DocumentEntityRecord,
    DocumentRecord,
    DocumentWithExtractionRecord,
)
from services.db.repositories.entity_repo import EntityUserRecord
from services.documents.upload_service import (
    DocumentUploadService,
    DocumentUploadServiceError,
    DocumentUploadServiceErrorCode,
    UploadDispatchReceipt,
    UploadFilePayload,
)
from services.jobs.service import JobRecord
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
        job_service=InMemoryJobService(),
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
        job_service=InMemoryJobService(),
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


def test_document_upload_rejects_exact_duplicate_files_in_same_close_run() -> None:
    """Exact duplicate uploads should fail fast instead of creating another document row."""

    repository = InMemoryDocumentRepository()
    storage = InMemoryStorageRepository()
    service = DocumentUploadService(
        repository=repository,
        storage_repository=storage,
        job_service=InMemoryJobService(),
        task_dispatcher=InMemoryTaskDispatcher(),
    )
    duplicate_payload = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog /Font <<>> >>\n%%EOF"

    first_response = service.upload_documents(
        actor_user=repository.actor,
        entity_id=repository.access_record.entity.id,
        close_run_id=repository.access_record.close_run.id,
        files=(
            UploadFilePayload(
                filename="Vendor Invoice.pdf",
                payload=duplicate_payload,
                declared_content_type="application/pdf",
            ),
        ),
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="req-doc-upload-first",
    )

    with pytest.raises(DocumentUploadServiceError) as error:
        service.upload_documents(
            actor_user=repository.actor,
            entity_id=repository.access_record.entity.id,
            close_run_id=repository.access_record.close_run.id,
            files=(
                UploadFilePayload(
                    filename="Vendor Invoice copy.pdf",
                    payload=duplicate_payload,
                    declared_content_type="application/pdf",
                ),
            ),
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id="req-doc-upload-duplicate",
        )

    assert len(first_response.uploaded_documents) == 1
    assert error.value.status_code == 409
    assert error.value.code is DocumentUploadServiceErrorCode.DUPLICATE_UPLOAD
    assert "already attached to this close run" in error.value.message
    assert len(repository.documents) == 1
    assert len(storage.objects) == 1
    assert repository.rolled_back is True


def test_document_delete_removes_document_records_jobs_and_storage_objects() -> None:
    """Deleting one uploaded document should remove its row, cancel work, and clear storage."""

    repository = InMemoryDocumentRepository()
    storage = InMemoryStorageRepository()
    job_service = InMemoryJobService()
    service = DocumentUploadService(
        repository=repository,
        storage_repository=storage,
        job_service=job_service,
        task_dispatcher=InMemoryTaskDispatcher(),
    )

    upload_response = service.upload_documents(
        actor_user=repository.actor,
        entity_id=repository.access_record.entity.id,
        close_run_id=repository.access_record.close_run.id,
        files=(
            UploadFilePayload(
                filename="Vendor Invoice.pdf",
                payload=b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog /Font <<>> >>\n%%EOF",
                declared_content_type="application/pdf",
            ),
        ),
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="req-doc-upload",
    )
    uploaded_document = upload_response.uploaded_documents[0].document
    dispatched_job_id = next(iter(job_service.dispatched_jobs))
    repository.active_job_ids_by_document[UUID(uploaded_document.id)] = (dispatched_job_id,)
    repository.derivative_storage_keys_by_document[UUID(uploaded_document.id)] = (
        "documents/derivatives/test-version/tables.json",
    )
    storage.objects["documents/derivatives/test-version/tables.json"] = b"{}"

    delete_response = service.delete_document(
        actor_user=repository.actor,
        entity_id=repository.access_record.entity.id,
        close_run_id=repository.access_record.close_run.id,
        document_id=UUID(uploaded_document.id),
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="req-doc-delete",
    )

    assert delete_response.deleted_document_id == uploaded_document.id
    assert delete_response.deleted_document_count == 1
    assert delete_response.canceled_job_count == 1
    assert UUID(uploaded_document.id) not in repository.documents
    assert dispatched_job_id in job_service.canceled_job_ids
    assert uploaded_document.storage_key not in storage.objects
    assert "documents/derivatives/test-version/tables.json" not in storage.objects
    assert repository.activity_events[-1]["event_type"] == "document.deleted"


def test_document_delete_waits_for_running_jobs_to_stop_before_removing_document_tree() -> None:
    """Deleting a document should wait for running linked jobs to become terminal first."""

    repository = InMemoryDocumentRepository()
    storage = InMemoryStorageRepository()
    job_service = InMemoryJobService()
    service = DocumentUploadService(
        repository=repository,
        storage_repository=storage,
        job_service=job_service,
        task_dispatcher=InMemoryTaskDispatcher(),
    )

    upload_response = service.upload_documents(
        actor_user=repository.actor,
        entity_id=repository.access_record.entity.id,
        close_run_id=repository.access_record.close_run.id,
        files=(
            UploadFilePayload(
                filename="Vendor Invoice.pdf",
                payload=b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog /Font <<>> >>\n%%EOF",
                declared_content_type="application/pdf",
            ),
        ),
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="req-doc-upload",
    )
    uploaded_document = upload_response.uploaded_documents[0].document
    dispatched_job_id = next(iter(job_service.dispatched_jobs))
    job_service.dispatched_jobs[dispatched_job_id] = replace(
        job_service.dispatched_jobs[dispatched_job_id],
        status=JobStatus.RUNNING,
        started_at=datetime.now(tz=UTC),
    )
    repository.active_job_ids_by_document[UUID(uploaded_document.id)] = (dispatched_job_id,)

    delete_response = service.delete_document(
        actor_user=repository.actor,
        entity_id=repository.access_record.entity.id,
        close_run_id=repository.access_record.close_run.id,
        document_id=UUID(uploaded_document.id),
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="req-doc-delete-running",
    )

    assert delete_response.deleted_document_id == uploaded_document.id
    assert dispatched_job_id in job_service.canceled_job_ids
    assert job_service.job_poll_counts[dispatched_job_id] >= 1
    assert job_service.dispatched_jobs[dispatched_job_id].status is JobStatus.CANCELED
    assert UUID(uploaded_document.id) not in repository.documents


def test_document_delete_rejects_missing_documents() -> None:
    """Deleting a missing document should surface a structured not-found error."""

    repository = InMemoryDocumentRepository()
    service = DocumentUploadService(
        repository=repository,
        storage_repository=InMemoryStorageRepository(),
        job_service=InMemoryJobService(),
        task_dispatcher=InMemoryTaskDispatcher(),
    )

    with pytest.raises(DocumentUploadServiceError) as error:
        service.delete_document(
            actor_user=repository.actor,
            entity_id=repository.access_record.entity.id,
            close_run_id=repository.access_record.close_run.id,
            document_id=uuid4(),
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id="req-doc-delete-missing",
        )

    assert error.value.status_code == 404
    assert error.value.code is DocumentUploadServiceErrorCode.DOCUMENT_NOT_FOUND


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
        self.active_job_ids_by_document: dict[UUID, tuple[UUID, ...]] = {}
        self.derivative_storage_keys_by_document: dict[UUID, tuple[str, ...]] = {}
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

    def list_documents_for_close_run_with_latest_extraction(
        self,
        *,
        close_run_id: UUID,
    ) -> tuple[DocumentWithExtractionRecord, ...]:
        """Return in-memory documents without extraction metadata for upload tests."""

        return tuple(
            DocumentWithExtractionRecord(document=document, latest_extraction=None)
            for document in self.list_documents_for_close_run(close_run_id=close_run_id)
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

    def get_document_deletion_plan_for_user(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        document_id: UUID,
        user_id: UUID,
    ) -> DocumentDeletionPlan | None:
        """Return one synthetic delete plan when the actor can access the document."""

        if (
            entity_id != self.access_record.entity.id
            or close_run_id != self.access_record.close_run.id
            or user_id != self.actor.id
        ):
            return None

        root_document = self.documents.get(document_id)
        if root_document is None:
            return None

        document_tree = self._collect_document_tree(document_id=document_id)
        derivative_storage_keys: list[str] = []
        active_job_ids: list[UUID] = []
        for document in document_tree:
            derivative_storage_keys.extend(
                self.derivative_storage_keys_by_document.get(document.id, ())
            )
            active_job_ids.extend(self.active_job_ids_by_document.get(document.id, ()))

        return DocumentDeletionPlan(
            root_document=root_document,
            documents=document_tree,
            source_storage_keys=tuple(document.storage_key for document in document_tree),
            derivative_storage_keys=tuple(dict.fromkeys(derivative_storage_keys)),
            active_job_ids=tuple(dict.fromkeys(active_job_ids)),
        )

    def delete_document_tree(self, *, document_ids: tuple[UUID, ...]) -> None:
        """Delete one in-memory document subtree and clear its synthetic linked state."""

        for document_id in document_ids:
            self.documents.pop(document_id, None)
            self.active_job_ids_by_document.pop(document_id, None)
            self.derivative_storage_keys_by_document.pop(document_id, None)

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

    def _collect_document_tree(self, *, document_id: UUID) -> tuple[DocumentRecord, ...]:
        """Return the root document and any descendants in parent-first order."""

        root_document = self.documents[document_id]
        documents = [root_document]
        frontier_ids = (root_document.id,)
        seen_document_ids = {root_document.id}
        while frontier_ids:
            children = tuple(
                document
                for document in self.documents.values()
                if document.parent_document_id in frontier_ids
            )
            frontier_ids = tuple(
                child.id for child in children if child.id not in seen_document_ids
            )
            for child in children:
                if child.id in seen_document_ids:
                    continue
                seen_document_ids.add(child.id)
                documents.append(child)

        return tuple(documents)


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

    def delete_source_document(self, *, storage_key: str) -> None:
        """Delete one source object from the in-memory object map."""

        self.objects.pop(storage_key, None)

    def delete_derivative_object(self, *, object_key: str) -> None:
        """Delete one derivative object from the in-memory object map."""

        self.objects.pop(object_key, None)


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


class InMemoryJobService:
    """Persist synthetic job rows around dispatches for upload-service tests."""

    def __init__(self) -> None:
        """Initialize the in-memory job capture state."""

        self.canceled_job_ids: list[UUID] = []
        self.dispatched_jobs: dict[UUID, JobRecord] = {}
        self.job_poll_counts: dict[UUID, int] = {}

    def dispatch_job(
        self,
        *,
        dispatcher: InMemoryTaskDispatcher,
        task_name: TaskName | str,
        payload: JsonObject,
        entity_id: UUID | None,
        close_run_id: UUID | None,
        document_id: UUID | None,
        actor_user_id: UUID | None,
        trace_id: str | None,
        checkpoint_payload: JsonObject | None = None,
        resumed_from_job_id: UUID | None = None,
        countdown: int | None = None,
    ) -> JobRecord:
        """Create a deterministic queued job record and forward the dispatch."""

        del checkpoint_payload, countdown
        task_id = str(uuid4())
        receipt = dispatcher.dispatch_task(
            task_name=task_name,
            kwargs=payload,
            task_id=task_id,
        )
        now = datetime.now(tz=UTC)
        job = JobRecord(
            id=UUID(task_id),
            entity_id=entity_id,
            close_run_id=close_run_id,
            document_id=document_id,
            actor_user_id=actor_user_id,
            canceled_by_user_id=None,
            resumed_from_job_id=resumed_from_job_id,
            task_name=receipt.task_name,
            queue_name=receipt.queue_name,
            routing_key=receipt.routing_key,
            status=JobStatus.QUEUED,
            payload=payload,
            checkpoint_payload={},
            result_payload=None,
            failure_reason=None,
            failure_details=None,
            blocking_reason=None,
            trace_id=trace_id,
            attempt_count=0,
            retry_count=0,
            max_retries=5,
            started_at=None,
            completed_at=None,
            cancellation_requested_at=None,
            canceled_at=None,
            dead_lettered_at=None,
            created_at=now,
            updated_at=now,
        )
        self.dispatched_jobs[job.id] = job
        return job

    def request_cancellation(
        self,
        *,
        entity_id: UUID,
        job_id: UUID,
        actor_user_id: UUID,
        reason: str,
    ) -> JobRecord:
        """Mark one synthetic job as canceled for delete-workflow coverage."""

        del entity_id, actor_user_id, reason
        job = self.dispatched_jobs[job_id]
        self.canceled_job_ids.append(job_id)
        now = datetime.now(tz=UTC)
        if job.status is JobStatus.RUNNING:
            running_job = replace(
                job,
                cancellation_requested_at=now,
            )
            self.dispatched_jobs[job_id] = running_job
            return running_job

        canceled_job = replace(
            job,
            status=JobStatus.CANCELED,
            cancellation_requested_at=now,
            canceled_at=now,
            completed_at=now,
        )
        self.dispatched_jobs[job_id] = canceled_job
        return canceled_job

    def get_job(self, *, job_id: UUID) -> JobRecord:
        """Return one synthetic job and resolve pending running cancellations on poll."""

        poll_count = self.job_poll_counts.get(job_id, 0) + 1
        self.job_poll_counts[job_id] = poll_count
        job = self.dispatched_jobs[job_id]
        if (
            job.status is JobStatus.RUNNING
            and job.cancellation_requested_at is not None
        ):
            now = datetime.now(tz=UTC)
            job = replace(
                job,
                status=JobStatus.CANCELED,
                canceled_at=now,
                completed_at=now,
            )
            self.dispatched_jobs[job_id] = job
        return job
