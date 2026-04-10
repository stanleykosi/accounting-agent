"""
Purpose: Persist and query local-auth users, sessions, and API tokens through SQLAlchemy.
Scope: Auth-specific CRUD operations, transactional commits, and thin record
mapping for the service layer.
Dependencies: SQLAlchemy ORM sessions plus the canonical auth models under
services/db/models/auth.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from services.db.models.auth import ApiToken as ApiTokenModel
from services.db.models.auth import Session as SessionModel
from services.db.models.auth import User, UserStatus
from sqlalchemy import delete, desc, select
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


@dataclass(frozen=True, slots=True)
class ApiTokenRecord:
    """Describe the subset of a personal access token row used by token services and responses."""

    id: UUID
    user_id: UUID
    name: str
    token_hash: str
    scope: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class ApiTokenWithUserRecord:
    """Join a persisted personal access token with its owning user for bearer auth checks."""

    api_token: ApiTokenRecord
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

    def create_api_token(
        self,
        *,
        user_id: UUID,
        name: str,
        token_hash: str,
        scope: tuple[str, ...],
        expires_at: datetime | None,
    ) -> ApiTokenRecord:
        """Stage a new personal access token for CLI authentication and flush it immediately."""

        api_token = ApiTokenModel(
            user_id=user_id,
            name=name,
            token_hash=token_hash,
            scope=list(scope),
            expires_at=expires_at,
        )
        self._db_session.add(api_token)
        self._db_session.flush()
        return _map_api_token(api_token)

    def list_api_tokens_for_user(self, *, user_id: UUID) -> tuple[ApiTokenRecord, ...]:
        """Return a deterministic newest-first list of personal access tokens for one user."""

        statement = (
            select(ApiTokenModel)
            .where(ApiTokenModel.user_id == user_id)
            .order_by(desc(ApiTokenModel.created_at), desc(ApiTokenModel.id))
        )
        api_tokens = self._db_session.execute(statement).scalars().all()
        return tuple(_map_api_token(api_token) for api_token in api_tokens)

    def get_api_token_by_id_for_user(
        self,
        *,
        token_id: UUID,
        user_id: UUID,
    ) -> ApiTokenRecord | None:
        """Return one personal access token by UUID when it belongs to the specified user."""

        statement = select(ApiTokenModel).where(
            ApiTokenModel.id == token_id,
            ApiTokenModel.user_id == user_id,
        )
        api_token = self._db_session.execute(statement).scalar_one_or_none()
        if api_token is None:
            return None

        return _map_api_token(api_token)

    def get_api_token_with_user_by_hash(
        self,
        *,
        token_hash: str,
    ) -> ApiTokenWithUserRecord | None:
        """Return one personal access token plus its user when the caller presents a known hash."""

        statement = (
            select(ApiTokenModel, User)
            .join(User, ApiTokenModel.user_id == User.id)
            .where(ApiTokenModel.token_hash == token_hash)
        )
        row = self._db_session.execute(statement).one_or_none()
        if row is None:
            return None

        api_token, user = row
        return ApiTokenWithUserRecord(api_token=_map_api_token(api_token), user=_map_user(user))

    def update_api_token_last_used(
        self,
        *,
        token_id: UUID,
        last_used_at: datetime,
    ) -> ApiTokenRecord:
        """Persist the latest successful bearer-auth use timestamp for one token."""

        api_token = self._load_api_token(token_id=token_id)
        api_token.last_used_at = last_used_at
        self._db_session.flush()
        return _map_api_token(api_token)

    def revoke_api_token(self, *, token_id: UUID, revoked_at: datetime) -> ApiTokenRecord:
        """Mark one personal access token as revoked so future bearer auth fails fast."""

        api_token = self._load_api_token(token_id=token_id)
        api_token.revoked_at = revoked_at
        self._db_session.flush()
        return _map_api_token(api_token)

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

    def _load_api_token(self, *, token_id: UUID) -> ApiTokenModel:
        """Load a personal access token row by UUID or fail fast on missing references."""

        statement = select(ApiTokenModel).where(ApiTokenModel.id == token_id)
        api_token = self._db_session.execute(statement).scalar_one_or_none()
        if api_token is None:
            message = f"API token {token_id} does not exist."
            raise LookupError(message)

        return api_token


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


def _map_api_token(api_token: ApiTokenModel) -> ApiTokenRecord:
    """Convert an ORM API token model into the immutable record consumed by token services."""

    return ApiTokenRecord(
        id=api_token.id,
        user_id=api_token.user_id,
        name=api_token.name,
        token_hash=api_token.token_hash,
        scope=tuple(str(scope_value) for scope_value in api_token.scope),
        created_at=api_token.created_at,
        updated_at=api_token.updated_at,
        last_used_at=api_token.last_used_at,
        revoked_at=api_token.revoked_at,
        expires_at=api_token.expires_at,
    )


__all__ = [
    "ApiTokenRecord",
    "ApiTokenWithUserRecord",
    "AuthRepository",
    "AuthSessionRecord",
    "AuthSessionWithUserRecord",
    "AuthUserRecord",
]
