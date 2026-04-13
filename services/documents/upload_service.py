"""
Purpose: Orchestrate the canonical document upload workflow for close runs.
Scope: Access validation, MIME sniffing, SHA-256 calculation, MinIO source storage,
document row persistence, parse-task dispatch metadata, and audit timeline events.
Dependencies: Document contracts, repository protocol, storage repository, MIME helpers,
and task dispatch abstractions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from services.auth.service import serialize_uuid
from services.common.types import JsonObject
from services.contracts.document_models import (
    BatchUploadDocumentsResponse,
    DocumentListResponse,
    DocumentProcessingDispatch,
    DocumentSummary,
    UploadedDocumentResult,
)
from services.contracts.storage_models import CloseRunStorageScope
from services.db.models.audit import AuditSourceSurface
from services.db.models.entity import EntityStatus
from services.db.repositories.document_repo import (
    DocumentCloseRunAccessRecord,
    DocumentRecord,
)
from services.db.repositories.entity_repo import EntityUserRecord
from services.documents.mime import UnsupportedDocumentMimeError, sniff_document_mime
from services.jobs.service import JobRecord
from services.jobs.task_names import TaskName
from services.storage.checksums import compute_sha256_bytes
from services.storage.repository import StorageRepository


@dataclass(frozen=True, slots=True)
class UploadFilePayload:
    """Describe one uploaded file after the API has read its multipart payload."""

    filename: str
    payload: bytes
    declared_content_type: str | None


@dataclass(frozen=True, slots=True)
class UploadDispatchReceipt:
    """Describe the parse task accepted for one uploaded document."""

    task_id: str
    task_name: str
    queue_name: str
    routing_key: str
    trace_id: str | None


class DocumentUploadServiceErrorCode(StrEnum):
    """Enumerate stable error codes surfaced by document upload workflows."""

    CLOSE_RUN_NOT_FOUND = "close_run_not_found"
    ENTITY_ARCHIVED = "entity_archived"
    EMPTY_BATCH = "empty_batch"
    FILE_TOO_LARGE = "file_too_large"
    INTEGRITY_CONFLICT = "integrity_conflict"
    INVALID_FILENAME = "invalid_filename"
    UNSUPPORTED_CONTENT = "unsupported_content"


class DocumentUploadServiceError(Exception):
    """Represent an expected document-upload-domain failure for API translation."""

    def __init__(
        self,
        *,
        status_code: int,
        code: DocumentUploadServiceErrorCode,
        message: str,
    ) -> None:
        """Capture HTTP status, stable code, and operator-facing recovery message."""

        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class DocumentRepositoryProtocol(Protocol):
    """Describe the persistence operations required by document upload workflows."""

    def get_close_run_for_user(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        user_id: UUID,
    ) -> DocumentCloseRunAccessRecord | None:
        """Return one close run when the user can access it."""

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
        """Persist one uploaded document row."""

    def list_documents_for_close_run(self, *, close_run_id: UUID) -> tuple[DocumentRecord, ...]:
        """Return documents attached to one close run."""

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
        """Persist one activity event."""

    def commit(self) -> None:
        """Commit the current unit of work."""

    def rollback(self) -> None:
        """Rollback the current unit of work."""

    def is_integrity_error(self, error: Exception) -> bool:
        """Return whether the provided exception originated from the database."""


class StorageRepositoryProtocol(Protocol):
    """Describe the storage operation required by document upload workflows."""

    def store_source_document(
        self,
        *,
        scope: CloseRunStorageScope,
        document_id: UUID,
        original_filename: str,
        payload: bytes,
        content_type: str,
        expected_sha256: str | None = None,
    ) -> SourceStorageMetadataProtocol:
        """Store one uploaded source document and return metadata with a storage reference."""


class SourceStorageReferenceProtocol(Protocol):
    """Describe the source-storage reference fields consumed after upload."""

    @property
    def object_key(self) -> str:
        """Return the stored object's canonical key."""


class SourceStorageMetadataProtocol(Protocol):
    """Describe the source-storage metadata fields consumed after upload."""

    @property
    def reference(self) -> SourceStorageReferenceProtocol:
        """Return the stored object's canonical reference."""


class TaskDispatcherProtocol(Protocol):
    """Describe the task-dispatch operation required by upload workflows."""

    def dispatch_task(
        self,
        *,
        task_name: TaskName | str,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        countdown: int | None = None,
        task_id: str | None = None,
    ) -> TaskReceiptProtocol:
        """Dispatch a JSON-serializable background job."""


