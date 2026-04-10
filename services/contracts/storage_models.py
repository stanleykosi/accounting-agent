"""
Purpose: Define strict Pydantic models for canonical object-storage scopes and metadata.
Scope: Close-run storage scoping, logical bucket vocabulary, derivative metadata,
and released artifact metadata shared by repositories, workers, and future APIs.
Dependencies: Pydantic contract defaults, canonical artifact enums, and shared
primitive types from the common service layer.
"""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from uuid import UUID

from pydantic import Field, field_validator, model_validator
from services.common.enums import ArtifactType
from services.common.types import NonNegativeInteger, PositiveInteger
from services.contracts.api_models import ContractModel

SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class StorageBucketKind(StrEnum):
    """Enumerate the logical storage buckets used by the canonical MinIO layout."""

    DOCUMENTS = "documents"
    DERIVATIVES = "derivatives"
    ARTIFACTS = "artifacts"


class DerivativeKind(StrEnum):
    """Enumerate derivative object families produced from uploaded source documents."""

    NORMALIZED_DOCUMENT = "normalized_document"
    OCR_TEXT = "ocr_text"
    SPLIT_DOCUMENT = "split_document"
    EXTRACTED_TABLES = "extracted_tables"
    SUPPORTING_EXPORT = "supporting_export"


class CloseRunStorageScope(ContractModel):
    """Capture the entity and period context every stored object must remain grounded to."""

    entity_id: UUID = Field(
        description="Entity workspace that owns the stored object.",
    )
    close_run_id: UUID = Field(
        description="Close run that the stored object belongs to.",
    )
    period_start: date = Field(
        description="Inclusive accounting-period start date for the close run.",
    )
    period_end: date = Field(
        description="Inclusive accounting-period end date for the close run.",
    )
    close_run_version_no: PositiveInteger = Field(
        description="Working close-run version number used to keep reopened periods traceable.",
    )

    @model_validator(mode="after")
    def validate_period_range(self) -> CloseRunStorageScope:
        """Reject invalid accounting-period ranges before any object keys are built."""

        if self.period_end < self.period_start:
            raise ValueError("period_end must be greater than or equal to period_start.")

        return self


class ObjectStorageReference(ContractModel):
    """Describe the exact storage location for one object in the canonical MinIO layout."""

    bucket_kind: StorageBucketKind = Field(
        description="Logical bucket family used by the repository layer.",
    )
    bucket_name: str = Field(
        min_length=3,
        description="Resolved physical bucket name configured for the active runtime.",
    )
    object_key: str = Field(
        min_length=1,
        description="Canonical slash-delimited key within the resolved bucket.",
    )

    @field_validator("object_key")
    @classmethod
    def validate_object_key(cls, value: str) -> str:
        """Enforce one canonical object-key form without path traversal or empty segments."""

        normalized = value.strip()
        if not normalized:
            raise ValueError("object_key cannot be empty.")
        if normalized.startswith("/") or normalized.endswith("/"):
            raise ValueError("object_key must not start or end with '/'.")
        if "//" in normalized:
            raise ValueError("object_key must not contain empty path segments.")

        segments = normalized.split("/")
        if any(segment in {"", ".", ".."} for segment in segments):
            raise ValueError("object_key contains an invalid path segment.")

        return normalized


class StoredObjectMetadata(ContractModel):
    """Capture the deterministic metadata recorded for any object written to storage."""

    reference: ObjectStorageReference = Field(
        description="Resolved storage location for the object.",
    )
    content_type: str = Field(
        min_length=1,
        description="Content type persisted alongside the stored object.",
    )
    size_bytes: NonNegativeInteger = Field(
        description="Exact byte size of the stored payload.",
    )
    sha256_checksum: str = Field(
        min_length=64,
        max_length=64,
        description="Lower-case SHA-256 checksum of the stored payload.",
    )
    etag: str = Field(
        min_length=1,
        description="ETag returned by the object-storage provider for the current write.",
    )
    version_id: str | None = Field(
        default=None,
        description="Provider version identifier if bucket versioning is enabled later.",
    )

    @field_validator("sha256_checksum")
    @classmethod
    def validate_sha256_checksum(cls, value: str) -> str:
        """Require storage checksums to use canonical lower-case SHA-256 hex strings."""

        normalized = value.strip().lower()
        if not SHA256_HEX_PATTERN.fullmatch(normalized):
            raise ValueError("sha256_checksum must be a 64-character lower-case hex digest.")

        return normalized


class SourceDocumentStorageMetadata(StoredObjectMetadata):
    """Describe an original uploaded source document stored in the document bucket."""

    document_id: UUID = Field(
        description="Document identifier that owns the stored source file.",
    )
    original_filename: str = Field(
        min_length=1,
        description="Original client-provided filename retained for operator traceability.",
    )

    @model_validator(mode="after")
    def validate_bucket_kind(self) -> SourceDocumentStorageMetadata:
        """Require source documents to live only in the canonical document bucket."""

        if self.reference.bucket_kind is not StorageBucketKind.DOCUMENTS:
            raise ValueError("Source documents must be stored in the document bucket.")

        return self


class DerivativeStorageMetadata(StoredObjectMetadata):
    """Describe a derived object such as OCR text or a normalized document payload."""

    document_id: UUID = Field(
        description="Source document identifier that this derivative was produced from.",
    )
    document_version_no: PositiveInteger = Field(
        description="Document-version number the derivative belongs to.",
    )
    derivative_kind: DerivativeKind = Field(
        description="Derivative family represented by the stored object.",
    )

    @model_validator(mode="after")
    def validate_bucket_kind(self) -> DerivativeStorageMetadata:
        """Require derivatives to live only in the canonical derivative bucket."""

        if self.reference.bucket_kind is not StorageBucketKind.DERIVATIVES:
            raise ValueError("Derivatives must be stored in the derivative bucket.")

        return self


class ArtifactStorageMetadata(StoredObjectMetadata):
    """Describe a released artifact such as a report pack, audit trail, or evidence pack."""

    artifact_type: ArtifactType = Field(
        description="Released artifact type represented by this stored object.",
    )
    close_run_version_no: PositiveInteger = Field(
        description="Close-run version number that the artifact snapshot belongs to.",
    )
    idempotency_key: str = Field(
        min_length=1,
        description="Stable idempotency key used to guard against duplicate releases.",
    )

    @model_validator(mode="after")
    def validate_bucket_kind(self) -> ArtifactStorageMetadata:
        """Require released artifacts to live only in the canonical artifact bucket."""

        if self.reference.bucket_kind is not StorageBucketKind.ARTIFACTS:
            raise ValueError("Artifacts must be stored in the artifact bucket.")

        return self


__all__ = [
    "SHA256_HEX_PATTERN",
    "ArtifactStorageMetadata",
    "CloseRunStorageScope",
    "DerivativeKind",
    "DerivativeStorageMetadata",
    "ObjectStorageReference",
    "SourceDocumentStorageMetadata",
    "StorageBucketKind",
    "StoredObjectMetadata",
]
