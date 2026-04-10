"""
Purpose: Issue, validate, revoke, and scope-check personal access tokens for the CLI.
Scope: Credential exchange, bearer-token authentication, revocation, expiration,
and last-used tracking for API tokens stored as hashes.
Dependencies: Password hashing, shared UTC helpers, and the auth repository
records that expose users and API tokens.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from services.auth.passwords import PasswordHasher
from services.common.types import utc_now
from services.db.models.auth import UserStatus
from services.db.repositories.auth_repo import (
    ApiTokenRecord,
    ApiTokenWithUserRecord,
    AuthUserRecord,
)

DEFAULT_API_TOKEN_EXPIRY_DAYS = 30
MAX_API_TOKEN_EXPIRY_DAYS = 365


class ApiTokenScope(StrEnum):
    """Enumerate the supported CLI bearer-token scopes for current API surfaces."""

    CLI_ACCESS = "cli:access"


DEFAULT_API_TOKEN_SCOPES: tuple[ApiTokenScope, ...] = (ApiTokenScope.CLI_ACCESS,)


class ApiTokenErrorCode(StrEnum):
    """Enumerate stable error codes surfaced by personal access token workflows."""

    INVALID_CREDENTIALS = "invalid_credentials"
    INVALID_TOKEN = "invalid_token"
    TOKEN_EXPIRED = "token_expired"
    TOKEN_REVOKED = "token_revoked"
    TOKEN_NOT_FOUND = "token_not_found"
    TOKEN_REQUIRED = "token_required"
    USER_DISABLED = "user_disabled"
    INSUFFICIENT_SCOPE = "insufficient_scope"


class ApiTokenServiceError(Exception):
    """Represent an expected PAT-domain failure that API routes can convert into HTTP errors."""

    def __init__(self, *, status_code: int, code: ApiTokenErrorCode, message: str) -> None:
        """Capture the HTTP status, stable error code, and operator-facing recovery message."""

        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class IssuedApiTokenResult:
    """Describe a newly created PAT and the raw token value returned exactly once to the caller."""

    user: AuthUserRecord
    api_token: ApiTokenRecord
    plain_text_token: str


@dataclass(frozen=True, slots=True)
class AuthenticatedApiTokenResult:
    """Describe a successfully authenticated PAT and its owning active user."""

    user: AuthUserRecord
    api_token: ApiTokenRecord


class ApiTokenRepositoryProtocol(Protocol):
    """Describe the persistence operations required by the canonical PAT service."""

    def get_user_by_email(self, *, email: str) -> AuthUserRecord | None:
        """Return a user by canonical email when present."""

    def get_user_by_id(self, *, user_id: UUID) -> AuthUserRecord | None:
        """Return a user by UUID when present."""

    def update_last_login(self, *, user_id: UUID, logged_in_at: datetime) -> AuthUserRecord:
        """Persist the most recent successful credential-login timestamp for a user."""

    def create_api_token(
        self,
        *,
        user_id: UUID,
        name: str,
        token_hash: str,
        scope: tuple[str, ...],
        expires_at: datetime | None,
    ) -> ApiTokenRecord:
        """Persist one new personal access token row and return its immutable view."""

    def list_api_tokens_for_user(self, *, user_id: UUID) -> tuple[ApiTokenRecord, ...]:
        """Return the current user's PAT rows in deterministic order."""

    def get_api_token_by_id_for_user(
        self,
        *,
        token_id: UUID,
        user_id: UUID,
    ) -> ApiTokenRecord | None:
        """Return one PAT by UUID when it belongs to the specified user."""

    def get_api_token_with_user_by_hash(
        self,
        *,
        token_hash: str,
    ) -> ApiTokenWithUserRecord | None:
        """Load a persisted PAT and owning user by token hash."""

    def update_api_token_last_used(
        self,
        *,
        token_id: UUID,
        last_used_at: datetime,
    ) -> ApiTokenRecord:
        """Persist the latest successful bearer-auth use timestamp for a PAT."""

    def revoke_api_token(self, *, token_id: UUID, revoked_at: datetime) -> ApiTokenRecord:
        """Mark a PAT as revoked so future bearer-auth attempts fail fast."""

    def commit(self) -> None:
        """Commit the current unit of work."""

    def rollback(self) -> None:
        """Rollback the current unit of work after an error."""


