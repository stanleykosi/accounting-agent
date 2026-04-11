"""
Purpose: Expose authenticated ownership, last-touch, and in-progress lock routes.
Scope: Entity-scoped GET, acquire-lock, release-lock, and touch endpoints used by
desktop review workflows to prevent silent collisions.
Dependencies: FastAPI, local-auth session helpers, ownership contracts/services, and DB dependency.
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
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from services.auth.service import (
    AuthenticatedSessionResult,
    AuthErrorCode,
    AuthService,
    AuthServiceError,
)
from services.common.enums import OwnershipTargetType
from services.common.settings import AppSettings, get_settings
from services.contracts.ownership_models import (
    AcquireOwnershipLockRequest,
    OwnershipState,
    ReleaseOwnershipLockRequest,
    TouchOwnershipTargetRequest,
)
from services.db.repositories.entity_repo import EntityUserRecord
from services.db.repositories.ownership_repo import OwnershipRepository
from services.ownership.service import OwnershipService, OwnershipServiceError

router = APIRouter(prefix="/entities/{entity_id}/ownership", tags=["ownership"])

SettingsDependency = Annotated[AppSettings, Depends(get_settings)]
AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]


def get_ownership_service(db_session: DatabaseSessionDependency) -> OwnershipService:
    """Construct the canonical ownership service from request-scoped persistence."""

    return OwnershipService(repository=OwnershipRepository(db_session=db_session))


OwnershipServiceDependency = Annotated[OwnershipService, Depends(get_ownership_service)]


@router.get(
    "/targets/{target_type}/{target_id}",
    response_model=OwnershipState,
    summary="Read ownership metadata for one target",
)
def read_ownership_state(
    entity_id: UUID,
    target_type: OwnershipTargetType,
    target_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    ownership_service: OwnershipServiceDependency,
    close_run_id: Annotated[UUID | None, Query()] = None,
) -> OwnershipState:
    """Return owner, current lock, and last-touch metadata for one accessible target."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return ownership_service.get_ownership_state(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            target_type=target_type,
            target_id=target_id,
            close_run_id=close_run_id,
        )
    except OwnershipServiceError as error:
        raise _build_ownership_http_exception(error) from error


@router.post(
    "/locks/acquire",
    response_model=OwnershipState,
    summary="Acquire an in-progress lock for one target",
)
def acquire_ownership_lock(
    entity_id: UUID,
    payload: AcquireOwnershipLockRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    ownership_service: OwnershipServiceDependency,
) -> OwnershipState:
    """Assign ownership and lock a target for the current review operator."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return ownership_service.acquire_lock(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            target_type=payload.target_type,
            target_id=payload.target_id,
            close_run_id=payload.close_run_id,
            owner_user_id=payload.owner_user_id,
            note=payload.note,
        )
    except OwnershipServiceError as error:
        raise _build_ownership_http_exception(error) from error


@router.post(
    "/locks/release",
    response_model=OwnershipState,
    summary="Release the caller's in-progress lock for one target",
)
def release_ownership_lock(
    entity_id: UUID,
    payload: ReleaseOwnershipLockRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    ownership_service: OwnershipServiceDependency,
) -> OwnershipState:
    """Release a lock only when the current operator holds it."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return ownership_service.release_lock(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            target_type=payload.target_type,
            target_id=payload.target_id,
            close_run_id=payload.close_run_id,
        )
    except OwnershipServiceError as error:
        raise _build_ownership_http_exception(error) from error


@router.post(
    "/touch",
    response_model=OwnershipState,
    summary="Record the current user as last touch for one target",
)
def touch_ownership_target(
    entity_id: UUID,
    payload: TouchOwnershipTargetRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    ownership_service: OwnershipServiceDependency,
) -> OwnershipState:
    """Record last-touch metadata without taking an in-progress lock."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return ownership_service.touch_target(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            target_type=payload.target_type,
            target_id=payload.target_id,
            close_run_id=payload.close_run_id,
        )
    except OwnershipServiceError as error:
        raise _build_ownership_http_exception(error) from error


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


def _to_entity_user(session_result: AuthenticatedSessionResult) -> EntityUserRecord:
    """Project the authenticated session user into the ownership actor record."""

    return EntityUserRecord(
        id=session_result.user.id,
        email=session_result.user.email,
        full_name=session_result.user.full_name,
    )


def _build_ownership_http_exception(error: OwnershipServiceError) -> HTTPException:
    """Convert an ownership-domain error into the API's structured HTTP shape."""

    return HTTPException(
        status_code=error.status_code,
        detail={
            "code": str(error.code),
            "message": error.message,
        },
    )


__all__ = ["router"]
