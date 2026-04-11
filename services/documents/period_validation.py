"""
Purpose: Implement period validation for document intake workflows.
Scope: Accounting period alignment validation for uploaded documents.
Dependencies: Document repository, close run models, and date utilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from services.db.repositories.document_repo import DocumentRepository


@dataclass(frozen=True, slots=True)
class PeriodValidationResult:
    """Result of period validation check."""

    is_valid: bool
    document_period_start: date | None = None
    document_period_end: date | None = None
    close_run_period_start: date | None = None
    close_run_period_end: date | None = None
    validation_method: str = "period_overlap_check"


class PeriodValidationProtocol(Protocol):
    """Protocol for period validation service."""

    def validate_period(
        self,
        *,
        document_period_start: date | None,
        document_period_end: date | None,
        close_run_period_start: date,
        close_run_period_end: date,
    ) -> PeriodValidationResult:
        """Validate document period alignment with close run period."""
        ...


class PeriodValidationService:
    """Service for validating document period alignment with close run periods."""

    def __init__(
        self,
        *,
        document_repo: DocumentRepository,
    ) -> None:
        """Initialize with required dependencies."""
        self._document_repo = document_repo

    def validate_period(
        self,
        *,
        document_period_start: date | None,
        document_period_end: date | None,
        close_run_period_start: date,
        close_run_period_end: date,
    ) -> PeriodValidationResult:
        """
        Validate that document period aligns with close run period.

        Args:
            document_period_start: Detected document period start (can be None)
            document_period_end: Detected document period end (can be None)
            close_run_period_start: Close run period start
            close_run_period_end: Close run period end

        Returns:
            PeriodValidationResult indicating if period alignment is valid
        """
        # If document period cannot be detected, we cannot validate
        if document_period_start is None or document_period_end is None:
            return PeriodValidationResult(
                is_valid=False,
                document_period_start=document_period_start,
                document_period_end=document_period_end,
                close_run_period_start=close_run_period_start,
                close_run_period_end=close_run_period_end,
                validation_method="period_overlap_check",
            )

        # Check if document period overlaps with close run period
        # Document is valid if it falls within or overlaps the close run period
        overlaps = (
            document_period_start <= close_run_period_end
            and document_period_end >= close_run_period_start
        )

        return PeriodValidationResult(
            is_valid=overlaps,
            document_period_start=document_period_start,
            document_period_end=document_period_end,
            close_run_period_start=close_run_period_start,
            close_run_period_end=close_run_period_end,
            validation_method="period_overlap_check",
        )


__all__ = ["PeriodValidationResult", "PeriodValidationProtocol", "PeriodValidationService"]
