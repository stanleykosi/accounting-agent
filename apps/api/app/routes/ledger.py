"""
Purpose: Expose authenticated imported-ledger baseline routes for entity workspaces.
Scope: Entity-level GL/TB workspace reads and upload APIs routed through the canonical
ledger-import service layer.
Dependencies: FastAPI auth/session helpers, ledger contracts/service, and the shared DB dependency.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from apps.api.app.dependencies.db import DatabaseSessionDependency
from apps.api.app.routes.auth import get_auth_service
from apps.api.app.routes.close_runs import (
    _require_authenticated_browser_session,
    _resolve_trace_id,
    _to_entity_user,
)
from apps.api.app.routes.request_auth import RequestAuthDependency
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from services.auth.service import AuthService
from services.common.settings import AppSettings, get_settings
from services.contracts.ledger_models import (
    GeneralLedgerExportSummary,
    GeneralLedgerImportUploadResponse,
    LedgerWorkspaceResponse,
    TrialBalanceImportUploadResponse,
)
from services.contracts.storage_models import StorageBucketKind
from services.db.models.audit import AuditSourceSurface
from services.ledger.export_service import (
    GeneralLedgerExportService,
    GeneralLedgerExportServiceError,
)
from services.ledger.service import (
    LedgerImportService,
    LedgerImportServiceError,
    LedgerRepository,
)
from services.storage.client import StorageClient
from services.storage.repository import StorageRepository

router = APIRouter(prefix="/entities/{entity_id}/ledger", tags=["ledger"])
close_run_router = APIRouter(
    prefix="/entities/{entity_id}/close-runs/{close_run_id}/ledger",
    tags=["ledger"],
)

SettingsDependency = Annotated[AppSettings, Depends(get_settings)]
AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]


def get_ledger_import_service(db_session: DatabaseSessionDependency) -> LedgerImportService:
    """Construct the canonical ledger-import service from request-scoped persistence."""

    return LedgerImportService(repository=LedgerRepository(db_session=db_session))


LedgerImportServiceDependency = Annotated[
    LedgerImportService,
    Depends(get_ledger_import_service),
]


def get_general_ledger_export_service(
    db_session: DatabaseSessionDependency,
) -> GeneralLedgerExportService:
    """Construct the canonical close-run GL export service from request-scoped dependencies."""

    return GeneralLedgerExportService(
        db_session=db_session,
        storage_repository=StorageRepository(),
    )


GeneralLedgerExportServiceDependency = Annotated[
    GeneralLedgerExportService,
    Depends(get_general_ledger_export_service),
]


@router.get(
    "",
    response_model=LedgerWorkspaceResponse,
    summary="Read the entity imported-ledger workspace",
)
def read_ledger_workspace(
    entity_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    ledger_service: LedgerImportServiceDependency,
) -> LedgerWorkspaceResponse:
    """Return imported GL/TB baselines and current close-run bindings for one entity."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return ledger_service.read_workspace(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
        )
    except LedgerImportServiceError as error:
        raise _build_ledger_http_exception(error) from error


@router.post(
    "/general-ledger/upload",
    response_model=GeneralLedgerImportUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a general-ledger baseline file",
)
async def upload_general_ledger(
    entity_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    ledger_service: LedgerImportServiceDependency,
    period_start: Annotated[date, Form(description="First day covered by the imported ledger.")],
    period_end: Annotated[date, Form(description="Last day covered by the imported ledger.")],
    file: Annotated[UploadFile, File(description="CSV or XLSX general-ledger file.")],
) -> GeneralLedgerImportUploadResponse:
    """Import one general-ledger baseline and auto-bind safe matching close runs."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return await _upload_general_ledger(
            entity_id=entity_id,
            session_result=session_result,
            request=request,
            file=file,
            period_start=period_start,
            period_end=period_end,
            ledger_service=ledger_service,
        )
    except LedgerImportServiceError as error:
        raise _build_ledger_http_exception(error) from error


@router.post(
    "/trial-balance/upload",
    response_model=TrialBalanceImportUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a trial-balance baseline file",
)
async def upload_trial_balance(
    entity_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    ledger_service: LedgerImportServiceDependency,
    period_start: Annotated[
        date,
        Form(description="First day covered by the imported trial balance."),
    ],
    period_end: Annotated[
        date,
        Form(description="Last day covered by the imported trial balance."),
    ],
    file: Annotated[UploadFile, File(description="CSV or XLSX trial-balance file.")],
) -> TrialBalanceImportUploadResponse:
    """Import one trial-balance baseline and auto-bind safe matching close runs."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        return await _upload_trial_balance(
            entity_id=entity_id,
            session_result=session_result,
            request=request,
            file=file,
            period_start=period_start,
            period_end=period_end,
            ledger_service=ledger_service,
        )
    except LedgerImportServiceError as error:
        raise _build_ledger_http_exception(error) from error


