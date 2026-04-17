"""
Purpose: Verify the canonical storage helpers, key builders, and repository behavior from Step 9.
Scope: Deterministic key generation, checksum handling, and storage repository uploads
without requiring a live MinIO instance.
Dependencies: services/storage/*.py and the strict storage contract models.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from typing import BinaryIO
from uuid import UUID, uuid4

from services.common.enums import ArtifactType
from services.contracts.storage_models import (
    CloseRunStorageScope,
    DerivativeKind,
    StorageBucketKind,
)
from services.storage.checksums import (
    compute_sha256_bytes,
    compute_sha256_stream,
    compute_sha256_text,
    ensure_matching_sha256,
)
from services.storage.client import StorageClient
from services.storage.keys import (
    build_artifact_key,
    build_close_run_storage_prefix,
    build_derivative_key,
    build_ocr_text_key,
    build_source_document_key,
    normalize_filename,
)
from services.storage.repository import StorageRepository


def test_checksum_helpers_hash_expected_payloads_and_restore_stream_position() -> None:
    """Ensure the SHA-256 helpers stay deterministic across bytes, text, and streams."""

    payload = b"finance-data"
    stream = BytesIO(payload)
    expected_checksum = compute_sha256_bytes(payload)

    assert compute_sha256_text("finance-data") == expected_checksum
    assert compute_sha256_stream(stream) == expected_checksum
    assert stream.tell() == 0


def test_ensure_matching_sha256_rejects_mismatch() -> None:
    """Ensure checksum mismatches fail fast instead of being tolerated silently."""

    checksum = compute_sha256_bytes(b"correct")

    try:
        ensure_matching_sha256(
            expected=checksum,
            actual=compute_sha256_bytes(b"incorrect"),
            context="unit-test payload",
        )
    except ValueError as error:
        assert "unit-test payload" in str(error)
    else:
        raise AssertionError("Expected checksum mismatch to raise ValueError.")


def test_storage_key_builders_use_canonical_close_run_layout() -> None:
    """Ensure every object family nests under the documented close-run prefix."""

    scope = _build_scope()
    document_id = UUID("11111111-1111-1111-1111-111111111111")

    prefix = build_close_run_storage_prefix(scope)

    assert prefix.endswith(f"close-runs/{scope.close_run_id}/versions/{scope.close_run_version_no}")
    assert normalize_filename(" Trial Balance FY26 .PDF ", default_stem="document") == (
        "trial-balance-fy26.pdf"
    )
    assert build_source_document_key(
        scope=scope,
        document_id=document_id,
        original_filename="Trial Balance FY26.PDF",
    ).startswith(f"{prefix}/documents/source/{document_id}/")
    assert build_ocr_text_key(
        scope=scope,
        document_id=document_id,
        document_version_no=2,
        source_filename="Trial Balance FY26.PDF",
    ) == f"{prefix}/documents/ocr/{document_id}/versions/2/trial-balance-fy26.txt"
    assert build_derivative_key(
        scope=scope,
        document_id=document_id,
        document_version_no=2,
        derivative_kind=DerivativeKind.NORMALIZED_DOCUMENT,
        filename="Trial Balance FY26.pdf",
    ) == (
        f"{prefix}/documents/derivatives/{document_id}/versions/2/"
        "normalized_document/trial-balance-fy26.pdf"
    )
    assert build_artifact_key(
        scope=scope,
        artifact_type=ArtifactType.REPORT_PDF,
        idempotency_key="close-run-2026-03-report",
        filename="Management Pack v1.pdf",
    ) == (
        f"{prefix}/artifacts/report_pdf/close-run-2026-03-report/management-pack-v1.pdf"
    )


def test_storage_repository_routes_documents_derivatives_and_artifacts_to_expected_buckets(
) -> None:
    """Ensure repository methods map each object family into its canonical bucket and key space."""

    fake_minio = FakeMinio()
    client = StorageClient(
        minio_client=fake_minio,
        bucket_names={
            StorageBucketKind.DOCUMENTS: "documents-bucket",
            StorageBucketKind.DERIVATIVES: "derivatives-bucket",
            StorageBucketKind.ARTIFACTS: "artifacts-bucket",
        },
    )
    repository = StorageRepository(client=client)
    scope = _build_scope()
    document_id = uuid4()

    source_metadata = repository.store_source_document(
        scope=scope,
        document_id=document_id,
        original_filename="Vendor Invoice 001.pdf",
        payload=b"%PDF-1.7",
        content_type="application/pdf",
    )
    ocr_metadata = repository.store_ocr_text(
        scope=scope,
        document_id=document_id,
        document_version_no=1,
        source_filename="Vendor Invoice 001.pdf",
        text="Invoice total NGN 10,000",
    )
    derivative_metadata = repository.store_derivative(
        scope=scope,
        document_id=document_id,
        document_version_no=1,
        derivative_kind=DerivativeKind.NORMALIZED_DOCUMENT,
        filename="Vendor Invoice 001-normalized.pdf",
        payload=b"normalized-pdf",
        content_type="application/pdf",
    )
    artifact_metadata = repository.store_evidence_pack(
        scope=scope,
        idempotency_key="evidence-pack-2026-03",
        filename="Evidence Pack.zip",
        payload=b"zip-bytes",
    )

    assert source_metadata.reference.bucket_kind is StorageBucketKind.DOCUMENTS
    assert source_metadata.reference.bucket_name == "documents-bucket"
    assert source_metadata.reference.object_key.endswith("/vendor-invoice-001.pdf")
    assert ocr_metadata.reference.bucket_kind is StorageBucketKind.DERIVATIVES
    assert ocr_metadata.derivative_kind is DerivativeKind.OCR_TEXT
    assert ocr_metadata.reference.object_key.endswith("/vendor-invoice-001.txt")
    assert derivative_metadata.reference.bucket_name == "derivatives-bucket"
    assert derivative_metadata.reference.object_key.endswith(
        "/normalized_document/vendor-invoice-001-normalized.pdf"
    )
    assert artifact_metadata.reference.bucket_kind is StorageBucketKind.ARTIFACTS
    assert artifact_metadata.artifact_type is ArtifactType.EVIDENCE_PACK
    assert artifact_metadata.reference.object_key.endswith(
        "/artifacts/evidence_pack/evidence-pack-2026-03/evidence-pack.zip"
    )

    downloaded_text = repository.download_text(reference=ocr_metadata.reference)
    assert downloaded_text == "Invoice total NGN 10,000"
    assert len(fake_minio.objects) == 4

    repository.delete_source_document(storage_key=source_metadata.reference.object_key)
    repository.delete_derivative_object(object_key=ocr_metadata.reference.object_key)
    repository.delete_artifact_object(object_key=artifact_metadata.reference.object_key)

    assert (
        source_metadata.reference.bucket_name,
        source_metadata.reference.object_key,
    ) not in fake_minio.objects
    assert (
        ocr_metadata.reference.bucket_name,
        ocr_metadata.reference.object_key,
    ) not in fake_minio.objects
    assert (
        artifact_metadata.reference.bucket_name,
        artifact_metadata.reference.object_key,
    ) not in fake_minio.objects


@dataclass
class FakeStoredObject:
    """Represent one object stored by the fake MinIO transport."""

    payload: bytes
    content_type: str
    etag: str
    version_id: str | None
    last_modified: datetime


@dataclass(frozen=True)
class FakePutResult:
    """Mimic the subset of MinIO put-object results used by the storage client."""

    bucket_name: str
    object_name: str
    etag: str
    version_id: str | None


@dataclass(frozen=True)
class FakeStatResult:
    """Mimic the subset of MinIO stat-object results used by the storage client."""

    bucket_name: str
    object_name: str
    etag: str
    version_id: str | None
    size: int
    content_type: str
    last_modified: datetime


class FakeGetObjectResponse:
    """Provide the cleanup hooks expected by the low-level storage client."""

    def __init__(self, payload: bytes) -> None:
        """Capture the payload that future reads should return."""

        self._payload = payload

    def read(self, amt: int | None = None) -> bytes:
        """Return the full payload or the caller-requested prefix."""

        if amt is None:
            return self._payload
        return self._payload[:amt]

    def close(self) -> None:
        """Mirror the MinIO response API without tracking connection state."""

    def release_conn(self) -> None:
        """Mirror the MinIO response API without pooling behavior."""


class FakeMinio:
    """Provide a tiny in-memory MinIO stand-in for storage unit tests."""

    def __init__(self) -> None:
        """Initialize the fake storage dictionary used by the repository tests."""

        self.objects: dict[tuple[str, str], FakeStoredObject] = {}

    def put_object(
        self,
        *,
        bucket_name: str,
        object_name: str,
        data: BinaryIO,
        length: int,
        content_type: str,
        metadata: dict[str, str | list[str] | tuple[str]] | None = None,
    ) -> FakePutResult:
        """Store the uploaded payload and return a deterministic fake write result."""

        del metadata

        payload = data.read(length)
        etag = compute_sha256_bytes(payload)[:32]
        self.objects[(bucket_name, object_name)] = FakeStoredObject(
            payload=payload,
            content_type=content_type,
            etag=etag,
            version_id=None,
            last_modified=datetime.now(tz=UTC),
        )
        return FakePutResult(
            bucket_name=bucket_name,
            object_name=object_name,
            etag=etag,
            version_id=None,
        )

    def get_object(self, *, bucket_name: str, object_name: str) -> FakeGetObjectResponse:
        """Return a readable response wrapper for an existing stored payload."""

        stored_object = self.objects[(bucket_name, object_name)]
        return FakeGetObjectResponse(stored_object.payload)

    def stat_object(self, *, bucket_name: str, object_name: str) -> FakeStatResult:
        """Return fake provider metadata for a stored object."""

        stored_object = self.objects[(bucket_name, object_name)]
        return FakeStatResult(
            bucket_name=bucket_name,
            object_name=object_name,
            etag=stored_object.etag,
            version_id=stored_object.version_id,
            size=len(stored_object.payload),
            content_type=stored_object.content_type,
            last_modified=stored_object.last_modified,
        )

    def remove_object(self, *, bucket_name: str, object_name: str) -> None:
        """Delete an object from the fake storage map."""

        del self.objects[(bucket_name, object_name)]


def _build_scope() -> CloseRunStorageScope:
    """Create a representative close-run storage scope for deterministic tests."""

    return CloseRunStorageScope(
        entity_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        close_run_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        period_start=datetime(2026, 3, 1, tzinfo=UTC).date(),
        period_end=datetime(2026, 3, 31, tzinfo=UTC).date(),
        close_run_version_no=2,
    )
