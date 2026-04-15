"""
Purpose: Persist and query close-run documents and upload audit events.
Scope: Document row creation, close-run access checks, document listing, and
transaction control for the primary file-ingestion workflow.
Dependencies: SQLAlchemy sessions, document ORM models, close-run/entity models, and audit service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from services.audit.service import AuditService
from services.common.enums import (
    AutonomyMode,
    DocumentSourceChannel,
    DocumentStatus,
    DocumentType,
)
from services.common.types import JsonObject
from services.db.models.audit import AuditSourceSurface
from services.db.models.close_run import CloseRun
from services.db.models.documents import Document, DocumentIssue, DocumentVersion
from services.db.models.extractions import DocumentExtraction, ExtractedField
from services.db.models.entity import Entity, EntityMembership, EntityStatus
from sqlalchemy import asc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class DocumentCloseRunRecord:
    """Describe the close-run fields required by document upload workflows."""

    id: UUID
    entity_id: UUID
    period_start: date
    period_end: date
    current_version_no: int


@dataclass(frozen=True, slots=True)
class DocumentEntityRecord:
    """Describe the owning entity fields needed for access and audit metadata."""

    id: UUID
    autonomy_mode: AutonomyMode
    status: EntityStatus


@dataclass(frozen=True, slots=True)
class DocumentCloseRunAccessRecord:
    """Describe an accessible close run and its owning entity."""

    close_run: DocumentCloseRunRecord
    entity: DocumentEntityRecord


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    """Describe one persisted document row as an immutable service-layer record."""

    id: UUID
    close_run_id: UUID
    parent_document_id: UUID | None
    document_type: DocumentType
    source_channel: DocumentSourceChannel
    storage_key: str
    original_filename: str
    mime_type: str
    file_size_bytes: int
    sha256_hash: str
    period_start: date | None
    period_end: date | None
    classification_confidence: float | None
    ocr_required: bool
    status: DocumentStatus
    owner_user_id: UUID | None
    last_touched_by_user_id: UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ExtractedFieldRecord:
    """Describe one persisted extracted field for review and correction workflows."""

    id: UUID
    document_extraction_id: UUID
    field_name: str
    field_value: object | None
    field_type: str
    confidence: float
    evidence_ref: dict[str, object]
    is_human_corrected: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DocumentIssueRecord:
    """Describe one persisted document issue for collection-phase verification workflows."""

    id: UUID
    document_id: UUID
    issue_type: str
    severity: str
    status: str
    details: JsonObject
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DocumentExtractionRecord:
    """Describe one persisted extraction version with its reviewable field set."""

    id: UUID
    document_id: UUID
    version_no: int
    schema_name: str
    schema_version: str
    extracted_payload: JsonObject
    confidence_summary: JsonObject
    needs_review: bool
    approved_version: bool
    created_at: datetime
    updated_at: datetime
    fields: tuple[ExtractedFieldRecord, ...]


@dataclass(frozen=True, slots=True)
class DocumentWithExtractionRecord:
    """Describe one close-run document together with its latest extraction, if any."""

    document: DocumentRecord
    latest_extraction: DocumentExtractionRecord | None
    open_issues: tuple[DocumentIssueRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class DocumentAccessRecord:
    """Describe one accessible document together with its close-run and entity context."""

    close_run: DocumentCloseRunRecord
    entity: DocumentEntityRecord
    document: DocumentRecord


@dataclass(frozen=True, slots=True)
class ParseDocumentRecord:
    """Describe one document and its close-run context for parser worker execution."""

    document: DocumentRecord
    close_run: DocumentCloseRunRecord
    entity: DocumentEntityRecord


@dataclass(frozen=True, slots=True)
class DocumentVersionRecord:
    """Describe one persisted parser output version."""

    id: UUID
    document_id: UUID
    version_no: int
    normalized_storage_key: str | None
    ocr_text_storage_key: str | None
    parser_name: str
    parser_version: str
    raw_parse_payload: JsonObject
    page_count: int | None
    checksum: str
    created_at: datetime
    updated_at: datetime


class DocumentRepository:
    """Execute canonical document persistence in one request-scoped DB session."""

    def __init__(self, *, db_session: Session) -> None:
        """Capture the SQLAlchemy session used by document workflows."""

        self._db_session = db_session

    def get_close_run_for_user(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        user_id: UUID,
    ) -> DocumentCloseRunAccessRecord | None:
        """Return one close run when the user can access its entity workspace."""

        statement = (
            select(CloseRun, Entity)
            .join(Entity, Entity.id == CloseRun.entity_id)
            .join(EntityMembership, EntityMembership.entity_id == Entity.id)
            .where(
                CloseRun.id == close_run_id,
                CloseRun.entity_id == entity_id,
                EntityMembership.user_id == user_id,
            )
        )
        row = self._db_session.execute(statement).one_or_none()
        if row is None:
            return None

        close_run, entity = row
        return DocumentCloseRunAccessRecord(
            close_run=DocumentCloseRunRecord(
                id=close_run.id,
                entity_id=close_run.entity_id,
                period_start=close_run.period_start,
                period_end=close_run.period_end,
                current_version_no=close_run.current_version_no,
            ),
            entity=DocumentEntityRecord(
                id=entity.id,
                autonomy_mode=_resolve_autonomy_mode(entity.autonomy_mode),
                status=EntityStatus(entity.status),
            ),
        )

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
        """Stage one uploaded source document row and return its generated identifier."""

        document = Document(
            id=document_id,
            close_run_id=close_run_id,
            parent_document_id=None,
            document_type=DocumentType.UNKNOWN.value,
            source_channel=DocumentSourceChannel.UPLOAD.value,
            storage_key=storage_key,
            original_filename=original_filename,
            mime_type=mime_type,
            file_size_bytes=file_size_bytes,
            sha256_hash=sha256_hash,
            period_start=None,
            period_end=None,
            classification_confidence=None,
            ocr_required=ocr_required,
            status=DocumentStatus.UPLOADED.value,
            owner_user_id=None,
            last_touched_by_user_id=actor_user_id,
        )
        self._db_session.add(document)
        self._db_session.flush()
        return _map_document(document)

    def list_documents_for_close_run(
        self,
        *,
        close_run_id: UUID,
    ) -> tuple[DocumentRecord, ...]:
        """Return documents for one close run in deterministic upload order."""

        statement = (
            select(Document)
            .where(Document.close_run_id == close_run_id)
            .order_by(asc(Document.created_at), asc(Document.original_filename), asc(Document.id))
        )
        return tuple(_map_document(document) for document in self._db_session.scalars(statement))

    def list_documents_for_close_run_with_latest_extraction(
        self,
        *,
        close_run_id: UUID,
    ) -> tuple[DocumentWithExtractionRecord, ...]:
        """Return close-run documents together with their latest extraction and fields."""

        documents = self.list_documents_for_close_run(close_run_id=close_run_id)
        if not documents:
            return ()

        document_ids = tuple(document.id for document in documents)
        extraction_rows = self._db_session.scalars(
            select(DocumentExtraction)
            .where(DocumentExtraction.document_id.in_(document_ids))
            .order_by(
                DocumentExtraction.document_id.asc(),
                DocumentExtraction.version_no.desc(),
                DocumentExtraction.created_at.desc(),
            )
        ).all()

        latest_extraction_models: dict[UUID, DocumentExtraction] = {}
        for extraction in extraction_rows:
            latest_extraction_models.setdefault(extraction.document_id, extraction)

        extraction_ids = tuple(extraction.id for extraction in latest_extraction_models.values())
        fields_by_extraction_id: dict[UUID, list[ExtractedFieldRecord]] = {
            extraction_id: [] for extraction_id in extraction_ids
        }
        if extraction_ids:
            field_rows = self._db_session.scalars(
                select(ExtractedField)
                .where(ExtractedField.document_extraction_id.in_(extraction_ids))
                .order_by(
                    ExtractedField.document_extraction_id.asc(),
                    ExtractedField.field_name.asc(),
                    ExtractedField.created_at.asc(),
                )
            ).all()
            for field in field_rows:
                fields_by_extraction_id.setdefault(field.document_extraction_id, []).append(
                    _map_extracted_field(field)
                )

        open_issue_rows = self._db_session.scalars(
            select(DocumentIssue)
            .where(
                DocumentIssue.document_id.in_(document_ids),
                DocumentIssue.status == "open",
            )
            .order_by(
                DocumentIssue.document_id.asc(),
                DocumentIssue.created_at.asc(),
            )
        ).all()
        issues_by_document_id: dict[UUID, list[DocumentIssueRecord]] = {
            document_id: [] for document_id in document_ids
        }
        for issue in open_issue_rows:
            issues_by_document_id.setdefault(issue.document_id, []).append(_map_document_issue(issue))

        results: list[DocumentWithExtractionRecord] = []
        for document in documents:
            extraction_model = latest_extraction_models.get(document.id)
            latest_extraction = (
                _map_document_extraction(
                    extraction_model,
                    tuple(fields_by_extraction_id.get(extraction_model.id, [])),
                )
                if extraction_model is not None
                else None
            )
            results.append(
                DocumentWithExtractionRecord(
                    document=document,
                    latest_extraction=latest_extraction,
                    open_issues=tuple(issues_by_document_id.get(document.id, [])),
                )
            )

        return tuple(results)

    def get_document_for_user(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        document_id: UUID,
        user_id: UUID,
    ) -> DocumentAccessRecord | None:
        """Return one document when it belongs to an accessible entity close run."""

        statement = (
            select(Document, CloseRun, Entity)
            .join(CloseRun, CloseRun.id == Document.close_run_id)
            .join(Entity, Entity.id == CloseRun.entity_id)
            .join(EntityMembership, EntityMembership.entity_id == Entity.id)
            .where(
                Document.id == document_id,
                Document.close_run_id == close_run_id,
                CloseRun.entity_id == entity_id,
                EntityMembership.user_id == user_id,
            )
        )
        row = self._db_session.execute(statement).one_or_none()
        if row is None:
            return None

        document, close_run, entity = row
        return DocumentAccessRecord(
            close_run=DocumentCloseRunRecord(
                id=close_run.id,
                entity_id=close_run.entity_id,
                period_start=close_run.period_start,
                period_end=close_run.period_end,
                current_version_no=close_run.current_version_no,
            ),
            entity=DocumentEntityRecord(
                id=entity.id,
                autonomy_mode=_resolve_autonomy_mode(entity.autonomy_mode),
                status=EntityStatus(entity.status),
            ),
            document=_map_document(document),
        )

    def get_document_for_parse(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        document_id: UUID,
    ) -> ParseDocumentRecord | None:
        """Return one parser-ready document when it belongs to the supplied close run."""

        statement = (
            select(Document, CloseRun, Entity)
            .join(CloseRun, CloseRun.id == Document.close_run_id)
            .join(Entity, Entity.id == CloseRun.entity_id)
            .where(
                Document.id == document_id,
                CloseRun.id == close_run_id,
                CloseRun.entity_id == entity_id,
            )
        )
        row = self._db_session.execute(statement).one_or_none()
        if row is None:
            return None

        document, close_run, entity = row
        return ParseDocumentRecord(
            document=_map_document(document),
            close_run=DocumentCloseRunRecord(
                id=close_run.id,
                entity_id=close_run.entity_id,
                period_start=close_run.period_start,
                period_end=close_run.period_end,
                current_version_no=close_run.current_version_no,
            ),
            entity=DocumentEntityRecord(
                id=entity.id,
                autonomy_mode=_resolve_autonomy_mode(entity.autonomy_mode),
                status=EntityStatus(entity.status),
            ),
        )

    def next_document_version_no(self, *, document_id: UUID) -> int:
        """Return the next parser output version number for one document."""

        statement = select(func.max(DocumentVersion.version_no)).where(
            DocumentVersion.document_id == document_id
        )
        current_max = self._db_session.execute(statement).scalar_one()
        if current_max is None:
            return 1

        return int(current_max) + 1

    def update_document_status(
        self,
        *,
        document_id: UUID,
        status: DocumentStatus,
        ocr_required: bool | None = None,
    ) -> DocumentRecord:
        """Update one document's parser lifecycle state and return the refreshed row."""

        document = self._load_document(document_id=document_id)
        document.status = status.value
        if ocr_required is not None:
            document.ocr_required = ocr_required
        self._db_session.flush()
        return _map_document(document)

    def update_document_classification(
        self,
        *,
        document_id: UUID,
        document_type: DocumentType,
        classification_confidence: float | None,
    ) -> DocumentRecord:
        """Update the document type and classification confidence after parsing."""

        document = self._load_document(document_id=document_id)
        document.document_type = document_type.value
        document.classification_confidence = classification_confidence
        self._db_session.flush()
        return _map_document(document)

    def update_document_period(
        self,
        *,
        document_id: UUID,
        period_start: date | None,
        period_end: date | None,
    ) -> DocumentRecord:
        """Persist the detected source-period window for one document."""

        document = self._load_document(document_id=document_id)
        document.period_start = period_start
        document.period_end = period_end
        self._db_session.flush()
        return _map_document(document)

    def create_document_version(
        self,
        *,
        document_id: UUID,
        version_no: int,
        normalized_storage_key: str | None,
        ocr_text_storage_key: str | None,
        parser_name: str,
        parser_version: str,
        raw_parse_payload: JsonObject,
        page_count: int | None,
        checksum: str,
    ) -> DocumentVersionRecord:
        """Persist parser metadata and derivative keys for one document version."""

        document_version = DocumentVersion(
            document_id=document_id,
            version_no=version_no,
            normalized_storage_key=normalized_storage_key,
            ocr_text_storage_key=ocr_text_storage_key,
            parser_name=parser_name,
            parser_version=parser_version,
            raw_parse_payload=dict(raw_parse_payload),
            page_count=page_count,
            checksum=checksum,
        )
        self._db_session.add(document_version)
        self._db_session.flush()
        return _map_document_version(document_version)

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
        """Persist one document-intake activity event for the workspace timeline."""

        AuditService(db_session=self._db_session).emit_audit_event(
            entity_id=entity_id,
            close_run_id=close_run_id,
            event_type=event_type,
            actor_user_id=actor_user_id,
            source_surface=source_surface,
            payload=dict(payload),
            trace_id=trace_id,
        )

    def commit(self) -> None:
        """Commit the current document transaction after a successful mutation."""

        self._db_session.commit()

    def rollback(self) -> None:
        """Rollback the current document transaction after a failed mutation."""

        self._db_session.rollback()

    @staticmethod
    def is_integrity_error(error: Exception) -> bool:
        """Return whether the provided exception originated from a DB integrity failure."""

        return isinstance(error, IntegrityError)

    def _load_document(self, *, document_id: UUID) -> Document:
        """Load one document by UUID or fail fast when service state is inconsistent."""

        statement = select(Document).where(Document.id == document_id)
        document = self._db_session.execute(statement).scalar_one_or_none()
        if document is None:
            raise LookupError(f"Document {document_id} does not exist.")

        return document


