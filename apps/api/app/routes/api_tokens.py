"""
Purpose: Expose personal access token routes for browser-based management and CLI login.
Scope: PAT creation, listing, revocation, credential exchange, and bearer-token
introspection using the canonical local auth and PAT services.
Dependencies: FastAPI, auth routes for cookie helpers, PAT/auth services, and
strict auth contract models.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from apps.api.app.dependencies.db import DatabaseSessionDependency
from apps.api.app.routes.auth import (
    _build_http_exception,
    _clear_session_cookie,
    _read_session_cookie,
    _resolve_ip_address,
    _set_session_cookie,
)
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from services.auth.api_tokens import (
    ApiTokenErrorCode,
    ApiTokenScope,
    ApiTokenService,
    ApiTokenServiceError,
    AuthenticatedApiTokenResult,
)
from services.auth.passwords import PasswordHasher
from services.auth.service import (
    AuthenticatedSessionResult,
    AuthErrorCode,
    AuthService,
    AuthServiceError,
)
from services.auth.sessions import SessionManager
from services.common.settings import AppSettings, get_settings
from services.contracts.auth_models import (
    ApiTokenAuthResponse,
    ApiTokenCreateRequest,
    ApiTokenCurrentResponse,
    ApiTokenListResponse,
    ApiTokenLoginRequest,
    ApiTokenRevocationResponse,
    ApiTokenSummary,
    AuthenticatedUser,
    IssuedApiToken,
)
from services.db.repositories.auth_repo import ApiTokenRecord, AuthRepository, AuthUserRecord

router = APIRouter(prefix="/api-tokens", tags=["api_tokens"])

SettingsDependency = Annotated[AppSettings, Depends(get_settings)]
bearer_scheme = HTTPBearer(auto_error=False)


def _get_auth_service(
    db_session: DatabaseSessionDependency,
    settings: SettingsDependency,
) -> AuthService:
    """Construct the canonical cookie-backed auth service for browser session checks."""

    return AuthService(
        repository=AuthRepository(db_session=db_session),
        password_hasher=PasswordHasher(),
        session_manager=SessionManager(settings=settings),
    )


def get_api_token_service(
    db_session: DatabaseSessionDependency,
    settings: SettingsDependency,
) -> ApiTokenService:
    """Construct the canonical PAT service from request-scoped persistence and shared settings."""

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

    return ApiTokenService(
        repository=AuthRepository(db_session=db_session),
        password_hasher=PasswordHasher(),
        token_signing_secret=token_signing_secret.get_secret_value(),
    )


ApiTokenServiceDependency = Annotated[ApiTokenService, Depends(get_api_token_service)]
AuthServiceDependency = Annotated[AuthService, Depends(_get_auth_service)]
BearerCredentialsDependency = Annotated[
    HTTPAuthorizationCredentials | None,
    Depends(bearer_scheme),
]


@router.post(
    "/login",
    response_model=ApiTokenAuthResponse,
    summary="Exchange local credentials for a CLI personal access token",
)
def login_for_api_token(
    payload: ApiTokenLoginRequest,
    api_token_service: ApiTokenServiceDependency,
) -> ApiTokenAuthResponse:
    """Verify email/password credentials and return a newly issued PAT for the CLI."""

    try:
        issued_token = api_token_service.login_with_password(
            email=payload.email,
            password=payload.password,
            name=payload.token_name,
            scopes=payload.scopes,
            expires_in_days=payload.expires_in_days,
        )
    except (ApiTokenServiceError, ValueError) as error:
        raise _build_api_token_http_exception(error) from error

    return ApiTokenAuthResponse(
        user=_build_authenticated_user(issued_token.user),
        api_token=_build_issued_api_token_summary(
            api_token=issued_token.api_token,
            token=issued_token.plain_text_token,
        ),
    )


@router.get(
    "",
    response_model=ApiTokenListResponse,
    summary="List the current user's personal access tokens",
)
def list_api_tokens(
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    api_token_service: ApiTokenServiceDependency,
) -> ApiTokenListResponse:
    """Return the authenticated browser user's current PAT inventory."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    tokens = api_token_service.list_tokens_for_user(user_id=session_result.user.id)
    return ApiTokenListResponse(tokens=tuple(_build_api_token_summary(token) for token in tokens))


@router.post(
    "",
    response_model=ApiTokenAuthResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a personal access token for the current user",
)
def create_api_token(
    payload: ApiTokenCreateRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    api_token_service: ApiTokenServiceDependency,
) -> ApiTokenAuthResponse:
    """Issue a new PAT for the authenticated browser user and return it once."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        issued_token = api_token_service.create_token_for_user(
            user_id=session_result.user.id,
            name=payload.name,
            scopes=payload.scopes,
            expires_in_days=payload.expires_in_days,
        )
    except (ApiTokenServiceError, ValueError) as error:
        raise _build_api_token_http_exception(error) from error

    return ApiTokenAuthResponse(
        user=_build_authenticated_user(issued_token.user),
        api_token=_build_issued_api_token_summary(
            api_token=issued_token.api_token,
            token=issued_token.plain_text_token,
        ),
    )


@router.post(
    "/{token_id}/revoke",
    response_model=ApiTokenRevocationResponse,
    summary="Revoke one personal access token for the current user",
)
def revoke_api_token(
    token_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    api_token_service: ApiTokenServiceDependency,
) -> ApiTokenRevocationResponse:
    """Revoke a PAT by UUID after validating the caller's browser session."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        revoked_token = api_token_service.revoke_token_for_user(
            user_id=session_result.user.id,
            token_id=token_id,
        )
    except ApiTokenServiceError as error:
        raise _build_api_token_http_exception(error) from error

    return ApiTokenRevocationResponse(api_token=_build_api_token_summary(revoked_token))


