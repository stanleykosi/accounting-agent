"""
Purpose: Implement document completeness checks for close-run workflows.
Scope: Validate that all required document types are present for a close run.
Dependencies: Document repository, close run models, and document type enums.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Set

from services.common.enums import DocumentType
from services.db.repositories.document_repo import DocumentRepository


@dataclass(frozen=True, slots=True)
class CompletenessCheckResult:
    """Result of document completeness check."""

    is_complete: bool
    missing_document_types: Set[DocumentType]
    present_document_types: Set[DocumentType]
    required_document_types: Set[DocumentType]


class CompletenessCheckProtocol(Protocol):
    """Protocol for document completeness check service."""

    def check_completeness(
        self,
        *,
        close_run_id: str,
        required_document_types: Set[DocumentType] | None = None,
    ) -> CompletenessCheckResult:
        """Check if all required document types are present in the close run."""
        ...


class CompletenessCheckService:
    """Service for checking document type completeness in close runs."""

    # Default required document types for a basic accounting close run
    DEFAULT_REQUIRED_DOCUMENT_TYPES = {
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
        required_document_types: Set[DocumentType] | None = None,
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

        # In a real implementation, we would query the database for documents
        # in the close run and check their document types
        # For now, we'll return a basic implementation

        # This is a simplified implementation - in production we'd query the database
        # for documents in the close run and extract their document types

        # For demonstration, let's assume we have some documents
        present_document_types: Set[DocumentType] = set()

        # TODO: Implement actual database query to get document types for close run
        # For now, we'll return incomplete status to demonstrate the structure

        missing_document_types = required_document_types - present_document_types

        return CompletenessCheckResult(
            is_complete=len(missing_document_types) == 0,
            missing_document_types=missing_document_types,
            present_document_types=present_document_types,
            required_document_types=required_document_types,
        )


__all__ = ["CompletenessCheckResult", "CompletenessCheckProtocol", "CompletenessCheckService"]
