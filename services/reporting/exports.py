"""
Purpose: Build export manifests that package all released artifacts for one close run
version, and orchestrate evidence-pack + artifact bundling with idempotent release controls.
Scope: Export-manifest assembly, artifact collection from report runs and direct exports,
evidence-pack integration, and idempotency-guarded release coordination.
Dependencies: Reporting builders, idempotency service, storage repository,
export contracts, and DB models for artifacts and export runs.

Design notes:
- The export manifest is a read-only assembly of all artifacts already released
  for a close run version.  It does not generate new artifacts itself.
- Idempotency keys ensure that duplicate export clicks or task retries return
  the same manifest instead of creating duplicate release records.
- The manifest builder delegates evidence-pack assembly to the evidence_pack module
  and artifact storage queries to the storage repository.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from services.common.enums import ArtifactType
from services.common.types import utc_now
from services.contracts.export_models import (
    EvidencePackBundle,
    ExportArtifactEntry,
)
from services.contracts.export_models import (
    ExportManifest as ExportManifestContract,
)
from services.contracts.storage_models import CloseRunStorageScope
from services.db.models.audit import AuditSourceSurface
from services.idempotency.service import (
    IdempotencyGuardError,
    IdempotencyGuardErrorCode,
    IdempotencyService,
    build_idempotency_key,
)
from services.reporting.evidence_pack import (
    EvidencePackInput,
    build_evidence_pack,
    upload_evidence_pack,
)
from services.storage.repository import StorageRepository


@dataclass(frozen=True, slots=True)
class ExportManifestInput:
    """Capture all inputs required to assemble one export manifest.

    Attributes:
        close_run_id: UUID of the close run this export belongs to.
        entity_id: UUID of the entity workspace that owns the close run.
        entity_name: Display name of the entity workspace.
        period_start: Start date of the accounting period.
        period_end: End date of the accounting period.
        close_run_version_no: Version number of the close run.
        artifact_records: List of already-released artifact metadata.
        include_evidence_pack: Whether to assemble an evidence pack bundle.
        include_audit_trail: Whether to include audit trail artifacts.
        evidence_pack_input: Optional pre-assembled evidence pack input data.
        generated_at: Timestamp when the manifest was assembled.
    """

    close_run_id: UUID
    entity_id: UUID
    entity_name: str
    period_start: datetime | None
    period_end: datetime | None
    close_run_version_no: int
    artifact_records: list[dict[str, Any]] = field(default_factory=list)
    include_evidence_pack: bool = True
    include_audit_trail: bool = True
    evidence_pack_input: EvidencePackInput | None = None
    generated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ExportManifestResult:
    """Describe the output of one export-manifest assembly operation.

    Attributes:
        manifest: The assembled export manifest contract.
        evidence_pack: Optional evidence pack bundle if one was assembled.
        idempotency_key: Deterministic idempotency key for this export.
    """

    manifest: ExportManifestContract
    evidence_pack: EvidencePackBundle | None
    idempotency_key: str


def build_export_manifest(
    input_data: ExportManifestInput,
) -> ExportManifestResult:
    """Assemble an export manifest for one close run version.

    The manifest collects all released artifacts and optionally includes an
    evidence pack bundle.  The idempotency key is computed deterministically
    so the same export action always produces the same key.

    Args:
        input_data: All inputs required for manifest assembly.

    Returns:
        ExportManifestResult with the manifest, optional evidence pack, and
        idempotency key.
    """

    generated_at = input_data.generated_at or utc_now()
    idempotency_key = build_idempotency_key(
        close_run_id=input_data.close_run_id,
        artifact_type="export_manifest",
        action_qualifier="full_export",
        version_override=input_data.close_run_version_no,
    )

    # Collect artifact entries from the released artifact records.
    artifact_entries: list[ExportArtifactEntry] = []
    for record in input_data.artifact_records:
        is_audit_trail = record.get("artifact_type") == ArtifactType.AUDIT_TRAIL.value
        if input_data.include_audit_trail or not is_audit_trail:
            artifact_entries.append(ExportArtifactEntry(
                artifact_type=record["artifact_type"],
                filename=record.get("filename", f"{record['artifact_type']}.dat"),
                storage_key=record["storage_key"],
                checksum=record["checksum"],
                size_bytes=record["size_bytes"],
                content_type=record.get("content_type", "application/octet-stream"),
                idempotency_key=record.get("idempotency_key", ""),
                released_at=record.get("released_at", generated_at),
            ))

    # Evidence pack is assembled separately and referenced in the manifest.
    evidence_pack: EvidencePackBundle | None = None

    manifest = ExportManifestContract(
        close_run_id=str(input_data.close_run_id),
        version_no=input_data.close_run_version_no,
        generated_at=generated_at,
        artifacts=tuple(artifact_entries),
        evidence_pack_ref=None,  # Populated after evidence pack assembly.
    )

    return ExportManifestResult(
        manifest=manifest,
        evidence_pack=evidence_pack,
        idempotency_key=idempotency_key,
    )


class ExportManifestBuilder:
    """Provide a fluent builder interface for assembling export manifests
    with evidence-pack integration and idempotency-guarded release.

    The builder collects artifact records, assembles the evidence pack when
    requested, and produces the final manifest contract ready for persistence
    and download.
    """

    def __init__(
        self,
        *,
        close_run_id: UUID,
        entity_id: UUID,
        entity_name: str,
        period_start: datetime | None,
        period_end: datetime | None,
        close_run_version_no: int,
    ) -> None:
        """Initialize the builder with close-run context.

        Args:
            close_run_id: UUID of the close run this export belongs to.
            entity_id: UUID of the entity workspace.
            entity_name: Display name of the entity.
            period_start: Start date of the accounting period.
            period_end: End date of the accounting period.
            close_run_version_no: Version number of the close run.
        """

        self._close_run_id = close_run_id
        self._entity_id = entity_id
        self._entity_name = entity_name
        self._period_start = period_start
        self._period_end = period_end
        self._close_run_version_no = close_run_version_no
        self._artifact_records: list[dict[str, Any]] = []
        self._include_evidence_pack = True
        self._include_audit_trail = True
        self._evidence_pack_input: EvidencePackInput | None = None
        self._generated_at: datetime | None = None

    def with_artifacts(
        self, records: list[dict[str, Any]]
    ) -> ExportManifestBuilder:
        """Add released artifact metadata records to the manifest.

        Args:
            records: List of artifact metadata dicts from the persistence layer.

        Returns:
            Self for fluent chaining.
        """

        self._artifact_records.extend(records)
        return self

    def include_evidence_pack(self, flag: bool = True) -> ExportManifestBuilder:
        """Set whether to assemble an evidence pack bundle.

        Args:
            flag: True to include evidence pack, False to omit.

        Returns:
            Self for fluent chaining.
        """

        self._include_evidence_pack = flag
        return self

    def include_audit_trail(self, flag: bool = True) -> ExportManifestBuilder:
        """Set whether to include audit trail artifacts in the manifest.

        Args:
            flag: True to include audit trail, False to omit.

        Returns:
            Self for fluent chaining.
        """

        self._include_audit_trail = flag
        return self

    def with_evidence_pack_input(
        self, evidence_input: EvidencePackInput | None
    ) -> ExportManifestBuilder:
        """Provide the evidence pack assembly input data.

        Args:
            evidence_input: Evidence pack input with source references, extractions,
                approvals, diffs, and report outputs.

        Returns:
            Self for fluent chaining.
        """

        self._evidence_pack_input = evidence_input
        return self

    def generated_at(self, timestamp: datetime) -> ExportManifestBuilder:
        """Set the generation timestamp for the manifest.

        Args:
            timestamp: UTC timestamp for manifest generation.

        Returns:
            Self for fluent chaining.
        """

        self._generated_at = timestamp
        return self

    def build(self) -> ExportManifestResult:
        """Assemble the export manifest and optional evidence pack.

        Returns:
            ExportManifestResult with the assembled manifest and evidence pack.
        """

        input_data = ExportManifestInput(
            close_run_id=self._close_run_id,
            entity_id=self._entity_id,
            entity_name=self._entity_name,
            period_start=self._period_start,
            period_end=self._period_end,
            close_run_version_no=self._close_run_version_no,
            artifact_records=self._artifact_records,
            include_evidence_pack=self._include_evidence_pack,
            include_audit_trail=self._include_audit_trail,
            evidence_pack_input=self._evidence_pack_input,
            generated_at=self._generated_at,
        )
        return build_export_manifest(input_data)


def assemble_and_release_evidence_pack(
    *,
    close_run_id: UUID,
    entity_id: UUID,
    entity_name: str,
    period_start: datetime | None,
    period_end: datetime | None,
    close_run_version_no: int,
    source_references: list[dict[str, Any]],
    extracted_values: list[dict[str, Any]],
    approval_records: list[dict[str, Any]],
    diff_entries: list[dict[str, Any]],
    report_outputs: list[dict[str, Any]],
    storage_repo: StorageRepository,
    scope: CloseRunStorageScope,
    idempotency_service: IdempotencyService,
    source_surface: AuditSourceSurface = AuditSourceSurface.SYSTEM,
) -> EvidencePackBundle:
    """Assemble an evidence pack, upload it to storage, and register the release
    with idempotency protection.

    Args:
        close_run_id: UUID of the close run this pack belongs to.
        entity_id: UUID of the entity workspace.
        entity_name: Display name of the entity.
        period_start: Start date of the accounting period.
        period_end: End date of the accounting period.
        close_run_version_no: Version number of the close run.
        source_references: Source-document metadata entries.
        extracted_values: Extracted-field payloads with confidence.
        approval_records: Review/approval action records.
        diff_entries: Before/after diff entries for changed items.
        report_outputs: Generated report artifact references.
        storage_repo: Canonical storage repository for uploads.
        scope: Close-run storage scope for key generation.
        idempotency_service: Idempotency guard for release deduplication.
        source_surface: Runtime surface that emitted this release.

    Returns:
        EvidencePackBundle with storage key and idempotency key populated.

    Raises:
        IdempotencyGuardError: When the evidence pack was already released.
    """

    idempotency_key = build_idempotency_key(
        close_run_id=close_run_id,
        artifact_type=ArtifactType.EVIDENCE_PACK.value,
        action_qualifier="evidence_pack",
        version_override=close_run_version_no,
    )

    # Check for duplicate release.
    existing = idempotency_service.guard_release(
        close_run_id=close_run_id,
        artifact_type=ArtifactType.EVIDENCE_PACK.value,
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        raise IdempotencyGuardError(
            status_code=409,
            code=IdempotencyGuardErrorCode.DUPLICATE_RELEASE,
            message=(
                "This evidence pack was already released for the given close run version. "
                "Use the existing pack instead of generating a duplicate."
            ),
            existing_artifact_ref=existing,
        )

    # Assemble the evidence pack ZIP.
    pack_input = EvidencePackInput(
        close_run_id=close_run_id,
        entity_id=entity_id,
        entity_name=entity_name,
        period_start=period_start,
        period_end=period_end,
        close_run_version_no=close_run_version_no,
        source_references=source_references,
        extracted_values=extracted_values,
        approval_records=approval_records,
        diff_entries=diff_entries,
        report_outputs=report_outputs,
    )
    pack_result = build_evidence_pack(pack_input)

    # Upload to object storage.
    bundle = upload_evidence_pack(
        result=pack_result,
        storage_repo=storage_repo,
        scope=scope,
        idempotency_key=idempotency_key,
    )
    if bundle.storage_key is None or bundle.checksum is None or bundle.size_bytes is None:
        raise RuntimeError("Evidence-pack upload did not return complete artifact metadata.")

    # Register the release with idempotency protection.
    idempotency_service.record_release(
        close_run_id=close_run_id,
        artifact_type=ArtifactType.EVIDENCE_PACK.value,
        idempotency_key=idempotency_key,
        storage_key=bundle.storage_key,
        checksum=bundle.checksum,
        size_bytes=bundle.size_bytes,
        content_type="application/zip",
        version_no=close_run_version_no,
        metadata={
            "entity_name": entity_name,
            "item_count": bundle.item_count,
            "source_surface": source_surface.value,
        },
    )

    return bundle


__all__ = [
    "ExportManifestBuilder",
    "ExportManifestInput",
    "ExportManifestResult",
    "assemble_and_release_evidence_pack",
    "build_export_manifest",
]
