"""
Purpose: Implement document issue creation and management for intake workflows.
Scope: Create, update, and resolve document-level issues that block workflow progress.
Dependencies: Document repository, issue models, and audit service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from services.audit.service import AuditService
from services.common.enums import (
    DocumentIssueSeverity,
    DocumentIssueStatus,
)
from services.db.models.audit import AuditSourceSurface
from services.db.models.close_run import CloseRun
from services.db.models.documents import Document, DocumentIssue
from services.db.repositories.document_repo import DocumentRepository
from sqlalchemy import select
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class DocumentIssueRecord:
    """Immutable record of a document issue."""

    id: UUID
    document_id: UUID
    issue_type: str
    severity: DocumentIssueSeverity
    status: DocumentIssueStatus
    details: dict
    assigned_to_user_id: UUID | None
    resolved_by_user_id: UUID | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DocumentIssueServiceProtocol(Protocol):
    """Protocol for document issue service."""

    def create_issue(
        self,
        *,
        document_id: UUID,
        issue_type: str,
        severity: DocumentIssueSeverity,
        details: dict,
        actor_user_id: UUID | None = None,
        source_surface: AuditSourceSurface,
    ) -> DocumentIssueRecord:
        """Create a new document issue."""
        ...

    def resolve_issue(
        self,
        *,
        issue_id: UUID,
        resolution_details: dict,
        actor_user_id: UUID,
        source_surface: AuditSourceSurface,
    ) -> DocumentIssueRecord:
        """Resolve an existing document issue."""
        ...

    def get_document_issues(
        self,
        *,
        document_id: UUID,
        status: DocumentIssueStatus | None = None,
    ) -> list[DocumentIssueRecord]:
        """Get all issues for a document."""
        ...


class DocumentIssueService:
    """Service for creating and managing document issues."""

    def __init__(
        self,
        *,
        db_session: Session,
        document_repo: DocumentRepository,
    ) -> None:
        """Initialize with required dependencies."""
        self._db_session = db_session
        self._document_repo = document_repo
        self._audit_service = AuditService(db_session=db_session)

    def create_issue(
        self,
        *,
        document_id: UUID,
        issue_type: str,
        severity: DocumentIssueSeverity,
        details: dict,
        actor_user_id: UUID | None = None,
        source_surface: AuditSourceSurface,
    ) -> DocumentIssueRecord:
        """
        Create a new document issue.

        Args:
            document_id: ID of the document to associate the issue with
            issue_type: Type of issue (e.g., 'duplicate', 'wrong_period')
            severity: Severity level of the issue
            details: Additional details about the issue
            actor_user_id: User creating the issue (optional)
            source_surface: Surface where the issue was created

        Returns:
            DocumentIssueRecord of the created issue
        """
        # Create the issue record
        issue = DocumentIssue(
            document_id=document_id,
            issue_type=issue_type,
            severity=severity.value,
            status=DocumentIssueStatus.OPEN.value,
            details=details,
            assigned_to_user_id=actor_user_id,
            resolved_by_user_id=None,
            resolved_at=None,
        )

        self._db_session.add(issue)
        self._db_session.flush()

        # Emit audit event
        self._audit_service.emit_audit_event(
            entity_id=self._get_entity_id_from_document(document_id),
            close_run_id=self._get_close_run_id_from_document(document_id),
            event_type="document_issue.created",
            actor_user_id=actor_user_id,
            source_surface=source_surface,
            payload={
                "issue_id": str(issue.id),
                "document_id": str(document_id),
                "issue_type": issue_type,
                "severity": severity.value,
                "details": details,
            },
        )

        return DocumentIssueRecord(
            id=issue.id,
            document_id=issue.document_id,
            issue_type=issue.issue_type,
            severity=DocumentIssueSeverity(issue.severity),
            status=DocumentIssueStatus(issue.status),
            details=issue.details,
            assigned_to_user_id=issue.assigned_to_user_id,
            resolved_by_user_id=issue.resolved_by_user_id,
            resolved_at=issue.resolved_at,
            created_at=issue.created_at,
            updated_at=issue.updated_at,
        )

    def resolve_issue(
        self,
        *,
        issue_id: UUID,
        resolution_details: dict,
        actor_user_id: UUID,
        source_surface: AuditSourceSurface,
    ) -> DocumentIssueRecord:
        """
        Resolve an existing document issue.

        Args:
            issue_id: ID of the issue to resolve
            resolution_details: Details about how the issue was resolved
            actor_user_id: User resolving the issue
            source_surface: Surface where the resolution occurred

        Returns:
            DocumentIssueRecord of the resolved issue
        """
        # Load the issue
        issue = self._db_session.get(DocumentIssue, issue_id)
        if issue is None:
            raise ValueError(f"Issue {issue_id} not found")

        # Update the issue
        issue.status = DocumentIssueStatus.RESOLVED.value
        issue.resolved_by_user_id = actor_user_id
        issue.resolved_at = datetime.utcnow()
        # Merge resolution details with existing details
        issue.details = {**issue.details, **resolution_details}

        self._db_session.flush()

        # Emit audit event
        self._audit_service.emit_audit_event(
            entity_id=self._get_entity_id_from_document(issue.document_id),
            close_run_id=self._get_close_run_id_from_document(issue.document_id),
            event_type="document_issue.resolved",
            actor_user_id=actor_user_id,
            source_surface=source_surface,
            payload={
                "issue_id": str(issue.id),
                "document_id": str(issue.document_id),
                "issue_type": issue.issue_type,
                "resolution_details": resolution_details,
            },
        )

        return DocumentIssueRecord(
            id=issue.id,
            document_id=issue.document_id,
            issue_type=issue.issue_type,
            severity=DocumentIssueSeverity(issue.severity),
            status=DocumentIssueStatus(issue.status),
            details=issue.details,
            assigned_to_user_id=issue.assigned_to_user_id,
            resolved_by_user_id=issue.resolved_by_user_id,
            resolved_at=issue.resolved_at,
            created_at=issue.created_at,
            updated_at=issue.updated_at,
        )

    def get_document_issues(
        self,
        *,
        document_id: UUID,
        status: DocumentIssueStatus | None = None,
    ) -> list[DocumentIssueRecord]:
        """
        Get all issues for a document.

        Args:
            document_id: ID of the document to get issues for
            status: Optional status filter

        Returns:
            List of DocumentIssueRecord objects
        """
        from sqlalchemy import select

        statement = select(DocumentIssue).where(DocumentIssue.document_id == document_id)
        if status is not None:
            statement = statement.where(DocumentIssue.status == status.value)

        issues = self._db_session.scalars(statement).all()

        return [
            DocumentIssueRecord(
                id=issue.id,
                document_id=issue.document_id,
                issue_type=issue.issue_type,
                severity=DocumentIssueSeverity(issue.severity),
                status=DocumentIssueStatus(issue.status),
                details=issue.details,
                assigned_to_user_id=issue.assigned_to_user_id,
                resolved_by_user_id=issue.resolved_by_user_id,
                resolved_at=issue.resolved_at,
                created_at=issue.created_at,
                updated_at=issue.updated_at,
            )
            for issue in issues
        ]

    def _get_entity_id_from_document(self, document_id: UUID) -> UUID:
        """Get entity ID from document ID."""

        statement = (
            select(CloseRun.entity_id)
            .select_from(Document)
            .join(CloseRun, CloseRun.id == Document.close_run_id)
            .where(Document.id == document_id)
        )
        entity_id = self._db_session.execute(statement).scalar_one()
        return entity_id

    def _get_close_run_id_from_document(self, document_id: UUID) -> UUID:
        """Get close run ID from document ID."""
        statement = select(Document.close_run_id).where(Document.id == document_id)
        close_run_id = self._db_session.execute(statement).scalar_one()
        return close_run_id


__all__ = ["DocumentIssueRecord", "DocumentIssueService", "DocumentIssueServiceProtocol"]
