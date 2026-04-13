"""
Purpose: Provide one authenticated request context for browser and CLI API callers.
Scope: Prefer the existing browser session cookie and allow CLI personal access token
bearer authentication for non-browser surfaces without requiring token setup for cookies.
Dependencies: FastAPI request/response dependencies, auth/session helpers, and PAT service.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Protocol

from apps.api.app.dependencies.db import DatabaseSessionDependency
from apps.api.app.routes.auth import (
    _build_http_exception,
    _clear_session_cookie,
    _read_session_cookie,
    _resolve_ip_address,
    _set_session_cookie,
    get_auth_service,
)
from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from services.auth.api_tokens import ApiTokenScope, ApiTokenService, ApiTokenServiceError
from services.auth.passwords import PasswordHasher
from services.auth.service import AuthErrorCode, AuthService, AuthServiceError
from services.common.settings import AppSettings, get_settings
from services.db.repositories.auth_repo import AuthRepository, AuthUserRecord

SettingsDependency = Annotated[AppSettings, Depends(get_settings)]
AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]
BearerCredentialsDependency = Annotated[
    HTTPAuthorizationCredentials | None,
    Depends(HTTPBearer(auto_error=False)),
]


class AuthenticatedUserContext(Protocol):
    """Describe the authenticated user shape route actor helpers consume."""

    @property
    def user(self) -> AuthUserRecord:
        """Return the authenticated user attached to the current request."""
        ...


@dataclass(frozen=True, slots=True)
class AuthenticatedRequestContext:
    """Describe the authenticated user resolved from a cookie session or CLI PAT."""

    user: AuthUserRecord
    session_token: str | None = None
    rotated: bool = False


def require_authenticated_request(
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    db_session: DatabaseSessionDependency,
    credentials: BearerCredentialsDependency,
) -> AuthenticatedRequestContext:
    """Authenticate a browser session cookie or CLI bearer token for shared API routes."""

    session_token = _read_session_cookie(request=request, settings=settings)
    if session_token is not None:
        try:
            session_result = auth_service.authenticate_session(
                session_token=session_token,
                user_agent=request.headers.get("user-agent"),
                ip_address=_resolve_ip_address(request),
            )
        except AuthServiceError as error:
            _clear_session_cookie(response=response, settings=settings)
            raise _build_http_exception(error) from error

        if session_result.session_token is not None:
            _set_session_cookie(
                response=response,
                settings=settings,
                session_token=session_result.session_token,
            )

        return AuthenticatedRequestContext(
            user=session_result.user,
            session_token=session_result.session_token,
            rotated=session_result.rotated,
        )

    if credentials is not None:
        token_signing_secret = settings.security.token_signing_secret
        if token_signing_secret is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "api_tokens_not_configured",
                    "message": (
                        "Personal access tokens are not configured. Set "
                        "security_token_signing_secret and restart the API."
                    ),
                },
            )

        api_token_service = ApiTokenService(
            repository=AuthRepository(db_session=db_session),
            password_hasher=PasswordHasher(),
            token_signing_secret=token_signing_secret.get_secret_value(),
        )
        try:
            token_result = api_token_service.authenticate_token(
                token=credentials.credentials,
                required_scopes=(ApiTokenScope.CLI_ACCESS,),
            )
        except ApiTokenServiceError as error:
            raise HTTPException(
                status_code=error.status_code,
                detail={
                    "code": str(error.code),
                    "message": error.message,
                },
            ) from error

        return AuthenticatedRequestContext(user=token_result.user)

    raise _build_http_exception(
        AuthServiceError(
            status_code=401,
            code=AuthErrorCode.SESSION_REQUIRED,
            message="Sign in or provide a CLI personal access token to continue.",
        )
    )


RequestAuthDependency = Annotated[
    AuthenticatedRequestContext,
    Depends(require_authenticated_request),
]

__all__ = [
    "AuthenticatedRequestContext",
    "AuthenticatedUserContext",
    "RequestAuthDependency",
    "require_authenticated_request",
]
