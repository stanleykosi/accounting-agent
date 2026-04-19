"""
Purpose: Orchestrate the canonical document upload workflow for close runs.
Scope: Access validation, MIME sniffing, SHA-256 calculation, MinIO source storage,
document row persistence, parse-task dispatch metadata, and audit timeline events.
Dependencies: Document contracts, repository protocol, storage repository, MIME helpers,
and task dispatch abstractions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from time import monotonic, sleep
from typing import Any, Protocol
from uuid import UUID, uuid4

from services.auth.service import serialize_uuid
from services.common.enums import DocumentStatus, JobStatus
from services.common.logging import get_logger
from services.common.types import JsonObject
from services.contracts.document_models import (
    AutoTransactionMatchSummary,
    BatchQueueDocumentsForParseResponse,
    BatchUploadDocumentsResponse,
    DocumentDeleteResponse,
    DocumentExtractionSummary,
    DocumentIssueSummary,
    DocumentListResponse,
    DocumentProcessingDispatch,
    DocumentReparseResponse,
    DocumentSummary,
    ExtractedFieldSummary,
    QueuedDocumentParseResult,
    UploadedDocumentResult,
)
from services.contracts.storage_models import CloseRunStorageScope
from services.db.models.audit import AuditSourceSurface
from services.db.models.entity import EntityStatus
from services.db.repositories.document_repo import (
    DocumentCloseRunAccessRecord,
    DocumentDeletionPlan,
    DocumentExtractionRecord,
    DocumentIssueRecord,
    DocumentRecord,
    DocumentReparsePlan,
    DocumentWithExtractionRecord,
    ExtractedFieldRecord,
)
from services.db.repositories.entity_repo import EntityUserRecord
from services.documents.mime import UnsupportedDocumentMimeError, sniff_document_mime
from services.documents.transaction_matching import (
    extract_auto_review_metadata,
    extract_auto_transaction_match_metadata,
)
from services.jobs.service import JobRecord, JobServiceError, JobServiceErrorCode
from services.jobs.task_names import TaskName
from services.storage.checksums import compute_sha256_bytes
from services.storage.repository import StorageRepository

logger = get_logger(__name__)


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
    DOCUMENT_NOT_FOUND = "document_not_found"
    ENTITY_ARCHIVED = "entity_archived"
    EMPTY_BATCH = "empty_batch"
    FILE_TOO_LARGE = "file_too_large"
    INTEGRITY_CONFLICT = "integrity_conflict"
    INVALID_FILENAME = "invalid_filename"
    NO_UPLOADED_DOCUMENTS = "no_uploaded_documents"
    PROCESSING_IN_PROGRESS = "processing_in_progress"
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

    def list_documents_for_close_run_with_latest_extraction(
        self,
        *,
        close_run_id: UUID,
    ) -> tuple[DocumentWithExtractionRecord, ...]:
        """Return close-run documents together with their latest extraction, if any."""

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

    def update_document_status(
        self,
        *,
        document_id: UUID,
        status: DocumentStatus,
        ocr_required: bool | None = None,
    ) -> DocumentRecord:
        """Update one document status and return the refreshed row."""

    def get_document_deletion_plan_for_user(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        document_id: UUID,
        user_id: UUID,
    ) -> DocumentDeletionPlan | None:
        """Return the delete plan for one accessible document subtree."""

    def get_document_reparse_plan_for_user(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        document_id: UUID,
        user_id: UUID,
    ) -> DocumentReparsePlan | None:
        """Return the reparse plan for one accessible document."""

    def delete_document_tree(self, *, document_ids: tuple[UUID, ...]) -> None:
        """Delete one document subtree after linked references are detached."""

    def reset_document_for_reparse(
        self,
        *,
        document_id: UUID,
        actor_user_id: UUID,
    ) -> DocumentRecord:
        """Delete parse artifacts and reset one document to a queued parse state."""

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

    def delete_source_document(self, *, storage_key: str) -> None:
        """Delete one uploaded source document from the canonical document bucket."""

    def delete_derivative_object(self, *, object_key: str) -> None:
        """Delete one derivative object from the canonical derivative bucket."""


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

    def request_cancellation(
        self,
        *,
        entity_id: UUID,
        job_id: UUID,
        actor_user_id: UUID,
        reason: str,
    ) -> JobRecord:
        """Request cancellation for one queued, running, or blocked job."""

    def get_job(self, *, job_id: UUID) -> JobRecord:
        """Return one durable job record by UUID for post-cancel polling."""


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
    active_job_settle_timeout_seconds = 5.0
    active_job_poll_interval_seconds = 0.1

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
        documents = self._repository.list_documents_for_close_run_with_latest_extraction(
            close_run_id=close_run_id
        )
        return DocumentListResponse(
            documents=tuple(
                _build_document_summary(
                    row.document,
                    row.latest_extraction,
                    row.open_issues,
                )
                for row in documents
            )
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
        """Validate, store, and persist one uploaded document batch."""

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

    def queue_uploaded_documents_for_parse(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> BatchQueueDocumentsForParseResponse:
        """Queue every staged uploaded document in one close run for parsing."""

        access_record = self._require_close_run_access(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
        uploaded_documents = tuple(
            document
            for document in self._repository.list_documents_for_close_run(
                close_run_id=access_record.close_run.id
            )
            if document.status is DocumentStatus.UPLOADED
        )
        if not uploaded_documents:
            raise DocumentUploadServiceError(
                status_code=409,
                code=DocumentUploadServiceErrorCode.NO_UPLOADED_DOCUMENTS,
                message="No newly uploaded source documents are waiting to be parsed.",
            )

        queued_documents: list[QueuedDocumentParseResult] = []
        try:
            for document in uploaded_documents:
                updated_document = self._repository.update_document_status(
                    document_id=document.id,
                    status=DocumentStatus.PROCESSING,
                )
                dispatch = self._dispatch_parse_task(
                    document=updated_document,
                    entity_id=access_record.close_run.entity_id,
                    close_run_id=access_record.close_run.id,
                    actor_user_id=actor_user.id,
                    trace_id=trace_id,
                )
                self._repository.create_activity_event(
                    entity_id=access_record.close_run.entity_id,
                    close_run_id=access_record.close_run.id,
                    actor_user_id=actor_user.id,
                    event_type="document.parse_requested",
                    source_surface=source_surface,
                    payload={
                        "summary": (
                            f"{actor_user.full_name} queued "
                            f"{updated_document.original_filename} for parsing."
                        ),
                        "document_id": serialize_uuid(updated_document.id),
                        "original_filename": updated_document.original_filename,
                        "parse_task_id": dispatch.task_id,
                    },
                    trace_id=trace_id,
                )
                queued_documents.append(
                    QueuedDocumentParseResult(
                        document=_build_document_summary(updated_document, None, ()),
                        dispatch=DocumentProcessingDispatch(
                            task_id=dispatch.task_id,
                            task_name=dispatch.task_name,
                            queue_name=dispatch.queue_name,
                            routing_key=dispatch.routing_key,
                            trace_id=dispatch.trace_id,
                        ),
                    )
                )
            self._repository.commit()
        except Exception as error:
            self._repository.rollback()
            if self._repository.is_integrity_error(error):
                raise DocumentUploadServiceError(
                    status_code=409,
                    code=DocumentUploadServiceErrorCode.INTEGRITY_CONFLICT,
                    message="The parse queue request conflicts with existing document state.",
                ) from error
            raise

        return BatchQueueDocumentsForParseResponse(queued_documents=tuple(queued_documents))

    def delete_document(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
        document_id: UUID,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> DocumentDeleteResponse:
        """Delete one accessible document subtree and clean its linked storage objects."""

        access_record = self._require_close_run_access(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
        deletion_plan = self._repository.get_document_deletion_plan_for_user(
            entity_id=entity_id,
            close_run_id=close_run_id,
            document_id=document_id,
            user_id=actor_user.id,
        )
        if deletion_plan is None:
            raise DocumentUploadServiceError(
                status_code=404,
                code=DocumentUploadServiceErrorCode.DOCUMENT_NOT_FOUND,
                message="The requested document was not found for this close run.",
            )

        canceled_job_count = self._cancel_document_jobs(
            actor_user=actor_user,
            access_record=access_record,
            document_id=deletion_plan.root_document.id,
            active_job_ids=deletion_plan.active_job_ids,
            cancellation_reason=(
                "Execution stopped because the linked source document was deleted by an "
                "operator."
            ),
        )
        self._wait_for_document_jobs_to_settle(
            actor_user=actor_user,
            access_record=access_record,
            document_id=deletion_plan.root_document.id,
            active_job_ids=deletion_plan.active_job_ids,
            in_progress_message=(
                "The document is still finishing background processing. Retry deletion after "
                "processing stops."
            ),
        )
        try:
            self._repository.delete_document_tree(
                document_ids=tuple(document.id for document in deletion_plan.documents)
            )
            self._repository.create_activity_event(
                entity_id=access_record.close_run.entity_id,
                close_run_id=access_record.close_run.id,
                actor_user_id=actor_user.id,
                event_type="document.deleted",
                source_surface=source_surface,
                payload={
                    "summary": (
                        f"{actor_user.full_name} deleted "
                        f"{deletion_plan.root_document.original_filename}."
                    ),
                    "deleted_document_id": serialize_uuid(deletion_plan.root_document.id),
                    "deleted_document_filename": deletion_plan.root_document.original_filename,
                    "deleted_document_count": len(deletion_plan.documents),
                    "canceled_job_count": canceled_job_count,
                },
                trace_id=trace_id,
            )
            self._repository.commit()
        except Exception as error:
            self._repository.rollback()
            if self._repository.is_integrity_error(error):
                raise DocumentUploadServiceError(
                    status_code=409,
                    code=DocumentUploadServiceErrorCode.INTEGRITY_CONFLICT,
                    message="The document could not be deleted because linked state changed.",
                ) from error
            raise

        self._delete_document_storage_objects(
            root_document_id=deletion_plan.root_document.id,
            source_storage_keys=deletion_plan.source_storage_keys,
            derivative_storage_keys=deletion_plan.derivative_storage_keys,
        )
        return DocumentDeleteResponse(
            deleted_document_id=serialize_uuid(deletion_plan.root_document.id),
            deleted_document_filename=deletion_plan.root_document.original_filename,
            deleted_document_count=len(deletion_plan.documents),
            canceled_job_count=canceled_job_count,
        )

    def reparse_document(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
        document_id: UUID,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> DocumentReparseResponse:
        """Clear one document's prior parse artifacts and queue a fresh parse."""

        access_record = self._require_close_run_access(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
        reparse_plan = self._repository.get_document_reparse_plan_for_user(
            entity_id=entity_id,
            close_run_id=close_run_id,
            document_id=document_id,
            user_id=actor_user.id,
        )
        if reparse_plan is None:
            raise DocumentUploadServiceError(
                status_code=404,
                code=DocumentUploadServiceErrorCode.DOCUMENT_NOT_FOUND,
                message="The requested document was not found for this close run.",
            )

        canceled_job_count = self._cancel_document_jobs(
            actor_user=actor_user,
            access_record=access_record,
            document_id=reparse_plan.document.id,
            active_job_ids=reparse_plan.active_job_ids,
            cancellation_reason=(
                "Execution stopped because the source document was queued for reparsing by an "
                "operator."
            ),
        )
        self._wait_for_document_jobs_to_settle(
            actor_user=actor_user,
            access_record=access_record,
            document_id=reparse_plan.document.id,
            active_job_ids=reparse_plan.active_job_ids,
            in_progress_message=(
                "The document is still finishing background processing. Retry reparsing after "
                "processing stops."
            ),
        )
        try:
            reparsed_document = self._repository.reset_document_for_reparse(
                document_id=document_id,
                actor_user_id=actor_user.id,
            )
            dispatch = self._dispatch_parse_task(
                document=reparsed_document,
                entity_id=access_record.close_run.entity_id,
                close_run_id=access_record.close_run.id,
                actor_user_id=actor_user.id,
                trace_id=trace_id,
            )
            self._repository.create_activity_event(
                entity_id=access_record.close_run.entity_id,
                close_run_id=access_record.close_run.id,
                actor_user_id=actor_user.id,
                event_type="document.reparse_requested",
                source_surface=source_surface,
                payload={
                    "summary": (
                        f"{actor_user.full_name} queued {reparse_plan.document.original_filename} "
                        "for reparsing."
                    ),
                    "document_id": serialize_uuid(reparse_plan.document.id),
                    "original_filename": reparse_plan.document.original_filename,
                    "cleared_extraction_count": reparse_plan.existing_extraction_count,
                    "cleared_issue_count": reparse_plan.existing_issue_count,
                    "cleared_version_count": reparse_plan.existing_version_count,
                    "canceled_job_count": canceled_job_count,
                    "parse_task_id": dispatch.task_id,
                },
                trace_id=trace_id,
            )
            self._repository.commit()
        except Exception as error:
            self._repository.rollback()
            if self._repository.is_integrity_error(error):
                raise DocumentUploadServiceError(
                    status_code=409,
                    code=DocumentUploadServiceErrorCode.INTEGRITY_CONFLICT,
                    message=(
                        "The document could not be queued for reparsing because linked "
                        "state changed."
                    ),
                ) from error
            raise

        self._delete_document_storage_objects(
            root_document_id=reparse_plan.document.id,
            source_storage_keys=(),
            derivative_storage_keys=reparse_plan.derivative_storage_keys,
        )
        return DocumentReparseResponse(
            reparsed_document_id=serialize_uuid(reparse_plan.document.id),
            reparsed_document_filename=reparse_plan.document.original_filename,
            cleared_extraction_count=reparse_plan.existing_extraction_count,
            cleared_issue_count=reparse_plan.existing_issue_count,
            cleared_version_count=reparse_plan.existing_version_count,
            canceled_job_count=canceled_job_count,
            dispatch=DocumentProcessingDispatch(
                task_id=dispatch.task_id,
                task_name=dispatch.task_name,
                queue_name=dispatch.queue_name,
                routing_key=dispatch.routing_key,
                trace_id=dispatch.trace_id,
            ),
        )

    def _upload_one_document(
        self,
        *,
        actor_user: EntityUserRecord,
        access_record: DocumentCloseRunAccessRecord,
        file_payload: UploadFilePayload,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> UploadedDocumentResult:
        """Process one file in a batch upload and return its staged document row."""

        filename = _normalize_filename_for_display(file_payload.filename)
        if len(file_payload.payload) > self.max_file_size_bytes:
            raise DocumentUploadServiceError(
                status_code=413,
                code=DocumentUploadServiceErrorCode.FILE_TOO_LARGE,
                message=f"{filename} exceeds the 50 MB hosted upload limit.",
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
            },
            trace_id=trace_id,
        )

        return UploadedDocumentResult(document=_build_document_summary(document, None, ()))

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

    def _cancel_document_jobs(
        self,
        *,
        actor_user: EntityUserRecord,
        access_record: DocumentCloseRunAccessRecord,
        document_id: UUID,
        active_job_ids: tuple[UUID, ...],
        cancellation_reason: str,
    ) -> int:
        """Cancel active linked jobs before their source documents are deleted."""

        canceled_job_count = 0
        for job_id in active_job_ids:
            try:
                self._job_service.request_cancellation(
                    entity_id=access_record.entity.id,
                    job_id=job_id,
                    actor_user_id=actor_user.id,
                    reason=cancellation_reason,
                )
                canceled_job_count += 1
            except JobServiceError as error:
                if error.code in {
                    JobServiceErrorCode.CANCEL_NOT_ALLOWED,
                    JobServiceErrorCode.JOB_NOT_FOUND,
                }:
                    logger.warning(
                        "Document deletion skipped job cancellation because the job state changed.",
                        job_id=serialize_uuid(job_id),
                        document_id=serialize_uuid(document_id),
                        error_code=str(error.code),
                    )
                    continue
                raise

        return canceled_job_count

    def _wait_for_document_jobs_to_settle(
        self,
        *,
        actor_user: EntityUserRecord,
        access_record: DocumentCloseRunAccessRecord,
        document_id: UUID,
        active_job_ids: tuple[UUID, ...],
        in_progress_message: str,
    ) -> None:
        """Block deletion until linked active jobs become terminal or timeout explicitly."""

        if not active_job_ids:
            return

        deadline = monotonic() + self.active_job_settle_timeout_seconds
        pending_job_ids = set(active_job_ids)
        active_statuses = {
            JobStatus.QUEUED,
            JobStatus.RUNNING,
            JobStatus.BLOCKED,
        }

        while pending_job_ids:
            completed_job_ids: list[UUID] = []
            for job_id in pending_job_ids:
                try:
                    job = self._job_service.get_job(job_id=job_id)
                except JobServiceError as error:
                    if error.code is JobServiceErrorCode.JOB_NOT_FOUND:
                        completed_job_ids.append(job_id)
                        continue
                    raise

                if job.status not in active_statuses:
                    completed_job_ids.append(job_id)

            for job_id in completed_job_ids:
                pending_job_ids.discard(job_id)

            if not pending_job_ids:
                return

            if monotonic() >= deadline:
                raise DocumentUploadServiceError(
                    status_code=409,
                    code=DocumentUploadServiceErrorCode.PROCESSING_IN_PROGRESS,
                    message=in_progress_message,
                )

            logger.info(
                "Document deletion is waiting for linked jobs to stop before removing rows.",
                document_id=serialize_uuid(document_id),
                entity_id=serialize_uuid(access_record.entity.id),
                actor_user_id=serialize_uuid(actor_user.id),
                pending_job_ids=[
                    serialize_uuid(job_id) for job_id in sorted(pending_job_ids, key=str)
                ],
            )
            sleep(self.active_job_poll_interval_seconds)

    def _delete_document_storage_objects(
        self,
        *,
        root_document_id: UUID,
        source_storage_keys: tuple[str, ...],
        derivative_storage_keys: tuple[str, ...],
    ) -> None:
        """Delete storage objects after the DB transaction commits successfully."""

        for storage_key in source_storage_keys:
            try:
                self._storage_repository.delete_source_document(storage_key=storage_key)
            except Exception:
                logger.exception(
                    "Document deletion left a source object behind in storage.",
                    document_id=serialize_uuid(root_document_id),
                    storage_key=storage_key,
                )
        for object_key in derivative_storage_keys:
            try:
                self._storage_repository.delete_derivative_object(object_key=object_key)
            except Exception:
                logger.exception(
                    "Document deletion left a derivative object behind in storage.",
                    document_id=serialize_uuid(root_document_id),
                    object_key=object_key,
                )


def _build_document_summary(
    document: DocumentRecord,
    latest_extraction: DocumentExtractionRecord | None,
    open_issues: tuple[DocumentIssueRecord, ...],
) -> DocumentSummary:
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
        latest_extraction=_build_extraction_summary(latest_extraction),
        open_issues=tuple(_build_document_issue_summary(issue) for issue in open_issues),
        created_at=document.created_at,
        updated_at=document.updated_at,
    )
