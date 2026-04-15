"""
Purpose: Run document quality checks as part of the document processing pipeline.
Scope: Execute duplicate detection, period validation, completeness checks,
auto transaction-linking, and issue creation.
Dependencies: Document upload service, issue service, quality check services, and deterministic
transaction matching.
"""

from __future__ import annotations

import logging
from uuid import UUID

from services.common.enums import DocumentIssueSeverity
from services.db.models.audit import AuditSourceSurface
from services.db.repositories.document_repo import DocumentRepository
from services.db.repositories.entity_repo import EntityRepository
from services.documents.completeness import CompletenessCheckService
from services.documents.duplicate_detection import DuplicateDetectionService
from services.documents.issues import DocumentIssueService
from services.documents.period_validation import PeriodValidationService
from services.documents.transaction_matching import (
    TransactionMatchingService,
)
from services.storage.repository import StorageRepository

logger = logging.getLogger(__name__)


def run_document_quality_checks(
    *,
    entity_id: UUID,
    close_run_id: UUID,
    document_id: UUID,
    document_hash: str,
    document_file_size: int,
    document_period_start,
    document_period_end,
    close_run_period_start,
    close_run_period_end,
    actor_user_id: UUID,
    document_repo: DocumentRepository,
    entity_repo: EntityRepository,
    storage_repo: StorageRepository,
    db_session,
) -> dict:
    """
    Run all document quality checks for an uploaded document.

    This function performs:
    1. Duplicate detection using SHA-256 hash
    2. Period validation (if period can be detected from document)
    3. Completeness check for the close run
    4. Deterministic transaction linking against bank-statement evidence
    5. Creates issues for any problems found

    Args:
        entity_id: Entity ID
        close_run_id: Close run ID
        document_id: Document ID to check
        document_hash: SHA-256 hash of the document
        document_file_size: Size of the document in bytes
        actor_user_id: User who uploaded the document
        document_repo: Document repository instance
        entity_repo: Entity repository instance
        storage_repo: Storage repository instance
        db_session: Database session

    Returns:
        Dictionary with check results and any issues created
    """
    logger.info(f"Running document quality checks for document {document_id}")

    # Initialize services
    duplicate_service = DuplicateDetectionService(
        document_repo=document_repo,
        storage_repo=storage_repo,
    )
    period_service = PeriodValidationService(document_repo=document_repo)
    completeness_service = CompletenessCheckService(document_repo=document_repo)
    issue_service = DocumentIssueService(
        db_session=db_session,
        document_repo=document_repo,
    )
    transaction_matcher = TransactionMatchingService(db_session=db_session)

    results = {
        "document_id": str(document_id),
        "checks_performed": [],
        "issues_created": [],
        "passed_all_checks": True,
        "transaction_match": None,
    }

    # 1. Duplicate Detection Check
    try:
        duplicate_result = duplicate_service.check_duplicate(
            document_hash=document_hash,
            close_run_id=str(close_run_id),
            entity_id=str(entity_id),
        )
        results["checks_performed"].append(
            {
                "check": "duplicate_detection",
                "result": duplicate_result.__dict__,
            }
        )

        if duplicate_result.is_duplicate:
            # Create duplicate issue
            issue = issue_service.create_issue(
                document_id=document_id,
                issue_type="duplicate_document",
                severity=DocumentIssueSeverity.BLOCKING,
                details={
                    "existing_document_id": duplicate_result.existing_document_id,
                    "similarity_score": duplicate_result.similarity_score,
                    "detection_method": duplicate_result.detection_method,
                    "document_hash": document_hash,
                },
                actor_user_id=actor_user_id,
                source_surface=AuditSourceSurface.WORKER,
            )
            results["issues_created"].append(
                {
                    "issue_id": str(issue.id),
                    "issue_type": "duplicate_document",
                    "severity": issue.severity.value,
                }
            )
            results["passed_all_checks"] = False

    except Exception as e:
        logger.error(f"Error running duplicate detection: {e}")
        results["checks_performed"].append(
            {
                "check": "duplicate_detection",
                "error": str(e),
            }
        )

    # 2. Period Validation Check
    try:
        period_result = period_service.validate_period(
            document_period_start=document_period_start,
            document_period_end=document_period_end,
            close_run_period_start=close_run_period_start,
            close_run_period_end=close_run_period_end,
        )
        results["checks_performed"].append(
            {
                "check": "period_validation",
                "result": period_result.__dict__,
            }
        )

        if not period_result.is_valid:
            issue = issue_service.create_issue(
                document_id=document_id,
                issue_type="wrong_period_document",
                severity=DocumentIssueSeverity.BLOCKING,
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
                    "close_run_period_start": (
                        period_result.close_run_period_start.isoformat()
                        if period_result.close_run_period_start is not None
                        else None
                    ),
                    "close_run_period_end": (
                        period_result.close_run_period_end.isoformat()
                        if period_result.close_run_period_end is not None
                        else None
                    ),
                    "validation_method": period_result.validation_method,
                },
                actor_user_id=actor_user_id,
                source_surface=AuditSourceSurface.WORKER,
            )
            results["issues_created"].append(
                {
                    "issue_id": str(issue.id),
                    "issue_type": "wrong_period_document",
                    "severity": issue.severity.value,
                }
            )
            results["passed_all_checks"] = False
    except Exception as e:
        logger.error(f"Error running period validation: {e}")
        results["checks_performed"].append(
            {
                "check": "period_validation",
                "error": str(e),
            }
        )

    # 3. Completeness Check
    try:
        completeness_result = completeness_service.check_completeness(
            close_run_id=str(close_run_id),
        )
        results["checks_performed"].append(
            {
                "check": "completeness_check",
                "result": completeness_result.__dict__,
            }
        )

        if not completeness_result.is_complete:
            # Create completeness issue
            issue = issue_service.create_issue(
                document_id=document_id,
                issue_type="incomplete_documentation",
                severity=DocumentIssueSeverity.WARNING,
                details={
                    "missing_document_types": [
                        dt.value for dt in completeness_result.missing_document_types
                    ],
                    "present_document_types": [
                        dt.value for dt in completeness_result.present_document_types
                    ],
                    "required_document_types": [
                        dt.value for dt in completeness_result.required_document_types
                    ],
                },
                actor_user_id=actor_user_id,
                source_surface=AuditSourceSurface.WORKER,
            )
            results["issues_created"].append(
                {
                    "issue_id": str(issue.id),
                    "issue_type": "incomplete_documentation",
                    "severity": issue.severity.value,
                }
            )
            # Completeness issues are warnings, not blocking for workflow progression.

    except Exception as e:
        logger.error(f"Error running completeness check: {e}")
        results["checks_performed"].append(
            {
                "check": "completeness_check",
                "error": str(e),
            }
        )

    # 4. Auto Transaction-Linking Check
    try:
        transaction_match_result = transaction_matcher.evaluate_and_persist(
            close_run_id=close_run_id,
            document_id=document_id,
        )
        results["checks_performed"].append(
            {
                "check": "transaction_linking",
                "result": transaction_match_result.to_payload(),
            }
        )
        results["transaction_match"] = transaction_match_result.to_payload()

        if transaction_match_result.should_block_collection:
            issue = issue_service.create_issue(
                document_id=document_id,
                issue_type="transaction_mismatch",
                severity=DocumentIssueSeverity.BLOCKING,
                details={
                    "reason": transaction_match_result.primary_reason,
                    "auto_transaction_match": transaction_match_result.to_payload(),
                },
                actor_user_id=actor_user_id,
                source_surface=AuditSourceSurface.WORKER,
            )
            results["issues_created"].append(
                {
                    "issue_id": str(issue.id),
                    "issue_type": "transaction_mismatch",
                    "severity": issue.severity.value,
                }
            )
            results["passed_all_checks"] = False
        else:
            for existing_issue in issue_service.get_document_issues(document_id=document_id):
                if (
                    existing_issue.status.value == "open"
                    and existing_issue.issue_type == "transaction_mismatch"
                ):
                    issue_service.resolve_issue(
                        issue_id=existing_issue.id,
                        resolution_details={
                            "resolution_reason": transaction_match_result.primary_reason,
                            "auto_transaction_match": transaction_match_result.to_payload(),
                        },
                        actor_user_id=actor_user_id,
                        source_surface=AuditSourceSurface.WORKER,
                    )
    except Exception as e:
        logger.error(f"Error running auto transaction-linking: {e}")
        results["checks_performed"].append(
            {
                "check": "transaction_linking",
                "error": str(e),
            }
        )

    logger.info(
        "Completed document quality checks for document %s. Issues created: %s",
        document_id,
        len(results["issues_created"]),
    )
    return results


__all__ = ["run_document_quality_checks"]
