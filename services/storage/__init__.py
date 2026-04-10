"""
Purpose: Export the canonical storage client, repositories, and helper functions.
Scope: Stable storage package surface for document intake, derivative generation,
report publishing, and future API/worker integrations.
Dependencies: services/storage/*.py modules plus shared storage contracts.
"""

from services.storage.checksums import (
    compute_sha256_bytes,
    compute_sha256_file,
    compute_sha256_stream,
    compute_sha256_text,
    ensure_matching_sha256,
    validate_sha256_hex,
)
from services.storage.client import (
    StorageClient,
    StorageError,
    StorageObjectNotFoundError,
    StorageObjectStat,
)
from services.storage.keys import (
    build_artifact_key,
    build_close_run_storage_prefix,
    build_derivative_key,
    build_ocr_text_key,
    build_source_document_key,
    normalize_filename,
    normalize_storage_segment,
)
from services.storage.repository import StorageRepository

__all__ = [
    "StorageClient",
    "StorageError",
    "StorageObjectNotFoundError",
    "StorageObjectStat",
    "StorageRepository",
    "build_artifact_key",
    "build_close_run_storage_prefix",
    "build_derivative_key",
    "build_ocr_text_key",
    "build_source_document_key",
    "compute_sha256_bytes",
    "compute_sha256_file",
    "compute_sha256_stream",
    "compute_sha256_text",
    "ensure_matching_sha256",
    "normalize_filename",
    "normalize_storage_segment",
    "validate_sha256_hex",
]
