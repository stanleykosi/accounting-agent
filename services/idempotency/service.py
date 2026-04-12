"""
Purpose: Provide idempotency-key generation, validation, and deduplication
logic for export and artifact-release workflows.
Scope: Key construction from canonical inputs, key validation, released-artifact
lookup, and guard checks that prevent duplicate release actions.
Dependencies: Shared type primitives, structured logging, storage repository,
and report/export contracts.

Design notes:
- Every export, evidence pack, and released artifact must carry a stable
  idempotency key so retries or duplicate clicks cannot create double releases.
- The key is derived deterministically from close-run identity, artifact type,
  and an explicit action qualifier so the same export attempted twice resolves
  to the same storage object and database row.
- The guard service checks whether a key was already released and either
  returns the existing artifact metadata or blocks the action with a clear
  recovery-oriented error.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from services.common.types import JsonObject
from services.db.models.audit import AuditSourceSurface

# ---------------------------------------------------------------------------
# Error domain
# ---------------------------------------------------------------------------

class IdempotencyGuardErrorCode(StrEnum):
    """Enumerate stable error codes surfaced by the idempotency guard."""

    DUPLICATE_RELEASE = "duplicate_release"
    INVALID_KEY = "invalid_idempotency_key"
    ARTIFACT_NOT_FOUND = "artifact_not_found"


class IdempotencyGuardError(Exception):
    """Represent an expected idempotency-guard failure for API translation."""

    def __init__(
        self,
        *,
        status_code: int,
        code: IdempotencyGuardErrorCode,
        message: str,
        existing_artifact_ref: dict[str, object] | None = None,
    ) -> None:
        """Capture HTTP status, stable error code, recovery message, and existing artifact."""

        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.existing_artifact_ref = existing_artifact_ref


# ---------------------------------------------------------------------------
# Key construction
# ---------------------------------------------------------------------------

def build_idempotency_key(
    *,
    close_run_id: UUID,
    artifact_type: str,
    action_qualifier: str | None = None,
    version_override: int | None = None,
    extra_segments: tuple[str, ...] = (),
) -> str:
    """Build a deterministic idempotency key for one export or artifact action.

    The key is constructed from the close-run identity, artifact type, and
    optional action qualifier so that the same export attempted multiple times
    always produces the same key.  Retries or duplicate clicks then resolve to
    the existing artifact instead of creating a duplicate.

    Args:
        close_run_id: UUID of the close run this export belongs to.
        artifact_type: Canonical artifact type string (e.g. "evidence_pack").
        action_qualifier: Optional action scope (e.g. "full_export", "regeneration").
        version_override: Explicit close-run version number when different from default.
        extra_segments: Additional deterministic key segments for disambiguation.

    Returns:
        A stable, ASCII-safe idempotency key string.
    """

    segments = [
        str(close_run_id),
        artifact_type.strip().lower(),
    ]
    if action_qualifier:
        segments.append(action_qualifier.strip().lower())
    if version_override is not None:
        segments.append(str(version_override))
    segments.extend(s.strip().lower() for s in extra_segments if s.strip())

    raw_key = ":".join(segments)
    # Produce a stable, compact hash-based key for storage and URL safety.
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
    return f"ik-{key_hash}"


def validate_idempotency_key(key: str) -> None:
    """Validate that an idempotency key matches the canonical format.

    Args:
        key: The idempotency key string to validate.

    Raises:
        IdempotencyGuardError: When the key format is invalid.
    """

    if not key or not key.strip():
        raise IdempotencyGuardError(
            status_code=400,
            code=IdempotencyGuardErrorCode.INVALID_KEY,
            message="Idempotency key must be a non-empty string.",
        )

    if not key.startswith("ik-"):
        raise IdempotencyGuardError(
            status_code=400,
            code=IdempotencyGuardErrorCode.INVALID_KEY,
            message="Idempotency key must start with 'ik-' prefix.",
        )

    suffix = key[3:]
    if len(suffix) < 8:
        raise IdempotencyGuardError(
            status_code=400,
            code=IdempotencyGuardErrorCode.INVALID_KEY,
            message="Idempotency key suffix must be at least 8 characters.",
        )


# ---------------------------------------------------------------------------
# Persistence protocol
# ---------------------------------------------------------------------------

class IdempotencyRepositoryProtocol(Protocol):
    """Describe the persistence operations required by the idempotency guard."""

    def get_released_artifact(
        self,
        *,
        close_run_id: UUID,
        artifact_type: str,
        idempotency_key: str,
    ) -> dict[str, object] | None:
        """Return an existing released artifact when the idempotency key matches."""

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
        """Persist a new released-artifact row with idempotency protection."""

    def commit(self) -> None:
        """Commit the current transaction."""

    def rollback(self) -> None:
        """Rollback the current transaction."""

    def is_integrity_error(self, error: Exception) -> bool:
        """Return whether the exception originated from a DB uniqueness violation."""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class IdempotencyService:
    """Provide the canonical idempotency guard for export and release workflows.

    The service ensures that every artifact-release action is protected against
    duplicate execution.  When a duplicate is detected, the existing artifact
    metadata is returned so callers can safely reuse it without re-uploading or
    re-generating.
    """

    def __init__(self, *, repository: IdempotencyRepositoryProtocol) -> None:
        """Capture the persistence boundary used by the idempotency guard."""

        self._repository = repository

    def guard_release(
        self,
        *,
        close_run_id: UUID,
        artifact_type: str,
        idempotency_key: str,
    ) -> dict[str, object] | None:
        """Check whether an artifact with the given idempotency key was already released.

        Args:
            close_run_id: UUID of the close run this export belongs to.
            artifact_type: Canonical artifact type identifier.
            idempotency_key: Deterministic key for this release action.

        Returns:
            Existing artifact metadata dict when a duplicate is found, else None.

        Raises:
            IdempotencyGuardError: When the idempotency key format is invalid.
        """

        validate_idempotency_key(idempotency_key)

        existing = self._repository.get_released_artifact(
            close_run_id=close_run_id,
            artifact_type=artifact_type,
            idempotency_key=idempotency_key,
        )
        return existing

    def record_release(
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
        source_surface: AuditSourceSurface | None = None,
    ) -> dict[str, object]:
        """Persist a new released-artifact record with idempotency protection.

        Args:
            close_run_id: UUID of the close run this export belongs to.
            artifact_type: Canonical artifact type identifier.
            idempotency_key: Deterministic key for this release action.
            storage_key: Object-storage key where the artifact lives.
            checksum: SHA-256 checksum of the artifact payload.
            size_bytes: Byte size of the artifact payload.
            content_type: MIME type of the artifact.
            version_no: Close-run version number this artifact belongs to.
            metadata: Optional additional metadata for the artifact.
            source_surface: The runtime surface that emitted this release.

        Returns:
            The persisted artifact record dict.

        Raises:
            IdempotencyGuardError: When a duplicate release is detected.
            IdempotencyGuardError: When the idempotency key format is invalid.
        """

        validate_idempotency_key(idempotency_key)

        # Check for duplicate before attempting insert.
        existing = self._repository.get_released_artifact(
            close_run_id=close_run_id,
            artifact_type=artifact_type,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            raise IdempotencyGuardError(
                status_code=409,
                code=IdempotencyGuardErrorCode.DUPLICATE_RELEASE,
                message=(
                    "This artifact was already released for the given idempotency key. "
                    "Use the existing artifact instead of generating a duplicate."
                ),
                existing_artifact_ref=existing,
            )

        try:
            record = self._repository.record_released_artifact(
                close_run_id=close_run_id,
                artifact_type=artifact_type,
                idempotency_key=idempotency_key,
                storage_key=storage_key,
                checksum=checksum,
                size_bytes=size_bytes,
                content_type=content_type,
                version_no=version_no,
                metadata=metadata,
            )
            self._repository.commit()
            return record
        except Exception as error:
            self._repository.rollback()
            if self._repository.is_integrity_error(error):
                raise IdempotencyGuardError(
                    status_code=409,
                    code=IdempotencyGuardErrorCode.DUPLICATE_RELEASE,
                    message=(
                        "A concurrent release action created this artifact first. "
                        "Use the existing artifact instead of retrying."
                    ),
                ) from error
            raise


__all__ = [
    "IdempotencyGuardError",
    "IdempotencyGuardErrorCode",
    "IdempotencyService",
    "build_idempotency_key",
    "validate_idempotency_key",
]
