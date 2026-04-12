"""
Purpose: Define strict Pydantic API contracts for export workflows, evidence-pack
bundles, and idempotent release manifests.
Scope: Evidence-pack item definitions, bundle summaries, export manifest
contracts, idempotency-key validation responses, and export listing models.
Dependencies: Pydantic contract defaults, canonical enums, and shared
API model base from services/contracts/api_models.py.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator, model_validator
from services.contracts.api_models import ContractModel

# ---------------------------------------------------------------------------
# Evidence pack item contracts
# ---------------------------------------------------------------------------

class EvidencePackItem(ContractModel):
    """Describe one bundle entry inside an evidence pack."""

    item_type: str = Field(
        min_length=1,
        max_length=80,
        description=(
            "Stable item category: source_reference, extracted_field, approval_record, "
            "diff_entry, or report_output."
        ),
    )
    label: str = Field(
        min_length=1,
        max_length=400,
        description="Human-readable label shown in evidence-pack viewers.",
    )
    description: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional explanation of what this evidence item represents.",
    )
    storage_key: str | None = Field(
        default=None,
        min_length=1,
        description="Object-storage key when the item has an associated binary payload.",
    )
    payload_ref: dict[str, object] = Field(
        default_factory=dict,
        description=(
            "Structured reference to the underlying data: document ID, field name, "
            "recommendation ID, artifact ID, etc."
        ),
    )
    checksum: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        description="SHA-256 checksum of the bundled payload when applicable.",
    )
    size_bytes: int | None = Field(
        default=None,
        ge=0,
        description="Byte size of the bundled payload when applicable.",
    )

    @field_validator("item_type")
    @classmethod
    def normalize_item_type(cls, value: str) -> str:
        """Normalize item type to lower-case snake_case."""

        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("item_type cannot be blank.")
        return normalized

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str) -> str:
        """Normalize required label text."""

        normalized = value.strip()
        if not normalized:
            raise ValueError("label cannot be blank.")
        return normalized


# ---------------------------------------------------------------------------
# Evidence pack bundle contracts
# ---------------------------------------------------------------------------

class EvidencePackBundle(ContractModel):
    """Describe the complete evidence-pack bundle for one close run."""

    close_run_id: str = Field(
        min_length=1,
        description="UUID of the close run this evidence pack belongs to.",
    )
    version_no: int = Field(
        ge=1,
        description="Close-run version number this pack represents.",
    )
    generated_at: datetime = Field(
        description="UTC timestamp when the evidence pack was assembled.",
    )
    items: tuple[EvidencePackItem, ...] = Field(
        default=(),
        description="Ordered collection of evidence items bundled into this pack.",
    )
    storage_key: str | None = Field(
        default=None,
        min_length=1,
        description="Object-storage key for the assembled ZIP evidence pack.",
    )
    checksum: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        description="SHA-256 checksum of the assembled ZIP payload.",
    )
    size_bytes: int | None = Field(
        default=None,
        ge=0,
        description="Total byte size of the assembled ZIP payload.",
    )
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        description="Deterministic idempotency key used for this pack release. Populated after upload.",
    )

    @property
    def item_count(self) -> int:
        """Return the number of evidence items in this bundle."""

        return len(self.items)


# ---------------------------------------------------------------------------
# Export manifest contracts
# ---------------------------------------------------------------------------

class ExportArtifactEntry(ContractModel):
    """Describe one released artifact inside an export manifest."""

    artifact_type: str = Field(
        min_length=1,
        max_length=80,
        description="Canonical artifact type (e.g. report_excel, evidence_pack).",
    )
    filename: str = Field(
        min_length=1,
        description="Downloadable filename exposed to the user.",
    )
    storage_key: str = Field(
        min_length=1,
        description="Object-storage key where the artifact lives.",
    )
    checksum: str = Field(
        min_length=64,
        max_length=64,
        description="SHA-256 checksum of the artifact payload.",
    )
    size_bytes: int = Field(
        ge=0,
        description="Byte size of the artifact payload.",
    )
    content_type: str = Field(
        min_length=1,
        description="MIME type of the artifact.",
    )
    idempotency_key: str = Field(
        min_length=1,
        description="Idempotency key that guards against duplicate release.",
    )
    released_at: datetime = Field(
        description="UTC timestamp when the artifact was released.",
    )


class ExportManifest(ContractModel):
    """Describe the full export manifest for one close run version."""

    close_run_id: str = Field(
        min_length=1,
        description="UUID of the close run this export manifest belongs to.",
    )
    version_no: int = Field(
        ge=1,
        description="Close-run version number this manifest represents.",
    )
    generated_at: datetime = Field(
        description="UTC timestamp when the manifest was assembled.",
    )
    artifacts: tuple[ExportArtifactEntry, ...] = Field(
        default=(),
        description="All released artifacts linked to this close run version.",
    )
    evidence_pack_ref: EvidencePackBundle | None = Field(
        default=None,
        description="Reference to the associated evidence pack bundle, if available.",
    )

    @property
    def artifact_count(self) -> int:
        """Return the number of artifacts in this manifest."""

        return len(self.artifacts)


# ---------------------------------------------------------------------------
# Export listing contracts
# ---------------------------------------------------------------------------

class ExportSummary(ContractModel):
    """Describe one export action for listing views."""

    id: str = Field(description="Stable UUID for this export record.")
    close_run_id: str = Field(description="Close run this export was generated for.")
    version_no: int = Field(ge=1, description="Close-run version number.")
    idempotency_key: str = Field(description="Idempotency key used for this export.")
    status: str = Field(min_length=1, description="Current export lifecycle status.")
    artifact_count: int = Field(ge=0, description="Number of artifacts in this export.")
    failure_reason: str | None = Field(
        default=None,
        description="Structured failure description when status indicates failure.",
    )
    created_at: datetime = Field(description="UTC timestamp when the export was created.")
    completed_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when the export reached a terminal state.",
    )


class ExportDetail(ExportSummary):
    """Extend the export summary with full manifest and evidence pack details."""

    manifest: ExportManifest | None = Field(
        default=None,
        description="Full export manifest with all artifact entries.",
    )
    evidence_pack: EvidencePackBundle | None = Field(
        default=None,
        description="Evidence pack bundle associated with this export.",
    )


class ExportListResponse(ContractModel):
    """Return all exports for one close run in newest-first order."""

    close_run_id: str = Field(description="Close run UUID the exports belong to.")
    exports: tuple[ExportSummary, ...] = Field(
        default=(),
        description="Export records for the close run, newest first.",
    )


# ---------------------------------------------------------------------------
# Export request contracts
# ---------------------------------------------------------------------------

class CreateExportRequest(ContractModel):
    """Capture the inputs required to trigger a new export for a close run."""

    include_evidence_pack: bool = Field(
        default=True,
        description="Whether to assemble an evidence pack bundle alongside the export.",
    )
    include_audit_trail: bool = Field(
        default=True,
        description="Whether to include the audit trail export in the manifest.",
    )
    action_qualifier: str | None = Field(
        default=None,
        max_length=120,
        description=(
            "Optional action scope for idempotency key disambiguation "
            "(e.g. 'full_export', 'regeneration')."
        ),
    )

    @field_validator("action_qualifier")
    @classmethod
    def normalize_action_qualifier(cls, value: str | None) -> str | None:
        """Normalize optional action qualifier."""

        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


# ---------------------------------------------------------------------------
# Idempotency response contracts
# ---------------------------------------------------------------------------

class IdempotencyKeyResponse(ContractModel):
    """Return the computed idempotency key for an export action."""

    idempotency_key: str = Field(
        min_length=1,
        description="Deterministic idempotency key for the requested export action.",
    )
    close_run_id: str = Field(description="Close run the key was computed for.")
    artifact_type: str = Field(description="Artifact type the key covers.")


class DuplicateExportResponse(ContractModel):
    """Return the existing export when a duplicate action is detected."""

    idempotency_key: str = Field(description="The idempotency key that matched.")
    existing_export_id: str = Field(description="UUID of the existing export record.")
    existing_artifact_keys: tuple[str, ...] = Field(
        default=(),
        description="Storage keys of the already-released artifacts.",
    )
    message: str = Field(
        min_length=1,
        description="Recovery-oriented explanation for the caller.",
    )


__all__ = [
    "CreateExportRequest",
    "DuplicateExportResponse",
    "ExportArtifactEntry",
    "ExportDetail",
    "ExportListResponse",
    "ExportManifest",
    "ExportSummary",
    "EvidencePackBundle",
    "EvidencePackItem",
    "IdempotencyKeyResponse",
]