def _map_document(document: Document) -> DocumentRecord:
    """Convert an ORM document row into the immutable repository record."""

    confidence = (
        float(document.classification_confidence)
        if document.classification_confidence is not None
        else None
    )
    return DocumentRecord(
        id=document.id,
        close_run_id=document.close_run_id,
        parent_document_id=document.parent_document_id,
        document_type=_resolve_document_type(document.document_type),
        source_channel=_resolve_source_channel(document.source_channel),
        storage_key=document.storage_key,
        original_filename=document.original_filename,
        mime_type=document.mime_type,
        file_size_bytes=document.file_size_bytes,
        sha256_hash=document.sha256_hash,
        period_start=document.period_start,
        period_end=document.period_end,
        classification_confidence=confidence,
        ocr_required=document.ocr_required,
        status=_resolve_document_status(document.status),
        owner_user_id=document.owner_user_id,
        last_touched_by_user_id=document.last_touched_by_user_id,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _map_document_version(document_version: DocumentVersion) -> DocumentVersionRecord:
    """Convert an ORM document-version row into an immutable repository record."""

    return DocumentVersionRecord(
        id=document_version.id,
        document_id=document_version.document_id,
        version_no=document_version.version_no,
        normalized_storage_key=document_version.normalized_storage_key,
        ocr_text_storage_key=document_version.ocr_text_storage_key,
        parser_name=document_version.parser_name,
        parser_version=document_version.parser_version,
        raw_parse_payload=dict(document_version.raw_parse_payload),
        page_count=document_version.page_count,
        checksum=document_version.checksum,
        created_at=document_version.created_at,
        updated_at=document_version.updated_at,
    )


def _map_document_extraction(
    extraction: DocumentExtraction,
    fields: tuple[ExtractedFieldRecord, ...],
) -> DocumentExtractionRecord:
    """Convert an ORM extraction row into the immutable service-layer record."""

    return DocumentExtractionRecord(
        id=extraction.id,
        document_id=extraction.document_id,
        version_no=extraction.version_no,
        schema_name=extraction.schema_name,
        schema_version=extraction.schema_version,
        extracted_payload=dict(extraction.extracted_payload),
        confidence_summary=dict(extraction.confidence_summary),
        needs_review=extraction.needs_review,
        approved_version=extraction.approved_version,
        created_at=extraction.created_at,
        updated_at=extraction.updated_at,
        fields=fields,
    )


def _map_document_issue(issue: DocumentIssue) -> DocumentIssueRecord:
    """Convert an ORM document-issue row into the immutable service-layer record."""

    return DocumentIssueRecord(
        id=issue.id,
        document_id=issue.document_id,
        issue_type=issue.issue_type,
        severity=issue.severity,
        status=issue.status,
        details=dict(issue.details),
        created_at=issue.created_at,
        updated_at=issue.updated_at,
    )


def _map_extracted_field(field: ExtractedField) -> ExtractedFieldRecord:
    """Convert an ORM extracted-field row into the immutable service-layer record."""

    return ExtractedFieldRecord(
        id=field.id,
        document_extraction_id=field.document_extraction_id,
        field_name=field.field_name,
        field_value=field.field_value,
        field_type=field.field_type,
        confidence=float(field.confidence),
        evidence_ref=dict(field.evidence_ref),
        is_human_corrected=field.is_human_corrected,
        created_at=field.created_at,
        updated_at=field.updated_at,
    )


def _resolve_autonomy_mode(value: str) -> AutonomyMode:
    """Resolve a stored autonomy-mode value or fail fast on schema drift."""

    for autonomy_mode in AutonomyMode:
        if autonomy_mode.value == value:
            return autonomy_mode

    raise ValueError(f"Unsupported autonomy mode value: {value}")


def _resolve_document_type(value: str) -> DocumentType:
    """Resolve a stored document-type value or fail fast on schema drift."""

    for document_type in DocumentType:
        if document_type.value == value:
            return document_type

    raise ValueError(f"Unsupported document type value: {value}")


def _resolve_source_channel(value: str) -> DocumentSourceChannel:
    """Resolve a stored source-channel value or fail fast on schema drift."""

    for source_channel in DocumentSourceChannel:
        if source_channel.value == value:
            return source_channel

    raise ValueError(f"Unsupported document source channel value: {value}")


def _resolve_document_status(value: str) -> DocumentStatus:
    """Resolve a stored document-status value or fail fast on schema drift."""

    for document_status in DocumentStatus:
        if document_status.value == value:
            return document_status

    raise ValueError(f"Unsupported document status value: {value}")


__all__ = [
    "DocumentAccessRecord",
    "DocumentCloseRunAccessRecord",
    "DocumentCloseRunRecord",
    "DocumentExtractionRecord",
    "DocumentEntityRecord",
    "DocumentIssueRecord",
    "DocumentRecord",
    "DocumentRepository",
    "DocumentVersionRecord",
    "DocumentWithExtractionRecord",
    "ExtractedFieldRecord",
    "ParseDocumentRecord",
]
