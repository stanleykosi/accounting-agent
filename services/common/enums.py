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

    def __new__(
        cls,
        value: str,
        label: str = "",
        description: str = "",
    ) -> Self:
        """Create a string enum member with stable render metadata."""

        if not label or not description:
            raise TypeError("Canonical domain enum members require label and description.")

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

    GENERAL_LEDGER_EXPORT = (
        "general_ledger_export",
        "General ledger export",
        (
            "Close-run effective ledger export combining any imported GL baseline with "
            "current-run adjustments."
        ),
    )
    GL_POSTING_PACKAGE = (
        "gl_posting_package",
        "GL posting package",
        "CSV or workbook package prepared for external ERP or GL journal import.",
    )
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


class AccountType(CanonicalDomainEnum):
    """Enumerate canonical GL account families used by deterministic accounting rules."""

    ASSET = (
        "asset",
        "Asset",
        "Resource controlled by the entity, including bank, receivable, inventory, and PPE.",
    )
    LIABILITY = (
        "liability",
        "Liability",
        "Present obligation such as payable, accrual, loan, tax, or payroll control accounts.",
    )
    EQUITY = (
        "equity",
        "Equity",
        "Owner residual interest, share capital, retained earnings, and reserves.",
    )
    REVENUE = (
        "revenue",
        "Revenue",
        "Income from ordinary trading or service activities.",
    )
    COST_OF_SALES = (
        "cost_of_sales",
        "Cost of sales",
        "Direct costs matched to revenue generation.",
    )
    EXPENSE = (
        "expense",
        "Expense",
        "Operating or administrative expense accounts.",
    )
    OTHER_INCOME = (
        "other_income",
        "Other income",
        "Income outside ordinary trading or service activities.",
    )
    OTHER_EXPENSE = (
        "other_expense",
        "Other expense",
        "Expense outside ordinary operating activities.",
    )


