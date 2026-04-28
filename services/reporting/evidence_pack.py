"""
Purpose: Build downloadable evidence-pack bundles that bundle source references,
extracted values, approvals, diffs, outputs, and export manifests for one close run.
Scope: Evidence-pack input contract, item collection logic, ZIP bundle assembly,
and checksum/size summaries ready for MinIO upload and idempotent release.
Dependencies: Python standard library (zipfile, json, io), shared enums,
storage repository, storage contracts, and export contracts.

Design notes:
- The evidence pack is a ZIP archive where each entry is a JSON or binary artifact
  representing one piece of evidence linked to the close run.
- The pack is deterministic: given the same input data, it produces the same
  structure so idempotency keys remain stable across retries.
- The builder never delegates to the LLM — it is a pure assembly operation.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from services.common.types import utc_now
from services.contracts.export_models import EvidencePackBundle, EvidencePackItem
from services.contracts.storage_models import CloseRunStorageScope
from services.storage.checksums import compute_sha256_bytes
from services.storage.repository import StorageRepository


@dataclass(frozen=True, slots=True)
class EvidencePackInput:
    """Capture all inputs required to assemble one evidence-pack bundle.

    Attributes:
        close_run_id: UUID of the close run this evidence pack belongs to.
        entity_id: UUID of the entity workspace that owns the close run.
        entity_name: Display name of the entity workspace.
        period_start: Start date of the accounting period.
        period_end: End date of the accounting period.
        close_run_version_no: Version number of the close run.
        source_references: List of source-document metadata entries.
        extracted_values: List of extracted-field payloads with confidence.
        approval_records: List of review/approval action records.
        diff_entries: List of before/after diff entries for changed items.
        report_outputs: List of generated report artifact references.
        generated_at: Timestamp when the pack was assembled.
    """

    close_run_id: UUID
    entity_id: UUID
    entity_name: str
    period_start: datetime | None  # date-like
    period_end: datetime | None  # date-like
    close_run_version_no: int
    source_references: list[dict[str, Any]] = field(default_factory=list)
    extracted_values: list[dict[str, Any]] = field(default_factory=list)
    approval_records: list[dict[str, Any]] = field(default_factory=list)
    diff_entries: list[dict[str, Any]] = field(default_factory=list)
    report_outputs: list[dict[str, Any]] = field(default_factory=list)
    generated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class EvidencePackResult:
    """Describe the output of one evidence-pack assembly operation.

    Attributes:
        payload: Raw ZIP bytes ready for storage or download.
        filename: Deterministic filename for the evidence pack.
        content_type: MIME type (always application/zip).
        bundle: The Pydantic evidence-pack bundle contract.
    """

    payload: bytes
    filename: str
    content_type: str
    bundle: EvidencePackBundle


def build_evidence_pack(input_data: EvidencePackInput) -> EvidencePackResult:
    """Assemble a deterministic ZIP evidence-pack bundle for one close run.

    The bundle contains JSON manifests for each evidence category and, when
    available, binary payloads referenced by source documents and report outputs.

    Args:
        input_data: All evidence inputs and close-run context.

    Returns:
        EvidencePackResult with ZIP bytes, filename, and bundle contract.
    """

    generated_at = input_data.generated_at or utc_now()
    items: list[EvidencePackItem] = []

    # Build the ZIP archive in memory.
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 1. Source references manifest
        _write_json_manifest(
            zf,
            "source_references.json",
            input_data.source_references,
        )
        for idx, ref in enumerate(input_data.source_references):
            items.append(EvidencePackItem(
                item_type="source_reference",
                label=ref.get("filename", f"Source document {idx + 1}"),
                description=ref.get("description"),
                storage_key=ref.get("storage_key"),
                payload_ref=_build_payload_ref("document", ref),
                checksum=ref.get("sha256_checksum"),
                size_bytes=ref.get("size_bytes"),
            ))

        # 2. Extracted values manifest
        _write_json_manifest(
            zf,
            "extracted_values.json",
            input_data.extracted_values,
        )
        for idx, extraction in enumerate(input_data.extracted_values):
            items.append(EvidencePackItem(
                item_type="extracted_field",
                label=extraction.get("field_name", f"Extracted field {idx + 1}"),
                description=extraction.get("description"),
                payload_ref=_build_payload_ref("extraction", extraction),
            ))

        # 3. Approval records manifest
        _write_json_manifest(
            zf,
            "approvals.json",
            input_data.approval_records,
        )
        for idx, approval in enumerate(input_data.approval_records):
            items.append(EvidencePackItem(
                item_type="approval_record",
                label=approval.get("actor_name", f"Approval {idx + 1}"),
                description=approval.get("reason"),
                payload_ref=_build_payload_ref("approval", approval),
            ))

        # 4. Diff entries manifest
        _write_json_manifest(
            zf,
            "diffs.json",
            input_data.diff_entries,
        )
        for idx, diff in enumerate(input_data.diff_entries):
            items.append(EvidencePackItem(
                item_type="diff_entry",
                label=diff.get("target_label", f"Diff entry {idx + 1}"),
                description=diff.get("description"),
                payload_ref=_build_payload_ref("diff", diff),
            ))

        # 5. Report outputs manifest
        _write_json_manifest(
            zf,
            "report_outputs.json",
            input_data.report_outputs,
        )
        for idx, output in enumerate(input_data.report_outputs):
            items.append(EvidencePackItem(
                item_type="report_output",
                label=output.get("filename", f"Report output {idx + 1}"),
                description=output.get("description"),
                storage_key=output.get("storage_key"),
                payload_ref=_build_payload_ref("report_output", output),
                checksum=output.get("sha256_checksum"),
                size_bytes=output.get("size_bytes"),
            ))

        # 6. Pack-level metadata manifest
        metadata = {
            "close_run_id": str(input_data.close_run_id),
            "entity_id": str(input_data.entity_id),
            "entity_name": input_data.entity_name,
            "period_start": (
                input_data.period_start.isoformat() if input_data.period_start else None
            ),
            "period_end": (
                input_data.period_end.isoformat() if input_data.period_end else None
            ),
            "close_run_version_no": input_data.close_run_version_no,
            "generated_at": generated_at.isoformat(),
            "item_counts": {
                "source_references": len(input_data.source_references),
                "extracted_values": len(input_data.extracted_values),
                "approval_records": len(input_data.approval_records),
                "diff_entries": len(input_data.diff_entries),
                "report_outputs": len(input_data.report_outputs),
            },
        }
        _write_json_manifest(zf, "metadata.json", metadata)

    zip_payload = zip_buffer.getvalue()
    checksum = compute_sha256_bytes(zip_payload)

    # Deterministic filename.
    period_str = "unknown"
    if input_data.period_start and input_data.period_end:
        period_str = (
            f"{input_data.period_start.strftime('%Y-%m-%d')}"
            f"-{input_data.period_end.strftime('%Y-%m-%d')}"
        )
    filename = (
        f"evidence-pack-{input_data.entity_name.strip().lower().replace(' ', '-')}"
        f"-v{input_data.close_run_version_no}-{period_str}.zip"
    )

    bundle = EvidencePackBundle(
        close_run_id=str(input_data.close_run_id),
        version_no=input_data.close_run_version_no,
        generated_at=generated_at,
        items=tuple(items),
        storage_key=None,  # Populated after upload.
        checksum=checksum,
        size_bytes=len(zip_payload),
        # idempotency_key is populated by the caller / export service after upload.
    )

    return EvidencePackResult(
        payload=zip_payload,
        filename=filename,
        content_type="application/zip",
        bundle=bundle,
    )


def upload_evidence_pack(
    *,
    result: EvidencePackResult,
    storage_repo: StorageRepository,
    scope: CloseRunStorageScope,
    idempotency_key: str,
) -> EvidencePackBundle:
    """Upload the assembled evidence-pack ZIP to object storage and return the enriched bundle.

    Args:
        result: The assembled evidence-pack result with ZIP bytes.
        storage_repo: Canonical storage repository for uploads.
        scope: Close-run storage scope for key generation.
        idempotency_key: Deterministic idempotency key for this release.

    Returns:
        EvidencePackBundle with storage_key and idempotency_key populated.
    """

    artifact_meta = storage_repo.store_evidence_pack(
        scope=scope,
        idempotency_key=idempotency_key,
        filename=result.filename,
        payload=result.payload,
        content_type=result.content_type,
    )

    return EvidencePackBundle(
        close_run_id=result.bundle.close_run_id,
        version_no=result.bundle.version_no,
        generated_at=result.bundle.generated_at,
        items=result.bundle.items,
        storage_key=artifact_meta.reference.object_key,
        checksum=artifact_meta.sha256_checksum,
        size_bytes=artifact_meta.size_bytes,
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _write_json_manifest(
    zf: zipfile.ZipFile,
    entry_name: str,
    data: Any,
) -> None:
    """Write one JSON manifest entry into the ZIP archive.

    Args:
        zf: Open ZIP file writer.
        entry_name: Filename within the ZIP for this manifest.
        data: Serializable data to write as JSON.
    """

    json_bytes = json.dumps(data, indent=2, default=str).encode("utf-8")
    zf.writestr(entry_name, json_bytes)


def _build_payload_ref(
    category: str,
    data: dict[str, Any],
) -> dict[str, object]:
    """Build a structured payload reference for one evidence item.

    Args:
        category: Evidence category label (document, extraction, etc.).
        data: Raw metadata dict with potential IDs and labels.

    Returns:
        Dictionary with category and extracted reference fields.
    """

    ref: dict[str, object] = {"category": category}
    for key in ("id", "document_id", "field_name", "approval_id", "diff_id", "artifact_id"):
        if key in data:
            ref[key] = data[key]
    return ref


__all__ = [
    "EvidencePackInput",
    "EvidencePackResult",
    "build_evidence_pack",
    "upload_evidence_pack",
]