def _build_extraction_summary(
    extraction: DocumentExtractionRecord | None,
) -> DocumentExtractionSummary | None:
    """Translate the latest extraction record into the API response contract."""

    if extraction is None:
        return None

    auto_review_metadata = extract_auto_review_metadata(extraction.extracted_payload)
    auto_transaction_match = extract_auto_transaction_match_metadata(extraction.extracted_payload)
    return DocumentExtractionSummary(
        id=serialize_uuid(extraction.id),
        version_no=extraction.version_no,
        schema_name=extraction.schema_name,
        schema_version=extraction.schema_version,
        confidence_summary=dict(extraction.confidence_summary),
        needs_review=extraction.needs_review,
        approved_version=extraction.approved_version,
        auto_approved=bool(
            auto_review_metadata and auto_review_metadata.get("auto_approved") is True
        ),
        auto_transaction_match=_build_auto_transaction_match_summary(auto_transaction_match),
        fields=tuple(_build_extracted_field_summary(field) for field in extraction.fields),
        created_at=extraction.created_at,
        updated_at=extraction.updated_at,
    )


def _build_auto_transaction_match_summary(
    metadata: object | None,
) -> AutoTransactionMatchSummary | None:
    """Translate persisted extraction metadata into the strict API contract."""

    if not isinstance(metadata, dict):
        return None

    reasons = metadata.get("reasons")
    return AutoTransactionMatchSummary(
        status=str(metadata.get("status") or "unmatched"),
        score=float(metadata["score"]) if isinstance(metadata.get("score"), (float, int)) else None,
        match_source=(
            str(metadata["match_source"]) if isinstance(metadata.get("match_source"), str) else None
        ),
        matched_document_id=(
            str(metadata["matched_document_id"])
            if isinstance(metadata.get("matched_document_id"), str)
            else None
        ),
        matched_document_filename=(
            str(metadata["matched_document_filename"])
            if isinstance(metadata.get("matched_document_filename"), str)
            else None
        ),
        matched_line_no=(
            int(metadata["matched_line_no"])
            if isinstance(metadata.get("matched_line_no"), int)
            else None
        ),
        matched_reference=(
            str(metadata["matched_reference"])
            if isinstance(metadata.get("matched_reference"), str)
            else None
        ),
        matched_description=(
            str(metadata["matched_description"])
            if isinstance(metadata.get("matched_description"), str)
            else None
        ),
        matched_date=(
            date.fromisoformat(str(metadata["matched_date"]))
            if isinstance(metadata.get("matched_date"), str)
            else None
        ),
        matched_amount=(
            str(metadata["matched_amount"])
            if isinstance(metadata.get("matched_amount"), str)
            else None
        ),
        reasons=tuple(str(reason) for reason in reasons) if isinstance(reasons, list) else (),
    )


def _build_extracted_field_summary(field: ExtractedFieldRecord) -> ExtractedFieldSummary:
    """Translate one extracted-field record into the strict API response contract."""

    return ExtractedFieldSummary(
        id=serialize_uuid(field.id),
        field_name=field.field_name,
        field_value=field.field_value,
        field_type=field.field_type,
        confidence=field.confidence,
        evidence_ref=dict(field.evidence_ref),
        is_human_corrected=field.is_human_corrected,
        created_at=field.created_at,
        updated_at=field.updated_at,
    )


def _build_document_issue_summary(issue: DocumentIssueRecord) -> DocumentIssueSummary:
    """Translate one document-issue record into the strict API response contract."""

    return DocumentIssueSummary(
        id=serialize_uuid(issue.id),
        issue_type=issue.issue_type,
        severity=issue.severity,
        status=issue.status,
        details=dict(issue.details),
        created_at=issue.created_at,
        updated_at=issue.updated_at,
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
