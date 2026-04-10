"""
Purpose: Define strict API contracts for local authentication, session lifecycle routes,
and CLI personal access token management.
Scope: Registration, login, current-session introspection, logout, and API token
issue/list/revoke payloads.
Dependencies: Pydantic contract defaults, shared time-aware response fields, and
the canonical PAT scope definitions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator
from services.auth.api_tokens import (
    DEFAULT_API_TOKEN_EXPIRY_DAYS,
    DEFAULT_API_TOKEN_SCOPES,
    MAX_API_TOKEN_EXPIRY_DAYS,
    ApiTokenScope,
)
from services.contracts.api_models import ContractModel


def _normalize_email(value: str) -> str:
    """Normalize an email-like identifier into the canonical lower-case storage form."""

    normalized = value.strip().casefold()
    if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
        message = "Enter a valid email address."
        raise ValueError(message)

    local_part, _, domain_part = normalized.partition("@")
    if "." not in domain_part or not local_part or domain_part.startswith("."):
        message = "Enter a valid email address."
        raise ValueError(message)

    return normalized


class RegistrationRequest(ContractModel):
    """Capture the fields required to create a new locally authenticated user account."""

    email: str = Field(
        min_length=3,
        max_length=320,
        description="Unique email address used to sign in to the local demo.",
    )
    full_name: str = Field(
        min_length=1,
        max_length=200,
        description="Human-readable operator name shown in audit trails and approvals.",
    )
    password: str = Field(
        min_length=12,
        max_length=1_024,
        description="Plaintext password that will be hashed before persistence.",
    )

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        """Enforce a canonical email shape before the request reaches the service layer."""

        return _normalize_email(value)

    @field_validator("full_name")
    @classmethod
    def normalize_full_name(cls, value: str) -> str:
        """Trim user names and reject blank display names."""

        normalized = value.strip()
        if not normalized:
            message = "Full name cannot be blank."
            raise ValueError(message)

        return normalized

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        """Reject whitespace-only registration passwords before the service hashes them."""

        if value.isspace():
            message = "Password cannot be blank."
            raise ValueError(message)

        return value


class LoginRequest(ContractModel):
    """Capture the credentials required to exchange for an authenticated session cookie."""

    email: str = Field(
        min_length=3,
        max_length=320,
        description="Email address associated with a previously registered local account.",
    )
    password: str = Field(
        min_length=1,
        max_length=1_024,
        description="Plaintext password submitted for verification against the stored hash.",
    )

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        """Normalize login identifiers so credential checks are case-insensitive."""

        return _normalize_email(value)

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        """Reject whitespace-only login passwords before credential verification."""

        if value.isspace():
            message = "Password cannot be blank."
            raise ValueError(message)

        return value


class AuthenticatedUser(ContractModel):
    """Describe the signed-in operator associated with the current local auth session."""

    id: str = Field(description="Stable UUID for the authenticated local user.")
    email: str = Field(min_length=3, max_length=320, description="Canonical user email address.")
    full_name: str = Field(min_length=1, max_length=200, description="Audit-friendly display name.")
    status: str = Field(min_length=1, description="Current user lifecycle state.")
    last_login_at: datetime | None = Field(
        default=None,
        description="UTC timestamp for the user's most recent successful credential login.",
    )


class SessionDetails(ContractModel):
    """Describe the server-side session row currently bound to the caller's cookie."""

    id: str = Field(description="Stable UUID for the current session row.")
    expires_at: datetime = Field(
        description="UTC timestamp after which the session becomes invalid."
    )
    last_seen_at: datetime = Field(
        description="UTC timestamp for the latest successful use or rotation of the session."
    )
    rotated: bool = Field(
        description="Indicates whether the session token was rotated during the current request."
    )


class AuthSessionResponse(ContractModel):
    """Return the authenticated user plus current session metadata after auth mutations."""

    user: AuthenticatedUser = Field(description="Authenticated operator profile.")
    session: SessionDetails = Field(description="Current cookie-backed session metadata.")


class LogoutResponse(ContractModel):
    """Acknowledge that the current browser or desktop session has been cleared."""

    status: str = Field(
        default="logged_out",
        description="Deterministic marker confirming the caller no longer has an active session.",
    )


class ApiTokenCreateRequest(ContractModel):
    """Capture the inputs required to issue a PAT for an already authenticated user."""

    name: str = Field(
        min_length=1,
        max_length=120,
        description=(
            "Operator-friendly label shown when listing or revoking personal access tokens."
        ),
    )
    scopes: tuple[ApiTokenScope, ...] = Field(
        default=DEFAULT_API_TOKEN_SCOPES,
        min_length=1,
        description="Scope set granted to the newly created personal access token.",
    )
    expires_in_days: int = Field(
        default=DEFAULT_API_TOKEN_EXPIRY_DAYS,
        ge=1,
        le=MAX_API_TOKEN_EXPIRY_DAYS,
        description="Number of days before the new personal access token expires.",
    )

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        """Trim PAT names and reject blank values after normalization."""

        normalized = value.strip()
        if not normalized:
            message = "Token name cannot be blank."
            raise ValueError(message)

        return normalized

    @field_validator("scopes")
    @classmethod
    def normalize_scopes(cls, value: tuple[ApiTokenScope, ...]) -> tuple[ApiTokenScope, ...]:
        """Remove duplicate PAT scopes while preserving the caller's declared order."""

        resolved: list[ApiTokenScope] = []
        seen_values: set[str] = set()
        for scope in value:
            if scope.value in seen_values:
                continue
            seen_values.add(scope.value)
            resolved.append(scope)

        if not resolved:
            message = "At least one personal access token scope is required."
            raise ValueError(message)

        return tuple(resolved)


