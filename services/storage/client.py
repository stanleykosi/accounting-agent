"""
Purpose: Wrap the MinIO SDK behind a typed client used by storage repositories.
Scope: Logical bucket resolution, upload/download/stat/delete operations,
and fail-fast error translation for canonical storage interactions.
Dependencies: MinIO, shared runtime settings, and storage contract models.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import BinaryIO, Protocol, cast

from minio import Minio
from minio.error import S3Error
from services.common.settings import AppSettings, get_settings
from services.contracts.storage_models import StorageBucketKind


class PutObjectResultLike(Protocol):
    """Describe the MinIO put-object fields that the wrapper depends on."""

    bucket_name: str
    object_name: str
    etag: str
    version_id: str | None


class StatObjectResultLike(Protocol):
    """Describe the MinIO stat-object fields that the wrapper depends on."""

    bucket_name: str
    object_name: str
    etag: str
    version_id: str | None
    size: int
    content_type: str | None
    last_modified: datetime | None


class GetObjectResponseLike(Protocol):
    """Describe the MinIO get-object response methods that must be cleaned up explicitly."""

    def read(self, amt: int | None = None) -> bytes:
        """Read bytes from the object body."""

    def close(self) -> None:
        """Close the underlying HTTP response."""

    def release_conn(self) -> None:
        """Return the pooled HTTP connection to MinIO."""


class MinioClientLike(Protocol):
    """Describe the subset of the MinIO SDK used by the canonical storage wrapper."""

    def put_object(
        self,
        bucket_name: str,
        object_name: str,
        data: BinaryIO,
        length: int,
        content_type: str,
        metadata: dict[str, str | list[str] | tuple[str]] | None = None,
    ) -> PutObjectResultLike:
        """Upload an object and return provider metadata for the write."""

    def get_object(self, bucket_name: str, object_name: str) -> GetObjectResponseLike:
        """Read an object's payload from the provider."""

    def stat_object(self, bucket_name: str, object_name: str) -> StatObjectResultLike:
        """Read provider metadata for one object."""

    def remove_object(self, bucket_name: str, object_name: str) -> None:
        """Delete one object from the provider."""


@dataclass(frozen=True)
class StorageObjectStat:
    """Capture the canonical metadata returned by low-level storage operations."""

    bucket_kind: StorageBucketKind
    bucket_name: str
    object_key: str
    size_bytes: int
    content_type: str
    etag: str
    version_id: str | None
    last_modified: datetime | None = None


class StorageError(RuntimeError):
    """Base exception for canonical storage failures that callers should treat as actionable."""


class StorageObjectNotFoundError(StorageError):
    """Raised when the requested object does not exist in the configured bucket."""


