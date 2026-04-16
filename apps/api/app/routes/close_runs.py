"""
Purpose: Expose authenticated close-run lifecycle routes for entity workspaces.
Scope: Create, list, read, transition, approve, archive, and reopen APIs that
translate close-run service rules into strict HTTP responses.
Dependencies: FastAPI, local-auth session helpers, close-run contracts and
services, and the shared DB dependency.
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
    get_auth_service,
)
from apps.api.app.routes.request_auth import AuthenticatedUserContext, RequestAuthDependency
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from services.auth.service import (
    AuthenticatedSessionResult,
    AuthErrorCode,
    AuthService,
    AuthServiceError,
)
from services.close_runs.service import CloseRunService, CloseRunServiceError
from services.common.settings import AppSettings, get_settings
from services.contracts.close_run_models import (
    CloseRunDecisionRequest,
    CloseRunListResponse,
    CloseRunRewindResponse,
    CloseRunReopenResponse,
    CloseRunSummary,
    CloseRunTransitionResponse,
    CreateCloseRunRequest,
    RewindCloseRunRequest,
    TransitionCloseRunRequest,
)
from services.db.models.audit import AuditSourceSurface
from services.db.repositories.close_run_repo import CloseRunRepository
from services.db.repositories.entity_repo import EntityUserRecord

router = APIRouter(prefix="/entities/{entity_id}/close-runs", tags=["close_runs"])

SettingsDependency = Annotated[AppSettings, Depends(get_settings)]
AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]


def get_close_run_service(db_session: DatabaseSessionDependency) -> CloseRunService:
    """Construct the canonical close-run service from request-scoped persistence."""

    return CloseRunService(repository=CloseRunRepository(db_session=db_session))


CloseRunServiceDependency = Annotated[CloseRunService, Depends(get_close_run_service)]


@router.get(
    "",
    response_model=CloseRunListResponse,
    summary="List close runs for one entity workspace",
)
def list_close_runs(
    entity_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    close_run_service: CloseRunServiceDependency,
    auth_context: RequestAuthDependency,
) -> CloseRunListResponse:
    """Return the authenticated caller's close runs for one accessible workspace."""

    session_result = auth_context
    try:
        return close_run_service.list_close_runs_for_entity(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
        )
    except CloseRunServiceError as error:
        raise _build_close_run_http_exception(error) from error


@router.post(
    "",
    response_model=CloseRunSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Create one close run",
)
def create_close_run(
    entity_id: UUID,
    payload: CreateCloseRunRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    close_run_service: CloseRunServiceDependency,
) -> CloseRunSummary:
    """Create a period close run and seed the five canonical workflow phase states."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return close_run_service.create_close_run(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            period_start=payload.period_start,
            period_end=payload.period_end,
            reporting_currency=payload.reporting_currency,
            allow_duplicate_period=payload.allow_duplicate_period,
            duplicate_period_reason=payload.duplicate_period_reason,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except CloseRunServiceError as error:
        raise _build_close_run_http_exception(error) from error


@router.get(
    "/{close_run_id}",
    response_model=CloseRunSummary,
    summary="Read one close run",
)
def read_close_run(
    entity_id: UUID,
    close_run_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    close_run_service: CloseRunServiceDependency,
    auth_context: RequestAuthDependency,
) -> CloseRunSummary:
    """Return one close run with calculated phase-gate state."""

    session_result = auth_context
    try:
        return close_run_service.get_close_run(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
    except CloseRunServiceError as error:
        raise _build_close_run_http_exception(error) from error


@router.post(
    "/{close_run_id}/transition",
    response_model=CloseRunTransitionResponse,
    summary="Advance one close run into the next workflow phase",
)
def transition_close_run(
    entity_id: UUID,
    close_run_id: UUID,
    payload: TransitionCloseRunRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    close_run_service: CloseRunServiceDependency,
) -> CloseRunTransitionResponse:
    """Complete the active ready phase and open the requested immediate successor."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return close_run_service.transition_close_run(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            close_run_id=close_run_id,
            target_phase=payload.target_phase,
            reason=payload.reason,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except CloseRunServiceError as error:
        raise _build_close_run_http_exception(error) from error


@router.post(
    "/{close_run_id}/rewind",
    response_model=CloseRunRewindResponse,
    summary="Reopen an earlier workflow phase on a mutable close run",
)
def rewind_close_run(
    entity_id: UUID,
    close_run_id: UUID,
    payload: RewindCloseRunRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    close_run_service: CloseRunServiceDependency,
) -> CloseRunRewindResponse:
    """Move a mutable close run back into an earlier canonical phase."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return close_run_service.rewind_close_run(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            close_run_id=close_run_id,
            target_phase=payload.target_phase,
            reason=payload.reason,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except CloseRunServiceError as error:
        raise _build_close_run_http_exception(error) from error


@router.post(
    "/{close_run_id}/approve",
    response_model=CloseRunSummary,
    summary="Approve and sign off one close run",
)
def approve_close_run(
    entity_id: UUID,
    close_run_id: UUID,
    payload: CloseRunDecisionRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    close_run_service: CloseRunServiceDependency,
) -> CloseRunSummary:
    """Approve a close run after all phase gates have reached Review / Sign-off readiness."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return close_run_service.approve_close_run(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            close_run_id=close_run_id,
            reason=payload.reason,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except CloseRunServiceError as error:
        raise _build_close_run_http_exception(error) from error


@router.post(
    "/{close_run_id}/archive",
    response_model=CloseRunSummary,
    summary="Archive one approved or exported close run",
)
def archive_close_run(
    entity_id: UUID,
    close_run_id: UUID,
    payload: CloseRunDecisionRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    close_run_service: CloseRunServiceDependency,
) -> CloseRunSummary:
    """Archive a signed-off or exported close run while preserving history."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return close_run_service.archive_close_run(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            close_run_id=close_run_id,
            reason=payload.reason,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except CloseRunServiceError as error:
        raise _build_close_run_http_exception(error) from error


@router.post(
    "/{close_run_id}/reopen",
    response_model=CloseRunReopenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Reopen one approved, exported, or archived close run",
)
def reopen_close_run(
    entity_id: UUID,
    close_run_id: UUID,
    payload: CloseRunDecisionRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    close_run_service: CloseRunServiceDependency,
) -> CloseRunReopenResponse:
    """Create a new reopened working version from a signed-off or released close run."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return close_run_service.reopen_close_run(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            close_run_id=close_run_id,
            reason=payload.reason,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except CloseRunServiceError as error:
        raise _build_close_run_http_exception(error) from error


def _require_authenticated_browser_session(
    *,
    request: Request,
    response: Response,
    settings: AppSettings,
    auth_service: AuthService,
) -> AuthenticatedSessionResult:
    """Validate the caller's browser session and keep rotated cookies synchronized."""

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

    return session_result


def _to_entity_user(session_result: AuthenticatedUserContext) -> EntityUserRecord:
    """Project the authenticated session user into the close-run actor record."""

    return EntityUserRecord(
        id=session_result.user.id,
        email=session_result.user.email,
        full_name=session_result.user.full_name,
    )


def _resolve_trace_id(request: Request) -> str | None:
    """Return the request ID bound by middleware so timeline events can link to logs."""

    request_id = getattr(request.state, "request_id", None)
    return str(request_id) if request_id is not None else None


def _build_close_run_http_exception(error: CloseRunServiceError) -> HTTPException:
    """Convert a close-run-domain error into the API's structured HTTP shape."""

    return HTTPException(
        status_code=error.status_code,
        detail={
            "code": str(error.code),
            "message": error.message,
        },
    )


__all__ = ["router"]
