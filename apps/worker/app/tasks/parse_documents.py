"""
Purpose: Execute the deterministic parser pipeline for uploaded close-run documents.
Scope: Celery task registration, source-object download, PDF/OCR/spreadsheet parser
selection, derivative storage, document-version persistence, status transitions, and
worker audit events.
Dependencies: Celery worker app, document repository, storage repository, parser adapters,
and shared observability context.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from apps.worker.app.celery_app import ObservedTask, celery_app
from services.common.enums import DocumentStatus
from services.common.types import JsonObject
from services.contracts.storage_models import CloseRunStorageScope, DerivativeKind
from services.db.models.audit import AuditSourceSurface
from services.db.repositories.document_repo import DocumentRepository, ParseDocumentRecord
from services.db.session import get_session_factory
from services.jobs.task_names import TaskName, resolve_task_route
from services.observability.context import current_trace_metadata
from services.parser.models import ParserPipelineError, ParserResult, ParserSourceDocument
from services.parser.ocr_router import OcrRouter
from services.parser.pdf_parser import parse_pdf_document
from services.parser.spreadsheet_parser import parse_spreadsheet_document
from services.storage.checksums import compute_sha256_text
from services.storage.repository import StorageRepository


@dataclass(frozen=True, slots=True)
class StoredParseDerivatives:
    """Describe object keys generated while storing parser derivatives."""

    normalized_storage_key: str | None
    ocr_text_storage_key: str | None
    extracted_tables_storage_key: str | None


class StorageRepositoryProtocol(Protocol):
    """Describe the storage methods consumed by the parser pipeline."""

    def download_source_document(self, *, storage_key: str) -> bytes:
        """Download original source bytes from canonical document storage."""

    def store_ocr_text(
        self,
        *,
        scope: CloseRunStorageScope,
        document_id: UUID,
        document_version_no: int,
        source_filename: str,
        text: str,
        content_type: str = "text/plain; charset=utf-8",
        expected_sha256: str | None = None,
    ) -> object:
        """Store OCR text and return derivative metadata."""

    def store_derivative(
        self,
        *,
        scope: CloseRunStorageScope,
        document_id: UUID,
        document_version_no: int,
        derivative_kind: DerivativeKind,
        filename: str,
        payload: bytes,
        content_type: str,
        expected_sha256: str | None = None,
    ) -> object:
        """Store a normalized derivative and return derivative metadata."""


def _run_parse_document_task(
    *,
    entity_id: str,
    close_run_id: str,
    document_id: str,
    actor_user_id: str,
) -> dict[str, object]:
    """Run parser work from a Celery invocation using JSON-serializable identifiers."""

    parsed_entity_id = UUID(entity_id)
    parsed_close_run_id = UUID(close_run_id)
    parsed_document_id = UUID(document_id)
    parsed_actor_user_id = UUID(actor_user_id)
    trace_id = current_trace_metadata().trace_id

    with get_session_factory()() as db_session:
        repository = DocumentRepository(db_session=db_session)
        try:
            parse_record = repository.get_document_for_parse(
                entity_id=parsed_entity_id,
                close_run_id=parsed_close_run_id,
                document_id=parsed_document_id,
            )
            if parse_record is None:
                raise LookupError(
                    "Document parse task cannot continue because the document was not found "
                    "for the supplied entity and close run."
                )

            repository.update_document_status(
                document_id=parse_record.document.id,
                status=DocumentStatus.PROCESSING,
            )
            repository.commit()
        except Exception:
            repository.rollback()
            raise

    storage_repository = StorageRepository()
    try:
        result = parse_and_store_document(
            parse_record=parse_record,
            storage_repository=storage_repository,
        )
    except ParserPipelineError as error:
        failure_status = (
            DocumentStatus.BLOCKED
            if error.code.value == "blocked_input"
            else DocumentStatus.FAILED
        )
        _record_parse_failure(
            parse_record=parse_record,
            actor_user_id=parsed_actor_user_id,
            status=failure_status,
            error_payload={"code": error.code.value, "message": error.message},
            trace_id=trace_id,
        )
        raise
    except Exception as error:
        _record_parse_failure(
            parse_record=parse_record,
            actor_user_id=parsed_actor_user_id,
            status=DocumentStatus.FAILED,
            error_payload={"code": "unexpected_parse_failure", "message": str(error)},
            trace_id=trace_id,
        )
        raise

    with get_session_factory()() as db_session:
        repository = DocumentRepository(db_session=db_session)
        try:
            repository.update_document_status(
                document_id=parse_record.document.id,
                status=DocumentStatus.PARSED,
                ocr_required=_raw_payload_requires_ocr(result.raw_parse_payload),
            )
            repository.create_activity_event(
                entity_id=parse_record.entity.id,
                close_run_id=parse_record.close_run.id,
                actor_user_id=parsed_actor_user_id,
                event_type="document.parsed",
                source_surface=AuditSourceSurface.WORKER,
                payload={
                    "summary": f"Parsed {parse_record.document.original_filename}.",
                    "document_id": str(parse_record.document.id),
                    "document_version_no": result.document_version_no,
                    "parser_name": result.parser_name,
                    "parser_version": result.parser_version,
                    "page_count": result.page_count,
                    "table_count": result.table_count,
                    "split_candidate_count": result.split_candidate_count,
                },
                trace_id=trace_id,
            )
            repository.commit()
        except Exception:
            repository.rollback()
            raise

    return {
        "document_id": str(parse_record.document.id),
        "document_version_no": result.document_version_no,
        "parser_name": result.parser_name,
        "parser_version": result.parser_version,
        "page_count": result.page_count,
        "table_count": result.table_count,
        "split_candidate_count": result.split_candidate_count,
    }


@dataclass(frozen=True, slots=True)
class ParsePipelineReceipt:
    """Describe the persisted parser output returned by parse-and-store execution."""

    document_version_no: int
    parser_name: str
    parser_version: str
    page_count: int | None
    table_count: int
    split_candidate_count: int
    checksum: str
    raw_parse_payload: JsonObject
    derivatives: StoredParseDerivatives


def parse_and_store_document(
    *,
    parse_record: ParseDocumentRecord,
    storage_repository: StorageRepository | StorageRepositoryProtocol,
) -> ParsePipelineReceipt:
    """Parse one source document, store derivatives, and persist its version metadata."""

    with get_session_factory()() as db_session:
        repository = DocumentRepository(db_session=db_session)
        document_version_no = repository.next_document_version_no(
            document_id=parse_record.document.id,
        )

    source_payload = storage_repository.download_source_document(
        storage_key=parse_record.document.storage_key
    )
    parser_result = parse_source_document(
        ParserSourceDocument(
            filename=parse_record.document.original_filename,
            mime_type=parse_record.document.mime_type,
            payload=source_payload,
            ocr_required=parse_record.document.ocr_required,
        )
    )
    scope = CloseRunStorageScope(
        entity_id=parse_record.close_run.entity_id,
        close_run_id=parse_record.close_run.id,
        period_start=parse_record.close_run.period_start,
        period_end=parse_record.close_run.period_end,
        close_run_version_no=parse_record.close_run.current_version_no,
    )
    derivatives = store_parse_derivatives(
        storage_repository=storage_repository,
        scope=scope,
        document_id=parse_record.document.id,
        document_version_no=document_version_no,
        source_filename=parse_record.document.original_filename,
        parser_result=parser_result,
    )
    raw_parse_payload = _build_raw_parse_payload(
        parser_result=parser_result,
        derivatives=derivatives,
    )
    checksum = compute_sha256_text(
        json.dumps(raw_parse_payload, ensure_ascii=True, sort_keys=True)
    )

    with get_session_factory()() as db_session:
        repository = DocumentRepository(db_session=db_session)
        try:
            repository.create_document_version(
                document_id=parse_record.document.id,
                version_no=document_version_no,
                normalized_storage_key=derivatives.normalized_storage_key,
                ocr_text_storage_key=derivatives.ocr_text_storage_key,
                parser_name=parser_result.parser_name,
                parser_version=parser_result.parser_version,
                raw_parse_payload=raw_parse_payload,
                page_count=parser_result.page_count,
                checksum=checksum,
            )
            repository.commit()
        except Exception:
            repository.rollback()
            raise

    return ParsePipelineReceipt(
        document_version_no=document_version_no,
        parser_name=parser_result.parser_name,
        parser_version=parser_result.parser_version,
        page_count=parser_result.page_count,
        table_count=len(parser_result.tables),
        split_candidate_count=len(parser_result.split_candidates),
        checksum=checksum,
        raw_parse_payload=raw_parse_payload,
        derivatives=derivatives,
    )


def parse_source_document(source_document: ParserSourceDocument) -> ParserResult:
    """Select and run the deterministic parser adapter for one uploaded source document."""

    mime_type = source_document.mime_type.lower()
    filename = source_document.filename.lower()
    if mime_type == "application/pdf" or filename.endswith(".pdf"):
        initial_parse = parse_pdf_document(
            payload=source_document.payload,
            filename=source_document.filename,
        )
        routing_decision, ocr_result = OcrRouter().run_if_required(
            payload=source_document.payload,
            filename=source_document.filename,
            initial_parse_result=initial_parse,
            intake_ocr_required=source_document.ocr_required,
        )
        if ocr_result is None:
            initial_parse.metadata["ocr_routing"] = routing_decision.model_dump(mode="json")
            return initial_parse

        parsed = parse_pdf_document(
            payload=ocr_result.searchable_pdf_payload or source_document.payload,
            filename=source_document.filename,
            ocr_text=ocr_result.text,
            normalized_payload_override=ocr_result.searchable_pdf_payload,
        )
        parsed.metadata["ocr_routing"] = routing_decision.model_dump(mode="json")
        parsed.metadata["ocr"] = ocr_result.metadata
        return parsed

    return parse_spreadsheet_document(
        payload=source_document.payload,
        filename=source_document.filename,
        mime_type=source_document.mime_type,
    )


def store_parse_derivatives(
    *,
    storage_repository: StorageRepository | StorageRepositoryProtocol,
    scope: CloseRunStorageScope,
    document_id: UUID,
    document_version_no: int,
    source_filename: str,
    parser_result: ParserResult,
) -> StoredParseDerivatives:
    """Store normalized documents, OCR text, and extracted-table payloads."""

    normalized_key: str | None = None
    normalized_payload = parser_result.normalized_payload()
    if (
        normalized_payload is not None
        and parser_result.normalized_filename is not None
        and parser_result.normalized_content_type is not None
    ):
        metadata = storage_repository.store_derivative(
            scope=scope,
            document_id=document_id,
            document_version_no=document_version_no,
            derivative_kind=DerivativeKind.NORMALIZED_DOCUMENT,
            filename=parser_result.normalized_filename,
            payload=normalized_payload,
            content_type=parser_result.normalized_content_type,
        )
        normalized_key = _extract_object_key(metadata)

    ocr_text_key: str | None = None
    if parser_result.ocr_text:
        metadata = storage_repository.store_ocr_text(
            scope=scope,
            document_id=document_id,
            document_version_no=document_version_no,
            source_filename=source_filename,
            text=parser_result.ocr_text,
        )
        ocr_text_key = _extract_object_key(metadata)

    extracted_tables_key: str | None = None
    if parser_result.tables:
        payload = json.dumps(
            {"tables": [table.model_dump(mode="json") for table in parser_result.tables]},
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        metadata = storage_repository.store_derivative(
            scope=scope,
            document_id=document_id,
            document_version_no=document_version_no,
            derivative_kind=DerivativeKind.EXTRACTED_TABLES,
            filename=f"{source_filename}-tables.json",
            payload=payload,
            content_type="application/json",
        )
        extracted_tables_key = _extract_object_key(metadata)

    return StoredParseDerivatives(
        normalized_storage_key=normalized_key,
        ocr_text_storage_key=ocr_text_key,
        extracted_tables_storage_key=extracted_tables_key,
    )


def _build_raw_parse_payload(
    *,
    parser_result: ParserResult,
    derivatives: StoredParseDerivatives,
) -> JsonObject:
    """Merge parser metadata with derivative storage keys for DB persistence."""

    payload = parser_result.raw_parse_payload()
    payload["derivatives"] = {
        "normalized_storage_key": derivatives.normalized_storage_key,
        "ocr_text_storage_key": derivatives.ocr_text_storage_key,
        "extracted_tables_storage_key": derivatives.extracted_tables_storage_key,
    }
    return payload


def _record_parse_failure(
    *,
    parse_record: ParseDocumentRecord,
    actor_user_id: UUID,
    status: DocumentStatus,
    error_payload: JsonObject,
    trace_id: str | None,
) -> None:
    """Persist a parser failure status and emit a worker audit event."""

    with get_session_factory()() as db_session:
        repository = DocumentRepository(db_session=db_session)
        try:
            repository.update_document_status(
                document_id=parse_record.document.id,
                status=status,
            )
            repository.create_activity_event(
                entity_id=parse_record.entity.id,
                close_run_id=parse_record.close_run.id,
                actor_user_id=actor_user_id,
                event_type="document.parse_failed",
                source_surface=AuditSourceSurface.WORKER,
                payload={
                    "summary": f"Parsing failed for {parse_record.document.original_filename}.",
                    "document_id": str(parse_record.document.id),
                    "error": error_payload,
                    "status": status.value,
                },
                trace_id=trace_id,
            )
            repository.commit()
        except Exception:
            repository.rollback()
            raise


def _extract_object_key(metadata: object) -> str:
    """Extract the object key from derivative metadata returned by storage repositories."""

    reference = getattr(metadata, "reference", None)
    object_key = getattr(reference, "object_key", None)
    if not isinstance(object_key, str) or not object_key:
        raise ValueError("Storage derivative metadata did not include a valid object key.")

    return object_key


def _raw_payload_requires_ocr(raw_parse_payload: JsonObject) -> bool:
    """Read the requires-OCR parser metadata flag from a JSON-safe payload."""

    metadata = raw_parse_payload.get("metadata")
    if not isinstance(metadata, dict):
        return False

    return metadata.get("requires_ocr") is True


parse_document = celery_app.task(
    bind=True,
    base=ObservedTask,
    name=TaskName.DOCUMENT_PARSE_AND_EXTRACT.value,
    autoretry_for=(RuntimeError,),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=resolve_task_route(TaskName.DOCUMENT_PARSE_AND_EXTRACT).max_retries,
)(_run_parse_document_task)


__all__ = [
    "ParsePipelineReceipt",
    "StoredParseDerivatives",
    "parse_and_store_document",
    "parse_document",
    "parse_source_document",
    "store_parse_derivatives",
]