class JobServiceProtocol(Protocol):
    """Describe the durable job-creation operation required by upload workflows."""

    def dispatch_job(
        self,
        *,
        dispatcher: TaskDispatcherProtocol,
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
        """Persist and dispatch one background job."""


class TaskReceiptProtocol(Protocol):
    """Describe task-dispatch receipt fields consumed by upload responses."""

    @property
    def task_id(self) -> str:
        """Return the Celery task identifier."""

    @property
    def task_name(self) -> str:
        """Return the canonical task name."""

    @property
    def queue_name(self) -> str:
        """Return the queue lane used for dispatch."""

    @property
    def routing_key(self) -> str:
        """Return the Celery routing key."""

    @property
    def trace_id(self) -> str | None:
        """Return the trace identifier associated with dispatch."""


class DocumentUploadService:
    """Provide the canonical batch document upload workflow used by API routes."""

    max_file_size_bytes = 50 * 1024 * 1024

    def __init__(
        self,
        *,
        repository: DocumentRepositoryProtocol,
        storage_repository: StorageRepository | StorageRepositoryProtocol,
        job_service: JobServiceProtocol,
        task_dispatcher: TaskDispatcherProtocol,
    ) -> None:
        """Capture persistence, storage, and background-task boundaries."""

        self._repository = repository
        self._storage_repository = storage_repository
        self._job_service = job_service
        self._task_dispatcher = task_dispatcher

    def list_documents(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
    ) -> DocumentListResponse:
        """Return documents for one accessible close run."""

        self._require_close_run_access(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
        documents = self._repository.list_documents_for_close_run(close_run_id=close_run_id)
        return DocumentListResponse(
            documents=tuple(_build_document_summary(row) for row in documents)
        )

    def upload_documents(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
        files: tuple[UploadFilePayload, ...],
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> BatchUploadDocumentsResponse:
        """Validate, store, persist, and dispatch parsing for one uploaded document batch."""

        if not files:
            raise DocumentUploadServiceError(
                status_code=400,
                code=DocumentUploadServiceErrorCode.EMPTY_BATCH,
                message="Upload at least one PDF, Excel workbook, or CSV file.",
            )

        access_record = self._require_close_run_access(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
        if access_record.entity.status is EntityStatus.ARCHIVED:
            raise DocumentUploadServiceError(
                status_code=409,
                code=DocumentUploadServiceErrorCode.ENTITY_ARCHIVED,
                message="Archived entity workspaces cannot accept new document uploads.",
            )

        uploaded_documents: list[UploadedDocumentResult] = []
        try:
            for file_payload in files:
                uploaded_documents.append(
                    self._upload_one_document(
                        actor_user=actor_user,
                        access_record=access_record,
                        file_payload=file_payload,
                        source_surface=source_surface,
                        trace_id=trace_id,
                    )
                )
            self._repository.commit()
        except Exception as error:
            self._repository.rollback()
            if self._repository.is_integrity_error(error):
                raise DocumentUploadServiceError(
                    status_code=409,
                    code=DocumentUploadServiceErrorCode.INTEGRITY_CONFLICT,
                    message="The uploaded document metadata conflicts with existing state.",
                ) from error
            raise

        return BatchUploadDocumentsResponse(uploaded_documents=tuple(uploaded_documents))

    def _upload_one_document(
        self,
        *,
        actor_user: EntityUserRecord,
        access_record: DocumentCloseRunAccessRecord,
        file_payload: UploadFilePayload,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> UploadedDocumentResult:
        """Process one file in a batch upload and return its document plus task receipt."""

        filename = _normalize_filename_for_display(file_payload.filename)
        if len(file_payload.payload) > self.max_file_size_bytes:
            raise DocumentUploadServiceError(
                status_code=413,
                code=DocumentUploadServiceErrorCode.FILE_TOO_LARGE,
                message=f"{filename} exceeds the 50 MB local-demo upload limit.",
            )

        try:
            sniffed = sniff_document_mime(filename=filename, payload=file_payload.payload)
        except UnsupportedDocumentMimeError as error:
            raise DocumentUploadServiceError(
                status_code=415,
                code=DocumentUploadServiceErrorCode.UNSUPPORTED_CONTENT,
                message=f"{filename}: {error}",
            ) from error

        document_id = uuid4()
        sha256_hash = compute_sha256_bytes(file_payload.payload)
        storage_metadata = self._storage_repository.store_source_document(
            scope=CloseRunStorageScope(
                entity_id=access_record.close_run.entity_id,
                close_run_id=access_record.close_run.id,
                period_start=access_record.close_run.period_start,
                period_end=access_record.close_run.period_end,
                close_run_version_no=access_record.close_run.current_version_no,
            ),
            document_id=document_id,
            original_filename=filename,
            payload=file_payload.payload,
            content_type=sniffed.mime_type.value,
            expected_sha256=sha256_hash,
        )
        storage_key = storage_metadata.reference.object_key
        document = self._repository.create_uploaded_document(
            document_id=document_id,
            close_run_id=access_record.close_run.id,
            storage_key=storage_key,
            original_filename=filename,
            mime_type=sniffed.mime_type.value,
            file_size_bytes=len(file_payload.payload),
            sha256_hash=sha256_hash,
            ocr_required=sniffed.ocr_required,
            actor_user_id=actor_user.id,
        )
        dispatch = self._dispatch_parse_task(
            document=document,
            entity_id=access_record.close_run.entity_id,
            close_run_id=access_record.close_run.id,
            actor_user_id=actor_user.id,
            trace_id=trace_id,
        )
        self._repository.create_activity_event(
            entity_id=access_record.close_run.entity_id,
            close_run_id=access_record.close_run.id,
            actor_user_id=actor_user.id,
            event_type="document.uploaded",
            source_surface=source_surface,
            payload={
                "summary": f"{actor_user.full_name} uploaded {filename}.",
                "document_id": serialize_uuid(document.id),
                "original_filename": filename,
                "mime_type": sniffed.mime_type.value,
                "declared_content_type": file_payload.declared_content_type,
                "file_size_bytes": len(file_payload.payload),
                "sha256_hash": sha256_hash,
                "parse_task_id": dispatch.task_id,
            },
            trace_id=trace_id,
        )

        return UploadedDocumentResult(
            document=_build_document_summary(document),
            dispatch=DocumentProcessingDispatch(
                task_id=dispatch.task_id,
                task_name=dispatch.task_name,
                queue_name=dispatch.queue_name,
                routing_key=dispatch.routing_key,
                trace_id=dispatch.trace_id,
            ),
        )

    def _dispatch_parse_task(
        self,
        *,
        document: DocumentRecord,
        entity_id: UUID,
        close_run_id: UUID,
        actor_user_id: UUID,
        trace_id: str | None,
    ) -> UploadDispatchReceipt:
        """Dispatch the downstream parser task with only JSON-serializable identifiers."""

        job = self._job_service.dispatch_job(
            dispatcher=self._task_dispatcher,
            task_name=TaskName.DOCUMENT_PARSE_AND_EXTRACT,
            payload={
                "entity_id": serialize_uuid(entity_id),
                "close_run_id": serialize_uuid(close_run_id),
                "document_id": serialize_uuid(document.id),
                "actor_user_id": serialize_uuid(actor_user_id),
            },
            entity_id=entity_id,
            close_run_id=close_run_id,
            document_id=document.id,
            actor_user_id=actor_user_id,
            trace_id=trace_id,
        )
        return UploadDispatchReceipt(
            task_id=serialize_uuid(job.id),
            task_name=job.task_name,
            queue_name=job.queue_name,
            routing_key=job.routing_key,
            trace_id=job.trace_id,
        )

    def _require_close_run_access(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
    ) -> DocumentCloseRunAccessRecord:
        """Return close-run access metadata or raise a structured domain error."""

        access_record = self._repository.get_close_run_for_user(
            entity_id=entity_id,
            close_run_id=close_run_id,
            user_id=actor_user.id,
        )
        if access_record is None:
            raise DocumentUploadServiceError(
                status_code=404,
                code=DocumentUploadServiceErrorCode.CLOSE_RUN_NOT_FOUND,
                message="Close run was not found for this entity, or you do not have access.",
            )

        return access_record


def _build_document_summary(document: DocumentRecord) -> DocumentSummary:
    """Translate a document repository record into the strict API response contract."""

    return DocumentSummary(
        id=serialize_uuid(document.id),
        close_run_id=serialize_uuid(document.close_run_id),
        parent_document_id=(
            serialize_uuid(document.parent_document_id)
            if document.parent_document_id is not None
            else None
        ),
        document_type=document.document_type,
        source_channel=document.source_channel,
        storage_key=document.storage_key,
        original_filename=document.original_filename,
        mime_type=document.mime_type,
        file_size_bytes=document.file_size_bytes,
        sha256_hash=document.sha256_hash,
        period_start=document.period_start,
        period_end=document.period_end,
        classification_confidence=document.classification_confidence,
        ocr_required=document.ocr_required,
        status=document.status,
        owner_user_id=(
            serialize_uuid(document.owner_user_id) if document.owner_user_id is not None else None
        ),
        last_touched_by_user_id=(
            serialize_uuid(document.last_touched_by_user_id)
            if document.last_touched_by_user_id is not None
            else None
        ),
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _normalize_filename_for_display(filename: str) -> str:
    """Normalize a multipart filename while preserving the user-facing basename."""

    normalized = filename.strip().replace("\\", "/").rsplit("/", maxsplit=1)[-1].strip()
    if not normalized or normalized in {".", ".."}:
        raise DocumentUploadServiceError(
            status_code=400,
            code=DocumentUploadServiceErrorCode.INVALID_FILENAME,
            message="Each uploaded file must include a non-empty filename.",
        )

    return normalized


__all__ = [
    "DocumentUploadService",
    "DocumentUploadServiceError",
    "DocumentUploadServiceErrorCode",
    "UploadDispatchReceipt",
    "UploadFilePayload",
]
