"""
Purpose: Orchestrate local registration, login, logout, and current-session validation flows.
Scope: Auth business rules, password verification, active-user checks,
session rotation, and response shaping.
Dependencies: Password hashing, session token management, and the auth
repository persistence boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from services.auth.passwords import PasswordHasher
from services.auth.sessions import SessionManager
from services.common.types import utc_now
from services.db.models.auth import UserStatus
from services.db.repositories.auth_repo import (
    AuthSessionRecord,
    AuthSessionWithUserRecord,
    AuthUserRecord,
)


class AuthErrorCode(StrEnum):
    """Enumerate deterministic auth error codes for API consumers and UI handling."""

    DUPLICATE_EMAIL = "duplicate_email"
    INVALID_CREDENTIALS = "invalid_credentials"
    SESSION_EXPIRED = "session_expired"
    SESSION_REQUIRED = "session_required"
    USER_DISABLED = "user_disabled"


class AuthServiceError(Exception):
    """Represent an expected auth-domain failure that the API should expose as an HTTP error."""

    def __init__(self, *, status_code: int, code: AuthErrorCode, message: str) -> None:
        """Capture the HTTP status, stable error code, and operator-facing recovery message."""

        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class AuthenticatedSessionResult:
    """Describe the authenticated user and session metadata returned to the API layer."""

    user: AuthUserRecord
    session: AuthSessionRecord
    session_token: str | None
    rotated: bool


class AuthRepositoryProtocol(Protocol):
    """Describe the persistence operations the auth service requires from a repository."""

    def get_user_by_email(self, *, email: str) -> AuthUserRecord | None:
        """Return a user by canonical email when present."""

    def create_user(self, *, email: str, password_hash: str, full_name: str) -> AuthUserRecord:
        """Persist a new user row and return its immutable record view."""

    def update_last_login(self, *, user_id: UUID, logged_in_at: datetime) -> AuthUserRecord:
        """Persist the latest successful credential-login timestamp for a user."""

    def create_session(
        self,
        *,
        user_id: UUID,
        session_token_hash: str,
        expires_at: datetime,
        last_seen_at: datetime,
        user_agent: str | None,
        ip_address: str | None,
    ) -> AuthSessionRecord:
        """Persist a new session row for a successful auth flow."""

    def get_session_with_user_by_hash(
        self,
        *,
        session_token_hash: str,
    ) -> AuthSessionWithUserRecord | None:
        """Load a persisted session and its owning user by token hash."""

    def rotate_session(
        self,
        *,
        session_id: UUID,
        session_token_hash: str,
        expires_at: datetime,
        last_seen_at: datetime,
        user_agent: str | None,
        ip_address: str | None,
    ) -> AuthSessionRecord:
        """Replace an existing session token hash and refresh the stored session metadata."""

    def touch_session(self, *, session_id: UUID, last_seen_at: datetime) -> AuthSessionRecord:
        """Update a session's last-seen timestamp after a successful request."""

    def delete_session(self, *, session_id: UUID) -> None:
        """Remove a session row so it can no longer authenticate future requests."""

    def commit(self) -> None:
        """Commit the current unit of work."""

    def rollback(self) -> None:
        """Rollback the current unit of work after an error."""

    def is_integrity_error(self, error: Exception) -> bool:
        """Return whether the provided exception was caused by a DB integrity failure."""


