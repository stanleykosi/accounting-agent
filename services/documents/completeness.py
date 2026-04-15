"""
Purpose: Implement document completeness checks for close-run workflows.
Scope: Validate that all required document types are present for a close run.
Dependencies: Document repository, close run models, and document type enums.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Protocol
from uuid import UUID

from services.common.enums import DocumentStatus, DocumentType
from services.db.repositories.document_repo import DocumentRepository


@dataclass(frozen=True, slots=True)
class CompletenessCheckResult:
    """Result of document completeness check."""

    is_complete: bool
    missing_document_types: set[DocumentType]
    present_document_types: set[DocumentType]
    required_document_types: set[DocumentType]


class CompletenessCheckProtocol(Protocol):
    """Protocol for document completeness check service."""

    def check_completeness(
        self,
        *,
        close_run_id: str,
        required_document_types: set[DocumentType] | None = None,
    ) -> CompletenessCheckResult:
        """Check if all required document types are present in the close run."""
        ...


class CompletenessCheckService:
    """Service for checking document type completeness in close runs."""

    # Default required document types for a basic accounting close run
    DEFAULT_REQUIRED_DOCUMENT_TYPES: ClassVar[set[DocumentType]] = {
        DocumentType.INVOICE,
        DocumentType.BANK_STATEMENT,
        DocumentType.RECEIPT,
    }

    def __init__(
        self,
        *,
        document_repo: DocumentRepository,
    ) -> None:
        """Initialize with required dependencies."""
        self._document_repo = document_repo

    def check_completeness(
        self,
        *,
        close_run_id: str,
        required_document_types: set[DocumentType] | None = None,
    ) -> CompletenessCheckResult:
        """
        Check if all required document types are present in the close run.

        Args:
            close_run_id: Close run ID to check
            required_document_types: Set of document types that are required.
                                   If None, uses default required types.

        Returns:
            CompletenessCheckResult indicating completeness status
        """
        if required_document_types is None:
            required_document_types = self.DEFAULT_REQUIRED_DOCUMENT_TYPES

        documents = self._document_repo.list_documents_for_close_run(
            close_run_id=UUID(close_run_id)
        )
        present_document_types: set[DocumentType] = set()
        ignored_statuses = {DocumentStatus.REJECTED, DocumentStatus.DUPLICATE}

        for document in documents:
            try:
                document_type = DocumentType(document.document_type)
                document_status = DocumentStatus(document.status)
            except ValueError:
                continue

            if document_type is DocumentType.UNKNOWN or document_status in ignored_statuses:
                continue
            present_document_types.add(document_type)

        missing_document_types = required_document_types - present_document_types

        return CompletenessCheckResult(
            is_complete=len(missing_document_types) == 0,
            missing_document_types=missing_document_types,
            present_document_types=present_document_types,
            required_document_types=required_document_types,
        )


__all__ = ["CompletenessCheckProtocol", "CompletenessCheckResult", "CompletenessCheckService"]
