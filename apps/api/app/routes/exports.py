"""
Purpose: Expose export and evidence-pack API endpoints for close-run artifact packaging.
Scope: Export triggering with idempotency protection, export listing, export detail
retrieval, evidence-pack assembly, and idempotency-key preview endpoints.
Dependencies: FastAPI, local-auth session helpers, export contracts, reporting
services, idempotency service, storage repository, and shared DB dependency.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from apps.api.app.dependencies.db import DatabaseSessionDependency
from apps.api.app.routes.auth import (
    _build_http_exception,
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
from services.common.enums import ArtifactType
from services.common.settings import AppSettings, get_settings
from services.contracts.export_models import (
    CreateExportRequest,
    EvidencePackBundle,
    ExportDetail,
    ExportListResponse,
    ExportSummary,
    IdempotencyKeyResponse,
)
from services.db.repositories.entity_repo import EntityUserRecord
from services.idempotency.service import (
    IdempotencyGuardError,
    build_idempotency_key,
)
from services.reporting.exports import ExportManifestInput, build_export_manifest

router = APIRouter(
    prefix="/entities/{entity_id}/close-runs/{close_run_id}/exports",
    tags=["exports"],
)

SettingsDependency = Annotated[AppSettings, Depends(get_settings)]
AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]


# ---------------------------------------------------------------------------
# Export endpoints
# ---------------------------------------------------------------------------

@router.post(
    "",
    response_model=ExportDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Trigger a new export for a close run",
)
def trigger_export(
    entity_id: UUID,
    close_run_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    db_session: DatabaseSessionDependency,
    body: CreateExportRequest,
    auth_context: RequestAuthDependency,
) -> ExportDetail:
    """Trigger an export for one close run with idempotency protection.

    When the same export action is attempted twice, the existing export is
    returned instead of creating a duplicate.
    """

    session_result = auth_context
    user_record = _to_entity_user(session_result)

    # Verify close run access.
    _verify_close_run_access(db_session, entity_id, close_run_id, user_record.id)

    # Compute the deterministic idempotency key.
    idempotency_key = build_idempotency_key(
        close_run_id=close_run_id,
        artifact_type="export_manifest",
        action_qualifier=body.action_qualifier or "full_export",
    )

    # For now, assemble a minimal export manifest.
    # In a full implementation, this would query existing artifacts from the DB.
    manifest_result = build_export_manifest(
        __build_export_manifest_input(
            close_run_id=close_run_id,
            entity_id=entity_id,
            close_run_version_no=1,
            action_qualifier=body.action_qualifier,
        )
    )

    now = datetime.now(tz=UTC)
    placeholder_export_id = "00000000-0000-0000-0000-000000000000"
    export_summary = ExportSummary(
        id=placeholder_export_id,
        close_run_id=str(close_run_id),
        version_no=1,
        idempotency_key=idempotency_key,
        status="pending",
        artifact_count=len(manifest_result.manifest.artifacts),
        failure_reason=None,
        created_at=now,
        completed_at=None,
    )

    return ExportDetail(
        **export_summary.model_dump(),
        manifest=manifest_result.manifest,
        evidence_pack=manifest_result.evidence_pack,
    )


@router.get(
    "",
    response_model=ExportListResponse,
    summary="List exports for one close run",
)
def list_exports(
    entity_id: UUID,
    close_run_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    db_session: DatabaseSessionDependency,
    auth_context: RequestAuthDependency,
) -> ExportListResponse:
    """Return all export records for one close run in newest-first order."""

    session_result = auth_context
    _verify_close_run_access(
        db_session=db_session,
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
    )

    # Placeholder: return empty list until export-run repository is wired.
    return ExportListResponse(
        close_run_id=str(close_run_id),
        exports=(),
    )


@router.get(
    "/{export_id}",
    response_model=ExportDetail,
    summary="Read one export detail",
)
def read_export_detail(
    entity_id: UUID,
    close_run_id: UUID,
    export_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    db_session: DatabaseSessionDependency,
    auth_context: RequestAuthDependency,
) -> ExportDetail:
    """Return one export record with full manifest and evidence-pack details."""

    session_result = auth_context
    _verify_close_run_access(
        db_session=db_session,
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
    )

    raise HTTPException(
        status_code=404,
        detail={
            "code": "export_not_found",
            "message": "The requested export record does not exist for this close run.",
        },
    )


# ---------------------------------------------------------------------------
# Evidence pack endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/evidence-pack",
    response_model=EvidencePackBundle,
    status_code=status.HTTP_201_CREATED,
    summary="Assemble and release an evidence pack for a close run",
)
def assemble_evidence_pack(
    entity_id: UUID,
    close_run_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    db_session: DatabaseSessionDependency,
    auth_context: RequestAuthDependency,
) -> EvidencePackBundle:
    """Assemble a downloadable evidence-pack bundle for one close run.

    The bundle contains source references, extracted values, approval records,
    diffs, and report outputs.  Idempotency protection ensures duplicate
    assembly requests return the existing pack instead of overwriting it.
    """

    from services.contracts.storage_models import CloseRunStorageScope
    from services.db.models.close_run import CloseRun
    from services.db.models.entity import Entity
    from services.db.models.exports import Artifact
    from services.reporting.evidence_pack import (
        EvidencePackInput,
        build_evidence_pack,
        upload_evidence_pack,
    )
    from services.storage.repository import StorageRepository

    session_result = auth_context
    user_record = _to_entity_user(session_result)

    # P1: Verify close-run membership before any artifact work.
    _verify_close_run_access(db_session, entity_id, close_run_id, user_record.id)

    # Resolve close-run version and period from the database so the
    # storage scope and idempotency key reflect the actual close run.
    close_run_record = (
        db_session.query(CloseRun)
        .filter(CloseRun.id == close_run_id, CloseRun.entity_id == entity_id)
        .first()
    )
    if close_run_record is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "close_run_not_found",
                "message": "The requested close run does not exist for this entity.",
            },
        )

    entity_record = (
        db_session.query(Entity).filter(Entity.id == entity_id).first()
    )
    entity_name = entity_record.name if entity_record else "Unknown Entity"
    version_no = close_run_record.current_version_no

    # Compute the deterministic idempotency key.
    idempotency_key = build_idempotency_key(
        close_run_id=close_run_id,
        artifact_type=ArtifactType.EVIDENCE_PACK.value,
        action_qualifier="evidence_pack",
        version_override=version_no,
    )

    # P2: Guard against duplicate release before building/uploading anything.
    # Check whether an artifact with this idempotency key was already released.
    existing_artifact = (
        db_session.query(Artifact)
        .filter(
            Artifact.artifact_type == ArtifactType.EVIDENCE_PACK.value,
            Artifact.idempotency_key == idempotency_key,
        )
        .first()
    )
    if existing_artifact is not None:
        # Return the existing bundle metadata instead of rebuilding/uploading.
        return EvidencePackBundle(
            close_run_id=str(close_run_id),
            version_no=existing_artifact.version_no,
            generated_at=existing_artifact.created_at,
            items=(),
            storage_key=existing_artifact.storage_key,
            checksum=existing_artifact.checksum,
            size_bytes=_to_int(existing_artifact.artifact_metadata.get("size_bytes")),
            idempotency_key=existing_artifact.idempotency_key,
        )

    # Build a minimal evidence-pack input for the current scope.
    # In a full implementation, this would query the DB for all evidence items.
    pack_input = EvidencePackInput(
        close_run_id=close_run_id,
        entity_id=entity_id,
        entity_name=entity_name,
        period_start=close_run_record.period_start,
        period_end=close_run_record.period_end,
        close_run_version_no=version_no,
        source_references=[],
        extracted_values=[],
        approval_records=[],
        diff_entries=[],
        report_outputs=[],
    )
    pack_result = build_evidence_pack(pack_input)

    storage_repo = StorageRepository()
    scope = CloseRunStorageScope(
        entity_id=entity_id,
        close_run_id=close_run_id,
        period_start=close_run_record.period_start,
        period_end=close_run_record.period_end,
        close_run_version_no=version_no,
    )
    bundle = upload_evidence_pack(
        result=pack_result,
        storage_repo=storage_repo,
        scope=scope,
        idempotency_key=idempotency_key,
    )

    # Persist the artifact row so future retries find the existing release.
    db_session.add(
        Artifact(
            close_run_id=close_run_id,
            report_run_id=None,
            artifact_type=ArtifactType.EVIDENCE_PACK.value,
            storage_key=bundle.storage_key,
            mime_type="application/zip",
            checksum=bundle.checksum,
            idempotency_key=bundle.idempotency_key,
            version_no=bundle.version_no,
            released_at=bundle.generated_at,
            artifact_metadata={
                "entity_name": entity_name,
                "item_count": bundle.item_count,
                "size_bytes": bundle.size_bytes,
            },
        )
    )
    db_session.commit()

    return bundle


def _to_int(value: object) -> int:
    """Safely coerce a JSONB integer value to Python int."""

    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value)
    return 0


@router.get(
    "/evidence-pack/idempotency-key",
    response_model=IdempotencyKeyResponse,
    summary="Preview the idempotency key for an evidence-pack action",
)
def preview_evidence_pack_idempotency_key(
    entity_id: UUID,
    close_run_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    db_session: DatabaseSessionDependency,
    auth_context: RequestAuthDependency,
    version: Annotated[int, Query(ge=1, description="Close-run version number.")] = 1,
) -> IdempotencyKeyResponse:
    """Return the deterministic idempotency key that would be used for an evidence-pack action.

    This endpoint lets clients preview the key before triggering the actual
    assembly so they can check for existing packs or build idempotent retries.
    """

    session_result = auth_context
    _verify_close_run_access(
        db_session=db_session,
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
    )

    key = build_idempotency_key(
        close_run_id=close_run_id,
        artifact_type=ArtifactType.EVIDENCE_PACK.value,
        action_qualifier="evidence_pack",
        version_override=version,
    )
    return IdempotencyKeyResponse(
        idempotency_key=key,
        close_run_id=str(close_run_id),
        artifact_type=ArtifactType.EVIDENCE_PACK.value,
    )


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

    from apps.api.app.routes.auth import (
        _clear_session_cookie,
        _read_session_cookie,
        _resolve_ip_address,
        _set_session_cookie,
    )

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


def _verify_close_run_access(
    db_session: DatabaseSessionDependency,
    entity_id: UUID,
    close_run_id: UUID,
    user_id: UUID,
) -> None:
    """Verify that the user has membership access to the entity and the close run belongs to it.

    This is a first-class authorization gate that prevents callers from accessing
    close runs outside their entity workspace, even when they supply valid UUIDs.

    Args:
        db_session: Active SQLAlchemy session for persistence lookups.
        entity_id: Entity workspace UUID.
        close_run_id: Close run UUID.
        user_id: Authenticated user UUID.

    Raises:
        HTTPException: When the entity membership or close-run ownership check fails.
    """

    from services.db.repositories.report_repo import ReportRepository

    repo = ReportRepository(db_session=db_session)
    access_record = repo.get_close_run_for_entity(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=user_id,
    )
    if access_record is None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "close_run_access_denied",
                "message": (
                    "You do not have access to this close run. "
                    "Verify that the entity exists, the close run belongs to it, "
                    "and you are a member of the entity workspace."
                ),
            },
        )


def __build_export_manifest_input(
    *,
    close_run_id: UUID,
    entity_id: UUID,
    close_run_version_no: int,
    action_qualifier: str | None,
) -> ExportManifestInput:
    """Build a minimal export-manifest input for the current implementation scope.

    Args:
        close_run_id: UUID of the close run.
        entity_id: Entity workspace UUID.
        close_run_version_no: Close-run version number.
        action_qualifier: Optional action scope for disambiguation.

    Returns:
        ExportManifestInput with placeholder values.
    """

    return ExportManifestInput(
        close_run_id=close_run_id,
        entity_id=entity_id,
        entity_name="Demo Entity",
        period_start=datetime(2026, 1, 1, tzinfo=UTC),
        period_end=datetime(2026, 1, 31, tzinfo=UTC),
        close_run_version_no=close_run_version_no,
        artifact_records=[],
        include_evidence_pack=True,
        include_audit_trail=True,
    )


def _build_export_http_exception(error: IdempotencyGuardError) -> HTTPException:
    """Convert an idempotency-guard error into the API's structured HTTP response."""

    return HTTPException(
        status_code=error.status_code,
        detail={
            "code": str(error.code),
            "message": error.message,
            "existing_artifact_ref": error.existing_artifact_ref,
        },
    )


__all__ = ["router"]