class AuthService:
    """Provide the canonical local-auth workflow used by API routes and later UI clients."""

    def __init__(
        self,
        *,
        repository: AuthRepositoryProtocol,
        password_hasher: PasswordHasher,
        session_manager: SessionManager,
    ) -> None:
        """Capture the persistence and security collaborators used by auth workflows."""

        self._repository = repository
        self._password_hasher = password_hasher
        self._session_manager = session_manager

    def register_user(
        self,
        *,
        email: str,
        full_name: str,
        password: str,
        user_agent: str | None,
        ip_address: str | None,
    ) -> AuthenticatedSessionResult:
        """Create a new active user account and immediately issue a signed-in local session."""

        existing_user = self._repository.get_user_by_email(email=email)
        if existing_user is not None:
            raise AuthServiceError(
                status_code=409,
                code=AuthErrorCode.DUPLICATE_EMAIL,
                message="An account with that email address already exists.",
            )

        now = utc_now()
        password_hash = self._password_hasher.hash_password(password)

        try:
            user = self._repository.create_user(
                email=email,
                password_hash=password_hash,
                full_name=full_name,
            )
            user = self._repository.update_last_login(user_id=user.id, logged_in_at=now)
            session_bundle = self._session_manager.issue_session(now=now)
            session = self._repository.create_session(
                user_id=user.id,
                session_token_hash=session_bundle.token_hash,
                expires_at=session_bundle.expires_at,
                last_seen_at=session_bundle.last_seen_at,
                user_agent=user_agent,
                ip_address=ip_address,
            )
            self._repository.commit()
        except Exception as error:
            self._repository.rollback()
            if self._repository.is_integrity_error(error):
                raise AuthServiceError(
                    status_code=409,
                    code=AuthErrorCode.DUPLICATE_EMAIL,
                    message="An account with that email address already exists.",
                ) from error
            raise

        return AuthenticatedSessionResult(
            user=user,
            session=session,
            session_token=session_bundle.token,
            rotated=False,
        )

    def login_user(
        self,
        *,
        email: str,
        password: str,
        user_agent: str | None,
        ip_address: str | None,
    ) -> AuthenticatedSessionResult:
        """Verify credentials, reject disabled users, and issue a fresh session token."""

        user = self._repository.get_user_by_email(email=email)
        if user is None:
            raise self._invalid_credentials_error()

        if user.status is UserStatus.DISABLED:
            raise AuthServiceError(
                status_code=403,
                code=AuthErrorCode.USER_DISABLED,
                message="This user account is disabled. Contact an administrator to reactivate it.",
            )

        if not self._password_hasher.verify_password(password, user.password_hash):
            raise self._invalid_credentials_error()

        now = utc_now()
        session_bundle = self._session_manager.issue_session(now=now)

        try:
            user = self._repository.update_last_login(user_id=user.id, logged_in_at=now)
            session = self._repository.create_session(
                user_id=user.id,
                session_token_hash=session_bundle.token_hash,
                expires_at=session_bundle.expires_at,
                last_seen_at=session_bundle.last_seen_at,
                user_agent=user_agent,
                ip_address=ip_address,
            )
            self._repository.commit()
        except Exception:
            self._repository.rollback()
            raise

        return AuthenticatedSessionResult(
            user=user,
            session=session,
            session_token=session_bundle.token,
            rotated=False,
        )

    def authenticate_session(
        self,
        *,
        session_token: str,
        user_agent: str | None,
        ip_address: str | None,
        now: datetime | None = None,
    ) -> AuthenticatedSessionResult:
        """Validate a session token, reject disabled users, and rotate when needed."""

        observed_at = now or utc_now()
        session_with_user = self._get_session_with_user(session_token=session_token)
        session = session_with_user.session
        user = session_with_user.user

        if self._session_manager.is_expired(expires_at=session.expires_at, now=observed_at):
            self._revoke_session(session_id=session.id)
            raise AuthServiceError(
                status_code=401,
                code=AuthErrorCode.SESSION_EXPIRED,
                message="Your session has expired. Sign in again to continue.",
            )

        if user.status is UserStatus.DISABLED:
            self._revoke_session(session_id=session.id)
            raise AuthServiceError(
                status_code=403,
                code=AuthErrorCode.USER_DISABLED,
                message="This user account is disabled. Contact an administrator to reactivate it.",
            )

        try:
            if self._session_manager.should_rotate(
                expires_at=session.expires_at,
                last_seen_at=session.last_seen_at,
                now=observed_at,
            ):
                session_bundle = self._session_manager.issue_session(now=observed_at)
                rotated_session = self._repository.rotate_session(
                    session_id=session.id,
                    session_token_hash=session_bundle.token_hash,
                    expires_at=session_bundle.expires_at,
                    last_seen_at=session_bundle.last_seen_at,
                    user_agent=user_agent,
                    ip_address=ip_address,
                )
                self._repository.commit()
                return AuthenticatedSessionResult(
                    user=user,
                    session=rotated_session,
                    session_token=session_bundle.token,
                    rotated=True,
                )

            touched_session = self._repository.touch_session(
                session_id=session.id,
                last_seen_at=observed_at,
            )
            self._repository.commit()
        except Exception:
            self._repository.rollback()
            raise

        return AuthenticatedSessionResult(
            user=user,
            session=touched_session,
            session_token=None,
            rotated=False,
        )

    def logout_session(self, *, session_token: str | None) -> None:
        """Revoke the current session token and treat missing tokens as already logged out."""

        if not session_token:
            return

        session_with_user = self._repository.get_session_with_user_by_hash(
            session_token_hash=self._session_manager.hash_token(session_token)
        )
        if session_with_user is None:
            return

        self._revoke_session(session_id=session_with_user.session.id)

    def _get_session_with_user(self, *, session_token: str) -> AuthSessionWithUserRecord:
        """Load a session by opaque token or raise the canonical missing-session error."""

        session_with_user = self._repository.get_session_with_user_by_hash(
            session_token_hash=self._session_manager.hash_token(session_token)
        )
        if session_with_user is None:
            raise AuthServiceError(
                status_code=401,
                code=AuthErrorCode.SESSION_REQUIRED,
                message="Sign in to continue.",
            )

        return session_with_user

    def _revoke_session(self, *, session_id: UUID) -> None:
        """Delete one persisted session row and rollback if the transaction cannot be committed."""

        try:
            self._repository.delete_session(session_id=session_id)
            self._repository.commit()
        except Exception:
            self._repository.rollback()
            raise

    @staticmethod
    def _invalid_credentials_error() -> AuthServiceError:
        """Return the shared invalid-credentials error to avoid user enumeration leaks."""

        return AuthServiceError(
            status_code=401,
            code=AuthErrorCode.INVALID_CREDENTIALS,
            message="Email or password is incorrect.",
        )


def serialize_uuid(value: UUID) -> str:
    """Convert UUID values into JSON-safe strings for auth response contracts."""

    return str(value)


__all__ = [
    "AuthErrorCode",
    "AuthRepositoryProtocol",
    "AuthService",
    "AuthServiceError",
    "AuthenticatedSessionResult",
    "serialize_uuid",
]
