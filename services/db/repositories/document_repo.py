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
from services.db.models.documents import Document
from services.db.models.entity import Entity, EntityMembership, EntityStatus
from sqlalchemy import asc, select
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
    "DocumentCloseRunAccessRecord",
    "DocumentCloseRunRecord",
    "DocumentEntityRecord",
    "DocumentRecord",
    "DocumentRepository",
]
