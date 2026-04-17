"""
Purpose: Define strict API contracts for document upload and listing workflows.
Scope: Batch upload responses, document summaries, and parse-dispatch receipts
for close-run-attached source documents.
Dependencies: Pydantic contract defaults and canonical document enums.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

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


class ExtractedFieldSummary(ContractModel):
    """Describe one structured extracted field available for review and correction."""

    id: str = Field(description="Stable UUID for the extracted field.")
    field_name: str = Field(min_length=1, description="Canonical extracted field name.")
    field_value: Any = Field(default=None, description="Structured extracted field value.")
    field_type: str = Field(min_length=1, description="Stored extracted field type label.")
    confidence: float = Field(ge=0.0, le=1.0, description="Field-level confidence score.")
    evidence_ref: dict[str, object] = Field(
        default_factory=dict,
        description="Structured evidence reference for this field.",
    )
    is_human_corrected: bool = Field(
        default=False,
        description="Whether a reviewer corrected this field after extraction.",
    )
    created_at: datetime = Field(description="UTC timestamp when the field row was created.")
    updated_at: datetime = Field(description="UTC timestamp when the field row was last updated.")


class DocumentIssueSummary(ContractModel):
    """Describe one open collection/verification issue attached to a document."""

    id: str = Field(description="Stable UUID for the issue row.")
    issue_type: str = Field(min_length=1, description="Canonical issue type code.")
    severity: str = Field(min_length=1, description="Issue severity label.")
    status: str = Field(min_length=1, description="Issue lifecycle status.")
    details: dict[str, object] = Field(
        default_factory=dict,
        description="Structured issue details for reviewer context.",
    )
    created_at: datetime = Field(description="UTC timestamp when the issue was created.")
    updated_at: datetime = Field(description="UTC timestamp when the issue last changed.")


class AutoTransactionMatchSummary(ContractModel):
    """Describe the current deterministic transaction-linking result for a document."""

    status: str = Field(description="matched, unmatched, or not_applicable.")
    score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Deterministic candidate score when a comparison was possible.",
    )
    match_source: str | None = Field(
        default=None,
        description="Source family used for the match, such as bank_statement_line.",
    )
    matched_document_id: str | None = Field(
        default=None,
        description="Matched source document UUID when a transaction candidate was found.",
    )
    matched_document_filename: str | None = Field(
        default=None,
        description="Matched source document filename when available.",
    )
    matched_line_no: int | None = Field(
        default=None,
        description="Matched line number within the source transaction document, when available.",
    )
    matched_reference: str | None = Field(
        default=None,
        description="Matched bank or transaction reference text, when available.",
    )
    matched_description: str | None = Field(
        default=None,
        description="Matched narration or description text, when available.",
    )
    matched_date: date | None = Field(
        default=None,
        description="Matched transaction date when available.",
    )
    matched_amount: str | None = Field(
        default=None,
        description="Matched transaction amount as a decimal string when available.",
    )
    reasons: tuple[str, ...] = Field(
        default=(),
        description="Human-readable deterministic reasons that explain the match outcome.",
    )


class DocumentExtractionSummary(ContractModel):
    """Describe the latest extraction payload attached to a document."""

    id: str = Field(description="Stable UUID for the extraction version.")
    version_no: int = Field(ge=1, description="Monotonic extraction version number.")
    schema_name: str = Field(min_length=1, description="Extraction schema family used.")
    schema_version: str = Field(min_length=1, description="Extraction schema version used.")
    confidence_summary: dict[str, object] = Field(
        default_factory=dict,
        description="Aggregate extraction confidence metrics.",
    )
    needs_review: bool = Field(
        description="Whether extraction policy routed this version to review.",
    )
    approved_version: bool = Field(
        description="Whether a reviewer approved this extraction version.",
    )
    auto_approved: bool = Field(
        default=False,
        description="Whether reduced-interruption mode auto-approved this extraction/document.",
    )
    auto_transaction_match: AutoTransactionMatchSummary | None = Field(
        default=None,
        description="Latest deterministic transaction-linking result for the document.",
    )
    fields: tuple[ExtractedFieldSummary, ...] = Field(
        default=(),
        description="Structured fields extracted from the document.",
    )
    created_at: datetime = Field(
        description="UTC timestamp when the extraction row was created.",
    )
    updated_at: datetime = Field(
        description="UTC timestamp when the extraction row was last updated.",
    )


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
    latest_extraction: DocumentExtractionSummary | None = Field(
        default=None,
        description="Most recent structured extraction result available for review.",
    )
    open_issues: tuple[DocumentIssueSummary, ...] = Field(
        default=(),
        description="Open document issues currently blocking or warning in review.",
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


class DocumentDeleteResponse(ContractModel):
    """Return the canonical outcome after deleting one document subtree."""

    deleted_document_id: str = Field(description="The root document UUID that was deleted.")
    deleted_document_filename: str = Field(
        min_length=1,
        description="Original filename of the deleted root document.",
    )
    deleted_document_count: int = Field(
        ge=1,
        description="Total number of document rows deleted, including descendants.",
    )
    canceled_job_count: int = Field(
        ge=0,
        description="Number of active linked jobs that were canceled before deletion.",
    )


class DocumentReviewDecisionRequest(ContractModel):
    """Capture a reviewer decision for one document queue item."""

    decision: Literal["approved", "rejected", "needs_info"] = Field(
        description="Reviewer decision applied to the document.",
    )
    reason: str | None = Field(
        default=None,
        max_length=1000,
        description="Optional reviewer note preserved in the audit trail.",
    )
    verified_complete: bool | None = Field(
        default=None,
        description=(
            "Whether the reviewer confirmed the document is complete for this workflow step."
        ),
    )
    verified_authorized: bool | None = Field(
        default=None,
        description="Whether the reviewer confirmed the document is authorized.",
    )
    verified_period: bool | None = Field(
        default=None,
        description="Whether the reviewer confirmed the document belongs to the close-run period.",
    )
    verified_transaction_match: bool | None = Field(
        default=None,
        description=(
            "Whether the reviewer confirmed the document matches the relevant transaction(s)."
        ),
    )


class FieldCorrectionRequest(ContractModel):
    """Capture a human correction for one extracted field."""

    corrected_value: Any = Field(description="Corrected field value to persist.")
    corrected_type: str = Field(
        min_length=1,
        description="Field type label describing the corrected value shape.",
    )
    reason: str | None = Field(
        default=None,
        max_length=1000,
        description="Optional reviewer note preserved in the audit trail.",
    )


class DocumentReviewActionResponse(ContractModel):
    """Return the refreshed document state after a persisted review decision."""

    document: DocumentSummary = Field(
        description="Refreshed document state after the review action.",
    )
    decision: Literal["approved", "rejected", "needs_info"] = Field(
        description="Decision that was applied.",
    )
    extraction_approved: bool = Field(
        description="Whether the latest extraction version is now approved.",
    )


class FieldCorrectionResponse(ContractModel):
    """Return the refreshed field and document state after a human correction."""

    document: DocumentSummary = Field(description="Refreshed document state after the correction.")
    field: ExtractedFieldSummary = Field(description="Updated extracted field.")


__all__ = [
    "AutoTransactionMatchSummary",
    "BatchUploadDocumentsResponse",
    "DocumentDeleteResponse",
    "DocumentExtractionSummary",
    "DocumentIssueSummary",
    "DocumentListResponse",
    "DocumentProcessingDispatch",
    "DocumentReviewActionResponse",
    "DocumentReviewDecisionRequest",
    "DocumentSummary",
    "ExtractedFieldSummary",
    "FieldCorrectionRequest",
    "FieldCorrectionResponse",
    "UploadedDocumentResult",
]
