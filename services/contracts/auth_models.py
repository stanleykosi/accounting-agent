"""
Purpose: Define strict API contracts for local authentication and session lifecycle routes.
Scope: Registration, login, current-session introspection, and logout response payloads.
Dependencies: Pydantic contract defaults and shared time-aware response fields.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator
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
