"""
Purpose: Define strict API contracts for imported ledger baselines and close-run bindings.
Scope: Entity-level general-ledger uploads, trial-balance uploads, workspace reads,
and close-run binding summaries.
Dependencies: Shared contract base model and standard date/time types.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field
from services.contracts.api_models import ContractModel


class CloseRunLedgerBindingSummary(ContractModel):
    """Describe the imported ledger baseline currently bound to one close run."""

    close_run_id: str = Field(description="Owning close-run UUID.")
    general_ledger_import_batch_id: str | None = Field(
        default=None,
        description="Bound general-ledger import batch UUID, if any.",
    )
    trial_balance_import_batch_id: str | None = Field(
        default=None,
        description="Bound trial-balance import batch UUID, if any.",
    )
    binding_source: str = Field(description="How the binding was created: auto or manual.")
    bound_by_user_id: str | None = Field(
        default=None,
        description="User who created the binding, if recorded.",
    )
    created_at: datetime = Field(description="UTC timestamp when the binding was created.")
    updated_at: datetime = Field(description="UTC timestamp when the binding was last changed.")


class GeneralLedgerImportSummary(ContractModel):
    """Describe one uploaded general-ledger baseline."""

    id: str = Field(description="General-ledger import batch UUID.")
    entity_id: str = Field(description="Owning entity UUID.")
    period_start: date = Field(description="First day covered by the imported ledger batch.")
    period_end: date = Field(description="Last day covered by the imported ledger batch.")
    source_format: str = Field(description="Upload format detected for the import batch.")
    uploaded_filename: str = Field(description="Original filename supplied by the operator.")
    row_count: int = Field(ge=1, description="Number of imported ledger rows.")
    imported_by_user_id: str | None = Field(
        default=None,
        description="User who uploaded the batch, if recorded.",
    )
    import_metadata: dict[str, object] = Field(
        default_factory=dict,
        description="Structured import diagnostics and header metadata.",
    )
    created_at: datetime = Field(description="UTC timestamp when the batch was created.")
    updated_at: datetime = Field(description="UTC timestamp when the batch was last changed.")


class TrialBalanceImportSummary(ContractModel):
    """Describe one uploaded trial-balance baseline."""

    id: str = Field(description="Trial-balance import batch UUID.")
    entity_id: str = Field(description="Owning entity UUID.")
    period_start: date = Field(description="First day covered by the imported trial balance.")
    period_end: date = Field(description="Last day covered by the imported trial balance.")
    source_format: str = Field(description="Upload format detected for the import batch.")
    uploaded_filename: str = Field(description="Original filename supplied by the operator.")
    row_count: int = Field(ge=1, description="Number of imported trial-balance rows.")
    imported_by_user_id: str | None = Field(
        default=None,
        description="User who uploaded the batch, if recorded.",
    )
    import_metadata: dict[str, object] = Field(
        default_factory=dict,
        description="Structured import diagnostics and header metadata.",
    )
    created_at: datetime = Field(description="UTC timestamp when the batch was created.")
    updated_at: datetime = Field(description="UTC timestamp when the batch was last changed.")


class GeneralLedgerExportSummary(ContractModel):
    """Describe one generated close-run general-ledger export artifact."""

    artifact_id: str = Field(description="Released artifact UUID for the export.")
    close_run_id: str = Field(description="Owning close-run UUID.")
    period_start: date = Field(description="Inclusive accounting-period start date.")
    period_end: date = Field(description="Inclusive accounting-period end date.")
    version_no: int = Field(ge=1, description="Close-run working version captured by the export.")
    generated_at: datetime = Field(
        description="UTC timestamp when the export artifact was generated.",
    )
    filename: str = Field(description="Operator-facing filename for the downloadable CSV.")
    content_type: str = Field(description="MIME type recorded for the stored artifact.")
    storage_key: str = Field(description="Artifact storage key in the canonical object store.")
    checksum: str = Field(description="SHA-256 checksum of the exported payload.")
    size_bytes: int = Field(ge=0, description="Byte size of the exported payload.")
    idempotency_key: str = Field(description="Idempotency key guarding this exact export snapshot.")
    row_count: int = Field(ge=1, description="Number of ledger rows written into the CSV export.")
    imported_line_count: int = Field(
        ge=0,
        description="How many rows came from the bound imported general-ledger baseline.",
    )
    adjustment_line_count: int = Field(
        ge=0,
        description="How many rows came from approved or applied close-run journals.",
    )
    composition_mode: str = Field(
        description=(
            "Whether the export contains imported GL only, adjustments only, or imported GL plus "
            "current-run adjustments."
        ),
    )
    includes_imported_baseline: bool = Field(
        description="Whether a bound imported general-ledger baseline is included in the export.",
    )


class LedgerWorkspaceResponse(ContractModel):
    """Return the imported ledger workspace for one entity."""

    general_ledger_imports: tuple[GeneralLedgerImportSummary, ...] = Field(
        default=(),
        description="General-ledger baseline imports in newest-first order.",
    )
    trial_balance_imports: tuple[TrialBalanceImportSummary, ...] = Field(
        default=(),
        description="Trial-balance baseline imports in newest-first order.",
    )
    close_run_bindings: tuple[CloseRunLedgerBindingSummary, ...] = Field(
        default=(),
        description="Close-run baseline bindings currently present for the entity.",
    )


class GeneralLedgerImportUploadResponse(ContractModel):
    """Return the result after uploading a general-ledger baseline."""

    imported_batch: GeneralLedgerImportSummary = Field(
        description="The imported general-ledger batch.",
    )
    auto_bound_close_run_ids: tuple[str, ...] = Field(
        default=(),
        description="Open close runs that were automatically bound to this import.",
    )
    skipped_close_run_ids: tuple[str, ...] = Field(
        default=(),
        description="Matching close runs left unbound because they already had ledger activity.",
    )
    workspace: LedgerWorkspaceResponse = Field(
        description="Refreshed ledger workspace after the upload.",
    )


class TrialBalanceImportUploadResponse(ContractModel):
    """Return the result after uploading a trial-balance baseline."""

    imported_batch: TrialBalanceImportSummary = Field(
        description="The imported trial-balance batch.",
    )
    auto_bound_close_run_ids: tuple[str, ...] = Field(
        default=(),
        description="Open close runs that were automatically bound to this import.",
    )
    skipped_close_run_ids: tuple[str, ...] = Field(
        default=(),
        description="Matching close runs left unbound because they already had ledger activity.",
    )
    workspace: LedgerWorkspaceResponse = Field(
        description="Refreshed ledger workspace after the upload.",
    )


__all__ = [
    "CloseRunLedgerBindingSummary",
    "GeneralLedgerExportSummary",
    "GeneralLedgerImportSummary",
    "GeneralLedgerImportUploadResponse",
    "LedgerWorkspaceResponse",
    "TrialBalanceImportSummary",
    "TrialBalanceImportUploadResponse",
]
