"""
Purpose: Persist document review decisions and extracted-field corrections.
Scope: Entity-scoped access validation, extraction approval, field correction,
issue resolution, and immutable audit/event emission for collection and processing review.
Dependencies: Document repository, extraction service, audit service, and document/extraction
ORM models.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any
from uuid import UUID

from services.audit.events import ReviewActionType
from services.audit.service import AuditService
from services.common.enums import (
    DocumentIssueSeverity,
    DocumentIssueStatus,
    DocumentStatus,
    DocumentType,
)
from services.contracts.document_models import (
    AutoTransactionMatchSummary,
    DocumentExtractionSummary,
    DocumentIssueSummary,
    DocumentReviewActionResponse,
    DocumentSummary,
    ExtractedFieldSummary,
    FieldCorrectionResponse,
)
from services.db.models.audit import AuditSourceSurface
from services.db.models.documents import Document, DocumentIssue
from services.db.models.entity import EntityStatus
from services.db.models.extractions import DocumentExtraction, ExtractedField
from services.db.repositories.document_repo import (
    DocumentAccessRecord,
    DocumentRepository,
)
from services.db.repositories.entity_repo import EntityUserRecord
from services.documents.period_validation import PeriodValidationService
from services.documents.transaction_matching import (
    TransactionMatchingService,
    extract_auto_review_metadata,
    extract_auto_transaction_match_metadata,
    update_extraction_auto_review_payload,
)
from services.extraction.service import ExtractionService
from sqlalchemy.orm import Session


class DocumentReviewServiceErrorCode(StrEnum):
    """Enumerate stable error codes surfaced by document review workflows."""

    CLOSE_RUN_NOT_FOUND = "close_run_not_found"
    DOCUMENT_NOT_FOUND = "document_not_found"
    FIELD_NOT_FOUND = "field_not_found"
    ENTITY_ARCHIVED = "entity_archived"
    INVALID_ACTION = "invalid_action"
    EXTRACTION_NOT_FOUND = "extraction_not_found"


class DocumentReviewServiceError(Exception):
    """Represent an expected document-review-domain failure for API translation."""

    def __init__(
        self,
        *,
        status_code: int,
        code: DocumentReviewServiceErrorCode,
        message: str,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class DocumentReviewResult:
    """Describe the outcome of one document review decision."""

    document: DocumentSummary
    decision: str
    extraction_approved: bool


class DocumentReviewService:
    """Provide the canonical review and correction workflow for close-run documents."""

    def __init__(
        self,
        *,
        db_session: Session,
        repository: DocumentRepository,
    ) -> None:
        self._db_session = db_session
        self._repository = repository
        self._audit_service = AuditService(db_session=db_session)
        self._extraction_service = ExtractionService(db_session)

    def review_document(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
        document_id: UUID,
        decision: str,
        reason: str | None,
        verified_complete: bool | None,
        verified_authorized: bool | None,
        verified_period: bool | None,
        verified_transaction_match: bool | None,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> DocumentReviewActionResponse:
        """Persist one reviewer decision for a document and return refreshed state."""

        access = self._require_document_access(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
            document_id=document_id,
        )
        document = self._load_document(document_id=document_id)
        before_status = document.status
        latest_extraction = self._extraction_service.get_latest_extraction(document_id=document_id)
        verification_checks = {
            "complete": verified_complete,
            "authorized": verified_authorized,
            "period": verified_period,
            "transaction_match": verified_transaction_match,
        }

        extraction_approved = False
        normalized_decision = decision.strip().lower()
        if normalized_decision == "approved":
            required_checks = ("complete", "authorized", "period")
            missing_checks = [
                label
                for label, value in verification_checks.items()
                if label in required_checks and value is not True
            ]
            if missing_checks:
                raise DocumentReviewServiceError(
                    status_code=422,
                    code=DocumentReviewServiceErrorCode.INVALID_ACTION,
                    message=(
                        "Approving a document requires reviewer confirmation for completeness, "
                        "authorization, and period alignment."
                    ),
                )
            document.status = DocumentStatus.APPROVED.value
            if latest_extraction is not None:
                latest_extraction.approved_version = True
                latest_extraction.needs_review = False
                extraction_approved = True
            self._resolve_open_issues(
                document_id=document_id,
                actor_user_id=actor_user.id,
            )
            review_action = ReviewActionType.APPROVE.value
        elif normalized_decision == "rejected":
            document.status = DocumentStatus.REJECTED.value
            if latest_extraction is not None:
                latest_extraction.approved_version = False
            self._record_verification_findings(
                document_id=document_id,
                actor_user_id=actor_user.id,
                verification_checks=verification_checks,
            )
            review_action = ReviewActionType.REJECT.value
        elif normalized_decision == "needs_info":
            document.status = DocumentStatus.NEEDS_REVIEW.value
            if latest_extraction is not None:
                latest_extraction.approved_version = False
            self._record_verification_findings(
                document_id=document_id,
                actor_user_id=actor_user.id,
                verification_checks=verification_checks,
            )
            review_action = ReviewActionType.REQUEST_INFO.value
        else:
            raise DocumentReviewServiceError(
                status_code=422,
                code=DocumentReviewServiceErrorCode.INVALID_ACTION,
                message="Document review decision must be approved, rejected, or needs_info.",
            )

        document.last_touched_by_user_id = actor_user.id
        self._audit_service.record_review_action(
            entity_id=access.entity.id,
            close_run_id=access.close_run.id,
            target_type="document",
            target_id=document_id,
            action=review_action,
            actor_user_id=actor_user.id,
            autonomy_mode=access.entity.autonomy_mode,
            source_surface=source_surface,
            reason=reason,
            before_payload={"status": before_status},
            after_payload={
                "status": document.status,
                "latest_extraction_approved": extraction_approved,
            },
            trace_id=trace_id,
            audit_payload={
                "summary": (
                    f"{actor_user.full_name} recorded a {normalized_decision} decision for "
                    f"{document.original_filename}."
                ),
                "document_id": str(document.id),
                "decision": normalized_decision,
                "verification_checks": verification_checks,
            },
        )
        self._repository.create_activity_event(
            entity_id=access.entity.id,
            close_run_id=access.close_run.id,
            actor_user_id=actor_user.id,
            event_type="document.reviewed",
            source_surface=source_surface,
            payload={
                "summary": (
                    f"{actor_user.full_name} marked {document.original_filename} as "
                    f"{normalized_decision.replace('_', ' ')}."
                ),
                "document_id": str(document.id),
                "decision": normalized_decision,
                "reason": reason,
                "verification_checks": verification_checks,
            },
            trace_id=trace_id,
        )
        self._db_session.commit()

        refreshed_summary = self._build_document_summary(
            self._require_document_access(
                actor_user=actor_user,
                entity_id=entity_id,
                close_run_id=close_run_id,
                document_id=document_id,
            ).document.id
        )
        return DocumentReviewActionResponse(
            document=refreshed_summary,
            decision=normalized_decision,  # type: ignore[arg-type]
            extraction_approved=extraction_approved,
        )

    def correct_extracted_field(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
        field_id: UUID,
        corrected_value: Any,
        corrected_type: str,
        reason: str | None,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> FieldCorrectionResponse:
        """Persist a human correction for one extracted field and return refreshed state."""

        field = self._db_session.get(ExtractedField, field_id)
        if field is None:
            raise DocumentReviewServiceError(
                status_code=404,
                code=DocumentReviewServiceErrorCode.FIELD_NOT_FOUND,
                message="The extracted field was not found.",
            )

        extraction = self._db_session.get(DocumentExtraction, field.document_extraction_id)
        if extraction is None:
            raise DocumentReviewServiceError(
                status_code=404,
                code=DocumentReviewServiceErrorCode.EXTRACTION_NOT_FOUND,
                message="The field's extraction record does not exist.",
            )

        access = self._require_document_access(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
            document_id=extraction.document_id,
        )
        document = self._load_document(document_id=extraction.document_id)

        before_payload = {
            "field_value": field.field_value,
            "field_type": field.field_type,
            "document_status": document.status,
        }

        if not self._extraction_service.apply_field_correction(
            field_id=field_id,
            corrected_value=corrected_value,
            corrected_type=corrected_type,
        ):
            raise DocumentReviewServiceError(
                status_code=404,
                code=DocumentReviewServiceErrorCode.FIELD_NOT_FOUND,
                message="The extracted field was not found.",
            )

        extraction.approved_version = False
        extraction.needs_review = True
        extraction.extracted_payload = update_extraction_auto_review_payload(
            extracted_payload=extraction.extracted_payload,
            auto_approved=False,
            autonomy_mode=access.entity.autonomy_mode.value,
            reasons=(
                "A reviewer corrected an extracted field and returned the document to review.",
            ),
        )
        document.status = DocumentStatus.NEEDS_REVIEW.value
        document.last_touched_by_user_id = actor_user.id
        self._refresh_document_derived_state(
            document=document,
            close_run=access.close_run,
            actor_user_id=actor_user.id,
        )

        self._audit_service.record_review_action(
            entity_id=access.entity.id,
            close_run_id=access.close_run.id,
            target_type="extracted_field",
            target_id=field_id,
            action=ReviewActionType.EDIT.value,
            actor_user_id=actor_user.id,
            autonomy_mode=access.entity.autonomy_mode,
            source_surface=source_surface,
            reason=reason,
            before_payload=before_payload,
            after_payload={
                "field_value": corrected_value,
                "field_type": corrected_type,
                "document_status": document.status,
            },
            trace_id=trace_id,
            audit_payload={
                "summary": (
                    f"{actor_user.full_name} corrected {field.field_name} on "
                    f"{document.original_filename}."
                ),
                "document_id": str(document.id),
                "field_name": field.field_name,
            },
        )
        self._repository.create_activity_event(
            entity_id=access.entity.id,
            close_run_id=access.close_run.id,
            actor_user_id=actor_user.id,
            event_type="document.field_corrected",
            source_surface=source_surface,
            payload={
                "summary": (
                    f"{actor_user.full_name} corrected {field.field_name} on "
                    f"{document.original_filename}."
                ),
                "document_id": str(document.id),
                "field_id": str(field.id),
                "field_name": field.field_name,
                "reason": reason,
            },
            trace_id=trace_id,
        )
        self._db_session.commit()

        refreshed_document = self._build_document_summary(document.id)
        refreshed_field = self._build_field_summary(
            self._db_session.get(ExtractedField, field_id),
        )
        return FieldCorrectionResponse(
            document=refreshed_document,
            field=refreshed_field,
        )

    def _require_document_access(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
        document_id: UUID,
    ) -> DocumentAccessRecord:
        """Return one accessible document or raise a structured access error."""

        access = self._repository.get_document_for_user(
            entity_id=entity_id,
            close_run_id=close_run_id,
            document_id=document_id,
            user_id=actor_user.id,
        )
        if access is None:
            raise DocumentReviewServiceError(
                status_code=404,
                code=DocumentReviewServiceErrorCode.DOCUMENT_NOT_FOUND,
                message="That document does not exist in this close run or is not accessible.",
            )
        if access.entity.status is EntityStatus.ARCHIVED:
            raise DocumentReviewServiceError(
                status_code=409,
                code=DocumentReviewServiceErrorCode.ENTITY_ARCHIVED,
                message="Archived workspaces cannot change document review state.",
            )
        return access

    def _resolve_open_issues(self, *, document_id: UUID, actor_user_id: UUID) -> None:
        """Resolve any open document issues once a reviewer approves the document."""

        issues = (
            self._db_session.query(DocumentIssue)
            .filter(
                DocumentIssue.document_id == document_id,
                DocumentIssue.status == DocumentIssueStatus.OPEN.value,
            )
            .all()
        )
        for issue in issues:
            issue.status = DocumentIssueStatus.RESOLVED.value
            issue.resolved_by_user_id = actor_user_id
            issue.resolved_at = document_now()

    def _record_verification_findings(
        self,
        *,
        document_id: UUID,
        actor_user_id: UUID,
        verification_checks: dict[str, bool | None],
    ) -> None:
        """Create issue rows for failed verification checks without duplicating active findings."""

        issue_mapping = {
            "complete": (
                "incomplete_documentation",
                "Document completeness is not yet verified.",
            ),
            "authorized": (
                "unauthorized_document",
                "Document authorization could not be verified.",
            ),
            "period": ("wrong_period_document", "Document period is outside the close-run window."),
        }
        existing_issue_types = {
            issue.issue_type
            for issue in self._db_session.query(DocumentIssue)
            .filter(
                DocumentIssue.document_id == document_id,
                DocumentIssue.status == DocumentIssueStatus.OPEN.value,
            )
            .all()
        }
        for check_name, passed in verification_checks.items():
            if passed is not False:
                continue
            if check_name not in issue_mapping:
                continue
            issue_type, message = issue_mapping[check_name]
            if issue_type in existing_issue_types:
                continue
            self._db_session.add(
                DocumentIssue(
                    document_id=document_id,
                    issue_type=issue_type,
                    severity=DocumentIssueSeverity.BLOCKING.value,
                    status=DocumentIssueStatus.OPEN.value,
                    details={"reason": message, "check_name": check_name},
                    assigned_to_user_id=actor_user_id,
                    resolved_by_user_id=None,
                    resolved_at=None,
                )
            )
        self._db_session.flush()

    def _refresh_document_derived_state(
        self,
        *,
        document: Document,
        close_run: Any,
        actor_user_id: UUID,
    ) -> None:
        """Recompute period and transaction-derived review state after a field edit."""

        latest_extraction = self._extraction_service.get_latest_extraction(document_id=document.id)
        extracted_period_start, extracted_period_end = self._derive_document_period(
            document_type=document.document_type,
            extraction_id=latest_extraction.id if latest_extraction is not None else None,
        )
        document.period_start = extracted_period_start
        document.period_end = extracted_period_end
        self._sync_period_issue(
            document=document,
            close_run=close_run,
            actor_user_id=actor_user_id,
        )

        transaction_matching_service = TransactionMatchingService(
            db_session=self._db_session,
        )
        transaction_matching_service.evaluate_and_persist(
            close_run_id=document.close_run_id,
            document_id=document.id,
        )
        if document.document_type == DocumentType.BANK_STATEMENT.value:
            transaction_matching_service.refresh_close_run_matches(
                close_run_id=document.close_run_id,
            )

    def _derive_document_period(
        self,
        *,
        document_type: str,
        extraction_id: UUID | None,
    ) -> tuple[date | None, date | None]:
        """Derive the document period window from the persisted extracted-field set."""

        if extraction_id is None:
            return (None, None)

        field_values = {
            field_name: field_value
            for field_name, field_value in self._db_session.query(
                ExtractedField.field_name,
                ExtractedField.field_value,
            )
            .filter(ExtractedField.document_extraction_id == extraction_id)
            .all()
        }

        try:
            resolved_document_type = DocumentType(document_type)
        except ValueError:
            return (None, None)

        if resolved_document_type is DocumentType.BANK_STATEMENT:
            return (
                _coerce_date(field_values.get("statement_start_date")),
                _coerce_date(field_values.get("statement_end_date")),
            )
        if resolved_document_type is DocumentType.PAYSLIP:
            pay_period_start = _coerce_date(field_values.get("pay_period_start"))
            pay_period_end = _coerce_date(field_values.get("pay_period_end"))
            pay_date = _coerce_date(field_values.get("pay_date"))
            return (pay_period_start or pay_date, pay_period_end or pay_date)
        if resolved_document_type is DocumentType.CONTRACT:
            effective_date = _coerce_date(field_values.get("effective_date"))
            expiration_date = _coerce_date(field_values.get("expiration_date"))
            contract_date = _coerce_date(field_values.get("contract_date"))
            return (effective_date or contract_date, expiration_date or contract_date)
        if resolved_document_type is DocumentType.INVOICE:
            invoice_date = _coerce_date(field_values.get("invoice_date"))
            return (invoice_date, invoice_date)
        if resolved_document_type is DocumentType.RECEIPT:
            receipt_date = _coerce_date(field_values.get("receipt_date"))
            return (receipt_date, receipt_date)
        return (None, None)

    def _sync_period_issue(
        self,
        *,
        document: Document,
        close_run: Any,
        actor_user_id: UUID,
    ) -> None:
        """Synchronize the wrong-period issue with the latest extracted dates."""

        period_result = PeriodValidationService(document_repo=self._repository).validate_period(
            document_period_start=document.period_start,
            document_period_end=document.period_end,
            close_run_period_start=close_run.period_start,
            close_run_period_end=close_run.period_end,
        )
        if period_result.is_valid:
            self._resolve_open_issue(
                document_id=document.id,
                issue_type="wrong_period_document",
                actor_user_id=actor_user_id,
                resolution_details={
                    "resolution_reason": "Updated extracted dates now align with the close run.",
                    "document_period_start": (
                        period_result.document_period_start.isoformat()
                        if period_result.document_period_start is not None
                        else None
                    ),
                    "document_period_end": (
                        period_result.document_period_end.isoformat()
                        if period_result.document_period_end is not None
                        else None
                    ),
                },
            )
            return

        self._upsert_open_issue(
            document_id=document.id,
            issue_type="wrong_period_document",
            details={
                "document_period_start": (
                    period_result.document_period_start.isoformat()
                    if period_result.document_period_start is not None
                    else None
                ),
                "document_period_end": (
                    period_result.document_period_end.isoformat()
                    if period_result.document_period_end is not None
                    else None
                ),
                "close_run_period_start": close_run.period_start.isoformat(),
                "close_run_period_end": close_run.period_end.isoformat(),
                "validation_method": period_result.validation_method,
            },
            actor_user_id=actor_user_id,
        )

    def _upsert_open_issue(
        self,
        *,
        document_id: UUID,
        issue_type: str,
        details: dict[str, Any],
        actor_user_id: UUID,
    ) -> None:
        """Create or refresh one blocking issue tied to derived review state."""

        issue = (
            self._db_session.query(DocumentIssue)
            .filter(
                DocumentIssue.document_id == document_id,
                DocumentIssue.issue_type == issue_type,
                DocumentIssue.status == DocumentIssueStatus.OPEN.value,
            )
            .first()
        )
        if issue is None:
            self._db_session.add(
                DocumentIssue(
                    document_id=document_id,
                    issue_type=issue_type,
                    severity=DocumentIssueSeverity.BLOCKING.value,
                    status=DocumentIssueStatus.OPEN.value,
                    details=details,
                    assigned_to_user_id=actor_user_id,
                    resolved_by_user_id=None,
                    resolved_at=None,
                )
            )
            self._db_session.flush()
            return

        issue.details = details
        issue.assigned_to_user_id = actor_user_id
        self._db_session.flush()

    def _resolve_open_issue(
        self,
        *,
        document_id: UUID,
        issue_type: str,
        actor_user_id: UUID,
        resolution_details: dict[str, Any],
    ) -> None:
        """Resolve one open derived-state issue when it no longer applies."""

        issue = (
            self._db_session.query(DocumentIssue)
            .filter(
                DocumentIssue.document_id == document_id,
                DocumentIssue.issue_type == issue_type,
                DocumentIssue.status == DocumentIssueStatus.OPEN.value,
            )
            .first()
        )
        if issue is None:
            return

        issue.status = DocumentIssueStatus.RESOLVED.value
        issue.resolved_by_user_id = actor_user_id
        issue.resolved_at = document_now()
        issue.details = {**dict(issue.details), **resolution_details}
        self._db_session.flush()

    def _load_document(self, *, document_id: UUID) -> Document:
        """Load one document or fail fast on inconsistent persistence state."""

        document = self._db_session.get(Document, document_id)
        if document is None:
            raise DocumentReviewServiceError(
                status_code=404,
                code=DocumentReviewServiceErrorCode.DOCUMENT_NOT_FOUND,
                message="The document could not be loaded.",
            )
        return document

    def _build_document_summary(self, document_id: UUID) -> DocumentSummary:
        """Build the refreshed document summary from canonical repository state."""

        rows = self._repository.list_documents_for_close_run_with_latest_extraction(
            close_run_id=self._load_document(document_id=document_id).close_run_id
        )
        for row in rows:
            if row.document.id == document_id:
                return _to_document_summary(row.document, row.latest_extraction, row.open_issues)

        raise DocumentReviewServiceError(
            status_code=404,
            code=DocumentReviewServiceErrorCode.DOCUMENT_NOT_FOUND,
            message="The refreshed document state could not be loaded.",
        )

    def _build_field_summary(self, field: ExtractedField | None) -> ExtractedFieldSummary:
        """Build the refreshed extracted-field summary from ORM state."""

        if field is None:
            raise DocumentReviewServiceError(
                status_code=404,
                code=DocumentReviewServiceErrorCode.FIELD_NOT_FOUND,
                message="The refreshed field state could not be loaded.",
            )

        return ExtractedFieldSummary(
            id=str(field.id),
            field_name=field.field_name,
            field_value=field.field_value,
            field_type=field.field_type,
            confidence=float(field.confidence),
            evidence_ref=dict(field.evidence_ref),
            is_human_corrected=field.is_human_corrected,
            created_at=field.created_at,
            updated_at=field.updated_at,
        )


def _to_document_summary(
    document: Any,
    latest_extraction: Any,
    open_issues: Any,
) -> DocumentSummary:
    """Translate repository records into the strict document summary contract."""

    return DocumentSummary(
        id=str(document.id),
        close_run_id=str(document.close_run_id),
        parent_document_id=(
            str(document.parent_document_id) if document.parent_document_id else None
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
        owner_user_id=str(document.owner_user_id) if document.owner_user_id else None,
        last_touched_by_user_id=(
            str(document.last_touched_by_user_id) if document.last_touched_by_user_id else None
        ),
        latest_extraction=_to_extraction_summary(latest_extraction),
        open_issues=tuple(_to_document_issue_summary(issue) for issue in open_issues),
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _to_extraction_summary(latest_extraction: Any) -> DocumentExtractionSummary | None:
    """Translate the repository extraction record into the strict API contract."""

    if latest_extraction is None:
        return None

    auto_review_metadata = extract_auto_review_metadata(latest_extraction.extracted_payload)
    auto_transaction_match = extract_auto_transaction_match_metadata(
        latest_extraction.extracted_payload
    )
    return DocumentExtractionSummary(
        id=str(latest_extraction.id),
        version_no=latest_extraction.version_no,
        schema_name=latest_extraction.schema_name,
        schema_version=latest_extraction.schema_version,
        confidence_summary=dict(latest_extraction.confidence_summary),
        needs_review=latest_extraction.needs_review,
        approved_version=latest_extraction.approved_version,
        auto_approved=bool(
            auto_review_metadata and auto_review_metadata.get("auto_approved") is True
        ),
        auto_transaction_match=_to_auto_transaction_match_summary(auto_transaction_match),
        fields=tuple(
            ExtractedFieldSummary(
                id=str(field.id),
                field_name=field.field_name,
                field_value=field.field_value,
                field_type=field.field_type,
                confidence=field.confidence,
                evidence_ref=dict(field.evidence_ref),
                is_human_corrected=field.is_human_corrected,
                created_at=field.created_at,
                updated_at=field.updated_at,
            )
            for field in latest_extraction.fields
        ),
        created_at=latest_extraction.created_at,
        updated_at=latest_extraction.updated_at,
    )


def _to_auto_transaction_match_summary(metadata: Any) -> AutoTransactionMatchSummary | None:
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


def _to_document_issue_summary(issue: Any) -> DocumentIssueSummary:
    """Translate the repository issue record into the strict API contract."""

    return DocumentIssueSummary(
        id=str(issue.id),
        issue_type=issue.issue_type,
        severity=issue.severity,
        status=issue.status,
        details=dict(issue.details),
        created_at=issue.created_at,
        updated_at=issue.updated_at,
    )


def _coerce_date(value: Any) -> date | None:
    """Coerce one extracted field value into a date when possible."""

    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def document_now() -> Any:
    """Return a timezone-naive UTC timestamp matching current document issue writes."""

    from datetime import UTC, datetime

    return datetime.now(tz=UTC).replace(tzinfo=None)


__all__ = [
    "DocumentReviewResult",
    "DocumentReviewService",
    "DocumentReviewServiceError",
    "DocumentReviewServiceErrorCode",
]