@router.get(
    "/current",
    response_model=ApiTokenCurrentResponse,
    summary="Validate the current bearer token and return its owner",
)
def read_current_api_token(
    credentials: BearerCredentialsDependency,
    api_token_service: ApiTokenServiceDependency,
) -> ApiTokenCurrentResponse:
    """Validate the provided PAT, enforce CLI scope, and return token metadata."""

    authenticated = _require_authenticated_api_token(
        credentials=credentials,
        api_token_service=api_token_service,
    )
    return ApiTokenCurrentResponse(
        user=_build_authenticated_user(authenticated.user),
        api_token=_build_api_token_summary(authenticated.api_token),
    )


@router.post(
    "/current/revoke",
    response_model=ApiTokenRevocationResponse,
    summary="Revoke the current bearer token",
)
def revoke_current_api_token(
    credentials: BearerCredentialsDependency,
    api_token_service: ApiTokenServiceDependency,
) -> ApiTokenRevocationResponse:
    """Authenticate the provided PAT, then revoke that exact token."""

    token = _extract_bearer_token(credentials)
    try:
        revoked_token = api_token_service.revoke_authenticated_token(
            token=token,
            required_scopes=(ApiTokenScope.CLI_ACCESS,),
        )
    except ApiTokenServiceError as error:
        raise _build_api_token_http_exception(error) from error

    return ApiTokenRevocationResponse(api_token=_build_api_token_summary(revoked_token))


def _require_authenticated_browser_session(
    *,
    request: Request,
    response: Response,
    settings: AppSettings,
    auth_service: AuthService,
) -> AuthenticatedSessionResult:
    """Authenticate the caller's session cookie and rotate it when needed."""

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

    return result


def _require_authenticated_api_token(
    *,
    credentials: HTTPAuthorizationCredentials | None,
    api_token_service: ApiTokenService,
) -> AuthenticatedApiTokenResult:
    """Authenticate one bearer token and enforce the CLI access scope."""

    token = _extract_bearer_token(credentials)
    try:
        return api_token_service.authenticate_token(
            token=token,
            required_scopes=(ApiTokenScope.CLI_ACCESS,),
        )
    except ApiTokenServiceError as error:
        raise _build_api_token_http_exception(error) from error


def _extract_bearer_token(credentials: HTTPAuthorizationCredentials | None) -> str:
    """Read the raw bearer token value from the optional FastAPI auth credentials object."""

    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise HTTPException(
            status_code=401,
            detail={
                "code": str(ApiTokenErrorCode.TOKEN_REQUIRED),
                "message": "Provide a personal access token in the Authorization header.",
            },
        )

    return credentials.credentials


def _build_authenticated_user(user: AuthUserRecord) -> AuthenticatedUser:
    """Convert an auth-repository user record into the shared authenticated-user contract."""

    return AuthenticatedUser(
        id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        status=user.status.value,
        last_login_at=user.last_login_at,
    )


def _build_api_token_summary(api_token: ApiTokenRecord) -> ApiTokenSummary:
    """Convert a persisted PAT record into the strict token-summary API contract."""

    return ApiTokenSummary(
        id=str(api_token.id),
        name=api_token.name,
        scopes=tuple(ApiTokenScope(scope_value) for scope_value in api_token.scope),
        created_at=api_token.created_at,
        updated_at=api_token.updated_at,
        last_used_at=api_token.last_used_at,
        revoked_at=api_token.revoked_at,
        expires_at=api_token.expires_at,
    )


def _build_issued_api_token_summary(*, api_token: ApiTokenRecord, token: str) -> IssuedApiToken:
    """Convert a persisted PAT into the issued-token response contract."""

    summary = _build_api_token_summary(api_token)
    return IssuedApiToken(
        id=summary.id,
        name=summary.name,
        scopes=summary.scopes,
        created_at=summary.created_at,
        updated_at=summary.updated_at,
        last_used_at=summary.last_used_at,
        revoked_at=summary.revoked_at,
        expires_at=summary.expires_at,
        token=token,
        token_type="Bearer",
    )


def _build_api_token_http_exception(error: Exception) -> HTTPException:
    """Convert PAT-domain failures and validation errors into structured HTTP exceptions."""

    if isinstance(error, ApiTokenServiceError):
        return HTTPException(
            status_code=error.status_code,
            detail={
                "code": str(error.code),
                "message": error.message,
            },
        )

    if isinstance(error, ValueError):
        return HTTPException(
            status_code=422,
            detail={
                "code": "invalid_api_token_request",
                "message": str(error),
            },
        )

    raise TypeError(f"Unsupported API token error type: {type(error)!r}")


__all__ = ["router"]
