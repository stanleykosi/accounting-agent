"""
Purpose: Expose authenticated entity workspace routes for creation,
listing, updates, and membership management.
Scope: Cookie-backed workspace APIs that translate entity service rules
into strict HTTP contracts and responses.
Dependencies: FastAPI, local-auth session validation helpers,
entity contracts and services, and the shared DB dependency.
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
from services.common.settings import AppSettings, get_settings
from services.contracts.entity_models import (
    CreateEntityMembershipRequest,
    CreateEntityRequest,
    EntityDeleteResponse,
    EntityListResponse,
    EntityWorkspace,
    UpdateEntityMembershipRequest,
    UpdateEntityRequest,
)
from services.db.models.audit import AuditSourceSurface
from services.db.repositories.entity_repo import EntityRepository, EntityUserRecord
from services.entity.delete_service import EntityDeleteService, EntityDeleteServiceError
from services.entity.service import EntityService, EntityServiceError
from services.jobs.service import JobService
from services.storage.repository import StorageRepository

router = APIRouter(prefix="/entities", tags=["entities"])

SettingsDependency = Annotated[AppSettings, Depends(get_settings)]
AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]


def get_entity_service(db_session: DatabaseSessionDependency) -> EntityService:
    """Construct the canonical entity service from request-scoped persistence."""

    return EntityService(repository=EntityRepository(db_session=db_session))


EntityServiceDependency = Annotated[EntityService, Depends(get_entity_service)]


def get_entity_delete_service(
    db_session: DatabaseSessionDependency,
) -> EntityDeleteService:
    """Construct the canonical destructive workspace-delete service."""

    return EntityDeleteService(
        repository=EntityRepository(db_session=db_session),
        storage_repository=StorageRepository(),
        job_service=JobService(db_session=db_session),
    )


EntityDeleteServiceDependency = Annotated[
    EntityDeleteService,
    Depends(get_entity_delete_service),
]


@router.get(
    "",
    response_model=EntityListResponse,
    summary="List the current user's entity workspaces",
)
def list_entities(
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    entity_service: EntityServiceDependency,
    auth_context: RequestAuthDependency,
) -> EntityListResponse:
    """Return the authenticated caller's accessible workspaces."""

    session_result = auth_context
    return entity_service.list_entities_for_user(user_id=session_result.user.id)


@router.post(
    "",
    response_model=EntityWorkspace,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new entity workspace",
)
def create_entity(
    payload: CreateEntityRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    entity_service: EntityServiceDependency,
) -> EntityWorkspace:
    """Create a workspace, seed the owner membership, and return the hydrated workspace detail."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return entity_service.create_entity(
            actor_user=_to_entity_user(session_result),
            name=payload.name,
            legal_name=payload.legal_name,
            base_currency=payload.base_currency,
            country_code=payload.country_code,
            timezone=payload.timezone,
            accounting_standard=payload.accounting_standard,
            autonomy_mode=payload.autonomy_mode,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except EntityServiceError as error:
        raise _build_entity_http_exception(error) from error


@router.get(
    "/{entity_id}",
    response_model=EntityWorkspace,
    summary="Read one entity workspace",
)
def read_entity_workspace(
    entity_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    entity_service: EntityServiceDependency,
) -> EntityWorkspace:
    """Return one accessible workspace detail including memberships and activity history."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return entity_service.get_entity_workspace(
            user_id=session_result.user.id,
            entity_id=entity_id,
        )
    except EntityServiceError as error:
        raise _build_entity_http_exception(error) from error


@router.patch(
    "/{entity_id}",
    response_model=EntityWorkspace,
    summary="Update one entity workspace",
)
def update_entity(
    entity_id: UUID,
    payload: UpdateEntityRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    entity_service: EntityServiceDependency,
) -> EntityWorkspace:
    """Update one accessible workspace and return the refreshed detail view."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return entity_service.update_entity(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            fields_to_update=frozenset(payload.model_fields_set),
            name=payload.name,
            legal_name=payload.legal_name,
            base_currency=payload.base_currency,
            country_code=payload.country_code,
            timezone=payload.timezone,
            accounting_standard=payload.accounting_standard,
            autonomy_mode=payload.autonomy_mode,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except EntityServiceError as error:
        raise _build_entity_http_exception(error) from error


@router.delete(
    "/{entity_id}",
    response_model=EntityDeleteResponse,
    summary="Delete one entity workspace",
)
def delete_entity(
    entity_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    entity_delete_service: EntityDeleteServiceDependency,
) -> EntityDeleteResponse:
    """Delete one accessible entity workspace when the caller is an owner."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return entity_delete_service.delete_entity(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
        )
    except EntityDeleteServiceError as error:
        raise _build_entity_delete_http_exception(error) from error


@router.post(
    "/{entity_id}/memberships",
    response_model=EntityWorkspace,
    status_code=status.HTTP_201_CREATED,
    summary="Add a membership to one entity workspace",
)
def create_entity_membership(
    entity_id: UUID,
    payload: CreateEntityMembershipRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    entity_service: EntityServiceDependency,
) -> EntityWorkspace:
    """Add an existing local operator to a workspace and return the refreshed detail view."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return entity_service.add_membership(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            user_email=payload.user_email,
            role=payload.role,
            is_default_actor=payload.is_default_actor,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except EntityServiceError as error:
        raise _build_entity_http_exception(error) from error


@router.patch(
    "/{entity_id}/memberships/{membership_id}",
    response_model=EntityWorkspace,
    summary="Update one entity workspace membership",
)
def update_entity_membership(
    entity_id: UUID,
    membership_id: UUID,
    payload: UpdateEntityMembershipRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    entity_service: EntityServiceDependency,
) -> EntityWorkspace:
    """Update one workspace membership and return the refreshed workspace detail."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return entity_service.update_membership(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            membership_id=membership_id,
            role=payload.role,
            is_default_actor=payload.is_default_actor,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except EntityServiceError as error:
        raise _build_entity_http_exception(error) from error


def _require_authenticated_browser_session(
    *,
    request: Request,
    response: Response,
    settings: AppSettings,
    auth_service: AuthService,
) -> AuthenticatedSessionResult:
    """Validate the caller's session cookie and keep rotated cookies in sync with the browser."""

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
    """Project the authenticated session user into the simpler entity-user record."""

    return EntityUserRecord(
        id=session_result.user.id,
        email=session_result.user.email,
        full_name=session_result.user.full_name,
    )


def _resolve_trace_id(request: Request) -> str | None:
    """Return the request ID bound by middleware so timeline events can link back to logs."""

    request_id = getattr(request.state, "request_id", None)
    return str(request_id) if request_id is not None else None


def _build_entity_http_exception(error: EntityServiceError) -> HTTPException:
    """Convert an entity-domain error into the structured HTTP shape used by the API."""

    return HTTPException(
        status_code=error.status_code,
        detail={
            "code": str(error.code),
            "message": error.message,
        },
    )


def _build_entity_delete_http_exception(error: EntityDeleteServiceError) -> HTTPException:
    """Convert an entity-delete-domain error into the structured HTTP shape used by the API."""

    return HTTPException(
        status_code=error.status_code,
        detail={
            "code": str(error.code),
            "message": error.message,
        },
    )


__all__ = ["router"]
