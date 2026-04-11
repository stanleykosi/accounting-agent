"""
Purpose: Define strict API contracts for document upload and listing workflows.
Scope: Batch upload responses, document summaries, and parse-dispatch receipts
for close-run-attached source documents.
Dependencies: Pydantic contract defaults and canonical document enums.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field
from services.common.enums import DocumentSourceChannel, DocumentStatus, DocumentType
from services.contracts.api_models import ContractModel


class DocumentProcessingDispatch(ContractModel):
    """Describe the background task queued for one uploaded document."""

    task_id: str = Field(min_length=1, description="Celery task identifier.")
    task_name: str = Field(min_length=1, description="Canonical task name.")
    queue_name: str = Field(min_length=1, description="Queue lane used by the task.")
    routing_key: str = Field(min_length=1, description="Task routing key.")
    trace_id: str | None = Field(default=None, description="Trace ID linked to task dispatch.")


class DocumentSummary(ContractModel):
    """Describe one document attached to a close run."""

    id: str = Field(description="Stable UUID for the document.")
    close_run_id: str = Field(description="Close run that owns the document.")
    parent_document_id: str | None = Field(
        default=None,
        description="Parent upload when this row is a split child document.",
    )
    document_type: DocumentType = Field(description="Current document classification.")
    source_channel: DocumentSourceChannel = Field(description="How the document entered the run.")
    storage_key: str = Field(min_length=1, description="Canonical object-storage key.")
    original_filename: str = Field(min_length=1, description="Original uploaded filename.")
    mime_type: str = Field(min_length=1, description="Sniffed true MIME type.")
    file_size_bytes: int = Field(ge=0, description="Exact uploaded payload byte size.")
    sha256_hash: str = Field(
        min_length=64,
        max_length=64,
        description="Lower-case SHA-256 checksum.",
    )
    period_start: date | None = Field(default=None, description="Detected source period start.")
    period_end: date | None = Field(default=None, description="Detected source period end.")
    classification_confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Classification confidence once available.",
    )
    ocr_required: bool = Field(description="Whether the source likely needs OCR.")
    status: DocumentStatus = Field(description="Current document lifecycle status.")
    owner_user_id: str | None = Field(default=None, description="Current owner user UUID.")
    last_touched_by_user_id: str | None = Field(
        default=None,
        description="Last operator to touch this document.",
    )
    created_at: datetime = Field(description="UTC timestamp when the row was created.")
    updated_at: datetime = Field(description="UTC timestamp when the row last changed.")


class UploadedDocumentResult(ContractModel):
    """Return one successfully uploaded document with its parse-dispatch receipt."""

    document: DocumentSummary = Field(description="Persisted document metadata.")
    dispatch: DocumentProcessingDispatch = Field(
        description="Background parse task receipt for this document.",
    )


class BatchUploadDocumentsResponse(ContractModel):
    """Return all documents accepted by one batch upload request."""

    uploaded_documents: tuple[UploadedDocumentResult, ...] = Field(
        default=(),
        description="Uploaded documents in the same order as the submitted files.",
    )


class DocumentListResponse(ContractModel):
    """Return documents for a close run in deterministic upload order."""

    documents: tuple[DocumentSummary, ...] = Field(
        default=(),
        description="Documents currently attached to the close run.",
    )


__all__ = [
    "BatchUploadDocumentsResponse",
    "DocumentListResponse",
    "DocumentProcessingDispatch",
    "DocumentSummary",
    "UploadedDocumentResult",
]
