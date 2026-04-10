"""
Purpose: Expose the canonical local-auth API routes for registration,
login, logout, and session reads.
Scope: HTTP request validation, cookie management, and translation of auth
service results into API contracts.
Dependencies: FastAPI, auth contracts, auth service orchestration, and the
shared DB/settings dependencies.
"""

from __future__ import annotations

from typing import Annotated

from apps.api.app.dependencies.db import DatabaseSessionDependency
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from services.auth.passwords import PasswordHasher
from services.auth.service import (
    AuthenticatedSessionResult,
    AuthErrorCode,
    AuthService,
    AuthServiceError,
    serialize_uuid,
)
from services.auth.sessions import SessionManager
from services.common.settings import AppSettings, get_settings
from services.common.types import DeploymentEnvironment
from services.contracts.auth_models import (
    AuthenticatedUser,
    AuthSessionResponse,
    LoginRequest,
    LogoutResponse,
    RegistrationRequest,
    SessionDetails,
)
from services.db.repositories.auth_repo import AuthRepository

router = APIRouter(prefix="/auth", tags=["auth"])

SettingsDependency = Annotated[AppSettings, Depends(get_settings)]


def get_auth_service(
    db_session: DatabaseSessionDependency,
    settings: SettingsDependency,
) -> AuthService:
    """Construct the canonical auth service from request-scoped persistence and shared settings."""

    return AuthService(
        repository=AuthRepository(db_session=db_session),
        password_hasher=PasswordHasher(),
        session_manager=SessionManager(settings=settings),
    )


AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]


@router.post(
    "/register",
    response_model=AuthSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new local user",
)
def register_user(
    payload: RegistrationRequest,
    request: Request,
    response: Response,
    auth_service: AuthServiceDependency,
    settings: SettingsDependency,
) -> AuthSessionResponse:
    """Create a new local user account and issue the initial authenticated session cookie."""

    try:
        result = auth_service.register_user(
            email=payload.email,
            full_name=payload.full_name,
            password=payload.password,
            user_agent=request.headers.get("user-agent"),
            ip_address=_resolve_ip_address(request),
        )
    except AuthServiceError as error:
        raise _build_http_exception(error) from error

    _set_session_cookie(response=response, settings=settings, session_token=result.session_token)
    return _build_auth_response(result)


@router.post(
    "/login",
    response_model=AuthSessionResponse,
    summary="Log in with local email and password",
)
def login_user(
    payload: LoginRequest,
    request: Request,
    response: Response,
    auth_service: AuthServiceDependency,
    settings: SettingsDependency,
) -> AuthSessionResponse:
    """Verify local credentials and issue a fresh authenticated session cookie."""

    try:
        result = auth_service.login_user(
            email=payload.email,
            password=payload.password,
            user_agent=request.headers.get("user-agent"),
            ip_address=_resolve_ip_address(request),
        )
    except AuthServiceError as error:
        raise _build_http_exception(error) from error

    _set_session_cookie(response=response, settings=settings, session_token=result.session_token)
    return _build_auth_response(result)


@router.get(
    "/session",
    response_model=AuthSessionResponse,
    summary="Read the current authenticated session",
)
def read_current_session(
    request: Request,
    response: Response,
    auth_service: AuthServiceDependency,
    settings: SettingsDependency,
) -> AuthSessionResponse:
    """Validate the caller's session cookie and rotate it when the configured window is reached."""

    session_token = _read_session_cookie(request=request, settings=settings)
    if session_token is None:
        raise _build_http_exception(
            AuthServiceError(
                status_code=401,
                code=AuthErrorCode.SESSION_REQUIRED,
                message="Sign in to continue.",
            )
        )

    try:
        result = auth_service.authenticate_session(
            session_token=session_token,
            user_agent=request.headers.get("user-agent"),
            ip_address=_resolve_ip_address(request),
        )
    except AuthServiceError as error:
        _clear_session_cookie(response=response, settings=settings)
        raise _build_http_exception(error) from error

    if result.session_token is not None:
        _set_session_cookie(
            response=response,
            settings=settings,
            session_token=result.session_token,
        )

    return _build_auth_response(result)


@router.post(
    "/logout",
    response_model=LogoutResponse,
    summary="Log out the current local session",
)
def logout_user(
    request: Request,
    response: Response,
    auth_service: AuthServiceDependency,
    settings: SettingsDependency,
) -> LogoutResponse:
    """Revoke the caller's current session cookie and clear it from the response."""

    auth_service.logout_session(
        session_token=_read_session_cookie(request=request, settings=settings)
    )
    _clear_session_cookie(response=response, settings=settings)
    return LogoutResponse()


def _build_auth_response(result: AuthenticatedSessionResult) -> AuthSessionResponse:
    """Translate an auth service result into the strict API response contract."""

    return AuthSessionResponse(
        user=AuthenticatedUser(
            id=serialize_uuid(result.user.id),
            email=result.user.email,
            full_name=result.user.full_name,
            status=result.user.status.value,
            last_login_at=result.user.last_login_at,
        ),
        session=SessionDetails(
            id=serialize_uuid(result.session.id),
            expires_at=result.session.expires_at,
            last_seen_at=result.session.last_seen_at,
            rotated=result.rotated,
        ),
    )


def _set_session_cookie(
    *,
    response: Response,
    settings: AppSettings,
    session_token: str | None,
) -> None:
    """Attach the opaque session token to the response using hardened local-cookie defaults."""

    if session_token is None:
        return

    response.set_cookie(
        key=settings.security.session_cookie_name,
        value=session_token,
        httponly=True,
        secure=settings.runtime.environment is DeploymentEnvironment.PRODUCTION,
        samesite="lax",
        path="/",
        max_age=settings.security.session_ttl_hours * 3_600,
    )


def _clear_session_cookie(*, response: Response, settings: AppSettings) -> None:
    """Expire the current session cookie on the client after logout or auth failure."""

    response.delete_cookie(
        key=settings.security.session_cookie_name,
        httponly=True,
        secure=settings.runtime.environment is DeploymentEnvironment.PRODUCTION,
        samesite="lax",
        path="/",
    )


def _read_session_cookie(*, request: Request, settings: AppSettings) -> str | None:
    """Read the opaque session token from the canonical local-auth cookie."""

    return request.cookies.get(settings.security.session_cookie_name)


def _resolve_ip_address(request: Request) -> str | None:
    """Return the client IP address if FastAPI exposed one on the incoming connection."""

    return request.client.host if request.client is not None else None


def _build_http_exception(error: AuthServiceError) -> HTTPException:
    """Convert a domain auth error into a structured FastAPI HTTP exception response."""

    return HTTPException(
        status_code=error.status_code,
        detail={
            "code": str(error.code),
            "message": error.message,
        },
    )


__all__ = ["router"]
