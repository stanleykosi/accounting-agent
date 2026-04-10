"""
Purpose: Provide secure password hashing and verification for local email/password authentication.
Scope: Password policy checks plus deterministic scrypt-based hashing and
constant-time verification.
Dependencies: Python's cryptographic standard-library primitives only,
keeping the auth path local and explicit.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import secrets
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _ScryptHash:
    """Capture the parsed pieces of a stored scrypt password hash."""

    n: int
    r: int
    p: int
    dklen: int
    salt: bytes
    digest: bytes

    @classmethod
    def parse(cls, value: str) -> _ScryptHash:
        """Parse a serialized hash string and fail fast if its shape is not canonical."""

        parts = value.split("$")
        if len(parts) != 7 or parts[0] != "scrypt":
            message = "Stored password hash is not in the canonical scrypt format."
            raise ValueError(message)

        try:
            return cls(
                n=int(parts[1]),
                r=int(parts[2]),
                p=int(parts[3]),
                dklen=int(parts[4]),
                salt=base64.urlsafe_b64decode(parts[5].encode("ascii")),
                digest=base64.urlsafe_b64decode(parts[6].encode("ascii")),
            )
        except (ValueError, UnicodeError, binascii.Error) as error:
            message = "Stored password hash could not be parsed."
            raise ValueError(message) from error


class PasswordHasher:
    """Hash and verify passwords with scrypt while enforcing a minimum-strength policy."""

    def __init__(
        self,
        *,
        minimum_length: int = 12,
        n: int = 16_384,
        r: int = 8,
        p: int = 1,
        dklen: int = 64,
        salt_bytes: int = 16,
    ) -> None:
        """Store hashing parameters and validate that the configured password policy is usable."""

        if minimum_length < 12:
            message = "Password minimum length must be at least 12 characters."
            raise ValueError(message)

        self._minimum_length = minimum_length
        self._n = n
        self._r = r
        self._p = p
        self._dklen = dklen
        self._salt_bytes = salt_bytes

    def hash_password(self, password: str) -> str:
        """Hash a plaintext password after validating the canonical password policy."""

        normalized_password = self._normalize_password(password)
        salt = secrets.token_bytes(self._salt_bytes)
        digest = hashlib.scrypt(
            normalized_password.encode("utf-8"),
            salt=salt,
            n=self._n,
            r=self._r,
            p=self._p,
            dklen=self._dklen,
        )
        return "$".join(
            (
                "scrypt",
                str(self._n),
                str(self._r),
                str(self._p),
                str(self._dklen),
                base64.urlsafe_b64encode(salt).decode("ascii"),
                base64.urlsafe_b64encode(digest).decode("ascii"),
            )
        )

    def verify_password(self, password: str, stored_hash: str) -> bool:
        """Verify a plaintext password against the canonical serialized scrypt hash."""

        normalized_password = self._normalize_password(password, validate_length=False)
        parsed_hash = _ScryptHash.parse(stored_hash)
        calculated_digest = hashlib.scrypt(
            normalized_password.encode("utf-8"),
            salt=parsed_hash.salt,
            n=parsed_hash.n,
            r=parsed_hash.r,
            p=parsed_hash.p,
            dklen=parsed_hash.dklen,
        )
        return secrets.compare_digest(calculated_digest, parsed_hash.digest)

    def _normalize_password(self, value: str, *, validate_length: bool = True) -> str:
        """Reject blank or whitespace-only passwords before hashing or verification work."""

        if not value or value.isspace():
            message = "Password cannot be blank."
            raise ValueError(message)

        if validate_length and len(value) < self._minimum_length:
            message = f"Password must be at least {self._minimum_length} characters long."
            raise ValueError(message)

        return value


__all__ = ["PasswordHasher"]
