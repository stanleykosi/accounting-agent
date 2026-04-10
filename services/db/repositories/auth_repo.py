"""
Purpose: Persist and query local-auth users and session rows through SQLAlchemy.
Scope: Auth-specific CRUD operations, transactional commits, and thin record
mapping for the service layer.
Dependencies: SQLAlchemy ORM sessions plus the canonical auth models under
services/db/models/auth.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from services.db.models.auth import Session as SessionModel
from services.db.models.auth import User, UserStatus
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class AuthUserRecord:
    """Describe the subset of a user row needed by auth services and response building."""

    id: UUID
    email: str
    password_hash: str
    full_name: str
    status: UserStatus
    last_login_at: datetime | None


@dataclass(frozen=True, slots=True)
class AuthSessionRecord:
    """Describe the subset of a session row needed by auth workflows and cookie management."""

    id: UUID
    user_id: UUID
    session_token_hash: str
    expires_at: datetime
    last_seen_at: datetime
    user_agent: str | None
    ip_address: str | None


@dataclass(frozen=True, slots=True)
class AuthSessionWithUserRecord:
    """Join a persisted session with its owning user for auth validation workflows."""

    session: AuthSessionRecord
    user: AuthUserRecord


class AuthRepository:
    """Execute canonical local-auth persistence operations within one SQLAlchemy session."""

    def __init__(self, *, db_session: Session) -> None:
        """Capture the request-scoped SQLAlchemy session used by the auth service."""

        self._db_session = db_session

    def get_user_by_email(self, *, email: str) -> AuthUserRecord | None:
        """Return one user by canonical email or None when the account does not exist."""

        statement = select(User).where(User.email == email)
        user = self._db_session.execute(statement).scalar_one_or_none()
        if user is None:
            return None

        return _map_user(user)

    def get_user_by_id(self, *, user_id: UUID) -> AuthUserRecord | None:
        """Return one user by UUID for post-mutation response hydration."""

        statement = select(User).where(User.id == user_id)
        user = self._db_session.execute(statement).scalar_one_or_none()
        if user is None:
            return None

        return _map_user(user)

    def create_user(self, *, email: str, password_hash: str, full_name: str) -> AuthUserRecord:
        """Stage a new active user row and flush it so dependent session rows can reference it."""

        user = User(email=email, password_hash=password_hash, full_name=full_name)
        self._db_session.add(user)
        self._db_session.flush()
        return _map_user(user)

    def update_last_login(self, *, user_id: UUID, logged_in_at: datetime) -> AuthUserRecord:
        """Persist the latest successful login timestamp for the specified user."""

        user = self._load_user(user_id=user_id)
        user.last_login_at = logged_in_at
        self._db_session.flush()
        return _map_user(user)

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
        """Stage a new session row for a successful desktop or web authentication flow."""

        session = SessionModel(
            user_id=user_id,
            session_token_hash=session_token_hash,
            expires_at=expires_at,
            last_seen_at=last_seen_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        self._db_session.add(session)
        self._db_session.flush()
        return _map_session(session)

    def get_session_with_user_by_hash(
        self,
        *,
        session_token_hash: str,
    ) -> AuthSessionWithUserRecord | None:
        """Return one session plus its user when the caller presents a known session token."""

        statement = (
            select(SessionModel, User)
            .join(User, SessionModel.user_id == User.id)
            .where(SessionModel.session_token_hash == session_token_hash)
        )
        row = self._db_session.execute(statement).one_or_none()
        if row is None:
            return None

        session, user = row
        return AuthSessionWithUserRecord(session=_map_session(session), user=_map_user(user))

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
        """Replace the stored token hash and refresh metadata for an existing session row."""

        session = self._load_session(session_id=session_id)
        session.session_token_hash = session_token_hash
        session.expires_at = expires_at
        session.last_seen_at = last_seen_at
        session.user_agent = user_agent
        session.ip_address = ip_address
        self._db_session.flush()
        return _map_session(session)

    def touch_session(self, *, session_id: UUID, last_seen_at: datetime) -> AuthSessionRecord:
        """Advance the session heartbeat timestamp after a successful non-rotating request."""

        session = self._load_session(session_id=session_id)
        session.last_seen_at = last_seen_at
        self._db_session.flush()
        return _map_session(session)

    def delete_session(self, *, session_id: UUID) -> None:
        """Remove one session row so the corresponding cookie can no longer authenticate."""

        statement = delete(SessionModel).where(SessionModel.id == session_id)
        self._db_session.execute(statement)
        self._db_session.flush()

    def commit(self) -> None:
        """Commit the current auth transaction and surface integrity problems unchanged."""

        self._db_session.commit()

    def rollback(self) -> None:
        """Rollback the current auth transaction after an expected or unexpected failure."""

        self._db_session.rollback()

    @staticmethod
    def is_integrity_error(error: Exception) -> bool:
        """Return whether an error was raised by the database uniqueness constraints."""

        return isinstance(error, IntegrityError)

    def _load_user(self, *, user_id: UUID) -> User:
        """Load a user row by UUID or fail fast when the auth service references missing data."""

        statement = select(User).where(User.id == user_id)
        user = self._db_session.execute(statement).scalar_one_or_none()
        if user is None:
            message = f"User {user_id} does not exist."
            raise LookupError(message)

        return user

    def _load_session(self, *, session_id: UUID) -> SessionModel:
        """Load a session row by UUID or fail fast when the auth service references missing data."""

        statement = select(SessionModel).where(SessionModel.id == session_id)
        session = self._db_session.execute(statement).scalar_one_or_none()
        if session is None:
            message = f"Session {session_id} does not exist."
            raise LookupError(message)

        return session


def _map_user(user: User) -> AuthUserRecord:
    """Convert an ORM user model into the immutable record consumed by auth services."""

    return AuthUserRecord(
        id=user.id,
        email=user.email,
        password_hash=user.password_hash,
        full_name=user.full_name,
        status=UserStatus(user.status),
        last_login_at=user.last_login_at,
    )


def _map_session(session: SessionModel) -> AuthSessionRecord:
    """Convert an ORM session model into the immutable record consumed by auth services."""

    return AuthSessionRecord(
        id=session.id,
        user_id=session.user_id,
        session_token_hash=session.session_token_hash,
        expires_at=session.expires_at,
        last_seen_at=session.last_seen_at,
        user_agent=session.user_agent,
        ip_address=session.ip_address,
    )


__all__ = [
    "AuthRepository",
    "AuthSessionRecord",
    "AuthSessionWithUserRecord",
    "AuthUserRecord",
]
