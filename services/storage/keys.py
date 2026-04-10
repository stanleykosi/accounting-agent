"""
Purpose: Define deterministic object-key builders for the canonical MinIO layout.
Scope: Close-run storage prefixes, filename normalization, and key generation for
source documents, OCR text, derivatives, and released artifacts.
Dependencies: Shared storage contract models and canonical artifact enums.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePath
from uuid import UUID

from services.common.enums import ArtifactType
from services.contracts.storage_models import CloseRunStorageScope, DerivativeKind

NON_ALPHANUMERIC_PATTERN = re.compile(r"[^a-z0-9]+")


def build_close_run_storage_prefix(scope: CloseRunStorageScope) -> str:
    """Build the canonical key prefix shared by all objects for one close-run version."""

    return (
        f"entities/{scope.entity_id}/"
        f"periods/{scope.period_start.isoformat()}_{scope.period_end.isoformat()}/"
        f"close-runs/{scope.close_run_id}/"
        f"versions/{scope.close_run_version_no}"
    )


def build_source_document_key(
    *,
    scope: CloseRunStorageScope,
    document_id: UUID,
    original_filename: str,
) -> str:
    """Build the canonical object key for one uploaded source document."""

    normalized_filename = normalize_filename(original_filename, default_stem="document")
    return (
        f"{build_close_run_storage_prefix(scope)}/"
        f"documents/source/{document_id}/{normalized_filename}"
    )


def build_ocr_text_key(
    *,
    scope: CloseRunStorageScope,
    document_id: UUID,
    document_version_no: int,
    source_filename: str,
) -> str:
    """Build the canonical object key for OCR text derived from one document version."""

    _validate_positive_integer(document_version_no=document_version_no)
    normalized_stem = _normalized_filename_stem(source_filename, default_stem="ocr-text")
    return (
        f"{build_close_run_storage_prefix(scope)}/"
        f"documents/ocr/{document_id}/versions/{document_version_no}/{normalized_stem}.txt"
    )


def build_derivative_key(
    *,
    scope: CloseRunStorageScope,
    document_id: UUID,
    document_version_no: int,
    derivative_kind: DerivativeKind,
    filename: str,
) -> str:
    """Build the canonical object key for a non-source derivative document payload."""

    _validate_positive_integer(document_version_no=document_version_no)
    normalized_filename = normalize_filename(filename, default_stem=derivative_kind.value)
    return (
        f"{build_close_run_storage_prefix(scope)}/"
        f"documents/derivatives/{document_id}/versions/{document_version_no}/"
        f"{derivative_kind.value}/{normalized_filename}"
    )


def build_artifact_key(
    *,
    scope: CloseRunStorageScope,
    artifact_type: ArtifactType,
    idempotency_key: str,
    filename: str,
) -> str:
    """Build the canonical object key for a released artifact snapshot."""

    normalized_idempotency_key = normalize_storage_segment(
        idempotency_key,
        label="idempotency_key",
    )
    normalized_filename = normalize_filename(filename, default_stem=artifact_type.value)
    return (
        f"{build_close_run_storage_prefix(scope)}/"
        f"artifacts/{artifact_type.value}/{normalized_idempotency_key}/{normalized_filename}"
    )


def normalize_filename(
    filename: str,
    *,
    default_stem: str,
    default_extension: str | None = None,
) -> str:
    """Normalize a client-provided filename into a deterministic ASCII-safe storage form."""

    candidate_name = PurePath(filename.strip()).name
    if not candidate_name or candidate_name in {".", ".."}:
        raise ValueError("filename must include a non-empty basename.")

    stem, dot, extension = candidate_name.rpartition(".")
    if not dot:
        stem = candidate_name
        extension = ""

    normalized_stem = normalize_storage_segment(stem or default_stem, label="filename stem")
    normalized_extension = _normalize_extension(extension or default_extension)
    return f"{normalized_stem}{normalized_extension}"


def normalize_storage_segment(value: str, *, label: str) -> str:
    """Normalize an arbitrary storage segment into a deterministic lower-case slug."""

    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    collapsed = NON_ALPHANUMERIC_PATTERN.sub("-", ascii_value.strip().lower())
    cleaned = collapsed.strip("-")
    if not cleaned:
        raise ValueError(f"{label} must contain at least one alphanumeric character.")

    return cleaned


def _normalized_filename_stem(filename: str, *, default_stem: str) -> str:
    """Return the normalized filename stem used by OCR text objects."""

    normalized_filename = normalize_filename(filename, default_stem=default_stem)
    stem, _, _ = normalized_filename.partition(".")
    return stem


def _normalize_extension(value: str | None) -> str:
    """Normalize a file extension so stored filenames remain deterministic."""

    if value is None or not value:
        return ""

    normalized = normalize_storage_segment(value.lstrip("."), label="filename extension")
    return f".{normalized}"


def _validate_positive_integer(*, document_version_no: int) -> None:
    """Reject non-positive document-version values before building object keys."""

    if document_version_no <= 0:
        raise ValueError("document_version_no must be greater than zero.")


__all__ = [
    "build_artifact_key",
    "build_close_run_storage_prefix",
    "build_derivative_key",
    "build_ocr_text_key",
    "build_source_document_key",
    "normalize_filename",
    "normalize_storage_segment",
]
