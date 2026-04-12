"""
Purpose: Expose authenticated chart-of-accounts routes for entity workspaces.
Scope: COA workspace reads, manual uploads, set activation, and account editor
mutations routed through the canonical COA service layer.
Dependencies: FastAPI, auth-session route helpers, COA contracts/service,
and the shared database dependency.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from apps.api.app.dependencies.db import DatabaseSessionDependency
from apps.api.app.routes.auth import get_auth_service
from apps.api.app.routes.close_runs import (
    _require_authenticated_browser_session,
    _resolve_trace_id,
    _to_entity_user,
)
from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from services.auth.service import AuthService
from services.coa.service import CoaRepository, CoaService, CoaServiceError
from services.common.settings import AppSettings, get_settings
from services.contracts.coa_models import (
    CoaAccountCreateRequest,
    CoaAccountUpdateRequest,
    CoaSetActivationRequest,
    CoaWorkspaceResponse,
)
from services.db.models.audit import AuditSourceSurface

router = APIRouter(prefix="/entities/{entity_id}/coa", tags=["coa"])

SettingsDependency = Annotated[AppSettings, Depends(get_settings)]
AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]


def get_coa_service(db_session: DatabaseSessionDependency) -> CoaService:
    """Construct the canonical COA service from request-scoped persistence."""

    return CoaService(repository=CoaRepository(db_session=db_session))


CoaServiceDependency = Annotated[CoaService, Depends(get_coa_service)]


@router.get(
    "",
    response_model=CoaWorkspaceResponse,
    summary="Read the entity chart-of-accounts workspace",
)
def read_coa_workspace(
    entity_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    coa_service: CoaServiceDependency,
) -> CoaWorkspaceResponse:
    """Return active COA state and version history, applying precedence when needed."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return coa_service.read_workspace(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except CoaServiceError as error:
        raise _build_coa_http_exception(error) from error


@router.post(
    "/upload",
    response_model=CoaWorkspaceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a manual chart-of-accounts file",
)
async def upload_manual_coa(
    entity_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    coa_service: CoaServiceDependency,
    file: Annotated[UploadFile, File(description="CSV or XLSX chart-of-accounts file.")],
) -> CoaWorkspaceResponse:
    """Import one manual COA file as a new active versioned set."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )

    try:
        return coa_service.upload_manual_coa(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            filename=file.filename or "coa_upload",
            payload=await file.read(),
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except CoaServiceError as error:
        raise _build_coa_http_exception(error) from error
    finally:
        await file.close()


@router.post(
    "/sets/{coa_set_id}/activate",
    response_model=CoaWorkspaceResponse,
    summary="Activate one chart-of-accounts set version",
)
def activate_coa_set(
    entity_id: UUID,
    coa_set_id: UUID,
    payload: CoaSetActivationRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    coa_service: CoaServiceDependency,
) -> CoaWorkspaceResponse:
    """Switch the active COA set version for the entity workspace."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )

    try:
        return coa_service.activate_coa_set(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            coa_set_id=coa_set_id,
            reason=payload.reason,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except CoaServiceError as error:
        raise _build_coa_http_exception(error) from error


@router.post(
    "/accounts",
    response_model=CoaWorkspaceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create one account through versioned COA editing",
)
def create_coa_account(
    entity_id: UUID,
    payload: CoaAccountCreateRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    coa_service: CoaServiceDependency,
) -> CoaWorkspaceResponse:
    """Create one COA account by materializing a new immutable manual revision."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )

    try:
        return coa_service.create_account(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            payload=payload,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except CoaServiceError as error:
        raise _build_coa_http_exception(error) from error


@router.patch(
    "/accounts/{account_id}",
    response_model=CoaWorkspaceResponse,
    summary="Update one account through versioned COA editing",
)
def update_coa_account(
    entity_id: UUID,
    account_id: UUID,
    payload: CoaAccountUpdateRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    coa_service: CoaServiceDependency,
) -> CoaWorkspaceResponse:
    """Update one account by materializing a new immutable manual revision."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )

    try:
        return coa_service.update_account(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            account_id=account_id,
            payload=payload,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except CoaServiceError as error:
        raise _build_coa_http_exception(error) from error


def _build_coa_http_exception(error: CoaServiceError) -> HTTPException:
    """Convert COA-domain failures into the API's structured HTTP shape."""

    return HTTPException(
        status_code=error.status_code,
        detail={
            "code": str(error.code),
            "message": error.message,
        },
    )


__all__ = ["router"]
