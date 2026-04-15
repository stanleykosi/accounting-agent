"""
Purpose: Expose export and evidence-pack API endpoints through the canonical
export workflow service.
Scope: Export triggering, listing, detail reads, evidence-pack assembly,
download, and idempotency-key preview.
Dependencies: FastAPI, request auth dependencies, export contracts, storage,
and the shared export service.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from apps.api.app.dependencies.db import DatabaseSessionDependency
from apps.api.app.routes.auth import get_auth_service
from apps.api.app.routes.request_auth import AuthenticatedUserContext, RequestAuthDependency
from apps.api.app.routes.workflow_phase import require_active_close_run_phase
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from services.auth.service import AuthService
from services.common.enums import ArtifactType, WorkflowPhase
from services.common.settings import AppSettings, get_settings
from services.contracts.export_models import (
    CreateExportRequest,
    DistributeExportRequest,
    EvidencePackBundle,
    ExportDetail,
    ExportListResponse,
    IdempotencyKeyResponse,
)
from services.contracts.storage_models import StorageBucketKind
from services.db.models.audit import AuditSourceSurface
from services.db.repositories.entity_repo import EntityUserRecord
from services.db.repositories.report_repo import ReportRepository
from services.exports.service import ExportService, ExportServiceError
from services.idempotency.service import build_idempotency_key
from services.storage.client import StorageClient

router = APIRouter(
    prefix="/entities/{entity_id}/close-runs/{close_run_id}/exports",
    tags=["exports"],
)

SettingsDependency = Annotated[AppSettings, Depends(get_settings)]
AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]


def get_export_service(
    db_session: DatabaseSessionDependency,
) -> ExportService:
    """Construct the canonical export workflow service from request-scoped dependencies."""

    return ExportService(
        db_session=db_session,
        report_repository=ReportRepository(db_session=db_session),
    )


ExportServiceDependency = Annotated[ExportService, Depends(get_export_service)]


@router.post(
    "",
    response_model=ExportDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Trigger a new export for a close run",
)
def trigger_export(
    entity_id: UUID,
    close_run_id: UUID,
    db_session: DatabaseSessionDependency,
    export_service: ExportServiceDependency,
    body: CreateExportRequest,
    auth_context: RequestAuthDependency,
) -> ExportDetail:
    """Trigger an export for one close run with idempotency protection."""

    require_active_close_run_phase(
        actor_user=_to_entity_user(auth_context),
        entity_id=entity_id,
        close_run_id=close_run_id,
        required_phase=WorkflowPhase.REVIEW_SIGNOFF,
        action_label="Export generation",
        db_session=db_session,
    )
    try:
        return export_service.trigger_export(
            actor_user=_to_entity_user(auth_context),
            entity_id=entity_id,
            close_run_id=close_run_id,
            request=body,
        )
    except ExportServiceError as error:
        _raise_export_http_error(error)


@router.get(
    "",
    response_model=ExportListResponse,
    summary="List exports for one close run",
)
def list_exports(
    entity_id: UUID,
    close_run_id: UUID,
    export_service: ExportServiceDependency,
    auth_context: RequestAuthDependency,
) -> ExportListResponse:
    """Return all export records for one close run in newest-first order."""

    try:
        return export_service.list_exports(
            actor_user=_to_entity_user(auth_context),
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
    except ExportServiceError as error:
        _raise_export_http_error(error)


@router.get(
    "/{export_id}",
    response_model=ExportDetail,
    summary="Read one export detail",
)
def read_export_detail(
    entity_id: UUID,
    close_run_id: UUID,
    export_id: UUID,
    export_service: ExportServiceDependency,
    auth_context: RequestAuthDependency,
) -> ExportDetail:
    """Return one export record with full manifest and evidence-pack details."""

    try:
        return export_service.read_export_detail(
            actor_user=_to_entity_user(auth_context),
            entity_id=entity_id,
            close_run_id=close_run_id,
            export_id=export_id,
        )
    except ExportServiceError as error:
        _raise_export_http_error(error)


@router.post(
    "/{export_id}/distribute",
    response_model=ExportDetail,
    summary="Record management distribution for one export",
)
def distribute_export(
    entity_id: UUID,
    close_run_id: UUID,
    export_id: UUID,
    db_session: DatabaseSessionDependency,
    export_service: ExportServiceDependency,
    body: DistributeExportRequest,
    auth_context: RequestAuthDependency,
) -> ExportDetail:
    """Record one stakeholder release event for a completed export package."""

    require_active_close_run_phase(
        actor_user=_to_entity_user(auth_context),
        entity_id=entity_id,
        close_run_id=close_run_id,
        required_phase=WorkflowPhase.REVIEW_SIGNOFF,
        action_label="Management distribution",
        db_session=db_session,
    )
    try:
        return export_service.distribute_export(
            actor_user=_to_entity_user(auth_context),
            entity_id=entity_id,
            close_run_id=close_run_id,
            export_id=export_id,
            request=body,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=None,
        )
    except ExportServiceError as error:
        _raise_export_http_error(error)


@router.post(
    "/evidence-pack",
    response_model=EvidencePackBundle,
    status_code=status.HTTP_201_CREATED,
    summary="Assemble and release an evidence pack for a close run",
)
def assemble_evidence_pack(
    entity_id: UUID,
    close_run_id: UUID,
    db_session: DatabaseSessionDependency,
    export_service: ExportServiceDependency,
    auth_context: RequestAuthDependency,
) -> EvidencePackBundle:
    """Assemble a downloadable evidence-pack bundle for one close run."""

    require_active_close_run_phase(
        actor_user=_to_entity_user(auth_context),
        entity_id=entity_id,
        close_run_id=close_run_id,
        required_phase=WorkflowPhase.REVIEW_SIGNOFF,
        action_label="Evidence-pack assembly",
        db_session=db_session,
    )
    try:
        return export_service.assemble_evidence_pack(
            actor_user=_to_entity_user(auth_context),
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
    except ExportServiceError as error:
        _raise_export_http_error(error)


@router.get(
    "/evidence-pack",
    response_model=EvidencePackBundle,
    summary="Read the latest evidence pack for a close run",
)
def read_latest_evidence_pack(
    entity_id: UUID,
    close_run_id: UUID,
    export_service: ExportServiceDependency,
    auth_context: RequestAuthDependency,
) -> EvidencePackBundle:
    """Return the latest released evidence-pack bundle metadata for one close run."""

    try:
        evidence_pack = export_service.get_latest_evidence_pack(
            actor_user=_to_entity_user(auth_context),
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
    except ExportServiceError as error:
        _raise_export_http_error(error)

    if evidence_pack is None or evidence_pack.storage_key is None:
        _raise_evidence_pack_not_found()
    return evidence_pack


@router.get(
    "/evidence-pack/download",
    summary="Download the latest evidence pack for a close run",
)
def download_evidence_pack(
    entity_id: UUID,
    close_run_id: UUID,
    settings: SettingsDependency,
    export_service: ExportServiceDependency,
    auth_context: RequestAuthDependency,
) -> Response:
    """Stream the latest released evidence-pack ZIP through the authenticated API surface."""

    try:
        evidence_pack = export_service.get_latest_evidence_pack(
            actor_user=_to_entity_user(auth_context),
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
    except ExportServiceError as error:
        _raise_export_http_error(error)

    if evidence_pack is None or evidence_pack.storage_key is None:
        _raise_evidence_pack_not_found()

    payload = StorageClient.from_settings(settings).download_bytes(
        bucket_kind=StorageBucketKind.ARTIFACTS,
        object_key=evidence_pack.storage_key,
    )
    filename = f"evidence-pack-v{evidence_pack.version_no}.zip"
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/evidence-pack/idempotency-key",
    response_model=IdempotencyKeyResponse,
    summary="Preview the idempotency key for an evidence-pack action",
)
def preview_evidence_pack_idempotency_key(
    entity_id: UUID,
    close_run_id: UUID,
    export_service: ExportServiceDependency,
    auth_context: RequestAuthDependency,
    version: Annotated[int, Query(ge=1, description="Close-run version number.")] = 1,
) -> IdempotencyKeyResponse:
    """Return the deterministic idempotency key that would be used for an evidence-pack action."""

    try:
        export_service.list_exports(
            actor_user=_to_entity_user(auth_context),
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
    except ExportServiceError as error:
        _raise_export_http_error(error)

    return IdempotencyKeyResponse(
        idempotency_key=build_idempotency_key(
            close_run_id=close_run_id,
            artifact_type=ArtifactType.EVIDENCE_PACK.value,
            action_qualifier="evidence_pack",
            version_override=version,
        ),
        close_run_id=str(close_run_id),
        artifact_type=ArtifactType.EVIDENCE_PACK.value,
    )


def _to_entity_user(session_result: AuthenticatedUserContext) -> EntityUserRecord:
    """Project the authenticated session user into the entity actor record."""

    return EntityUserRecord(
        id=session_result.user.id,
        email=session_result.user.email,
        full_name=session_result.user.full_name,
    )


def _raise_export_http_error(error: ExportServiceError) -> None:
    """Translate one export service error into the API's structured HTTP response."""

    raise HTTPException(
        status_code=error.status_code,
        detail={"code": str(error.code), "message": error.message},
    ) from error


def _raise_evidence_pack_not_found() -> None:
    """Raise the canonical not-found response for missing evidence-pack metadata."""

    raise HTTPException(
        status_code=404,
        detail={
            "code": "evidence_pack_not_found",
            "message": "No evidence pack has been assembled for this close run.",
        },
    )


__all__ = ["router"]
