"""
Purpose: Define the canonical domain enums that anchor workflow, lifecycle,
review, autonomy, and artifact language across the backend.
Scope: String enums shared by API routes, persistence models, worker logic,
contract models, and the future desktop and CLI surfaces.
Dependencies: Python's enum module only, so these values stay importable from
low-level modules without creating circular dependencies.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self


class CanonicalDomainEnum(StrEnum):
    """Attach human-readable labels and descriptions to canonical string enums."""

    label: str
    description: str

    def __new__(cls, value: str, label: str, description: str) -> Self:
        """Create a string enum member with stable render metadata."""

        enum_member = str.__new__(cls, value)
        enum_member._value_ = value
        enum_member.label = label
        enum_member.description = description
        return enum_member

    @classmethod
    def values(cls) -> tuple[str, ...]:
        """Return the canonical serialized values in declaration order."""

        return tuple(member.value for member in cls)


class WorkflowPhase(CanonicalDomainEnum):
    """Enumerate the non-negotiable five-phase accounting workflow backbone."""

    COLLECTION = (
        "collection",
        "Collection",
        "Collect required source documents and validate that the close run can proceed.",
    )
    PROCESSING = (
        "processing",
        "Processing",
        "Parse documents, extract fields, and draft accounting recommendations with evidence.",
    )
    RECONCILIATION = (
        "reconciliation",
        "Reconciliation",
        "Resolve matches, exceptions, and control checks before reports are prepared.",
    )
    REPORTING = (
        "reporting",
        "Reporting",
        "Generate the required statements, schedules, commentary, and export-ready outputs.",
    )
    REVIEW_SIGNOFF = (
        "review_signoff",
        "Review / Sign-off",
        "Capture reviewer decisions, sign-off records, and release controls for the period.",
    )


class CloseRunStatus(CanonicalDomainEnum):
    """Enumerate the lifecycle states of a close run."""

    DRAFT = (
        "draft",
        "Draft",
        "The close run is being assembled and has not entered formal review yet.",
    )
    IN_REVIEW = (
        "in_review",
        "In review",
        "The close run is active and waiting on reviewer actions or unresolved issues.",
    )
    APPROVED = (
        "approved",
        "Approved",
        "All required review decisions were recorded and the close run was signed off.",
    )
    EXPORTED = (
        "exported",
        "Exported",
        "Release artifacts or export-ready files were issued for this close run version.",
    )
    ARCHIVED = (
        "archived",
        "Archived",
        "The close run is closed to normal editing and retained for traceable history.",
    )
    REOPENED = (
        "reopened",
        "Reopened",
        "A previously approved or exported period was reopened as a new working state.",
    )


class CloseRunPhaseStatus(CanonicalDomainEnum):
    """Enumerate per-phase progress states tracked within a close run."""

    NOT_STARTED = (
        "not_started",
        "Not started",
        "Work for this phase has not begun yet.",
    )
    IN_PROGRESS = (
        "in_progress",
        "In progress",
        "This phase has active work underway but is not ready to advance.",
    )
    BLOCKED = (
        "blocked",
        "Blocked",
        "This phase cannot advance until an explicit blocking issue is resolved.",
    )
    READY = (
        "ready",
        "Ready",
        "This phase passed its entry checks and is ready for human or system execution.",
    )
    COMPLETED = (
        "completed",
        "Completed",
        "This phase finished and the close run can move to the next gate.",
    )


class JobStatus(CanonicalDomainEnum):
    """Enumerate canonical background-job states used across API and worker services."""

    QUEUED = (
        "queued",
        "Queued",
        "The job was accepted and is waiting for worker capacity.",
    )
    RUNNING = (
        "running",
        "Running",
        "The worker is actively executing the job.",
    )
    BLOCKED = (
        "blocked",
        "Blocked",
        "The job is paused on a dependency, input issue, or manual recovery step.",
    )
    FAILED = (
        "failed",
        "Failed",
        "The job stopped with an error and requires explicit retry or intervention.",
    )
    CANCELED = (
        "canceled",
        "Canceled",
        "Execution was intentionally stopped before normal completion.",
    )
    COMPLETED = (
        "completed",
        "Completed",
        "The job finished successfully and its outputs are ready for use.",
    )


class AutonomyMode(CanonicalDomainEnum):
    """Enumerate the user-controlled routing modes for AI-suggested changes."""

    HUMAN_REVIEW = (
        "human_review",
        "Human review",
        "Suggested changes must wait for explicit human approval before they apply.",
    )
    REDUCED_INTERRUPTION = (
        "reduced_interruption",
        "Reduced interruption",
        "Low-risk changes may update working state after policy checks while staying audited.",
    )


class ReviewStatus(CanonicalDomainEnum):
    """Enumerate the lifecycle states of reviewable changes such as recommendations."""

    DRAFT = (
        "draft",
        "Draft",
        "The item exists as a working proposal that has not entered review routing yet.",
    )
    PENDING_REVIEW = (
        "pending_review",
        "Pending review",
        "The item is waiting for a reviewer because autonomy or policy prevented direct apply.",
    )
    APPROVED = (
        "approved",
        "Approved",
        "A reviewer accepted the item and it is eligible for downstream materialization.",
    )
    REJECTED = (
        "rejected",
        "Rejected",
        "A reviewer declined the item and it should not affect current working state.",
    )
    SUPERSEDED = (
        "superseded",
        "Superseded",
        "A newer revision replaced this item before it reached a terminal outcome.",
    )
    APPLIED = (
        "applied",
        "Applied",
        "The reviewed item was committed into working accounting state with lineage preserved.",
    )


class ArtifactType(CanonicalDomainEnum):
    """Enumerate the released artifact categories linked to close run versions."""

    REPORT_EXCEL = (
        "report_excel",
        "Excel report pack",
        "Accountant-ready Excel workbook pack generated for a close run version.",
    )
    REPORT_PDF = (
        "report_pdf",
        "PDF report pack",
        "Executive-ready PDF management report pack generated for a close run version.",
    )
    AUDIT_TRAIL = (
        "audit_trail",
        "Audit trail export",
        "Immutable approval, override, and change-history export for a close run.",
    )
    EVIDENCE_PACK = (
        "evidence_pack",
        "Evidence pack",
        "Bundle of source references, extracted values, approvals, diffs, and outputs.",
    )
    QUICKBOOKS_EXPORT = (
        "quickbooks_export",
        "QuickBooks export file",
        "Stable export-ready file prepared for accountant upload into QuickBooks Online.",
    )


class OwnershipTargetType(CanonicalDomainEnum):
    """Enumerate the business objects that can carry ownership and in-progress locks."""

    ENTITY = (
        "entity",
        "Entity",
        "Entity workspace touched by an operator.",
    )
    CLOSE_RUN = (
        "close_run",
        "Close run",
        "Period close run touched or locked during workflow review.",
    )
    DOCUMENT = (
        "document",
        "Document",
        "Source document under collection, extraction, or review.",
    )
    RECOMMENDATION = (
        "recommendation",
        "Recommendation",
        "Accounting recommendation or journal proposal under review.",
    )
    REVIEW_TARGET = (
        "review_target",
        "Review target",
        "Generic reviewable item awaiting an accountant disposition.",
    )


class DocumentSourceChannel(CanonicalDomainEnum):
    """Enumerate the canonical ways source documents can enter a close run."""

    UPLOAD = (
        "upload",
        "Upload",
        "Primary file-ingestion path for accountant-provided source documents.",
    )
    API_IMPORT = (
        "api_import",
        "API import",
        "Secondary ingestion path for bank or integration-provided source documents.",
    )
    MANUAL_ENTRY = (
        "manual_entry",
        "Manual entry",
        "Operator-created source record without an uploaded binary payload.",
    )


class DocumentStatus(CanonicalDomainEnum):
    """Enumerate the lifecycle states of a document attached to a close run."""

    UPLOADED = (
        "uploaded",
        "Uploaded",
        "The source file was stored and is waiting for parsing or quality checks.",
    )
    PROCESSING = (
        "processing",
        "Processing",
        "The document is being parsed, normalized, OCR processed, or extracted.",
    )
    PARSED = (
        "parsed",
        "Parsed",
        "The document was parsed successfully and can feed downstream extraction.",
    )
    NEEDS_REVIEW = (
        "needs_review",
        "Needs review",
        "The document has low-confidence or exception signals requiring review.",
    )
    APPROVED = (
        "approved",
        "Approved",
        "A reviewer accepted the document's current extracted state.",
    )
    REJECTED = (
        "rejected",
        "Rejected",
        "A reviewer rejected the document for this close run.",
    )
    FAILED = (
        "failed",
        "Failed",
        "Processing failed with an explicit recovery reason.",
    )
    DUPLICATE = (
        "duplicate",
        "Duplicate",
        "The document appears to duplicate another source and needs disposition.",
    )
    BLOCKED = (
        "blocked",
        "Blocked",
        "The document cannot proceed until an explicit input issue is resolved.",
    )


class DocumentType(CanonicalDomainEnum):
    """Enumerate document classifications supported by the intake pipeline."""

    UNKNOWN = (
        "unknown",
        "Unknown",
        "Document type has not yet been classified by deterministic or review logic.",
    )
    INVOICE = (
        "invoice",
        "Invoice",
        "Vendor or customer invoice requiring extraction and accounting treatment.",
    )
    BANK_STATEMENT = (
        "bank_statement",
        "Bank statement",
        "Bank or card statement used for reconciliation and support.",
    )
    PAYSLIP = (
        "payslip",
        "Payslip",
        "Payroll support document used for payroll control and review.",
    )
    RECEIPT = (
        "receipt",
        "Receipt",
        "Payment receipt or expense support document.",
    )
    CONTRACT = (
        "contract",
        "Contract",
        "Contract or agreement used for accounting evidence and recognition checks.",
    )


class DocumentIssueSeverity(CanonicalDomainEnum):
    """Enumerate review severity levels for document issues."""

    INFO = (
        "info",
        "Info",
        "Informational issue that does not block workflow progression.",
    )
    WARNING = (
        "warning",
        "Warning",
        "Review issue that should be dispositioned but is not necessarily blocking.",
    )
    BLOCKING = (
        "blocking",
        "Blocking",
        "Issue that prevents workflow progression until resolved or dismissed.",
    )


class DocumentIssueStatus(CanonicalDomainEnum):
    """Enumerate the lifecycle states of a document issue."""

    OPEN = (
        "open",
        "Open",
        "The issue is active and awaiting system or reviewer disposition.",
    )
    RESOLVED = (
        "resolved",
        "Resolved",
        "The issue was remediated and no longer blocks its target.",
    )
    DISMISSED = (
        "dismissed",
        "Dismissed",
        "A reviewer dismissed the issue with tracked context.",
    )


CANONICAL_WORKFLOW_PHASES: tuple[WorkflowPhase, ...] = tuple(WorkflowPhase)

__all__ = [
    "CANONICAL_WORKFLOW_PHASES",
    "ArtifactType",
    "AutonomyMode",
    "CanonicalDomainEnum",
    "CloseRunPhaseStatus",
    "CloseRunStatus",
    "DocumentIssueSeverity",
    "DocumentIssueStatus",
    "DocumentSourceChannel",
    "DocumentStatus",
    "DocumentType",
    "JobStatus",
    "OwnershipTargetType",
    "ReviewStatus",
    "WorkflowPhase",
]
