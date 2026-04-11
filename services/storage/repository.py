"""
Purpose: Expose canonical repository methods for source files, derivatives, and released artifacts.
Scope: Object-key generation, checksum enforcement, MinIO uploads, and metadata
materialization for document intake, OCR, reporting, and evidence-pack workflows.
Dependencies: Storage client, checksum helpers, canonical domain enums, and storage contracts.
"""

from __future__ import annotations

from pathlib import PurePath
from uuid import UUID

from services.common.enums import ArtifactType
from services.common.settings import AppSettings
from services.contracts.storage_models import (
    ArtifactStorageMetadata,
    CloseRunStorageScope,
    DerivativeKind,
    DerivativeStorageMetadata,
    ObjectStorageReference,
    SourceDocumentStorageMetadata,
    StorageBucketKind,
)
from services.storage.checksums import (
    compute_sha256_bytes,
    compute_sha256_text,
    ensure_matching_sha256,
)
from services.storage.client import StorageClient
from services.storage.keys import (
    build_artifact_key,
    build_derivative_key,
    build_ocr_text_key,
    build_source_document_key,
)


class StorageRepository:
    """Provide semantic object-storage operations grounded in close-run workflow context."""

    def __init__(
        self,
        *,
        client: StorageClient | None = None,
        settings: AppSettings | None = None,
    ) -> None:
        """Create the repository with either an injected client or the canonical runtime config."""

        self._client = client or StorageClient.from_settings(settings)

    def store_source_document(
        self,
        *,
        scope: CloseRunStorageScope,
        document_id: UUID,
        original_filename: str,
        payload: bytes,
        content_type: str,
        expected_sha256: str | None = None,
    ) -> SourceDocumentStorageMetadata:
        """Store an original uploaded file in the canonical document bucket."""

        object_key = build_source_document_key(
            scope=scope,
            document_id=document_id,
            original_filename=original_filename,
        )
        checksum = compute_sha256_bytes(payload)
        if expected_sha256 is not None:
            ensure_matching_sha256(
                expected=expected_sha256,
                actual=checksum,
                context=f"source document {document_id}",
            )

        stat = self._client.upload_bytes(
            bucket_kind=StorageBucketKind.DOCUMENTS,
            object_key=object_key,
            payload=payload,
            content_type=content_type,
        )
        return SourceDocumentStorageMetadata(
            reference=_build_reference(stat.bucket_kind, stat.bucket_name, stat.object_key),
            content_type=stat.content_type,
            size_bytes=stat.size_bytes,
            sha256_checksum=checksum,
            etag=stat.etag,
            version_id=stat.version_id,
            document_id=document_id,
            original_filename=_extract_basename(original_filename),
        )

    def store_ocr_text(
        self,
        *,
        scope: CloseRunStorageScope,
        document_id: UUID,
        document_version_no: int,
        source_filename: str,
        text: str,
        content_type: str = "text/plain; charset=utf-8",
        expected_sha256: str | None = None,
    ) -> DerivativeStorageMetadata:
        """Store OCR text for a document version in the canonical derivative bucket."""

        payload = text.encode("utf-8")
        checksum = compute_sha256_text(text)
        if expected_sha256 is not None:
            ensure_matching_sha256(
                expected=expected_sha256,
                actual=checksum,
                context=f"OCR text for document {document_id} version {document_version_no}",
            )

        object_key = build_ocr_text_key(
            scope=scope,
            document_id=document_id,
            document_version_no=document_version_no,
            source_filename=source_filename,
        )
        stat = self._client.upload_bytes(
            bucket_kind=StorageBucketKind.DERIVATIVES,
            object_key=object_key,
            payload=payload,
            content_type=content_type,
        )
        return DerivativeStorageMetadata(
            reference=_build_reference(stat.bucket_kind, stat.bucket_name, stat.object_key),
            content_type=stat.content_type,
            size_bytes=stat.size_bytes,
            sha256_checksum=checksum,
            etag=stat.etag,
            version_id=stat.version_id,
            document_id=document_id,
            document_version_no=document_version_no,
            derivative_kind=DerivativeKind.OCR_TEXT,
        )

    def store_derivative(
        self,
        *,
        scope: CloseRunStorageScope,
        document_id: UUID,
        document_version_no: int,
        derivative_kind: DerivativeKind,
        filename: str,
        payload: bytes,
        content_type: str,
        expected_sha256: str | None = None,
    ) -> DerivativeStorageMetadata:
        """Store a normalized or otherwise derived document payload in the derivative bucket."""

        checksum = compute_sha256_bytes(payload)
        if expected_sha256 is not None:
            ensure_matching_sha256(
                expected=expected_sha256,
                actual=checksum,
                context=f"{derivative_kind.value} for document {document_id}",
            )

        object_key = build_derivative_key(
            scope=scope,
            document_id=document_id,
            document_version_no=document_version_no,
            derivative_kind=derivative_kind,
            filename=filename,
        )
        stat = self._client.upload_bytes(
            bucket_kind=StorageBucketKind.DERIVATIVES,
            object_key=object_key,
            payload=payload,
            content_type=content_type,
        )
        return DerivativeStorageMetadata(
            reference=_build_reference(stat.bucket_kind, stat.bucket_name, stat.object_key),
            content_type=stat.content_type,
            size_bytes=stat.size_bytes,
            sha256_checksum=checksum,
            etag=stat.etag,
            version_id=stat.version_id,
            document_id=document_id,
            document_version_no=document_version_no,
            derivative_kind=derivative_kind,
        )

    def store_artifact(
        self,
        *,
        scope: CloseRunStorageScope,
        artifact_type: ArtifactType,
        idempotency_key: str,
        filename: str,
        payload: bytes,
        content_type: str,
        expected_sha256: str | None = None,
    ) -> ArtifactStorageMetadata:
        """Store a released artifact such as a report pack or QuickBooks export."""

        checksum = compute_sha256_bytes(payload)
        if expected_sha256 is not None:
            ensure_matching_sha256(
                expected=expected_sha256,
                actual=checksum,
                context=f"{artifact_type.value} artifact for close run {scope.close_run_id}",
            )

        object_key = build_artifact_key(
            scope=scope,
            artifact_type=artifact_type,
            idempotency_key=idempotency_key,
            filename=filename,
        )
        stat = self._client.upload_bytes(
            bucket_kind=StorageBucketKind.ARTIFACTS,
            object_key=object_key,
            payload=payload,
            content_type=content_type,
        )
        return ArtifactStorageMetadata(
            reference=_build_reference(stat.bucket_kind, stat.bucket_name, stat.object_key),
            content_type=stat.content_type,
            size_bytes=stat.size_bytes,
            sha256_checksum=checksum,
            etag=stat.etag,
            version_id=stat.version_id,
            artifact_type=artifact_type,
            close_run_version_no=scope.close_run_version_no,
            idempotency_key=idempotency_key,
        )

    def store_evidence_pack(
        self,
        *,
        scope: CloseRunStorageScope,
        idempotency_key: str,
        filename: str,
        payload: bytes,
        content_type: str = "application/zip",
        expected_sha256: str | None = None,
    ) -> ArtifactStorageMetadata:
        """Store the canonical evidence-pack artifact snapshot for a close-run version."""

        return self.store_artifact(
            scope=scope,
            artifact_type=ArtifactType.EVIDENCE_PACK,
            idempotency_key=idempotency_key,
            filename=filename,
            payload=payload,
            content_type=content_type,
            expected_sha256=expected_sha256,
        )

    def download_bytes(self, *, reference: ObjectStorageReference) -> bytes:
        """Read an object's full byte payload from the resolved bucket and key."""

        return self._client.download_bytes(
            bucket_kind=reference.bucket_kind,
            object_key=reference.object_key,
        )

    def download_source_document(self, *, storage_key: str) -> bytes:
        """Read one original source document from the canonical document bucket."""

        if not storage_key.strip():
            raise ValueError("storage_key must be a non-empty object key.")

        return self._client.download_bytes(
            bucket_kind=StorageBucketKind.DOCUMENTS,
            object_key=storage_key,
        )

    def download_text(
        self,
        *,
        reference: ObjectStorageReference,
        encoding: str = "utf-8",
    ) -> str:
        """Read an object's byte payload and decode it into text."""

        payload = self.download_bytes(reference=reference)
        return payload.decode(encoding)

    def delete_object(self, *, reference: ObjectStorageReference) -> None:
        """Delete an object through the low-level client when higher-level policy allows it."""

        self._client.delete_object(
            bucket_kind=reference.bucket_kind,
            object_key=reference.object_key,
        )


def _build_reference(
    bucket_kind: StorageBucketKind,
    bucket_name: str,
    object_key: str,
) -> ObjectStorageReference:
    """Materialize a validated storage reference from client operation results."""

    return ObjectStorageReference(
        bucket_kind=bucket_kind,
        bucket_name=bucket_name,
        object_key=object_key,
    )


def _extract_basename(filename: str) -> str:
    """Return the basename of a client-provided filename and reject empty values."""

    basename = PurePath(filename.strip()).name
    if not basename or basename in {".", ".."}:
        raise ValueError("filename must include a non-empty basename.")

    return basename


__all__ = ["StorageRepository"]
