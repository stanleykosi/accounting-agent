"""
Purpose: Expose report-template management and commentary API routes for entity workspaces.
Scope: Template creation, listing, activation, guardrail validation, commentary updates,
and approval workflows scoped to entities and close runs.
Dependencies: FastAPI, local-auth session helpers, reporting contracts and services,
and the shared DB dependency.
"""

from __future__ import annotations

from typing import Annotated, cast
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
from services.common.types import JsonObject
from services.contracts.report_models import (
    ActivateReportTemplateRequest,
    ApproveCommentaryRequest,
    CommentarySummary,
    CreateReportTemplateRequest,
    GuardrailValidationResponse,
    ReportRunDetail,
    ReportRunListResponse,
    ReportTemplateDetail,
    ReportTemplateListResponse,
    UpdateCommentaryRequest,
)
from services.db.models.audit import AuditSourceSurface
from services.db.repositories.entity_repo import EntityUserRecord
from services.db.repositories.report_repo import ReportRepository
from services.reporting.service import ReportService, ReportServiceError

router = APIRouter(prefix="/entities/{entity_id}/reports", tags=["report_templates"])

SettingsDependency = Annotated[AppSettings, Depends(get_settings)]
AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]


def get_report_service(db_session: DatabaseSessionDependency) -> ReportService:
    """Construct the canonical report service from request-scoped persistence."""

    return ReportService(repository=ReportRepository(db_session=db_session))


ReportServiceDependency = Annotated[ReportService, Depends(get_report_service)]


# ---------------------------------------------------------------------------
# Template routes
# ---------------------------------------------------------------------------

@router.get(
    "/templates",
    response_model=ReportTemplateListResponse,
    summary="List report templates for one entity",
)
def list_report_templates(
    entity_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    report_service: ReportServiceDependency,
) -> ReportTemplateListResponse:
    """Return all report template versions for the entity workspace."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return report_service.list_templates_for_entity(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
        )
    except ReportServiceError as error:
        raise _build_report_http_exception(error) from error


@router.get(
    "/templates/{template_id}",
    response_model=ReportTemplateDetail,
    summary="Read one report template",
)
def read_report_template(
    entity_id: UUID,
    template_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    report_service: ReportServiceDependency,
) -> ReportTemplateDetail:
    """Return one report template with full section definitions and guardrail config."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return report_service.get_template(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            template_id=template_id,
        )
    except ReportServiceError as error:
        raise _build_report_http_exception(error) from error


@router.post(
    "/templates",
    response_model=ReportTemplateDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create one report template",
)
def create_report_template(
    entity_id: UUID,
    payload: CreateReportTemplateRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    report_service: ReportServiceDependency,
) -> ReportTemplateDetail:
    """Create a new entity-scoped report template with guardrail validation."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return report_service.create_template(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            name=payload.name,
            description=payload.description,
            sections=payload.sections,
            guardrail_config=cast(JsonObject, payload.guardrail_config),
            activate_immediately=payload.activate_immediately,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except ReportServiceError as error:
        raise _build_report_http_exception(error) from error


@router.post(
    "/templates/{template_id}/activate",
    response_model=ReportTemplateDetail,
    summary="Activate one report template",
)
def activate_report_template(
    entity_id: UUID,
    template_id: UUID,
    payload: ActivateReportTemplateRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    report_service: ReportServiceDependency,
) -> ReportTemplateDetail:
    """Activate a report template version for the entity workspace."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return report_service.activate_template(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            template_id=template_id,
            reason=payload.reason,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except ReportServiceError as error:
        raise _build_report_http_exception(error) from error


@router.get(
    "/templates/{template_id}/validate",
    response_model=GuardrailValidationResponse,
    summary="Validate template guardrails",
)
def validate_template_guardrails_route(
    entity_id: UUID,
    template_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    report_service: ReportServiceDependency,
) -> GuardrailValidationResponse:
    """Run guardrail validation against one template and return the result."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return report_service.validate_template(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            template_id=template_id,
        )
    except ReportServiceError as error:
        raise _build_report_http_exception(error) from error


# ---------------------------------------------------------------------------
# Report run routes
# ---------------------------------------------------------------------------

@router.get(
    "/close-runs/{close_run_id}/runs",
    response_model=ReportRunListResponse,
    summary="List report runs for one close run",
)
def list_report_runs(
    entity_id: UUID,
    close_run_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    report_service: ReportServiceDependency,
    auth_context: RequestAuthDependency,
) -> ReportRunListResponse:
    """Return all report generation runs for one close run."""

    session_result = auth_context
    try:
        return report_service.list_report_runs(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
    except ReportServiceError as error:
        raise _build_report_http_exception(error) from error


@router.get(
    "/close-runs/{close_run_id}/runs/{report_run_id}",
    response_model=ReportRunDetail,
    summary="Read one report run",
)
def read_report_run(
    entity_id: UUID,
    close_run_id: UUID,
    report_run_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    report_service: ReportServiceDependency,
) -> ReportRunDetail:
    """Return one report run with commentary and artifact references."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return report_service.get_report_run(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            close_run_id=close_run_id,
            report_run_id=report_run_id,
        )
    except ReportServiceError as error:
        raise _build_report_http_exception(error) from error


# ---------------------------------------------------------------------------
# Commentary routes
# ---------------------------------------------------------------------------

@router.put(
    "/close-runs/{close_run_id}/runs/{report_run_id}/commentary/{section_key}",
    response_model=CommentarySummary,
    summary="Update commentary for one report section",
)
def update_commentary(
    entity_id: UUID,
    close_run_id: UUID,
    report_run_id: UUID,
    section_key: str,
    payload: UpdateCommentaryRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    report_service: ReportServiceDependency,
) -> CommentarySummary:
    """Update or create draft commentary text for one report section."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return report_service.update_commentary(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            close_run_id=close_run_id,
            report_run_id=report_run_id,
            section_key=section_key,
            body=payload.body,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except ReportServiceError as error:
        raise _build_report_http_exception(error) from error


@router.post(
    "/close-runs/{close_run_id}/runs/{report_run_id}/commentary/{section_key}/approve",
    response_model=CommentarySummary,
    summary="Approve commentary for one report section",
)
def approve_commentary(
    entity_id: UUID,
    close_run_id: UUID,
    report_run_id: UUID,
    section_key: str,
    payload: ApproveCommentaryRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    report_service: ReportServiceDependency,
) -> CommentarySummary:
    """Approve commentary for one report section, optionally with a final text edit."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return report_service.approve_commentary(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            close_run_id=close_run_id,
            report_run_id=report_run_id,
            section_key=section_key,
            body=payload.body,
            reason=payload.reason,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except ReportServiceError as error:
        raise _build_report_http_exception(error) from error


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

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
    """Project the authenticated session user into the entity actor record."""

    return EntityUserRecord(
        id=session_result.user.id,
        email=session_result.user.email,
        full_name=session_result.user.full_name,
    )


def _resolve_trace_id(request: Request) -> str | None:
    """Return the request ID bound by middleware so timeline events can link to logs."""

    request_id = getattr(request.state, "request_id", None)
    return str(request_id) if request_id is not None else None


def _build_report_http_exception(error: ReportServiceError) -> HTTPException:
    """Convert a report-domain error into the API's structured HTTP shape."""

    return HTTPException(
        status_code=error.status_code,
        detail={
            "code": str(error.code),
            "message": error.message,
        },
    )


__all__ = ["router"]
