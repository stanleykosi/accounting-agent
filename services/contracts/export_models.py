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

from pydantic import Field, field_validator
from services.contracts.api_models import ContractModel

EXPORT_DELIVERY_CHANNELS = (
    "secure_email",
    "management_portal",
    "board_pack",
    "file_share",
)


def _normalize_required_text(value: str, *, field_name: str) -> str:
    """Normalize one required text field and reject blank input."""

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} cannot be blank.")
    return normalized


def _normalize_optional_text(value: str | None) -> str | None:
    """Collapse blank optional text to null."""

    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_recipient_email(value: str) -> str:
    """Normalize and minimally validate a stakeholder email address."""

    normalized = value.strip().lower()
    if (
        not normalized
        or "@" not in normalized
        or normalized.startswith("@")
        or normalized.endswith("@")
    ):
        raise ValueError("recipient_email must be a valid email address.")
    return normalized


def _normalize_delivery_channel(value: str) -> str:
    """Normalize delivery channel and enforce the canonical options."""

    normalized = value.strip().lower()
    if normalized not in EXPORT_DELIVERY_CHANNELS:
        allowed = ", ".join(EXPORT_DELIVERY_CHANNELS)
        raise ValueError(f"delivery_channel must be one of: {allowed}.")
    return normalized


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
        description=(
            "Deterministic idempotency key used for this pack release. Populated after upload."
        ),
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


class ExportDistributionRecord(ContractModel):
    """Describe one stakeholder distribution event for an export package."""

    id: str = Field(
        min_length=1,
        description="Stable UUID for this distribution record.",
    )
    recipient_name: str = Field(
        min_length=1,
        max_length=200,
        description="Human-readable stakeholder name that received the package.",
    )
    recipient_email: str = Field(
        min_length=3,
        max_length=320,
        description="Delivery email for the stakeholder release record.",
    )
    recipient_role: str | None = Field(
        default=None,
        max_length=120,
        description="Optional stakeholder role such as CFO or Finance Manager.",
    )
    delivery_channel: str = Field(
        min_length=1,
        max_length=80,
        description="Controlled delivery channel used for the release.",
    )
    note: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional operator note recorded alongside the distribution event.",
    )
    distributed_at: datetime = Field(
        description="UTC timestamp when the package was distributed.",
    )
    distributed_by_user_id: str | None = Field(
        default=None,
        description="User who recorded the distribution event.",
    )

    @field_validator("recipient_name")
    @classmethod
    def normalize_recipient_name(cls, value: str) -> str:
        """Normalize required stakeholder names."""

        return _normalize_required_text(value, field_name="recipient_name")

    @field_validator("recipient_email")
    @classmethod
    def normalize_recipient_email(cls, value: str) -> str:
        """Normalize and minimally validate the stakeholder email."""

        return _normalize_recipient_email(value)

    @field_validator("recipient_role", "note")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        """Collapse blank optional strings to null."""

        return _normalize_optional_text(value)

    @field_validator("delivery_channel")
    @classmethod
    def normalize_delivery_channel(cls, value: str) -> str:
        """Normalize delivery channel and enforce the canonical options."""

        return _normalize_delivery_channel(value)


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
    distribution_count: int = Field(
        default=0,
        ge=0,
        description="Number of management-distribution records attached to this export.",
    )
    latest_distribution_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the most recent management distribution, if any.",
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
    distribution_records: tuple[ExportDistributionRecord, ...] = Field(
        default=(),
        description="Recorded management distributions for this export version.",
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


class DistributeExportRequest(ContractModel):
    """Capture one management-distribution action for an export package."""

    recipient_name: str = Field(
        min_length=1,
        max_length=200,
        description="Stakeholder name receiving the package.",
    )
    recipient_email: str = Field(
        min_length=3,
        max_length=320,
        description="Stakeholder email receiving the package.",
    )
    recipient_role: str | None = Field(
        default=None,
        max_length=120,
        description="Optional stakeholder role such as CFO or Finance Manager.",
    )
    delivery_channel: str = Field(
        default="secure_email",
        description="Controlled delivery channel used for this release.",
    )
    note: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional operator note about the distribution or sign-off context.",
    )

    @field_validator("recipient_name")
    @classmethod
    def validate_recipient_name(cls, value: str) -> str:
        """Normalize required stakeholder name input."""

        return _normalize_required_text(value, field_name="recipient_name")

    @field_validator("recipient_email")
    @classmethod
    def validate_recipient_email(cls, value: str) -> str:
        """Normalize required stakeholder email input."""

        return _normalize_recipient_email(value)

    @field_validator("recipient_role", "note")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        """Normalize optional stakeholder metadata."""

        return _normalize_optional_text(value)

    @field_validator("delivery_channel")
    @classmethod
    def validate_delivery_channel(cls, value: str) -> str:
        """Normalize delivery channel input against the canonical list."""

        return _normalize_delivery_channel(value)


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
    "EXPORT_DELIVERY_CHANNELS",
    "CreateExportRequest",
    "DistributeExportRequest",
    "DuplicateExportResponse",
    "EvidencePackBundle",
    "EvidencePackItem",
    "ExportArtifactEntry",
    "ExportDetail",
    "ExportDistributionRecord",
    "ExportListResponse",
    "ExportManifest",
    "ExportSummary",
    "IdempotencyKeyResponse",
]