class RiskLevel(CanonicalDomainEnum):
    """Enumerate deterministic risk bands used by policy gates and review routing."""

    LOW = (
        "low",
        "Low",
        "The accounting action is low-risk and may follow standard approval routing.",
    )
    MEDIUM = (
        "medium",
        "Medium",
        "The accounting action needs reviewer attention before export or direct application.",
    )
    HIGH = (
        "high",
        "High",
        "The accounting action must be blocked from automatic application and explicitly reviewed.",
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


class ReconciliationType(CanonicalDomainEnum):
    """Enumerate the canonical reconciliation categories within a close run."""

    BANK_RECONCILIATION = (
        "bank_reconciliation",
        "Bank reconciliation",
        "Match bank statement lines to ledger transactions and resolve differences.",
    )
    AR_AGEING = (
        "ar_ageing",
        "AR ageing",
        "Accounts receivable ageing analysis and collectability review.",
    )
    AP_AGEING = (
        "ap_ageing",
        "AP ageing",
        "Accounts payable ageing analysis and obligation review.",
    )
    INTERCOMPANY = (
        "intercompany",
        "Intercompany",
        "Intercompany balance matching and elimination across related entities.",
    )
    PAYROLL_CONTROL = (
        "payroll_control",
        "Payroll control",
        "Payroll totals, deductions, and statutory contribution reconciliation.",
    )
    FIXED_ASSETS = (
        "fixed_assets",
        "Fixed assets",
        "Fixed asset register verification, depreciation, and disposals review.",
    )
    LOAN_AMORTISATION = (
        "loan_amortisation",
        "Loan amortisation",
        "Loan balance, interest accrual, and scheduled payment reconciliation.",
    )
    ACCRUAL_TRACKER = (
        "accrual_tracker",
        "Accrual tracker",
        "Accrued income and expense tracking against expected obligations.",
    )
    BUDGET_VS_ACTUAL = (
        "budget_vs_actual",
        "Budget vs actual",
        "Comparison of budgeted amounts to actual ledger postings with variance analysis.",
    )
    TRIAL_BALANCE = (
        "trial_balance",
        "Trial balance",
        "Debit-equals-credit verification and anomaly detection across all accounts.",
    )


DEFAULT_RECONCILIATION_EXECUTION_TYPES = (
    ReconciliationType.BANK_RECONCILIATION,
    ReconciliationType.FIXED_ASSETS,
    ReconciliationType.LOAN_AMORTISATION,
    ReconciliationType.ACCRUAL_TRACKER,
    ReconciliationType.BUDGET_VS_ACTUAL,
    ReconciliationType.TRIAL_BALANCE,
)


class SupportingScheduleType(CanonicalDomainEnum):
    """Enumerate the standalone supporting schedules maintained during Step 6."""

    FIXED_ASSETS = (
        "fixed_assets",
        "Fixed asset register",
        "Maintain the fixed asset register, depreciation, disposals, and book values.",
    )
    LOAN_AMORTISATION = (
        "loan_amortisation",
        "Loan amortisation",
        "Maintain lender schedules, payment sequencing, balances, and interest allocations.",
    )
    ACCRUAL_TRACKER = (
        "accrual_tracker",
        "Accrual tracker",
        "Maintain accrued income and expense schedules across periods and reversals.",
    )
    BUDGET_VS_ACTUAL = (
        "budget_vs_actual",
        "Budget vs actual",
        "Maintain the budget workpaper used for variance analysis against ledger actuals.",
    )


class SupportingScheduleStatus(CanonicalDomainEnum):
    """Enumerate the lifecycle states of a standalone supporting schedule."""

    DRAFT = (
        "draft",
        "Draft",
        "The schedule exists but has not entered formal review yet.",
    )
    IN_REVIEW = (
        "in_review",
        "In review",
        "The schedule has content and is waiting for accountant review or completion.",
    )
    APPROVED = (
        "approved",
        "Approved",
        "The schedule was reviewed and accepted for the current close run.",
    )
    NOT_APPLICABLE = (
        "not_applicable",
        "Not applicable",
        "The schedule is explicitly not required for this entity or reporting period.",
    )


class MatchStatus(CanonicalDomainEnum):
    """Enumerate the matching outcomes for reconciliation items."""

    MATCHED = (
        "matched",
        "Matched",
        "The item was matched exactly to its counterpart with no unresolved differences.",
    )
    PARTIALLY_MATCHED = (
        "partially_matched",
        "Partially matched",
        "The item was matched with minor differences that may need reviewer explanation.",
    )
    UNMATCHED = (
        "unmatched",
        "Unmatched",
        "No counterpart was found and the item remains open for investigation.",
    )
    EXCEPTION = (
        "exception",
        "Exception",
        "A counterpart was found but significant differences require explicit resolution.",
    )


class DispositionAction(CanonicalDomainEnum):
    """Enumerate the reviewer disposition choices for unresolved reconciliation items."""

    RESOLVED = (
        "resolved",
        "Resolved",
        "The reviewer confirmed the item is correct or no longer requires action.",
    )
    ADJUSTED = (
        "adjusted",
        "Adjusted",
        "The reviewer made or requested an adjusting entry to correct the item.",
    )
    ACCEPTED_AS_IS = (
        "accepted_as_is",
        "Accepted as-is",
        "The reviewer accepted the unmatched or exception state with documented reasoning.",
    )
    ESCALATED = (
        "escalated",
        "Escalated",
        "The item was escalated to a senior reviewer or external party for resolution.",
    )
    PENDING_INFO = (
        "pending_info",
        "Pending info",
        "The item is waiting on additional information before a final disposition.",
    )


class ReconciliationStatus(CanonicalDomainEnum):
    """Enumerate the lifecycle states of a reconciliation object."""

    DRAFT = (
        "draft",
        "Draft",
        "The reconciliation is being assembled and matching has not completed.",
    )
    IN_REVIEW = (
        "in_review",
        "In review",
        "Matching is complete and the reconciliation awaits reviewer disposition of exceptions.",
    )
    APPROVED = (
        "approved",
        "Approved",
        "All required dispositions were recorded and the reconciliation is accepted.",
    )
    BLOCKED = (
        "blocked",
        "Blocked",
        "The reconciliation cannot proceed due to unresolved blocking exceptions.",
    )


class AnomalyType(CanonicalDomainEnum):
    """Enumerate trial-balance and reconciliation anomaly categories."""

    DEBIT_CREDIT_IMBALANCE = (
        "debit_credit_imbalance",
        "Debit/credit imbalance",
        "Total debits do not equal total credits for the close run.",
    )
    UNUSUAL_ACCOUNT_BALANCE = (
        "unusual_account_balance",
        "Unusual account balance",
        "An account shows a balance direction unexpected for its account type.",
    )
    UNEXPLAINED_VARIANCE = (
        "unexplained_variance",
        "Unexplained variance",
        "Month-over-month variance exceeds the configured threshold without explanation.",
    )
    ZERO_BALANCE_ACTIVE = (
        "zero_balance_active",
        "Zero balance on active account",
        "A normally active account shows zero balance and may need investigation.",
    )
    ROUNDING_DIFFERENCE = (
        "rounding_difference",
        "Rounding difference",
        "A small imbalance likely caused by decimal rounding in currency conversion.",
    )
    MISSING_ACCOUNT = (
        "missing_account",
        "Missing account",
        "An expected account from the chart of accounts has no balance in the trial balance.",
    )


class ReportSectionKey(CanonicalDomainEnum):
    """Enumerate mandatory report section identifiers enforced by template guardrails.

    These sections correspond to the required reporting outputs defined in the
    5-phase accounting workflow backbone and must never be removed by custom
    templates or entity-level overrides.
    """

    PROFIT_AND_LOSS = (
        "profit_and_loss",
        "Profit and Loss",
        "Income statement showing revenue, costs, and net profit for the period.",
    )
    BALANCE_SHEET = (
        "balance_sheet",
        "Balance Sheet",
        "Statement of assets, liabilities, and equity at period end.",
    )
    CASH_FLOW = (
        "cash_flow",
        "Cash Flow",
        "Cash movement summary showing operating, investing, and financing flows.",
    )
    BUDGET_VARIANCE = (
        "budget_variance",
        "Budget Variance Analysis",
        "Comparison of budgeted amounts to actual results with variance commentary.",
    )
    KPI_DASHBOARD = (
        "kpi_dashboard",
        "KPI Dashboard",
        "Key performance indicators and financial metrics for management review.",
    )


class ReconciliationSourceType(CanonicalDomainEnum):
    """Enumerate the sources that feed into reconciliation items."""

    BANK_STATEMENT_LINE = (
        "bank_statement_line",
        "Bank statement line",
        "A transaction line extracted from an uploaded or imported bank statement.",
    )
    LEDGER_TRANSACTION = (
        "ledger_transaction",
        "Ledger transaction",
        "A posted journal line or transaction from the close run ledger.",
    )
    RECOMMENDATION = (
        "recommendation",
        "Recommendation",
        "An accounting recommendation that contributes to reconciliation balances.",
    )
    EXTERNAL_BALANCE = (
        "external_balance",
        "External balance",
        "A balance imported from an external source such as a counter-entity or bank feed.",
    )
    MANUAL_ADJUSTMENT = (
        "manual_adjustment",
        "Manual adjustment",
        "A reviewer-created adjustment to reconcile differences.",
    )


# Keep existing exports plus new reconciliation enums
CANONICAL_WORKFLOW_PHASES: tuple[WorkflowPhase, ...] = tuple(WorkflowPhase)

__all__ = [
    "AccountType",
    "AnomalyType",
    "ArtifactType",
    "AutonomyMode",
    "CanonicalDomainEnum",
    "CloseRunPhaseStatus",
    "CloseRunStatus",
    "DispositionAction",
    "DocumentIssueSeverity",
    "DocumentIssueStatus",
    "DocumentSourceChannel",
    "DocumentStatus",
    "DocumentType",
    "JobStatus",
    "MatchStatus",
    "OwnershipTargetType",
    "ReconciliationSourceType",
    "ReconciliationStatus",
    "ReconciliationType",
    "ReportSectionKey",
    "ReviewStatus",
    "RiskLevel",
    "WorkflowPhase",
]
