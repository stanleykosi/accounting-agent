"""
Purpose: Expose authenticated document upload and listing routes for close runs.
Scope: Multipart batch upload handling, browser-session validation, service construction,
and structured translation of document-intake domain errors into HTTP responses.
Dependencies: FastAPI, local auth helpers, task dispatch dependency, storage repository,
document contracts, service, and repository.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from apps.api.app.dependencies.db import DatabaseSessionDependency
from apps.api.app.dependencies.tasks import TaskDispatcherDependency
from apps.api.app.routes.auth import get_auth_service
from apps.api.app.routes.close_runs import (
    _require_authenticated_browser_session,
    _resolve_trace_id,
    _to_entity_user,
)
from apps.api.app.routes.request_auth import RequestAuthDependency
from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from services.auth.service import AuthService
from services.common.settings import AppSettings, get_settings
from services.contracts.document_models import BatchUploadDocumentsResponse, DocumentListResponse
from services.db.models.audit import AuditSourceSurface
from services.db.repositories.document_repo import DocumentRepository
from services.documents.upload_service import (
    DocumentUploadService,
    DocumentUploadServiceError,
    UploadFilePayload,
)
from services.storage.repository import StorageRepository

router = APIRouter(
    prefix="/entities/{entity_id}/close-runs/{close_run_id}/documents",
    tags=["documents"],
)

SettingsDependency = Annotated[AppSettings, Depends(get_settings)]
AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]


def get_document_upload_service(
    db_session: DatabaseSessionDependency,
    task_dispatcher: TaskDispatcherDependency,
) -> DocumentUploadService:
    """Construct the canonical document upload service from request-scoped dependencies."""

    return DocumentUploadService(
        repository=DocumentRepository(db_session=db_session),
        storage_repository=StorageRepository(),
        task_dispatcher=task_dispatcher,
    )


DocumentUploadServiceDependency = Annotated[
    DocumentUploadService,
    Depends(get_document_upload_service),
]


@router.get(
    "",
    response_model=DocumentListResponse,
    summary="List documents for one close run",
)
def list_documents(
    entity_id: UUID,
    close_run_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    document_upload_service: DocumentUploadServiceDependency,
    auth_context: RequestAuthDependency,
) -> DocumentListResponse:
    """Return source documents attached to an accessible close run."""

    session_result = auth_context
    try:
        return document_upload_service.list_documents(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
    except DocumentUploadServiceError as error:
        raise _build_document_http_exception(error) from error


@router.post(
    "/upload",
    response_model=BatchUploadDocumentsResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a batch of source documents",
)
async def upload_documents(
    entity_id: UUID,
    close_run_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    document_upload_service: DocumentUploadServiceDependency,
    files: Annotated[tuple[UploadFile, ...], File(description="PDF, Excel, or CSV files.")],
) -> BatchUploadDocumentsResponse:
    """Accept PDF, Excel, and CSV source files and enqueue deterministic parser work."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    try:
        upload_payloads: list[UploadFilePayload] = []
        for file in files:
            upload_payloads.append(
                UploadFilePayload(
                    filename=file.filename or "",
                    payload=await file.read(),
                    declared_content_type=file.content_type,
                )
            )
        return document_upload_service.upload_documents(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            close_run_id=close_run_id,
            files=tuple(upload_payloads),
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except DocumentUploadServiceError as error:
        raise _build_document_http_exception(error) from error
    finally:
        for file in files:
            await file.close()


def _build_document_http_exception(error: DocumentUploadServiceError) -> HTTPException:
    """Convert a document-domain error into the API's structured HTTP shape."""

    return HTTPException(
        status_code=error.status_code,
        detail={
            "code": str(error.code),
            "message": error.message,
        },
    )


__all__ = ["router"]
