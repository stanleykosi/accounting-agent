"""
Purpose: Celery task for extracting structured fields from parsed documents.
Scope: Async task that runs field extraction, confidence computation,
and persistence for document extractions.
Dependencies: Parser task (Step 20), extraction schemas, field extractors,
and extraction database models.
"""

from __future__ import annotations

from uuid import UUID

from apps.worker.app.celery_app import ObservedTask, celery_app
from celery import Task
from services.common.enums import DocumentStatus, DocumentType
from services.common.logging import get_logger
from services.db.models.documents import Document
from services.db.models.extractions import (
    DocumentExtraction,
    DocumentLineItem,
    ExtractedField,
)
from services.db.session import get_session_factory
from services.extraction.field_extractors import (
    compute_confidence_summary,
    extract_fields_by_document_type,
)
from services.extraction.schemas import DocumentLineItem as SchemaDocumentLineItem
from services.jobs.task_names import TaskName, resolve_task_route

logger = get_logger(__name__)


@celery_app.task(
    bind=True,
    base=ObservedTask,
    name=TaskName.DOCUMENT_EXTRACT.value,
    autoretry_for=(RuntimeError,),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=resolve_task_route(TaskName.DOCUMENT_EXTRACT).max_retries,
)
def extract_document(
    self: Task,
    document_id: str,
    parser_output: dict,
) -> dict:
    """Extract structured fields from a parsed document.

    This task runs after parse_document and transforms raw parser output
    into structured extraction fields with confidence scores and evidence refs.

    Args:
        document_id: UUID of the document to extract from.
        parser_output: Raw parser output from the parse_document task.

    Returns:
        Dictionary with extraction_id, needs_review, and field_count.
    """

    document_uuid = UUID(document_id)

    with get_session_factory()() as db:
        document = db.query(Document).filter(Document.id == document_uuid).first()

        if document is None:
            logger.error("document_not_found", document_id=document_id)
            raise self.retry(Exception(f"Document {document_id} not found"))

        document_type = document.document_type
        if document_type == DocumentType.UNKNOWN:
            logger.warning(
                "extraction_skipped_unknown_type",
                document_id=document_id,
            )
            return {
                "document_id": document_id,
                "skipped": True,
                "reason": "unknown_document_type",
            }

        fields = extract_fields_by_document_type(document_type, parser_output)

        confidence_summary = compute_confidence_summary(fields)
        needs_review = confidence_summary.low_confidence_fields > 0

        extraction = DocumentExtraction(
            id=document_uuid,
            document_id=document_uuid,
            version_no=1,
            schema_name=document_type,
            schema_version="1.0.0",
            extracted_payload={
                "fields": [f.model_dump(mode="json") for f in fields],
                "parser_output": parser_output,
            },
            confidence_summary=confidence_summary.model_dump(mode="json"),
            needs_review=needs_review,
        )
        db.add(extraction)

        for field in fields:
            extracted_field = ExtractedField(
                document_extraction_id=extraction.id,
                field_name=field.field_name,
                field_value=field.model_dump(mode="json")["field_value"],
                field_type=field.field_type,
                confidence=field.confidence,
                evidence_ref=field.evidence_ref.model_dump(mode="json"),
                is_human_corrected=field.is_human_corrected,
            )
            db.add(extracted_field)

        line_items = parser_output.get("line_items", [])
        for line_data in line_items:
            line_item = SchemaDocumentLineItem.model_validate(line_data)
            doc_line = DocumentLineItem(
                document_extraction_id=extraction.id,
                line_no=line_item.line_no,
                description=line_item.description,
                quantity=float(line_item.quantity) if line_item.quantity is not None else None,
                unit_price=(
                    float(line_item.unit_price) if line_item.unit_price is not None else None
                ),
                amount=float(line_item.amount) if line_item.amount is not None else None,
                tax_amount=(
                    float(line_item.tax_amount) if line_item.tax_amount is not None else None
                ),
                dimensions=line_item.dimensions,
                evidence_ref=line_item.evidence_ref.model_dump(mode="json"),
            )
            db.add(doc_line)

        new_status = DocumentStatus.NEEDS_REVIEW if needs_review else DocumentStatus.PARSED
        document.status = new_status
        db.commit()

        logger.info(
            "extraction_completed",
            document_id=document_id,
            document_type=document_type,
            field_count=len(fields),
            needs_review=needs_review,
            overall_confidence=confidence_summary.overall_confidence,
        )

        return {
            "extraction_id": str(extraction.id),
            "document_id": document_id,
            "needs_review": needs_review,
            "field_count": len(fields),
            "overall_confidence": confidence_summary.overall_confidence,
        }


__all__ = ["extract_document"]
