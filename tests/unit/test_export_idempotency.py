"""
Purpose: Verify idempotency-key generation, validation, deduplication, and
export-manifest assembly behavior for the evidence-pack and export workflows.
Scope: Idempotency service key construction, validation, guard checks,
duplicate-release detection, and evidence-pack bundle integrity.
Dependencies: services/idempotency/service.py, services/reporting/evidence_pack.py,
services/reporting/exports.py, and the export contract models.

Design notes:
- Tests use an in-memory fake repository to simulate persistence without
  requiring a live database.
- Idempotency keys must be deterministic: the same inputs always produce the
  same key so retries and duplicate clicks resolve to the same artifact.
- Duplicate-release detection must return the existing artifact metadata
  instead of creating a new row.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from services.common.enums import ArtifactType
from services.common.types import JsonObject
from services.contracts.export_models import (
    EvidencePackBundle,
    EvidencePackItem,
    ExportManifest,
)
from services.db.models.audit import AuditSourceSurface
from services.idempotency.service import (
    IdempotencyGuardError,
    IdempotencyGuardErrorCode,
    IdempotencyService,
    build_idempotency_key,
    validate_idempotency_key,
)
from services.reporting.evidence_pack import (
    EvidencePackInput,
    build_evidence_pack,
)
from services.reporting.exports import (
    ExportManifestBuilder,
    ExportManifestInput,
    build_export_manifest,
)


# ---------------------------------------------------------------------------
# Fake repository for idempotency tests
# ---------------------------------------------------------------------------

class FakeIdempotencyRepository:
    """Provide an in-memory store for idempotency-key deduplication tests."""

    def __init__(self) -> None:
        """Initialize the fake released-artifact dictionary."""

        self._artifacts: dict[tuple[str, str, str], dict[str, object]] = {}
        self._committed = True
        self._force_integrity_error = False

    def get_released_artifact(
        self,
        *,
        close_run_id: UUID,
        artifact_type: str,
        idempotency_key: str,
    ) -> dict[str, object] | None:
        """Return an existing artifact when the composite key matches."""

        key = (str(close_run_id), artifact_type, idempotency_key)
        return self._artifacts.get(key)

    def record_released_artifact(
        self,
        *,
        close_run_id: UUID,
        artifact_type: str,
        idempotency_key: str,
        storage_key: str,
        checksum: str,
        size_bytes: int,
        content_type: str,
        version_no: int,
        metadata: JsonObject | None = None,
    ) -> dict[str, object]:
        """Persist a new released-artifact row in memory."""

        if self._force_integrity_error:
            raise Exception("Simulated integrity constraint violation")

        key = (str(close_run_id), artifact_type, idempotency_key)
        record: dict[str, object] = {
            "close_run_id": str(close_run_id),
            "artifact_type": artifact_type,
            "idempotency_key": idempotency_key,
            "storage_key": storage_key,
            "checksum": checksum,
            "size_bytes": size_bytes,
            "content_type": content_type,
            "version_no": version_no,
            "metadata": metadata or {},
            "released_at": datetime.now(tz=UTC),
        }
        self._artifacts[key] = record
        return record

    def commit(self) -> None:
        """Mark the current transaction as committed."""

        self._committed = True

    def rollback(self) -> None:
        """Roll back the current transaction."""

        self._committed = False

    def is_integrity_error(self, error: Exception) -> bool:
        """Simulate integrity-error detection for concurrent-duplicate tests."""

        return self._force_integrity_error

    def simulate_integrity_error(self, flag: bool = True) -> None:
        """Toggle whether the next record_released_artifact raises an integrity error."""

        self._force_integrity_error = flag


# ---------------------------------------------------------------------------
# Idempotency key construction tests
# ---------------------------------------------------------------------------

class TestIdempotencyKeyConstruction:
    """Verify deterministic idempotency-key generation."""

    def test_build_idempotency_key_is_deterministic(self) -> None:
        """Same inputs must always produce the same idempotency key."""

        close_run_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

        key1 = build_idempotency_key(
            close_run_id=close_run_id,
            artifact_type="evidence_pack",
            action_qualifier="full_export",
        )
        key2 = build_idempotency_key(
            close_run_id=close_run_id,
            artifact_type="evidence_pack",
            action_qualifier="full_export",
        )
        assert key1 == key2, "Idempotency keys must be deterministic."

    def test_build_idempotency_key_varies_by_inputs(self) -> None:
        """Different inputs must produce different idempotency keys."""

        close_run_id_a = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        close_run_id_b = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

        key_a = build_idempotency_key(
            close_run_id=close_run_id_a,
            artifact_type="evidence_pack",
        )
        key_b = build_idempotency_key(
            close_run_id=close_run_id_b,
            artifact_type="evidence_pack",
        )
        assert key_a != key_b, "Different close runs must produce different keys."

    def test_build_idempotency_key_varies_by_artifact_type(self) -> None:
        """Different artifact types must produce different keys for the same close run."""

        close_run_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

        key_pack = build_idempotency_key(
            close_run_id=close_run_id,
            artifact_type=ArtifactType.EVIDENCE_PACK.value,
        )
        key_excel = build_idempotency_key(
            close_run_id=close_run_id,
            artifact_type=ArtifactType.REPORT_EXCEL.value,
        )
        assert key_pack != key_excel, "Different artifact types must yield different keys."

    def test_build_idempotency_key_includes_action_qualifier(self) -> None:
        """Action qualifier should disambiguate keys for the same close run and type."""

        close_run_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

        key_full = build_idempotency_key(
            close_run_id=close_run_id,
            artifact_type="evidence_pack",
            action_qualifier="full_export",
        )
        key_regen = build_idempotency_key(
            close_run_id=close_run_id,
            artifact_type="evidence_pack",
            action_qualifier="regeneration",
        )
        assert key_full != key_regen, (
            "Different action qualifiers must produce different keys."
        )

    def test_build_idempotency_key_starts_with_ik_prefix(self) -> None:
        """Keys must use the canonical ik- prefix for URL and storage safety."""

        key = build_idempotency_key(
            close_run_id=uuid4(),
            artifact_type="report_excel",
        )
        assert key.startswith("ik-"), "Idempotency key must start with 'ik-'."


# ---------------------------------------------------------------------------
# Idempotency key validation tests
# ---------------------------------------------------------------------------

class TestIdempotencyKeyValidation:
    """Validate idempotency-key format enforcement."""

    def test_validate_accepts_canonical_key(self) -> None:
        """A well-formed ik- key should pass without error."""

        validate_idempotency_key("ik-abcdef1234567890")

    def test_validate_rejects_empty_key(self) -> None:
        """Empty keys must be rejected."""

        with pytest.raises(IdempotencyGuardError) as exc_info:
            validate_idempotency_key("")

        assert exc_info.value.code is IdempotencyGuardErrorCode.INVALID_KEY

    def test_validate_rejects_missing_prefix(self) -> None:
        """Keys without ik- prefix must be rejected."""

        with pytest.raises(IdempotencyGuardError) as exc_info:
            validate_idempotency_key("abcdef1234567890")

        assert exc_info.value.code is IdempotencyGuardErrorCode.INVALID_KEY

    def test_validate_rejects_short_suffix(self) -> None:
        """Keys with too-short suffixes must be rejected."""

        with pytest.raises(IdempotencyGuardError) as exc_info:
            validate_idempotency_key("ik-short")

        assert exc_info.value.code is IdempotencyGuardErrorCode.INVALID_KEY


# ---------------------------------------------------------------------------
# Idempotency service guard tests
# ---------------------------------------------------------------------------

class TestIdempotencyServiceGuard:
    """Verify the idempotency guard deduplication behavior."""

    def _make_service(self) -> tuple[IdempotencyService, FakeIdempotencyRepository]:
        """Create an idempotency service with a fake repository."""

        repo = FakeIdempotencyRepository()
        return IdempotencyService(repository=repo), repo

    def test_guard_release_returns_none_for_new_key(self) -> None:
        """A previously unseen idempotency key should return None (no duplicate)."""

        service, _ = self._make_service()
        result = service.guard_release(
            close_run_id=uuid4(),
            artifact_type="evidence_pack",
            idempotency_key="ik-newkey1234567890",
        )
        assert result is None, "New keys should not find an existing artifact."

    def test_guard_release_returns_existing_for_duplicate(self) -> None:
        """When the key was already released, return the existing artifact."""

        service, repo = self._make_service()
        close_run_id = uuid4()
        idempotency_key = "ik-duplicate12345678"

        # Simulate a prior release.
        repo.record_released_artifact(
            close_run_id=close_run_id,
            artifact_type="evidence_pack",
            idempotency_key=idempotency_key,
            storage_key="artifacts/evidence_pack/test.zip",
            checksum="a" * 64,
            size_bytes=1024,
            content_type="application/zip",
            version_no=1,
        )

        result = service.guard_release(
            close_run_id=close_run_id,
            artifact_type="evidence_pack",
            idempotency_key=idempotency_key,
        )
        assert result is not None, "Duplicate key should find existing artifact."
        assert result["storage_key"] == "artifacts/evidence_pack/test.zip"

    def test_record_release_succeeds_for_new_key(self) -> None:
        """A new key should persist the artifact record."""

        service, _ = self._make_service()
        close_run_id = uuid4()
        idempotency_key = "ik-newrelease123456"

        record = service.record_release(
            close_run_id=close_run_id,
            artifact_type="evidence_pack",
            idempotency_key=idempotency_key,
            storage_key="artifacts/evidence_pack/new.zip",
            checksum="b" * 64,
            size_bytes=2048,
            content_type="application/zip",
            version_no=1,
        )
        assert record["storage_key"] == "artifacts/evidence_pack/new.zip"

    def test_record_release_rejects_duplicate(self) -> None:
        """A duplicate record_release should raise a 409 conflict."""

        service, repo = self._make_service()
        close_run_id = uuid4()
        idempotency_key = "ik-duprelease1234"

        # First release succeeds.
        repo.record_released_artifact(
            close_run_id=close_run_id,
            artifact_type="evidence_pack",
            idempotency_key=idempotency_key,
            storage_key="artifacts/evidence_pack/first.zip",
            checksum="c" * 64,
            size_bytes=512,
            content_type="application/zip",
            version_no=1,
        )

        # Second release should be blocked.
        with pytest.raises(IdempotencyGuardError) as exc_info:
            service.record_release(
                close_run_id=close_run_id,
                artifact_type="evidence_pack",
                idempotency_key=idempotency_key,
                storage_key="artifacts/evidence_pack/second.zip",
                checksum="d" * 64,
                size_bytes=512,
                content_type="application/zip",
                version_no=1,
            )

        assert exc_info.value.code is IdempotencyGuardErrorCode.DUPLICATE_RELEASE
        assert exc_info.value.status_code == 409

    def test_record_release_handles_concurrent_integrity_error(self) -> None:
        """When a concurrent write creates a duplicate, the guard must translate to 409."""

        service, repo = self._make_service()
        repo.simulate_integrity_error(True)

        with pytest.raises(IdempotencyGuardError) as exc_info:
            service.record_release(
                close_run_id=uuid4(),
                artifact_type="evidence_pack",
                idempotency_key="ik-concurrent123",
                storage_key="artifacts/test.zip",
                checksum="e" * 64,
                size_bytes=100,
                content_type="application/zip",
                version_no=1,
            )

        assert exc_info.value.code is IdempotencyGuardErrorCode.DUPLICATE_RELEASE


# ---------------------------------------------------------------------------
# Evidence pack assembly tests
# ---------------------------------------------------------------------------

class TestEvidencePackAssembly:
    """Verify evidence-pack ZIP assembly and bundle contract integrity."""

    def test_build_evidence_pack_produces_valid_zip(self) -> None:
        """Evidence pack builder must return non-empty ZIP bytes."""

        close_run_id = uuid4()
        pack_input = EvidencePackInput(
            close_run_id=close_run_id,
            entity_id=uuid4(),
            entity_name="Test Entity",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            close_run_version_no=1,
            source_references=[
                {"filename": "invoice.pdf", "storage_key": "docs/invoice.pdf"},
            ],
            approval_records=[
                {"actor_name": "Test User", "reason": "Approved"},
            ],
        )
        result = build_evidence_pack(pack_input)

        assert result.payload, "ZIP payload must not be empty."
        assert result.filename.endswith(".zip"), "Filename must be a ZIP."
        assert result.content_type == "application/zip"
        assert result.bundle.close_run_id == str(close_run_id)
        assert result.bundle.version_no == 1
        assert result.bundle.item_count >= 2, (
            "Should have at least source_reference and approval_record items."
        )

    def test_evidence_pack_bundle_has_required_fields(self) -> None:
        """The bundle contract must have all required fields populated."""

        close_run_id = uuid4()
        entity_id = uuid4()
        pack_input = EvidencePackInput(
            close_run_id=close_run_id,
            entity_id=entity_id,
            entity_name="Bundle Test",
            period_start=None,
            period_end=None,
            close_run_version_no=2,
            source_references=[],
            extracted_values=[
                {"field_name": "invoice_total", "description": "Total amount"},
            ],
            diff_entries=[
                {"target_label": "GL Code", "description": "Changed from 4000 to 5000"},
            ],
        )
        result = build_evidence_pack(pack_input)
        bundle = result.bundle

        assert bundle.close_run_id == str(close_run_id)
        assert bundle.version_no == 2
        assert bundle.generated_at is not None
        assert bundle.checksum is not None
        assert bundle.size_bytes is not None
        assert bundle.item_count >= 2

    def test_evidence_pack_handles_empty_inputs_gracefully(self) -> None:
        """Empty evidence-pack inputs should still produce a valid ZIP."""

        pack_input = EvidencePackInput(
            close_run_id=uuid4(),
            entity_id=uuid4(),
            entity_name="Empty Entity",
            period_start=None,
            period_end=None,
            close_run_version_no=1,
        )
        result = build_evidence_pack(pack_input)

        assert result.payload, "Even empty inputs must produce a ZIP."
        assert len(result.payload) > 0
        # ZIP magic number.
        assert result.payload[:2] == b"PK"


# ---------------------------------------------------------------------------
# Export manifest assembly tests
# ---------------------------------------------------------------------------

class TestExportManifestAssembly:
    """Verify export-manifest builder and assembly behavior."""

    def test_build_export_manifest_collects_artifacts(self) -> None:
        """Manifest should collect all provided artifact records."""

        close_run_id = uuid4()
        artifact_records = [
            {
                "artifact_type": "report_excel",
                "storage_key": "artifacts/excel/report.xlsx",
                "checksum": "f" * 64,
                "size_bytes": 50000,
                "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "idempotency_key": "ik-excel123",
                "released_at": datetime.now(tz=UTC),
            },
            {
                "artifact_type": "report_pdf",
                "storage_key": "artifacts/pdf/report.pdf",
                "checksum": "g" * 64,
                "size_bytes": 100000,
                "content_type": "application/pdf",
                "idempotency_key": "ik-pdf123",
                "released_at": datetime.now(tz=UTC),
            },
        ]

        result = build_export_manifest(
            ExportManifestInput(
                close_run_id=close_run_id,
                entity_id=uuid4(),
                entity_name="Manifest Test",
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 31),
                close_run_version_no=1,
                artifact_records=artifact_records,
            )
        )

        assert len(result.manifest.artifacts) == 2
        assert result.manifest.close_run_id == str(close_run_id)
        assert result.manifest.version_no == 1
        assert result.idempotency_key.startswith("ik-")

    def test_export_manifest_builder_fluent_api(self) -> None:
        """Fluent builder should produce a valid manifest."""

        close_run_id = uuid4()
        entity_id = uuid4()

        result = (
            ExportManifestBuilder(
                close_run_id=close_run_id,
                entity_id=entity_id,
                entity_name="Fluent Test",
                period_start=date(2026, 3, 1),
                period_end=date(2026, 3, 31),
                close_run_version_no=1,
            )
            .with_artifacts([
                {
                    "artifact_type": "evidence_pack",
                    "storage_key": "artifacts/evidence/pack.zip",
                    "checksum": "h" * 64,
                    "size_bytes": 20000,
                    "content_type": "application/zip",
                    "idempotency_key": "ik-pack123",
                    "released_at": datetime.now(tz=UTC),
                },
            ])
            .include_evidence_pack(True)
            .include_audit_trail(False)
            .build()
        )

        assert result.manifest.artifact_count == 1
        assert result.idempotency_key.startswith("ik-")

    def test_export_manifest_excludes_audit_trail_when_disabled(self) -> None:
        """When include_audit_trail is False, audit trail artifacts should be excluded."""

        close_run_id = uuid4()
        artifact_records = [
            {
                "artifact_type": "audit_trail",
                "storage_key": "artifacts/audit/audit.json",
                "checksum": "i" * 64,
                "size_bytes": 5000,
                "content_type": "application/json",
                "idempotency_key": "ik-audit123",
                "released_at": datetime.now(tz=UTC),
            },
            {
                "artifact_type": "report_excel",
                "storage_key": "artifacts/excel/report.xlsx",
                "checksum": "j" * 64,
                "size_bytes": 50000,
                "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "idempotency_key": "ik-excel456",
                "released_at": datetime.now(tz=UTC),
            },
        ]

        result = build_export_manifest(
            ExportManifestInput(
                close_run_id=close_run_id,
                entity_id=uuid4(),
                entity_name="Audit Exclude Test",
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 31),
                close_run_version_no=1,
                artifact_records=artifact_records,
                include_audit_trail=False,
            )
        )

        # Audit trail should be excluded.
        artifact_types = [a.artifact_type for a in result.manifest.artifacts]
        assert "audit_trail" not in artifact_types
        assert "report_excel" in artifact_types


__all__ = [
    "FakeIdempotencyRepository",
    "TestEvidencePackAssembly",
    "TestExportManifestAssembly",
    "TestIdempotencyKeyConstruction",
    "TestIdempotencyKeyValidation",
    "TestIdempotencyServiceGuard",
]
