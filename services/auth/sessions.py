"""
Purpose: Issue, rotate, and validate opaque session tokens for desktop and web authentication.
Scope: Session token generation, token hashing, expiration windows, and rotation heuristics.
Dependencies: Application settings for TTL controls plus shared UTC time helpers.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from services.common.settings import AppSettings
from services.common.types import utc_now


@dataclass(frozen=True, slots=True)
class SessionTokenBundle:
    """Describe one opaque session token plus the metadata needed for persistence."""

    token: str
    token_hash: str
    expires_at: datetime
    last_seen_at: datetime


class SessionManager:
    """Manage the lifecycle of opaque session tokens stored as hashes in the database."""

    def __init__(self, *, settings: AppSettings) -> None:
        """Capture the canonical session TTL and rotation windows from application settings."""

        self._ttl = timedelta(hours=settings.security.session_ttl_hours)
        self._rotation_window = timedelta(minutes=settings.security.session_rotation_minutes)

    def issue_session(self, *, now: datetime | None = None) -> SessionTokenBundle:
        """Create a fresh opaque session token for a successful login or registration."""

        issued_at = now or utc_now()
        token = secrets.token_urlsafe(48)
        return SessionTokenBundle(
            token=token,
            token_hash=self.hash_token(token),
            expires_at=issued_at + self._ttl,
            last_seen_at=issued_at,
        )

    def should_rotate(
        self,
        *,
        expires_at: datetime,
        last_seen_at: datetime,
        now: datetime | None = None,
    ) -> bool:
        """Return whether a successful session use should rotate its token immediately."""

        observed_at = now or utc_now()
        if self.is_expired(expires_at=expires_at, now=observed_at):
            return False

        return observed_at - last_seen_at >= self._rotation_window

    def is_expired(self, *, expires_at: datetime, now: datetime | None = None) -> bool:
        """Return whether the stored session expiry timestamp has already passed."""

        observed_at = now or utc_now()
        return observed_at >= expires_at

    @staticmethod
    def hash_token(token: str) -> str:
        """Hash an opaque session token before persistence to protect live sessions."""

        return hashlib.sha256(token.encode("utf-8")).hexdigest()


__all__ = ["SessionManager", "SessionTokenBundle"]