class StorageClient:
    """Provide typed, logical-bucket access to the canonical MinIO runtime."""

    def __init__(
        self,
        *,
        minio_client: object,
        bucket_names: dict[StorageBucketKind, str],
    ) -> None:
        """Create a storage client with an already-configured MinIO transport."""

        missing_bucket_kinds = sorted(
            bucket_kind.value
            for bucket_kind in StorageBucketKind
            if bucket_kind not in bucket_names
        )
        if missing_bucket_kinds:
            formatted = ", ".join(missing_bucket_kinds)
            raise ValueError(f"Missing bucket configuration for: {formatted}.")

        self._minio_client = minio_client
        self._bucket_names = dict(bucket_names)

    @classmethod
    def from_settings(
        cls,
        settings: AppSettings | None = None,
    ) -> StorageClient:
        """Construct the canonical storage client from repository settings."""

        resolved_settings = settings or get_settings()
        minio_client = Minio(
            endpoint=resolved_settings.storage.endpoint,
            access_key=resolved_settings.storage.access_key,
            secret_key=resolved_settings.storage.secret_key.get_secret_value(),
            secure=resolved_settings.storage.secure,
            region=resolved_settings.storage.region,
        )
        return cls(
            minio_client=minio_client,
            bucket_names={
                StorageBucketKind.DOCUMENTS: resolved_settings.storage.document_bucket,
                StorageBucketKind.DERIVATIVES: resolved_settings.storage.derivative_bucket,
                StorageBucketKind.ARTIFACTS: resolved_settings.storage.artifact_bucket,
            },
        )

    def resolve_bucket_name(self, bucket_kind: StorageBucketKind) -> str:
        """Resolve a logical bucket kind into the configured physical MinIO bucket name."""

        return self._bucket_names[bucket_kind]

    def upload_bytes(
        self,
        *,
        bucket_kind: StorageBucketKind,
        object_key: str,
        payload: bytes,
        content_type: str,
        metadata: dict[str, str | list[str] | tuple[str]] | None = None,
    ) -> StorageObjectStat:
        """Upload an in-memory payload into the requested logical bucket."""

        return self.upload_stream(
            bucket_kind=bucket_kind,
            object_key=object_key,
            stream=BytesIO(payload),
            content_type=content_type,
            length=len(payload),
            metadata=metadata,
        )

    def upload_stream(
        self,
        *,
        bucket_kind: StorageBucketKind,
        object_key: str,
        stream: BinaryIO,
        content_type: str,
        length: int,
        metadata: dict[str, str | list[str] | tuple[str]] | None = None,
    ) -> StorageObjectStat:
        """Upload a binary stream into the requested logical bucket with explicit metadata."""

        if length < 0:
            raise ValueError("length must be zero or greater.")

        bucket_name = self.resolve_bucket_name(bucket_kind)
        minio_client = cast(MinioClientLike, self._minio_client)
        try:
            result = minio_client.put_object(
                bucket_name=bucket_name,
                object_name=object_key,
                data=stream,
                length=length,
                content_type=content_type,
                metadata=metadata,
            )
        except S3Error as error:
            raise self._translate_error(
                error,
                bucket_name=bucket_name,
                object_key=object_key,
            ) from error

        return StorageObjectStat(
            bucket_kind=bucket_kind,
            bucket_name=result.bucket_name,
            object_key=result.object_name,
            size_bytes=length,
            content_type=content_type,
            etag=_normalize_etag(result.etag),
            version_id=result.version_id,
        )

    def download_bytes(
        self,
        *,
        bucket_kind: StorageBucketKind,
        object_key: str,
    ) -> bytes:
        """Read the full contents of one object from the requested logical bucket."""

        bucket_name = self.resolve_bucket_name(bucket_kind)
        minio_client = cast(MinioClientLike, self._minio_client)
        response: GetObjectResponseLike | None = None
        try:
            response = minio_client.get_object(
                bucket_name=bucket_name,
                object_name=object_key,
            )
            return response.read()
        except S3Error as error:
            raise self._translate_error(
                error,
                bucket_name=bucket_name,
                object_key=object_key,
            ) from error
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    def stat_object(
        self,
        *,
        bucket_kind: StorageBucketKind,
        object_key: str,
    ) -> StorageObjectStat:
        """Read the current provider metadata for one stored object."""

        bucket_name = self.resolve_bucket_name(bucket_kind)
        minio_client = cast(MinioClientLike, self._minio_client)
        try:
            result = minio_client.stat_object(
                bucket_name=bucket_name,
                object_name=object_key,
            )
        except S3Error as error:
            raise self._translate_error(
                error,
                bucket_name=bucket_name,
                object_key=object_key,
            ) from error

        return StorageObjectStat(
            bucket_kind=bucket_kind,
            bucket_name=result.bucket_name,
            object_key=result.object_name,
            size_bytes=result.size,
            content_type=result.content_type or "application/octet-stream",
            etag=_normalize_etag(result.etag),
            version_id=result.version_id,
            last_modified=result.last_modified,
        )

    def delete_object(
        self,
        *,
        bucket_kind: StorageBucketKind,
        object_key: str,
    ) -> None:
        """Delete an object from storage when higher-level retention policy allows it."""

        bucket_name = self.resolve_bucket_name(bucket_kind)
        minio_client = cast(MinioClientLike, self._minio_client)
        try:
            minio_client.remove_object(
                bucket_name=bucket_name,
                object_name=object_key,
            )
        except S3Error as error:
            raise self._translate_error(
                error,
                bucket_name=bucket_name,
                object_key=object_key,
            ) from error

    def _translate_error(
        self,
        error: S3Error,
        *,
        bucket_name: str,
        object_key: str,
    ) -> StorageError:
        """Translate MinIO SDK errors into explicit repository-facing storage exceptions."""

        if error.code in {"NoSuchKey", "NoSuchObject", "NoSuchVersion"}:
            return StorageObjectNotFoundError(
                f"Object not found: bucket={bucket_name} key={object_key}."
            )

        return StorageError(
            "Storage operation failed for "
            f"bucket={bucket_name} key={object_key}: {error.code} {error.message}"
        )


def _normalize_etag(value: str) -> str:
    """Strip surrounding quotes from provider ETags so metadata stays stable."""

    return value.strip('"')


__all__ = [
    "StorageClient",
    "StorageError",
    "StorageObjectNotFoundError",
    "StorageObjectStat",
]
