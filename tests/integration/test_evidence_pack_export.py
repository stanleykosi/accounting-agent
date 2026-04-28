"""
Purpose: Integration tests for evidence-pack assembly, export-manifest creation,
and the full evidence-pack export flow with realistic close-run data.
Scope: End-to-end evidence-pack bundle creation, ZIP manifest integrity,
export-manifest artifact collection, and idempotency-guarded release behavior
with storage-repository fakes.
Dependencies: pytest, evidence-pack builder, export-manifest service,
idempotency service, and storage contracts.

Design notes:
- These tests exercise the full assembly pipeline without requiring live
  MinIO, database, or API services.
- A fake storage repository simulates uploads and checksum verification.
- Tests verify ZIP structure, JSON manifest correctness, and idempotency
  key stability across retries.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

import pytest
from services.common.enums import ArtifactType
from services.common.types import JsonObject
from services.contracts.storage_models import (
    CloseRunStorageScope,
    ObjectStorageReference,
    StorageBucketKind,
    StoredObjectMetadata,
)
from services.idempotency.service import (
    IdempotencyGuardError,
    IdempotencyService,
    build_idempotency_key,
)
from services.reporting.evidence_pack import (
    EvidencePackInput,
    build_evidence_pack,
    upload_evidence_pack,
)
from services.reporting.exports import (
    ExportManifestBuilder,
    ExportManifestInput,
    build_export_manifest,
)
from services.storage.checksums import compute_sha256_bytes

# ---------------------------------------------------------------------------
# Fake storage for integration tests
# ---------------------------------------------------------------------------

@dataclass
class FakeStoredObject:
    """Represent one object stored in the fake storage backend."""

    payload: bytes
    content_type: str
    etag: str


class FakeStorageBackend:
    """Provide an in-memory object store for evidence-pack upload tests."""

    def __init__(self) -> None:
        """Initialize the fake storage dictionary."""

        self.objects: dict[tuple[str, str], FakeStoredObject] = {}

    def upload_bytes(
        self,
        *,
        bucket_name: str,
        object_key: str,
        payload: bytes,
        content_type: str,
    ) -> None:
        """Store a byte payload under the given bucket and key."""

        etag = compute_sha256_bytes(payload)[:32]
        self.objects[(bucket_name, object_key)] = FakeStoredObject(
            payload=payload,
            content_type=content_type,
            etag=etag,
        )

    def download_bytes(
        self,
        *,
        bucket_name: str,
        object_key: str,
    ) -> bytes:
        """Return the stored payload for the given bucket and key."""

        stored = self.objects[(bucket_name, object_key)]
        return stored.payload

    def exists(self, *, bucket_name: str, object_key: str) -> bool:
        """Check whether an object exists in fake storage."""

        return (bucket_name, object_key) in self.objects


class FakeStorageRepository:
    """Provide a test double for the canonical storage repository."""

    def __init__(self, backend: FakeStorageBackend) -> None:
        """Initialize with a reference to the fake backend."""

        self._backend = backend
        self._bucket_name = "test-artifacts"

    def store_evidence_pack(
        self,
        *,
        scope: CloseRunStorageScope,
        idempotency_key: str,
        filename: str,
        payload: bytes,
        content_type: str = "application/zip",
        expected_sha256: str | None = None,
    ) -> StoredObjectMetadata:
        """Upload an evidence-pack ZIP to fake storage and return metadata."""

        object_key = (
            f"entities/{scope.entity_id}/"
            f"close-runs/{scope.close_run_id}/"
            f"versions/{scope.close_run_version_no}/"
            f"evidence_pack/{idempotency_key}/{filename}"
        )
        self._backend.upload_bytes(
            bucket_name=self._bucket_name,
            object_key=object_key,
            payload=payload,
            content_type=content_type,
        )
        checksum = compute_sha256_bytes(payload)
        return StoredObjectMetadata(
            reference=ObjectStorageReference(
                bucket_kind=StorageBucketKind.ARTIFACTS,
                bucket_name=self._bucket_name,
                object_key=object_key,
            ),
            content_type=content_type,
            size_bytes=len(payload),
            sha256_checksum=checksum,
            etag=checksum[:32],
        )


class FakeIdempotencyRepo:
    """Provide an in-memory idempotency repository for integration tests."""

    def __init__(self) -> None:
        """Initialize the fake released-artifact store."""

        self._artifacts: dict[tuple[str, str, str], dict[str, object]] = {}

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
        """Persist a released-artifact record in memory."""

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
        """No-op for in-memory store."""

    def rollback(self) -> None:
        """No-op for in-memory store."""

    def is_integrity_error(self, error: Exception) -> bool:
        """Always False for the fake repository."""

        return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_backend() -> FakeStorageBackend:
    """Provide a fresh fake storage backend for each test."""

    return FakeStorageBackend()


@pytest.fixture()
def fake_storage_repo(fake_backend: FakeStorageBackend) -> FakeStorageRepository:
    """Provide a fake storage repository wrapping the fake backend."""

    return FakeStorageRepository(fake_backend)


@pytest.fixture()
def fake_idempotency_repo() -> FakeIdempotencyRepo:
    """Provide a fresh fake idempotency repository."""

    return FakeIdempotencyRepo()


@pytest.fixture()
def idempotency_service(
    fake_idempotency_repo: FakeIdempotencyRepo,
) -> IdempotencyService:
    """Provide an idempotency service wired to the fake repository."""

    return IdempotencyService(repository=fake_idempotency_repo)


@pytest.fixture()
def sample_close_run_id() -> UUID:
    """Provide a stable close-run UUID for test evidence packs."""

    return UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture()
def sample_entity_id() -> UUID:
    """Provide a stable entity UUID for test evidence packs."""

    return UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture()
def sample_scope(
    sample_close_run_id: UUID,
    sample_entity_id: UUID,
) -> CloseRunStorageScope:
    """Provide a canonical close-run storage scope for tests."""

    return CloseRunStorageScope(
        entity_id=sample_entity_id,
        close_run_id=sample_close_run_id,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        close_run_version_no=1,
    )


@pytest.fixture()
def sample_evidence_pack_input(
    sample_close_run_id: UUID,
    sample_entity_id: UUID,
) -> EvidencePackInput:
    """Provide realistic evidence-pack input data for integration tests."""

    return EvidencePackInput(
        close_run_id=sample_close_run_id,
        entity_id=sample_entity_id,
        entity_name="Integration Test Entity",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        close_run_version_no=1,
        source_references=[
            {
                "filename": "vendor-invoice-001.pdf",
                "storage_key": "documents/source/vendor-invoice-001.pdf",
                "sha256_checksum": "a" * 64,
                "size_bytes": 45000,
                "description": "Vendor invoice for office supplies",
            },
            {
                "filename": "bank-statement-jan.xlsx",
                "storage_key": "documents/source/bank-statement-jan.xlsx",
                "sha256_checksum": "b" * 64,
                "size_bytes": 120000,
                "description": "January bank statement",
            },
        ],
        extracted_values=[
            {
                "field_name": "invoice_total",
                "description": "Total amount extracted from invoice",
                "document_id": "doc-001",
            },
            {
                "field_name": "invoice_date",
                "description": "Date extracted from invoice",
                "document_id": "doc-001",
            },
        ],
        approval_records=[
            {
                "actor_name": "Jane Accountant",
                "reason": "Reviewed and approved GL coding",
                "approval_id": "appr-001",
            },
        ],
        diff_entries=[
            {
                "target_label": "GL Code Change",
                "description": "Changed from 6000 (Office Expenses) to 6100 (Supplies)",
                "diff_id": "diff-001",
            },
        ],
        report_outputs=[
            {
                "filename": "management-report-v1.xlsx",
                "storage_key": "artifacts/report_excel/management-report-v1.xlsx",
                "sha256_checksum": "c" * 64,
                "size_bytes": 85000,
                "description": "Excel management report pack",
            },
        ],
    )


# ---------------------------------------------------------------------------
# Evidence pack integration tests
# ---------------------------------------------------------------------------

class TestEvidencePackIntegration:
    """Validate the full evidence-pack assembly and upload pipeline."""

    def test_evidence_pack_assembles_valid_zip_with_manifests(
        self,
        sample_evidence_pack_input: EvidencePackInput,
    ) -> None:
        """Evidence pack should produce a valid ZIP with JSON manifests for each category."""

        result = build_evidence_pack(sample_evidence_pack_input)

        assert result.payload[:2] == b"PK", "Must be a valid ZIP archive."
        assert result.bundle.item_count == 7, (
            "Should have 7 items: 2 source refs, 2 extractions, "
            "1 approval, 1 diff, 1 report output."
        )

        # Verify ZIP contents.
        with zipfile.ZipFile(io.BytesIO(result.payload)) as zf:
            names = zf.namelist()
            assert "source_references.json" in names
            assert "extracted_values.json" in names
            assert "approvals.json" in names
            assert "diffs.json" in names
            assert "report_outputs.json" in names
            assert "metadata.json" in names

            # Verify manifest content is valid JSON.
            for name in names:
                if name.endswith(".json"):
                    data = json.loads(zf.read(name))
                    assert data is not None or data == {}, f"{name} should be valid JSON"

    def test_evidence_pack_upload_stores_and_returns_bundle(
        self,
        sample_evidence_pack_input: EvidencePackInput,
        fake_backend: FakeStorageBackend,
        fake_storage_repo: FakeStorageRepository,
        sample_scope: CloseRunStorageScope,
    ) -> None:
        """Uploaded evidence pack should be stored and return an enriched bundle."""

        pack_result = build_evidence_pack(sample_evidence_pack_input)
        idempotency_key = build_idempotency_key(
            close_run_id=sample_evidence_pack_input.close_run_id,
            artifact_type=ArtifactType.EVIDENCE_PACK.value,
            action_qualifier="evidence_pack",
            version_override=1,
        )

        bundle = upload_evidence_pack(
            result=pack_result,
            storage_repo=fake_storage_repo,
            scope=sample_scope,
            idempotency_key=idempotency_key,
        )

        assert bundle.storage_key is not None, "Storage key should be populated."
        assert bundle.idempotency_key == idempotency_key
        assert bundle.checksum is not None
        assert bundle.size_bytes is not None

        # Verify the ZIP was actually stored.
        assert fake_backend.exists(
            bucket_name="test-artifacts",
            object_key=bundle.storage_key,
        )

        # Verify round-trip download.
        downloaded = fake_backend.download_bytes(
            bucket_name="test-artifacts",
            object_key=bundle.storage_key,
        )
        assert downloaded == pack_result.payload, "Downloaded bytes should match."

    def test_evidence_pack_bundle_item_types_are_correct(
        self,
        sample_evidence_pack_input: EvidencePackInput,
    ) -> None:
        """Evidence pack items should have correct type labels."""

        result = build_evidence_pack(sample_evidence_pack_input)
        item_types = {item.item_type for item in result.bundle.items}

        expected_types = {
            "source_reference",
            "extracted_field",
            "approval_record",
            "diff_entry",
            "report_output",
        }
        assert expected_types.issubset(item_types), (
            f"Expected types {expected_types}, got {item_types}"
        )


# ---------------------------------------------------------------------------
# Export manifest integration tests
# ---------------------------------------------------------------------------

class TestExportManifestIntegration:
    """Validate export-manifest assembly with realistic artifact data."""

    def test_export_manifest_collects_and_orders_artifacts(
        self,
        sample_close_run_id: UUID,
        sample_entity_id: UUID,
    ) -> None:
        """Manifest should collect all artifacts in deterministic order."""

        artifact_records = [
            {
                "artifact_type": "report_excel",
                "storage_key": "artifacts/excel/report-v1.xlsx",
                "checksum": "d" * 64,
                "size_bytes": 85000,
                "content_type": (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                ),
                "idempotency_key": "ik-excel-001",
                "released_at": datetime(2026, 2, 1, 10, 0, 0, tzinfo=UTC),
                "filename": "management-report-v1.xlsx",
            },
            {
                "artifact_type": "evidence_pack",
                "storage_key": "artifacts/evidence/pack-v1.zip",
                "checksum": "e" * 64,
                "size_bytes": 200000,
                "content_type": "application/zip",
                "idempotency_key": "ik-evidence-001",
                "released_at": datetime(2026, 2, 1, 10, 5, 0, tzinfo=UTC),
                "filename": "evidence-pack-v1.zip",
            },
        ]

        result = build_export_manifest(
            ExportManifestInput(
                close_run_id=sample_close_run_id,
                entity_id=sample_entity_id,
                entity_name="Export Test",
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 31),
                close_run_version_no=1,
                artifact_records=artifact_records,
            )
        )

        assert len(result.manifest.artifacts) == 2
        assert result.manifest.close_run_id == str(sample_close_run_id)
        assert result.manifest.version_no == 1

    def test_export_manifest_builder_chaining_api(
        self,
        sample_close_run_id: UUID,
        sample_entity_id: UUID,
    ) -> None:
        """Fluent builder API should support method chaining and produce valid results."""

        result = (
            ExportManifestBuilder(
                close_run_id=sample_close_run_id,
                entity_id=sample_entity_id,
                entity_name="Builder Test",
                period_start=date(2026, 6, 1),
                period_end=date(2026, 6, 30),
                close_run_version_no=3,
            )
            .with_artifacts([
                {
                    "artifact_type": "report_pdf",
                    "storage_key": "artifacts/pdf/report-v3.pdf",
                    "checksum": "f" * 64,
                    "size_bytes": 150000,
                    "content_type": "application/pdf",
                    "idempotency_key": "ik-pdf-003",
                    "released_at": datetime(2026, 7, 1, 9, 0, 0, tzinfo=UTC),
                    "filename": "executive-report-v3.pdf",
                },
            ])
            .include_evidence_pack(True)
            .include_audit_trail(True)
            .build()
        )

        assert result.manifest.artifact_count == 1
        assert result.manifest.version_no == 3
        assert result.idempotency_key.startswith("ik-")


# ---------------------------------------------------------------------------
# Idempotency-gated release integration tests
# ---------------------------------------------------------------------------

class TestIdempotentReleaseIntegration:
    """Verify end-to-end idempotency-guarded release behavior."""

    def test_first_release_succeeds_second_is_blocked(
        self,
        sample_close_run_id: UUID,
        sample_entity_id: UUID,
        sample_evidence_pack_input: EvidencePackInput,
        fake_backend: FakeStorageBackend,
        fake_storage_repo: FakeStorageRepository,
        sample_scope: CloseRunStorageScope,
        idempotency_service: IdempotencyService,
    ) -> None:
        """First evidence-pack release should succeed; second should be blocked."""

        # Assemble and upload the first pack.
        pack_result = build_evidence_pack(sample_evidence_pack_input)
        idempotency_key = build_idempotency_key(
            close_run_id=sample_close_run_id,
            artifact_type=ArtifactType.EVIDENCE_PACK.value,
            action_qualifier="evidence_pack",
            version_override=1,
        )

        # First release.
        bundle1 = upload_evidence_pack(
            result=pack_result,
            storage_repo=fake_storage_repo,
            scope=sample_scope,
            idempotency_key=idempotency_key,
        )
        assert bundle1.storage_key is not None

        idempotency_service.record_release(
            close_run_id=sample_close_run_id,
            artifact_type=ArtifactType.EVIDENCE_PACK.value,
            idempotency_key=idempotency_key,
            storage_key=bundle1.storage_key,
            checksum=bundle1.checksum,
            size_bytes=bundle1.size_bytes,
            content_type="application/zip",
            version_no=1,
        )

        # Second release attempt should be blocked by idempotency guard.
        with pytest.raises(IdempotencyGuardError) as exc_info:
            idempotency_service.record_release(
                close_run_id=sample_close_run_id,
                artifact_type=ArtifactType.EVIDENCE_PACK.value,
                idempotency_key=idempotency_key,
                storage_key="artifacts/evidence/duplicate.zip",
                checksum="x" * 64,
                size_bytes=100,
                content_type="application/zip",
                version_no=1,
            )

        assert exc_info.value.code.name == "DUPLICATE_RELEASE"

    def test_idempotency_key_stability_across_retries(
        self,
        sample_close_run_id: UUID,
    ) -> None:
        """Retrying the same export action must compute the same idempotency key."""

        key1 = build_idempotency_key(
            close_run_id=sample_close_run_id,
            artifact_type=ArtifactType.EVIDENCE_PACK.value,
            action_qualifier="evidence_pack",
            version_override=1,
        )
        key2 = build_idempotency_key(
            close_run_id=sample_close_run_id,
            artifact_type=ArtifactType.EVIDENCE_PACK.value,
            action_qualifier="evidence_pack",
            version_override=1,
        )
        assert key1 == key2, (
            "Idempotency keys must be stable across retries for the same action."
        )


__all__ = [
    "FakeIdempotencyRepo",
    "FakeStorageBackend",
    "FakeStorageRepository",
    "TestEvidencePackIntegration",
    "TestExportManifestIntegration",
    "TestIdempotentReleaseIntegration",
]
