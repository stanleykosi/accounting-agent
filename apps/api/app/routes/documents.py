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
from apps.api.app.routes.workflow_phase import require_active_close_run_phase
from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from services.auth.service import AuthService
from services.common.enums import WorkflowPhase
from services.common.settings import AppSettings, get_settings
from services.contracts.document_models import (
    BatchUploadDocumentsResponse,
    DocumentListResponse,
    DocumentReviewActionResponse,
    DocumentReviewDecisionRequest,
    FieldCorrectionRequest,
    FieldCorrectionResponse,
)
from services.db.models.audit import AuditSourceSurface
from services.db.repositories.document_repo import DocumentRepository
from services.documents.review_service import (
    DocumentReviewService,
    DocumentReviewServiceError,
)
from services.documents.upload_service import (
    DocumentUploadService,
    DocumentUploadServiceError,
    UploadFilePayload,
)
from services.jobs.service import JobService
from services.storage.repository import StorageRepository

router = APIRouter(
    prefix="/entities/{entity_id}/close-runs/{close_run_id}/documents",
    tags=["documents"],
)

SettingsDependency = Annotated[AppSettings, Depends(get_settings)]
AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]
DbSessionDep = Annotated[DatabaseSessionDependency, Depends()]


def get_document_upload_service(
    db_session: DatabaseSessionDependency,
    task_dispatcher: TaskDispatcherDependency,
) -> DocumentUploadService:
    """Construct the canonical document upload service from request-scoped dependencies."""

    return DocumentUploadService(
        repository=DocumentRepository(db_session=db_session),
        storage_repository=StorageRepository(),
        job_service=JobService(db_session=db_session),
        task_dispatcher=task_dispatcher,
    )


DocumentUploadServiceDependency = Annotated[
    DocumentUploadService,
    Depends(get_document_upload_service),
]


def get_document_review_service(
    db_session: DatabaseSessionDependency,
) -> DocumentReviewService:
    """Construct the canonical document review service from request-scoped dependencies."""

    return DocumentReviewService(
        db_session=db_session,
        repository=DocumentRepository(db_session=db_session),
    )


DocumentReviewServiceDependency = Annotated[
    DocumentReviewService,
    Depends(get_document_review_service),
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
    "/{document_id}/review",
    response_model=DocumentReviewActionResponse,
    summary="Persist one review decision for a close-run document",
)
def review_document(
    entity_id: UUID,
    close_run_id: UUID,
    document_id: UUID,
    payload: DocumentReviewDecisionRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    document_review_service: DocumentReviewServiceDependency,
    db_session: DbSessionDep,
) -> DocumentReviewActionResponse:
    """Persist a reviewer decision for one document in the collection/processing queue."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    require_active_close_run_phase(
        actor_user=_to_entity_user(session_result),
        entity_id=entity_id,
        close_run_id=close_run_id,
        required_phase=WorkflowPhase.COLLECTION,
        action_label="Document review",
        db_session=db_session,
    )
    try:
        return document_review_service.review_document(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            close_run_id=close_run_id,
            document_id=document_id,
            decision=payload.decision,
            reason=payload.reason,
            verified_complete=payload.verified_complete,
            verified_authorized=payload.verified_authorized,
            verified_period=payload.verified_period,
            verified_transaction_match=payload.verified_transaction_match,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except DocumentReviewServiceError as error:
        raise _build_document_review_http_exception(error) from error


@router.put(
    "/fields/{field_id}",
    response_model=FieldCorrectionResponse,
    summary="Persist one human correction for an extracted field",
)
def correct_extracted_field(
    entity_id: UUID,
    close_run_id: UUID,
    field_id: UUID,
    payload: FieldCorrectionRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    document_review_service: DocumentReviewServiceDependency,
    db_session: DbSessionDep,
) -> FieldCorrectionResponse:
    """Persist a human correction to one extracted field and refresh the document state."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    require_active_close_run_phase(
        actor_user=_to_entity_user(session_result),
        entity_id=entity_id,
        close_run_id=close_run_id,
        required_phase=WorkflowPhase.COLLECTION,
        action_label="Extracted-field correction",
        db_session=db_session,
    )
    try:
        return document_review_service.correct_extracted_field(
            actor_user=_to_entity_user(session_result),
            entity_id=entity_id,
            close_run_id=close_run_id,
            field_id=field_id,
            corrected_value=payload.corrected_value,
            corrected_type=payload.corrected_type,
            reason=payload.reason,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=_resolve_trace_id(request),
        )
    except DocumentReviewServiceError as error:
        raise _build_document_review_http_exception(error) from error


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
    db_session: DbSessionDep,
    files: Annotated[tuple[UploadFile, ...], File(description="PDF, Excel, or CSV files.")],
) -> BatchUploadDocumentsResponse:
    """Accept PDF, Excel, and CSV source files and enqueue deterministic parser work."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    require_active_close_run_phase(
        actor_user=_to_entity_user(session_result),
        entity_id=entity_id,
        close_run_id=close_run_id,
        required_phase=WorkflowPhase.COLLECTION,
        action_label="Source-document upload",
        db_session=db_session,
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


def _build_document_review_http_exception(error: DocumentReviewServiceError) -> HTTPException:
    """Convert a document-review-domain error into the API's structured HTTP shape."""

    return HTTPException(
        status_code=error.status_code,
        detail={
            "code": str(error.code),
            "message": error.message,
        },
    )


__all__ = ["router"]
