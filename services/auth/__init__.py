"""
Purpose: Expose the canonical local-auth service primitives for API routes and tests.
Scope: Package marker plus explicit exports for password, session, and service helpers.
Dependencies: Individual auth modules under services/auth/.
"""

from services.auth.api_tokens import (
    ApiTokenErrorCode,
    ApiTokenScope,
    ApiTokenService,
    ApiTokenServiceError,
    AuthenticatedApiTokenResult,
    IssuedApiTokenResult,
)
from services.auth.passwords import PasswordHasher
from services.auth.service import AuthErrorCode, AuthService, AuthServiceError
from services.auth.sessions import SessionManager, SessionTokenBundle

__all__ = [
    "ApiTokenErrorCode",
    "ApiTokenScope",
    "ApiTokenService",
    "ApiTokenServiceError",
    "AuthErrorCode",
    "AuthService",
    "AuthServiceError",
    "AuthenticatedApiTokenResult",
    "IssuedApiTokenResult",
    "PasswordHasher",
    "SessionManager",
    "SessionTokenBundle",
]
