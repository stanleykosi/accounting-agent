"""
Purpose: Provide the canonical encryption boundary for persisted integration credentials.
Scope: Authenticated encryption of JSON payloads before they enter business-data tables.
Dependencies: cryptography's AES-GCM primitive plus shared JSON type aliases.
"""

from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr
from services.common.types import JsonObject

_ENVELOPE_VERSION = 1
_ALGORITHM_NAME = "aes-256-gcm"
_NONCE_BYTES = 12
_KEY_BYTES = 32


class CredentialCipherError(ValueError):
    """Represent an expected credential-encryption failure with a clear recovery message."""


class CredentialCipher:
    """Encrypt and decrypt JSON credential payloads for integration persistence."""

    def __init__(self, *, base64_key: SecretStr | str) -> None:
        """Validate the configured key material and initialize the AES-GCM primitive."""

        self._key = _decode_encryption_key(base64_key=base64_key)
        self._aesgcm = AESGCM(self._key)

    def encrypt_json(self, *, payload: JsonObject, context: str) -> JsonObject:
        """Encrypt a JSON object and return a storage-safe envelope for JSONB columns."""

        if not context.strip():
            raise CredentialCipherError("Credential encryption context cannot be empty.")

        plaintext = _serialize_json(payload)
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext, context.encode("utf-8"))
        return {
            "version": _ENVELOPE_VERSION,
            "algorithm": _ALGORITHM_NAME,
            "nonce": _base64_encode(nonce),
            "ciphertext": _base64_encode(ciphertext),
        }

    def decrypt_json(self, *, envelope: JsonObject, context: str) -> JsonObject:
        """Decrypt one encrypted envelope and restore the original JSON object."""

        if not context.strip():
            raise CredentialCipherError("Credential decryption context cannot be empty.")

        version = envelope.get("version")
        algorithm = envelope.get("algorithm")
        nonce = envelope.get("nonce")
        ciphertext = envelope.get("ciphertext")

        if version != _ENVELOPE_VERSION:
            raise CredentialCipherError(
                "Unsupported credential envelope version. Rotate or re-encrypt the stored data."
            )
        if algorithm != _ALGORITHM_NAME:
            raise CredentialCipherError(
                "Unsupported credential encryption algorithm. Reconfigure the canonical cipher."
            )
        if not isinstance(nonce, str) or not isinstance(ciphertext, str):
            raise CredentialCipherError(
                "Encrypted credential envelopes must include string nonce and ciphertext fields."
            )

        try:
            plaintext = self._aesgcm.decrypt(
                _base64_decode(nonce),
                _base64_decode(ciphertext),
                context.encode("utf-8"),
            )
        except Exception as error:  # pragma: no cover - cryptography raises multiple subclasses.
            raise CredentialCipherError(
                "Credential decryption failed. Check the configured encryption key and context."
            ) from error

        return _deserialize_json_object(plaintext)


def _decode_encryption_key(*, base64_key: SecretStr | str) -> bytes:
    """Decode a base64url-encoded 32-byte AES key and fail fast when it is malformed."""

    raw_key = (
        base64_key.get_secret_value()
        if isinstance(base64_key, SecretStr)
        else base64_key
    ).strip()
    if not raw_key:
        raise CredentialCipherError(
            "Credential encryption key is missing. Set security_credential_encryption_key."
        )

    try:
        decoded_key = _base64_decode(raw_key)
    except CredentialCipherError:
        raise
    except Exception as error:  # pragma: no cover - defensive guard around base64 parsing.
        raise CredentialCipherError(
            "Credential encryption key must be valid URL-safe base64 text."
        ) from error

    if len(decoded_key) != _KEY_BYTES:
        raise CredentialCipherError(
            "Credential encryption key must decode to exactly 32 bytes for AES-256-GCM."
        )

    return decoded_key


def _serialize_json(payload: JsonObject) -> bytes:
    """Serialize a JSON object deterministically so encrypted payloads remain interoperable."""

    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except TypeError as error:
        raise CredentialCipherError(
            "Credential payloads must be JSON-serializable before encryption."
        ) from error


def _deserialize_json_object(value: bytes) -> JsonObject:
    """Deserialize decrypted JSON bytes and reject non-object payloads."""

    try:
        decoded = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CredentialCipherError(
            "Decrypted credential payload is not valid UTF-8 JSON. Re-encrypt the credentials."
        ) from error

    if not isinstance(decoded, dict):
        raise CredentialCipherError("Decrypted credential payloads must be JSON objects.")

    return decoded


def _base64_encode(value: bytes) -> str:
    """Encode binary values into URL-safe base64 without newline handling concerns."""

    return base64.urlsafe_b64encode(value).decode("ascii")


def _base64_decode(value: str) -> bytes:
    """Decode URL-safe base64 text while accepting omitted padding from environment values."""

    padding = (-len(value)) % 4
    normalized = f"{value}{'=' * padding}"
    try:
        return base64.urlsafe_b64decode(normalized.encode("ascii"))
    except Exception as error:
        raise CredentialCipherError("Value must be valid URL-safe base64 text.") from error


__all__ = ["CredentialCipher", "CredentialCipherError"]