class ApiTokenLoginRequest(ContractModel):
    """Capture the credentials and token settings used by the CLI login flow."""

    email: str = Field(
        min_length=3,
        max_length=320,
        description="Email address associated with the local account requesting a PAT.",
    )
    password: str = Field(
        min_length=1,
        max_length=1_024,
        description="Plaintext password used to verify the local account before issuing a PAT.",
    )
    token_name: str = Field(
        min_length=1,
        max_length=120,
        description="Operator-friendly label stored alongside the issued personal access token.",
    )
    scopes: tuple[ApiTokenScope, ...] = Field(
        default=DEFAULT_API_TOKEN_SCOPES,
        min_length=1,
        description="Scope set granted to the issued personal access token.",
    )
    expires_in_days: int = Field(
        default=DEFAULT_API_TOKEN_EXPIRY_DAYS,
        ge=1,
        le=MAX_API_TOKEN_EXPIRY_DAYS,
        description="Number of days before the issued personal access token expires.",
    )

    @field_validator("email")
    @classmethod
    def normalize_token_email(cls, value: str) -> str:
        """Normalize CLI login identifiers so credential checks remain case-insensitive."""

        return _normalize_email(value)

    @field_validator("password")
    @classmethod
    def normalize_token_password(cls, value: str) -> str:
        """Reject blank CLI login passwords before credential verification."""

        if value.isspace():
            message = "Password cannot be blank."
            raise ValueError(message)

        return value

    @field_validator("token_name")
    @classmethod
    def normalize_token_name(cls, value: str) -> str:
        """Trim the PAT label the CLI stores on the server."""

        normalized = value.strip()
        if not normalized:
            message = "Token name cannot be blank."
            raise ValueError(message)

        return normalized

    @field_validator("scopes")
    @classmethod
    def normalize_login_scopes(cls, value: tuple[ApiTokenScope, ...]) -> tuple[ApiTokenScope, ...]:
        """Reuse the canonical deduplication rules for CLI-requested PAT scopes."""

        return ApiTokenCreateRequest.normalize_scopes(value)


class ApiTokenSummary(ContractModel):
    """Describe a persisted personal access token without exposing its raw secret value."""

    id: str = Field(description="Stable UUID for the stored personal access token row.")
    name: str = Field(min_length=1, max_length=120, description="Operator-friendly token label.")
    scopes: tuple[ApiTokenScope, ...] = Field(
        min_length=1,
        description="Scope set granted to the personal access token.",
    )
    created_at: datetime = Field(description="UTC timestamp when the token was originally issued.")
    updated_at: datetime = Field(
        description="UTC timestamp when the token row was last mutated."
    )
    last_used_at: datetime | None = Field(
        default=None,
        description="UTC timestamp for the most recent successful bearer-auth use.",
    )
    revoked_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when the token was revoked, if it is no longer active.",
    )
    expires_at: datetime | None = Field(
        default=None,
        description="UTC timestamp after which the token can no longer authenticate.",
    )


class IssuedApiToken(ApiTokenSummary):
    """Describe a newly created PAT and include the raw secret returned exactly once."""

    token: str = Field(
        min_length=1,
        description=(
            "Opaque bearer token value. This field is only returned when a token is created."
        ),
    )
    token_type: Literal["Bearer"] = Field(
        default="Bearer",
        description="Authorization scheme callers must use when sending the token back to the API.",
    )


class ApiTokenAuthResponse(ContractModel):
    """Return the authenticated user plus a newly issued personal access token."""

    user: AuthenticatedUser = Field(description="User profile that owns the issued PAT.")
    api_token: IssuedApiToken = Field(description="Newly issued personal access token details.")


class ApiTokenCurrentResponse(ContractModel):
    """Return the authenticated user and currently validated PAT metadata for bearer auth checks."""

    user: AuthenticatedUser = Field(description="User profile that owns the authenticated PAT.")
    api_token: ApiTokenSummary = Field(
        description="Stored metadata for the personal access token used on the current request."
    )


class ApiTokenListResponse(ContractModel):
    """Return the current user's stored personal access tokens in deterministic order."""

    tokens: tuple[ApiTokenSummary, ...] = Field(
        default=(),
        description="Newest-first list of personal access tokens for the authenticated user.",
    )


class ApiTokenRevocationResponse(ContractModel):
    """Acknowledge that a PAT was revoked and can no longer authenticate future requests."""

    status: Literal["revoked"] = Field(
        default="revoked",
        description="Deterministic marker confirming the personal access token is revoked.",
    )
    api_token: ApiTokenSummary = Field(
        description="Updated metadata for the token after revocation was persisted."
    )
