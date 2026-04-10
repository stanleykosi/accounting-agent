"""
Purpose: Verify the canonical local-auth service behavior for registration,
login, session rotation, and logout.
Scope: Pure unit coverage over auth rules using an in-memory repository
double instead of a live database.
Dependencies: Auth service modules plus shared UTC-aware timestamps from the
common types helper.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from services.auth.passwords import PasswordHasher
from services.auth.service import AuthErrorCode, AuthService, AuthServiceError
from services.auth.sessions import SessionManager
from services.common.settings import AppSettings
from services.db.models.auth import UserStatus
from services.db.repositories.auth_repo import (
    AuthSessionRecord,
    AuthSessionWithUserRecord,
    AuthUserRecord,
)


def test_register_user_hashes_password_and_issues_session() -> None:
    """Ensure registration persists a hashed password and returns an authenticated session."""

    repository = InMemoryAuthRepository()
    service = build_auth_service(repository=repository)

    result = service.register_user(
        email="finance@example.com",
        full_name="Finance Lead",
        password="CorrectHorse12",
        user_agent="pytest",
        ip_address="127.0.0.1",
    )

    persisted_user = repository.get_user_by_email(email="finance@example.com")
    assert persisted_user is not None
    assert persisted_user.password_hash != "CorrectHorse12"
    assert result.user.email == "finance@example.com"
    assert result.session.user_id == result.user.id
    assert result.session_token is not None
    assert repository.commit_calls == 1


def test_register_user_rejects_duplicate_email() -> None:
    """Ensure the service fails fast when a caller tries to reuse an existing email address."""

    repository = InMemoryAuthRepository()
    service = build_auth_service(repository=repository)
    service.register_user(
        email="finance@example.com",
        full_name="Finance Lead",
        password="CorrectHorse12",
        user_agent=None,
        ip_address=None,
    )

    with pytest.raises(AuthServiceError) as error:
        service.register_user(
            email="finance@example.com",
            full_name="Other User",
            password="AnotherCorrect12",
            user_agent=None,
            ip_address=None,
        )

    assert error.value.status_code == 409
    assert error.value.code is AuthErrorCode.DUPLICATE_EMAIL


def test_login_user_rejects_wrong_password_without_leaking_existence() -> None:
    """Ensure invalid password attempts return the generic invalid-credentials error."""

    repository = InMemoryAuthRepository()
    service = build_auth_service(repository=repository)
    service.register_user(
        email="finance@example.com",
        full_name="Finance Lead",
        password="CorrectHorse12",
        user_agent=None,
        ip_address=None,
    )

    with pytest.raises(AuthServiceError) as error:
        service.login_user(
            email="finance@example.com",
            password="incorrect-password",
            user_agent=None,
            ip_address=None,
        )

    assert error.value.status_code == 401
    assert error.value.code is AuthErrorCode.INVALID_CREDENTIALS


def test_login_user_rejects_disabled_accounts() -> None:
    """Ensure disabled users cannot exchange correct credentials for an authenticated session."""

    repository = InMemoryAuthRepository()
    service = build_auth_service(repository=repository)
    result = service.register_user(
        email="finance@example.com",
        full_name="Finance Lead",
        password="CorrectHorse12",
        user_agent=None,
        ip_address=None,
    )
    repository.users[result.user.id] = replace(result.user, status=UserStatus.DISABLED)

    with pytest.raises(AuthServiceError) as error:
        service.login_user(
            email="finance@example.com",
            password="CorrectHorse12",
            user_agent=None,
            ip_address=None,
        )

    assert error.value.status_code == 403
    assert error.value.code is AuthErrorCode.USER_DISABLED


def test_authenticate_session_rotates_after_rotation_window() -> None:
    """Ensure session reads rotate the cookie token after the configured inactivity window."""

    repository = InMemoryAuthRepository()
    settings = AppSettings()
    service = build_auth_service(repository=repository, settings=settings)
    issued = service.register_user(
        email="finance@example.com",
        full_name="Finance Lead",
        password="CorrectHorse12",
        user_agent="pytest",
        ip_address="127.0.0.1",
    )
    original_token = issued.session_token
    assert original_token is not None

    observed_at = issued.session.last_seen_at + timedelta(
        minutes=settings.security.session_rotation_minutes + 1
    )
    rotated = service.authenticate_session(
        session_token=original_token,
        user_agent="pytest",
        ip_address="127.0.0.1",
        now=observed_at,
    )

    assert rotated.rotated is True
    assert rotated.session_token is not None
    assert rotated.session_token != original_token
    assert rotated.session.last_seen_at == observed_at


def test_authenticate_session_rejects_expired_sessions() -> None:
    """Ensure expired sessions are revoked and callers are told to sign in again."""

    repository = InMemoryAuthRepository()
    settings = AppSettings()
    service = build_auth_service(repository=repository, settings=settings)
    issued = service.register_user(
        email="finance@example.com",
        full_name="Finance Lead",
        password="CorrectHorse12",
        user_agent=None,
        ip_address=None,
    )
    token = issued.session_token
    assert token is not None

    observed_at = issued.session.expires_at + timedelta(minutes=1)
    with pytest.raises(AuthServiceError) as error:
        service.authenticate_session(
            session_token=token,
            user_agent=None,
            ip_address=None,
            now=observed_at,
        )

    assert error.value.status_code == 401
    assert error.value.code is AuthErrorCode.SESSION_EXPIRED
    assert repository.sessions == {}


def test_logout_session_revokes_persisted_session() -> None:
    """Ensure logout deletes the current session row so the cookie cannot be reused."""

    repository = InMemoryAuthRepository()
    service = build_auth_service(repository=repository)
    issued = service.register_user(
        email="finance@example.com",
        full_name="Finance Lead",
        password="CorrectHorse12",
        user_agent=None,
        ip_address=None,
    )
    token = issued.session_token
    assert token is not None

    service.logout_session(session_token=token)

    assert repository.sessions == {}


def build_auth_service(
    *,
    repository: InMemoryAuthRepository,
    settings: AppSettings | None = None,
) -> AuthService:
    """Construct the auth service with in-memory persistence for pure unit coverage."""

    return AuthService(
        repository=repository,
        password_hasher=PasswordHasher(),
        session_manager=SessionManager(settings=settings or AppSettings()),
    )


class InMemoryAuthRepository:
    """Provide the minimal repository surface needed by the auth service for unit tests."""

    def __init__(self) -> None:
        """Initialize in-memory user and session stores that mimic repository behavior."""

        self.users: dict[UUID, AuthUserRecord] = {}
        self.sessions: dict[UUID, AuthSessionRecord] = {}
        self.commit_calls = 0

    def get_user_by_email(self, *, email: str) -> AuthUserRecord | None:
        """Return one user by canonical email, matching the production repository contract."""

        return next((user for user in self.users.values() if user.email == email), None)

    def get_user_by_id(self, *, user_id: UUID) -> AuthUserRecord | None:
        """Return one user by UUID, matching the production repository contract."""

        return self.users.get(user_id)

    def create_user(self, *, email: str, password_hash: str, full_name: str) -> AuthUserRecord:
        """Store one new active user record with a generated UUID."""

        user = AuthUserRecord(
            id=uuid4(),
            email=email,
            password_hash=password_hash,
            full_name=full_name,
            status=UserStatus.ACTIVE,
            last_login_at=None,
        )
        self.users[user.id] = user
        return user

    def update_last_login(self, *, user_id: UUID, logged_in_at: datetime) -> AuthUserRecord:
        """Persist the most recent successful login timestamp for a user."""

        user = self.users[user_id]
        updated_user = replace(user, last_login_at=logged_in_at)
        self.users[user_id] = updated_user
        return updated_user

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
        """Store one new session row associated with the provided user."""

        session = AuthSessionRecord(
            id=uuid4(),
            user_id=user_id,
            session_token_hash=session_token_hash,
            expires_at=expires_at,
            last_seen_at=last_seen_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        self.sessions[session.id] = session
        return session

    def get_session_with_user_by_hash(
        self,
        *,
        session_token_hash: str,
    ) -> AuthSessionWithUserRecord | None:
        """Return a joined session and user record when the token hash is known."""

        session = next(
            (
                candidate
                for candidate in self.sessions.values()
                if candidate.session_token_hash == session_token_hash
            ),
            None,
        )
        if session is None:
            return None

        user = self.users[session.user_id]
        return AuthSessionWithUserRecord(session=session, user=user)

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
        """Replace one session's token hash and refresh its expiration metadata."""

        rotated_session = replace(
            self.sessions[session_id],
            session_token_hash=session_token_hash,
            expires_at=expires_at,
            last_seen_at=last_seen_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        self.sessions[session_id] = rotated_session
        return rotated_session

    def touch_session(self, *, session_id: UUID, last_seen_at: datetime) -> AuthSessionRecord:
        """Update the last-seen timestamp for one existing session."""

        touched_session = replace(self.sessions[session_id], last_seen_at=last_seen_at)
        self.sessions[session_id] = touched_session
        return touched_session

    def delete_session(self, *, session_id: UUID) -> None:
        """Remove one session from the in-memory store."""

        self.sessions.pop(session_id, None)

    def commit(self) -> None:
        """Record successful transaction boundaries for assertions."""

        self.commit_calls += 1

    def rollback(self) -> None:
        """Match the production repository interface for service error handling."""

        return None

    @staticmethod
    def is_integrity_error(error: Exception) -> bool:
        """Mirror the repository helper used by the auth service on DB failures."""

        return False