async def _upload_general_ledger(
    *,
    entity_id: UUID,
    session_result,
    request: Request,
    file: UploadFile,
    period_start: date,
    period_end: date,
    ledger_service: LedgerImportService,
) -> GeneralLedgerImportUploadResponse:
    """Execute the GL upload workflow and close the inbound file handle."""

    try:
        return ledger_service.upload_general_ledger(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            period_start=period_start,
            period_end=period_end,
            filename=file.filename or "general_ledger_upload",
            payload=await file.read(),
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    finally:
        await file.close()


@close_run_router.post(
    "/general-ledger-export",
    response_model=GeneralLedgerExportSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Generate the current close-run general-ledger export",
)
def generate_general_ledger_export(
    entity_id: UUID,
    close_run_id: UUID,
    general_ledger_export_service: GeneralLedgerExportServiceDependency,
    auth_context: RequestAuthDependency,
) -> GeneralLedgerExportSummary:
    """Generate or reuse the current-version effective-ledger export for one close run."""

    try:
        return general_ledger_export_service.generate_export(
            actor_user=_to_entity_user(auth_context),
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
    except GeneralLedgerExportServiceError as error:
        raise _build_general_ledger_export_http_exception(error) from error


@close_run_router.get(
    "/general-ledger-export",
    response_model=GeneralLedgerExportSummary,
    summary="Read the latest current-version general-ledger export",
)
def read_latest_general_ledger_export(
    entity_id: UUID,
    close_run_id: UUID,
    general_ledger_export_service: GeneralLedgerExportServiceDependency,
    auth_context: RequestAuthDependency,
) -> GeneralLedgerExportSummary:
    """Return the latest current-version effective-ledger export for one close run."""

    try:
        export_summary = general_ledger_export_service.get_latest_export(
            actor_user=_to_entity_user(auth_context),
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
    except GeneralLedgerExportServiceError as error:
        raise _build_general_ledger_export_http_exception(error) from error

    if export_summary is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "general_ledger_export_not_found",
                "message": (
                    "No general-ledger export has been generated for this close run version."
                ),
            },
        )
    return export_summary


@close_run_router.get(
    "/general-ledger-export/download",
    summary="Download the latest current-version general-ledger export",
)
def download_latest_general_ledger_export(
    entity_id: UUID,
    close_run_id: UUID,
    settings: SettingsDependency,
    general_ledger_export_service: GeneralLedgerExportServiceDependency,
    auth_context: RequestAuthDependency,
) -> Response:
    """Stream the latest current-version effective-ledger CSV through the API surface."""

    try:
        export_summary = general_ledger_export_service.get_latest_export(
            actor_user=_to_entity_user(auth_context),
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
    except GeneralLedgerExportServiceError as error:
        raise _build_general_ledger_export_http_exception(error) from error

    if export_summary is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "general_ledger_export_not_found",
                "message": (
                    "No general-ledger export has been generated for this close run version."
                ),
            },
        )

    payload = StorageClient.from_settings(settings).download_bytes(
        bucket_kind=StorageBucketKind.ARTIFACTS,
        object_key=export_summary.storage_key,
    )
    return Response(
        content=payload,
        media_type=export_summary.content_type,
        headers={"Content-Disposition": f'attachment; filename="{export_summary.filename}"'},
    )


async def _upload_trial_balance(
    *,
    entity_id: UUID,
    session_result,
    request: Request,
    file: UploadFile,
    period_start: date,
    period_end: date,
    ledger_service: LedgerImportService,
) -> TrialBalanceImportUploadResponse:
    """Execute the TB upload workflow and close the inbound file handle."""

    try:
        return ledger_service.upload_trial_balance(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            period_start=period_start,
            period_end=period_end,
            filename=file.filename or "trial_balance_upload",
            payload=await file.read(),
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    finally:
        await file.close()


def _build_ledger_http_exception(error: LedgerImportServiceError) -> HTTPException:
    """Translate ledger-domain failures into strict FastAPI HTTP responses."""

    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code.value, "message": error.message},
    )


def _build_general_ledger_export_http_exception(
    error: GeneralLedgerExportServiceError,
) -> HTTPException:
    """Translate GL export-domain failures into strict FastAPI HTTP responses."""

    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code.value, "message": error.message},
    )


__all__ = ["close_run_router", "router"]
