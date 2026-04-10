"""
Purpose: Provide canonical SHA-256 helpers for uploaded files and generated artifacts.
Scope: Byte, text, file, and stream hashing plus explicit checksum validation.
Dependencies: Python hashlib and IO primitives only so helpers stay usable in low-level code.
"""

from __future__ import annotations

import hashlib
from collections.abc import Buffer
from pathlib import Path
from typing import BinaryIO, Final

from services.contracts.storage_models import SHA256_HEX_PATTERN

DEFAULT_HASH_CHUNK_SIZE: Final[int] = 1024 * 1024


def compute_sha256_bytes(payload: bytes | bytearray | memoryview | Buffer) -> str:
    """Return the lower-case SHA-256 digest for an in-memory byte payload."""

    hasher = hashlib.sha256()
    hasher.update(bytes(payload))
    return hasher.hexdigest()


def compute_sha256_text(text: str, *, encoding: str = "utf-8") -> str:
    """Return the lower-case SHA-256 digest for a text payload using the given encoding."""

    return compute_sha256_bytes(text.encode(encoding))


def compute_sha256_stream(
    stream: BinaryIO,
    *,
    chunk_size: int = DEFAULT_HASH_CHUNK_SIZE,
    restore_position: bool = True,
) -> str:
    """Return the lower-case SHA-256 digest for a binary stream without loading it all at once."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero.")

    start_position = _tell_if_supported(stream) if restore_position else None
    hasher = hashlib.sha256()

    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        hasher.update(chunk)

    if restore_position and start_position is not None:
        stream.seek(start_position)

    return hasher.hexdigest()


def compute_sha256_file(
    path: str | Path,
    *,
    chunk_size: int = DEFAULT_HASH_CHUNK_SIZE,
) -> str:
    """Return the lower-case SHA-256 digest for a file on disk."""

    resolved_path = Path(path)
    with resolved_path.open("rb") as file_object:
        return compute_sha256_stream(file_object, chunk_size=chunk_size, restore_position=False)


def validate_sha256_hex(value: str) -> str:
    """Validate and normalize a SHA-256 hex digest for use in storage workflows."""

    normalized = value.strip().lower()
    if not SHA256_HEX_PATTERN.fullmatch(normalized):
        raise ValueError("Expected a 64-character lower-case SHA-256 hex digest.")

    return normalized


def ensure_matching_sha256(*, expected: str, actual: str, context: str) -> str:
    """Fail fast when a computed checksum does not match the caller's expected digest."""

    normalized_expected = validate_sha256_hex(expected)
    normalized_actual = validate_sha256_hex(actual)
    if normalized_expected != normalized_actual:
        raise ValueError(
            f"SHA-256 mismatch for {context}. "
            f"Expected {normalized_expected} but computed {normalized_actual}."
        )

    return normalized_actual


def _tell_if_supported(stream: BinaryIO) -> int | None:
    """Read the current stream position only when the object supports seeking."""

    if not stream.seekable():
        return None

    return stream.tell()


__all__ = [
    "DEFAULT_HASH_CHUNK_SIZE",
    "compute_sha256_bytes",
    "compute_sha256_file",
    "compute_sha256_stream",
    "compute_sha256_text",
    "ensure_matching_sha256",
    "validate_sha256_hex",
]