class ApiTokenService:
    """Provide the canonical personal access token workflows used by API routes and the CLI."""

    def __init__(
        self,
        *,
        repository: ApiTokenRepositoryProtocol,
        password_hasher: PasswordHasher,
        token_signing_secret: str,
    ) -> None:
        """Capture the persistence boundary, password verifier, and token hashing secret."""

        self._repository = repository
        self._password_hasher = password_hasher
        self._token_signing_secret = token_signing_secret.encode("utf-8")

    def create_token_for_user(
        self,
        *,
        user_id: UUID,
        name: str,
        scopes: tuple[ApiTokenScope | str, ...] | None = None,
        expires_in_days: int | None = None,
        now: datetime | None = None,
    ) -> IssuedApiTokenResult:
        """Issue a new PAT for an already authenticated user session."""

        user = self._require_user(user_id=user_id)
        self._ensure_user_is_active(user=user)
        normalized_name = _normalize_token_name(name)
        normalized_scopes = _normalize_scopes(scopes)
        issued_at = now or utc_now()
        expires_at = _resolve_expiration(issued_at=issued_at, expires_in_days=expires_in_days)
        plain_text_token = self._generate_token()
        token_hash = self.hash_token(plain_text_token)

        try:
            api_token = self._repository.create_api_token(
                user_id=user.id,
                name=normalized_name,
                token_hash=token_hash,
                scope=tuple(scope.value for scope in normalized_scopes),
                expires_at=expires_at,
            )
            self._repository.commit()
        except Exception:
            self._repository.rollback()
            raise

        return IssuedApiTokenResult(
            user=user,
            api_token=api_token,
            plain_text_token=plain_text_token,
        )

    def login_with_password(
        self,
        *,
        email: str,
        password: str,
        name: str,
        scopes: tuple[ApiTokenScope | str, ...] | None = None,
        expires_in_days: int | None = None,
        now: datetime | None = None,
    ) -> IssuedApiTokenResult:
        """Verify email/password credentials and exchange them for a new PAT."""

        user = self._repository.get_user_by_email(email=email)
        if user is None:
            raise self._invalid_credentials_error()

        self._ensure_user_is_active(user=user)
        if not self._password_hasher.verify_password(password, user.password_hash):
            raise self._invalid_credentials_error()

        issued_at = now or utc_now()
        normalized_name = _normalize_token_name(name)
        normalized_scopes = _normalize_scopes(scopes)
        expires_at = _resolve_expiration(issued_at=issued_at, expires_in_days=expires_in_days)
        plain_text_token = self._generate_token()
        token_hash = self.hash_token(plain_text_token)

        try:
            updated_user = self._repository.update_last_login(
                user_id=user.id,
                logged_in_at=issued_at,
            )
            api_token = self._repository.create_api_token(
                user_id=user.id,
                name=normalized_name,
                token_hash=token_hash,
                scope=tuple(scope.value for scope in normalized_scopes),
                expires_at=expires_at,
            )
            self._repository.commit()
        except Exception:
            self._repository.rollback()
            raise

        return IssuedApiTokenResult(
            user=updated_user,
            api_token=api_token,
            plain_text_token=plain_text_token,
        )

    def list_tokens_for_user(self, *, user_id: UUID) -> tuple[ApiTokenRecord, ...]:
        """Return PAT rows after verifying the referenced user still exists and is active."""

        user = self._require_user(user_id=user_id)
        self._ensure_user_is_active(user=user)
        return self._repository.list_api_tokens_for_user(user_id=user.id)

    def authenticate_token(
        self,
        *,
        token: str,
        required_scopes: tuple[ApiTokenScope | str, ...] | None = None,
        now: datetime | None = None,
    ) -> AuthenticatedApiTokenResult:
        """Validate a raw bearer token, enforce scope requirements, and touch last-used metadata."""

        stripped_token = token.strip()
        if not stripped_token:
            raise ApiTokenServiceError(
                status_code=401,
                code=ApiTokenErrorCode.TOKEN_REQUIRED,
                message="Provide a personal access token in the Authorization header.",
            )

        observed_at = now or utc_now()
        token_with_user = self._repository.get_api_token_with_user_by_hash(
            token_hash=self.hash_token(stripped_token)
        )
        if token_with_user is None:
            raise ApiTokenServiceError(
                status_code=401,
                code=ApiTokenErrorCode.INVALID_TOKEN,
                message="The personal access token is not valid. Run the CLI login command again.",
            )

        api_token = token_with_user.api_token
        user = token_with_user.user
        self._ensure_user_is_active(user=user)

        if api_token.revoked_at is not None:
            raise ApiTokenServiceError(
                status_code=401,
                code=ApiTokenErrorCode.TOKEN_REVOKED,
                message="This personal access token was revoked. Create a new token and try again.",
            )

        if api_token.expires_at is not None and observed_at >= api_token.expires_at:
            raise ApiTokenServiceError(
                status_code=401,
                code=ApiTokenErrorCode.TOKEN_EXPIRED,
                message="This personal access token has expired. Create a new token and try again.",
            )

        missing_scopes = _resolve_missing_scopes(
            granted_scopes=api_token.scope,
            required_scopes=_normalize_scopes(required_scopes),
        )
        if missing_scopes:
            missing_scope_values = ", ".join(scope.value for scope in missing_scopes)
            raise ApiTokenServiceError(
                status_code=403,
                code=ApiTokenErrorCode.INSUFFICIENT_SCOPE,
                message=(
                    "This personal access token does not grant the required scope(s): "
                    f"{missing_scope_values}."
                ),
            )

        try:
            touched_api_token = self._repository.update_api_token_last_used(
                token_id=api_token.id,
                last_used_at=observed_at,
            )
            self._repository.commit()
        except Exception:
            self._repository.rollback()
            raise

        return AuthenticatedApiTokenResult(user=user, api_token=touched_api_token)

    def revoke_token_for_user(
        self,
        *,
        user_id: UUID,
        token_id: UUID,
        now: datetime | None = None,
    ) -> ApiTokenRecord:
        """Revoke one PAT when it belongs to the specified active user."""

        user = self._require_user(user_id=user_id)
        self._ensure_user_is_active(user=user)
        api_token = self._repository.get_api_token_by_id_for_user(
            token_id=token_id,
            user_id=user.id,
        )
        if api_token is None:
            raise ApiTokenServiceError(
                status_code=404,
                code=ApiTokenErrorCode.TOKEN_NOT_FOUND,
                message="The requested personal access token does not exist for this user.",
            )

        return self._revoke_token(token_id=api_token.id, now=now)

    def revoke_authenticated_token(
        self,
        *,
        token: str,
        required_scopes: tuple[ApiTokenScope | str, ...] | None = None,
        now: datetime | None = None,
    ) -> ApiTokenRecord:
        """Authenticate one raw bearer token and revoke that exact token in a single flow."""

        authenticated = self.authenticate_token(
            token=token,
            required_scopes=required_scopes,
            now=now,
        )
        return self._revoke_token(token_id=authenticated.api_token.id, now=now)

    @staticmethod
    def supported_scopes() -> tuple[ApiTokenScope, ...]:
        """Return the canonical PAT scope values currently supported by the application."""

        return tuple(ApiTokenScope)

    def hash_token(self, token: str) -> str:
        """Hash a raw PAT with the configured signing secret before persistence or lookup."""

        return hmac.new(
            self._token_signing_secret,
            token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _generate_token(self) -> str:
        """Generate a high-entropy PAT string with a stable application-specific prefix."""

        return f"aat_{secrets.token_urlsafe(48)}"

    def _revoke_token(self, *, token_id: UUID, now: datetime | None = None) -> ApiTokenRecord:
        """Revoke one PAT and commit the mutation with rollback on unexpected failure."""

        revoked_at = now or utc_now()
        try:
            api_token = self._repository.revoke_api_token(token_id=token_id, revoked_at=revoked_at)
            self._repository.commit()
        except Exception:
            self._repository.rollback()
            raise

        return api_token

    def _require_user(self, *, user_id: UUID) -> AuthUserRecord:
        """Load one user by UUID or fail fast when a PAT workflow references missing data."""

        user = self._repository.get_user_by_id(user_id=user_id)
        if user is None:
            message = f"User {user_id} does not exist."
            raise LookupError(message)

        return user

    @staticmethod
    def _ensure_user_is_active(*, user: AuthUserRecord) -> None:
        """Reject PAT creation and bearer auth for disabled users."""

        if user.status is UserStatus.DISABLED:
            raise ApiTokenServiceError(
                status_code=403,
                code=ApiTokenErrorCode.USER_DISABLED,
                message="This user account is disabled. Contact an administrator to reactivate it.",
            )

    @staticmethod
    def _invalid_credentials_error() -> ApiTokenServiceError:
        """Return the canonical credentials error without leaking account existence details."""

        return ApiTokenServiceError(
            status_code=401,
            code=ApiTokenErrorCode.INVALID_CREDENTIALS,
            message="The email address or password is incorrect.",
        )


def _normalize_token_name(value: str) -> str:
    """Trim PAT display names and reject blank or whitespace-only names."""

    normalized = value.strip()
    if not normalized:
        message = "Token name cannot be blank."
        raise ValueError(message)

    return normalized


def _normalize_scopes(
    scopes: tuple[ApiTokenScope | str, ...] | None,
) -> tuple[ApiTokenScope, ...]:
    """Normalize PAT scopes into a stable tuple of supported unique enum values."""

    resolved_scopes = scopes or DEFAULT_API_TOKEN_SCOPES
    normalized_values: list[ApiTokenScope] = []
    seen_scope_values: set[str] = set()
    for raw_scope in resolved_scopes:
        normalized_scope = ApiTokenScope(str(raw_scope).strip())
        if normalized_scope.value in seen_scope_values:
            continue
        seen_scope_values.add(normalized_scope.value)
        normalized_values.append(normalized_scope)

    if not normalized_values:
        message = "At least one personal access token scope is required."
        raise ValueError(message)

    return tuple(normalized_values)


def _resolve_expiration(*, issued_at: datetime, expires_in_days: int | None) -> datetime:
    """Resolve the PAT expiry timestamp and reject unsupported retention windows."""

    resolved_days = (
        expires_in_days
        if expires_in_days is not None
        else DEFAULT_API_TOKEN_EXPIRY_DAYS
    )
    if resolved_days < 1 or resolved_days > MAX_API_TOKEN_EXPIRY_DAYS:
        message = (
            f"Personal access tokens must expire between 1 and {MAX_API_TOKEN_EXPIRY_DAYS} days."
        )
        raise ValueError(message)

    return issued_at + timedelta(days=resolved_days)


def _resolve_missing_scopes(
    *,
    granted_scopes: tuple[str, ...],
    required_scopes: tuple[ApiTokenScope, ...],
) -> tuple[ApiTokenScope, ...]:
    """Return required scopes that are not currently granted by the authenticated PAT."""

    granted_scope_values = {scope_value.casefold() for scope_value in granted_scopes}
    return tuple(
        scope
        for scope in required_scopes
        if scope.value.casefold() not in granted_scope_values
    )


__all__ = [
    "DEFAULT_API_TOKEN_EXPIRY_DAYS",
    "DEFAULT_API_TOKEN_SCOPES",
    "MAX_API_TOKEN_EXPIRY_DAYS",
    "ApiTokenErrorCode",
    "ApiTokenScope",
    "ApiTokenService",
    "ApiTokenServiceError",
    "AuthenticatedApiTokenResult",
    "IssuedApiTokenResult",
]
