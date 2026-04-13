"""
Purpose: Expose report generation, status, and download API routes.
Scope: Trigger report generation, query report run status, list report runs,
and retrieve report run details scoped to entities and close runs.
Dependencies: FastAPI, local-auth session helpers, reporting contracts and services,
Celery task dispatch, and the shared DB dependency.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from apps.api.app.dependencies.db import DatabaseSessionDependency
from apps.api.app.dependencies.tasks import TaskDispatcherDependency
from apps.api.app.routes.auth import (
    _build_http_exception,
    _clear_session_cookie,
    _read_session_cookie,
    _resolve_ip_address,
    _set_session_cookie,
    get_auth_service,
)
from apps.api.app.routes.request_auth import AuthenticatedUserContext, RequestAuthDependency
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from services.auth.service import (
    AuthenticatedSessionResult,
    AuthErrorCode,
    AuthService,
    AuthServiceError,
)
from services.common.settings import AppSettings, get_settings
from services.contracts.report_models import (
    ReportRunDetail,
    ReportRunListResponse,
    ReportRunSummary,
    ReportTemplateDetail,
    ReportTemplateListResponse,
)
from services.db.models.reporting import ReportRunStatus
from services.db.repositories.entity_repo import EntityUserRecord
from services.db.repositories.report_repo import ReportRepository
from services.jobs.service import JobService, JobServiceError
from services.jobs.task_names import TaskName
from services.reporting.service import ReportService, ReportServiceError

router = APIRouter(prefix="/entities/{entity_id}/reports", tags=["reports"])

SettingsDependency = Annotated[AppSettings, Depends(get_settings)]
AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]


def get_report_service(db_session: DatabaseSessionDependency) -> ReportService:
    """Construct the canonical report service from request-scoped persistence."""

    return ReportService(repository=ReportRepository(db_session=db_session))


ReportServiceDependency = Annotated[ReportService, Depends(get_report_service)]
TemplateIdQuery = Annotated[UUID | None, Query(description="Optional template to use.")]
GenerateCommentaryQuery = Annotated[bool, Query(description="Generate commentary drafts.")]
UseLlmCommentaryQuery = Annotated[bool, Query(description="Use LLM-enhanced commentary.")]


# ---------------------------------------------------------------------------
# Report generation routes
# ---------------------------------------------------------------------------

@router.post(
    "/close-runs/{close_run_id}/generate",
    response_model=ReportRunSummary,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger report generation for a close run",
)
def trigger_report_generation(
    entity_id: UUID,
    close_run_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    report_service: ReportServiceDependency,
    db_session: DatabaseSessionDependency,
    task_dispatcher: TaskDispatcherDependency,
    auth_context: RequestAuthDependency,
    template_id: TemplateIdQuery = None,
    generate_commentary: GenerateCommentaryQuery = True,
    use_llm_commentary: UseLlmCommentaryQuery = False,
) -> ReportRunSummary:
    """Trigger asynchronous report generation for a close run.

    This endpoint creates a persistent report-run record, dispatches a Celery
    task to generate Excel and PDF report packs, and returns the report-run
    summary so callers can poll the detail endpoint for status.
    """

    session_result = auth_context

    # Verify close run access
    try:
        report_service.list_report_runs(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
    except ReportServiceError as error:
        raise _build_report_http_exception(error) from error

    # Resolve template: explicit ID, entity active, or global default
    repo = ReportRepository(db_session=db_session)
    resolved_template_id = _resolve_template_id(repo, entity_id, template_id)

    # Create the report-run record BEFORE dispatching the task.
    version_no = repo.next_version_no_for_close_run(
        close_run_id=close_run_id,
    )
    run_record = repo.create_report_run(
        close_run_id=close_run_id,
        template_id=resolved_template_id,
        version_no=version_no,
        status=ReportRunStatus.PENDING,
        generation_config={
            "generate_commentary": generate_commentary,
            "use_llm_commentary": use_llm_commentary,
        },
        generated_by_user_id=session_result.user.id,
    )
    repo.commit()

    # Dispatch Celery task through the canonical durable job service.
    job_service = JobService(db_session=db_session)
    try:
        job_service.dispatch_job(
            dispatcher=task_dispatcher,
            task_name=TaskName.REPORTING_GENERATE_CLOSE_RUN_PACK,
            payload={
                "close_run_id": str(close_run_id),
                "report_run_id": str(run_record.id),
                "actor_user_id": str(session_result.user.id),
                "generate_commentary_flag": generate_commentary,
                "use_llm_commentary": use_llm_commentary,
            },
            entity_id=entity_id,
            close_run_id=close_run_id,
            document_id=None,
            actor_user_id=session_result.user.id,
            trace_id=str(request.state.request_id),
        )
    except JobServiceError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={
                "code": str(error.code),
                "message": error.message,
            },
        ) from error

    now = datetime.now(tz=UTC)
    return ReportRunSummary(
        id=str(run_record.id),
        close_run_id=str(close_run_id),
        template_id=str(resolved_template_id),
        version_no=version_no,
        status=ReportRunStatus.PENDING.value,
        failure_reason=None,
        generated_by_user_id=str(session_result.user.id),
        completed_at=None,
        created_at=now,
        updated_at=now,
    )


def _resolve_template_id(
    repo: ReportRepository,
    entity_id: UUID,
    template_id: UUID | None,
) -> UUID:
    """Resolve which template to use: explicit, entity active, or global default."""

    if template_id is not None:
        return template_id

    active = repo.get_active_template_for_entity(entity_id=entity_id)
    if active is not None:
        return active.id

    global_template = repo.get_active_global_template()
    if global_template is not None:
        return global_template.id

    raise HTTPException(
        status_code=400,
        detail={
            "code": "no_template_available",
            "message": (
                "No report template is available for this entity. "
                "Upload or activate a template before generating reports."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Report run query routes (delegated to report service)
# ---------------------------------------------------------------------------

@router.get(
    "/close-runs/{close_run_id}/runs",
    response_model=ReportRunListResponse,
    summary="List report runs for one close run",
)
def list_report_runs_for_close_run(
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
def read_report_run_detail(
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
# Template routes (re-exported for convenience under the same prefix)
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
def read_report_template_detail(
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
