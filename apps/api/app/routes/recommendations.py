"""
Purpose: Expose API routes for accounting recommendations and journal entries.
Scope: Recommendation listing, approval, rejection, journal listing, approval, rejection,
apply actions, and autonomy-mode routing for the accounting engine.
Dependencies: FastAPI, local-auth session helpers, recommendation/journal contracts and
services, and the shared DB dependency.

Design notes:
- Every route authorizes the caller against the entity workspace before proceeding.
- Recommendation mutations are scoped to the route's close_run_id to prevent cross-entity
  mutation when a recommendation UUID is known.
- The request-scoped SQLAlchemy session is injected as a dependency and used for all
  repository operations; commits happen on the same session so service mutations are
  persisted atomically.
"""

from __future__ import annotations

from typing import Annotated, Any
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
from apps.api.app.routes.close_runs import _to_entity_user
from apps.api.app.routes.request_auth import RequestAuthDependency
from apps.api.app.routes.workflow_phase import require_active_close_run_phase
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from services.accounting.recommendation_apply import (
    ActorContext,
    RecommendationApplyError,
    RecommendationApplyErrorCode,
    RecommendationApplyService,
)
from services.auth.service import (
    AuthenticatedSessionResult,
    AuthErrorCode,
    AuthService,
    AuthServiceError,
)
from services.common.enums import DocumentStatus, ReviewStatus, WorkflowPhase
from services.common.settings import AppSettings, get_settings
from services.contracts.journal_models import (
    ApplyJournalRequest,
    ApproveJournalRequest,
    JournalActionResponse,
    JournalLineSummary,
    JournalListResponse,
    JournalPostingSummary,
    JournalSummary,
    RejectJournalRequest,
)
from services.contracts.storage_models import StorageBucketKind
from services.db.models.audit import AuditSourceSurface
from services.db.models.documents import Document
from services.db.models.extractions import DocumentExtraction
from services.db.models.recommendations import Recommendation
from services.db.repositories.entity_repo import EntityUserRecord
from services.db.repositories.integration_repo import IntegrationRepository
from services.db.repositories.recommendation_journal_repo import (
    RecommendationJournalRepository,
)
from services.documents.recommendation_eligibility import (
    GL_CODING_RECOMMENDATION_ELIGIBLE_TYPE_VALUES,
)
from services.jobs.service import JobService, JobServiceError
from services.jobs.task_names import TaskName
from services.storage.client import StorageClient
from services.storage.repository import StorageRepository

RECOMMENDATIONS_TAG = "recommendations"
REC_PREFIX = "/entities/{entity_id}/close-runs/{close_run_id}"
router = APIRouter(prefix=REC_PREFIX, tags=[RECOMMENDATIONS_TAG])

SettingsDependency = Annotated[AppSettings, Depends(get_settings)]
AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]
DbSessionDep = DatabaseSessionDependency


def _get_recommendation_journal_service(
    db_session: DatabaseSessionDependency,
) -> RecommendationApplyService:
    """Construct the canonical recommendation apply service from request-scoped persistence."""
    repository = RecommendationJournalRepository(db_session=db_session)
    from services.audit.service import AuditService

    audit_service = AuditService(db_session=db_session)
    return RecommendationApplyService(
        repository=repository,
        audit_service=audit_service,
        db_session=db_session,
        integration_repository=IntegrationRepository(db_session=db_session),
        storage_repository=StorageRepository(),
    )


RecommendationServiceDependency = Annotated[
    RecommendationApplyService, Depends(_get_recommendation_journal_service)
]


class GenerateRecommendationsRequest(BaseModel):
    """Capture an explicit request to generate recommendations for a close run."""

    force: bool = Field(
        default=False,
        description="Queue jobs even when a document already has an active recommendation.",
    )
    document_ids: list[UUID] | None = Field(
        default=None,
        description="Optional subset of document IDs to process.",
    )


# ---------------------------------------------------------------------------
# Recommendation routes
# ---------------------------------------------------------------------------


def _require_close_run_access(
    *,
    entity_id: UUID,
    close_run_id: UUID,
    user_id: UUID,
    db_session: DatabaseSessionDependency,
) -> tuple[EntityUserRecord, bool]:
    """Verify the user can access the entity and the close run belongs to it.

    Returns:
        A tuple of (entity_user_record, close_run_belongs_to_entity).
        The caller should check the boolean before returning data.
    """
    from services.db.repositories.close_run_repo import CloseRunRepository

    close_run_repo = CloseRunRepository(db_session=db_session)
    access = close_run_repo.get_close_run_for_user(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=user_id,
    )
    if access is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "access_denied",
                "message": "You do not have access to this close run.",
            },
        )
    return (
        EntityUserRecord(
            id=access.entity.id,
            email=access.entity.name,
            full_name=access.entity.name,
        ),
        True,
    )


@router.get(
    "/recommendations",
    response_model=object,
    summary="List recommendations for one close run",
)
def list_recommendations(
    entity_id: UUID,
    close_run_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    db_session: DbSessionDep,
    auth_context: RequestAuthDependency,
) -> dict[str, object]:
    """Return recommendations for an authenticated user's close run."""
    session_result = auth_context
    _require_close_run_access(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
        db_session=db_session,
    )
    repo = RecommendationJournalRepository(db_session=db_session)
    recommendations = repo.list_recommendations_for_close_run(close_run_id=close_run_id)
    return {
        "recommendations": [
            {
                "id": str(rec.id),
                "close_run_id": str(rec.close_run_id),
                "document_id": str(rec.document_id) if rec.document_id else None,
                "recommendation_type": rec.recommendation_type,
                "status": rec.status,
                "confidence": rec.confidence,
                "reasoning_summary": rec.reasoning_summary,
                "prompt_version": rec.prompt_version,
                "rule_version": rec.rule_version,
                "schema_version": rec.schema_version,
                "created_at": rec.created_at.isoformat(),
                "updated_at": rec.updated_at.isoformat(),
            }
            for rec in recommendations
        ]
    }


@router.post(
    "/recommendations/generate",
    response_model=object,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue recommendation generation for eligible close-run documents",
)
def generate_recommendations_for_close_run(
    entity_id: UUID,
    close_run_id: UUID,
    payload: GenerateRecommendationsRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    db_session: DbSessionDep,
    task_dispatcher: TaskDispatcherDependency,
    auth_context: RequestAuthDependency,
) -> dict[str, object]:
    """Queue accounting recommendation jobs for parsed documents in this close run."""

    session_result = auth_context
    _require_close_run_access(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
        db_session=db_session,
    )
    require_active_close_run_phase(
        actor_user=_to_entity_user(session_result),
        entity_id=entity_id,
        close_run_id=close_run_id,
        required_phase=WorkflowPhase.PROCESSING,
        action_label="Recommendation generation",
        db_session=db_session,
    )

    document_query = (
        db_session.query(Document)
        .join(DocumentExtraction, DocumentExtraction.document_id == Document.id)
        .filter(
            Document.close_run_id == close_run_id,
            Document.status == DocumentStatus.APPROVED.value,
            Document.document_type.in_(GL_CODING_RECOMMENDATION_ELIGIBLE_TYPE_VALUES),
        )
        .order_by(Document.created_at.asc(), Document.id.asc())
    )
    if payload.document_ids:
        document_query = document_query.filter(Document.id.in_(payload.document_ids))

    eligible_documents = document_query.all()
    existing_recommendations = set()
    if not payload.force:
        existing_recommendations = {
            recommendation.document_id
            for recommendation in db_session.query(Recommendation)
            .filter(
                Recommendation.close_run_id == close_run_id,
                Recommendation.document_id.isnot(None),
                Recommendation.superseded_by_id.is_(None),
            )
            .all()
            if recommendation.document_id is not None
        }

    job_service = JobService(db_session=db_session)
    queued_jobs: list[dict[str, object]] = []
    skipped_document_ids: list[str] = []

    for document in eligible_documents:
        if not payload.force and document.id in existing_recommendations:
            skipped_document_ids.append(str(document.id))
            continue

        try:
            job = job_service.dispatch_job(
                dispatcher=task_dispatcher,
                task_name=TaskName.ACCOUNTING_RECOMMEND_CLOSE_RUN,
                payload={
                    "entity_id": str(entity_id),
                    "close_run_id": str(close_run_id),
                    "document_id": str(document.id),
                    "actor_user_id": str(session_result.user.id),
                },
                entity_id=entity_id,
                close_run_id=close_run_id,
                document_id=document.id,
                actor_user_id=session_result.user.id,
                trace_id=str(getattr(request.state, "request_id", "")),
            )
        except JobServiceError as error:
            raise HTTPException(
                status_code=error.status_code,
                detail={
                    "code": str(error.code),
                    "message": error.message,
                },
            ) from error

        queued_jobs.append(
            {
                "job_id": str(job.id),
                "document_id": str(document.id),
                "task_name": job.task_name,
                "status": job.status.value,
            }
        )

    return {
        "queued_count": len(queued_jobs),
        "queued_jobs": queued_jobs,
        "skipped_document_ids": skipped_document_ids,
    }


@router.post(
    "/recommendations/{recommendation_id}/approve",
    response_model=object,
    summary="Approve one recommendation and generate its journal draft",
)
def approve_recommendation(
    entity_id: UUID,
    close_run_id: UUID,
    recommendation_id: UUID,
    payload: ApproveJournalRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    recommendation_service: RecommendationServiceDependency,
    db_session: DbSessionDep,
    auth_context: RequestAuthDependency,
) -> dict[str, object]:
    """Approve a pending recommendation and generate its journal draft."""
    session_result = auth_context
    _require_close_run_access(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
        db_session=db_session,
    )
    require_active_close_run_phase(
        actor_user=_to_entity_user(session_result),
        entity_id=entity_id,
        close_run_id=close_run_id,
        required_phase=WorkflowPhase.PROCESSING,
        action_label="Recommendation approval",
        db_session=db_session,
    )
    actor = ActorContext(
        user_id=session_result.user.id,
        full_name=session_result.user.full_name,
        email=session_result.user.email,
    )
    try:
        result = recommendation_service.approve_recommendation(
            recommendation_id=recommendation_id,
            entity_id=entity_id,
            close_run_id=close_run_id,
            actor=actor,
            reason=payload.reason,
            trace_id=_resolve_trace_id(request),
            source_surface=AuditSourceSurface.DESKTOP,
        )
        db_session.commit()
        return {
            "recommendation_id": str(result.recommendation_id),
            "initial_status": result.initial_status.value,
            "final_status": result.final_status.value,
            "journal_draft": (
                {
                    "journal_id": str(result.journal_draft_result.journal_id),
                    "journal_number": result.journal_draft_result.journal_number,
                    "status": result.journal_draft_result.status.value,
                    "total_debits": result.journal_draft_result.total_debits,
                    "total_credits": result.journal_draft_result.total_credits,
                    "line_count": result.journal_draft_result.line_count,
                }
                if result.journal_draft_result
                else None
            ),
        }
    except RecommendationApplyError as error:
        raise _build_recommendation_http_exception(error) from error


@router.post(
    "/recommendations/{recommendation_id}/reject",
    response_model=object,
    summary="Reject one recommendation",
)
def reject_recommendation(
    entity_id: UUID,
    close_run_id: UUID,
    recommendation_id: UUID,
    payload: RejectJournalRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    recommendation_service: RecommendationServiceDependency,
    db_session: DbSessionDep,
    auth_context: RequestAuthDependency,
) -> dict[str, object]:
    """Reject a pending recommendation so it does not affect working state."""
    session_result = auth_context
    _require_close_run_access(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
        db_session=db_session,
    )
    require_active_close_run_phase(
        actor_user=_to_entity_user(session_result),
        entity_id=entity_id,
        close_run_id=close_run_id,
        required_phase=WorkflowPhase.PROCESSING,
        action_label="Recommendation rejection",
        db_session=db_session,
    )
    actor = ActorContext(
        user_id=session_result.user.id,
        full_name=session_result.user.full_name,
        email=session_result.user.email,
    )
    try:
        recommendation_service.reject_recommendation(
            recommendation_id=recommendation_id,
            entity_id=entity_id,
            close_run_id=close_run_id,
            actor=actor,
            reason=payload.reason,
            trace_id=_resolve_trace_id(request),
            source_surface=AuditSourceSurface.DESKTOP,
        )
        db_session.commit()
        return {"recommendation_id": str(recommendation_id), "status": "rejected"}
    except RecommendationApplyError as error:
        raise _build_recommendation_http_exception(error) from error


# ---------------------------------------------------------------------------
# Journal routes
# ---------------------------------------------------------------------------


@router.get(
    "/journals",
    response_model=JournalListResponse,
    summary="List journal entries for one close run",
)
def list_journals(
    entity_id: UUID,
    close_run_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    db_session: DbSessionDep,
    auth_context: RequestAuthDependency,
) -> JournalListResponse:
    """Return journal entries for an authenticated user's close run."""
    session_result = auth_context
    _require_close_run_access(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
        db_session=db_session,
    )
    repo = RecommendationJournalRepository(db_session=db_session)
    journals = repo.list_journals_for_close_run(close_run_id=close_run_id)
    postings_by_journal_id = repo.list_postings_for_journal_ids(
        journal_entry_ids=tuple(journal.id for journal in journals),
    )
    return JournalListResponse(
        journals=tuple(
            _build_journal_summary(
                entry=j,
                lines=(),
                postings=postings_by_journal_id.get(j.id, ()),
            )
            for j in journals
        )
    )


@router.get(
    "/journals/{journal_id}",
    response_model=JournalSummary,
    summary="Read one journal entry with lines",
)
def read_journal(
    entity_id: UUID,
    close_run_id: UUID,
    journal_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    db_session: DbSessionDep,
) -> JournalSummary:
    """Return one journal entry with its attached lines."""
    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    _require_close_run_access(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
        db_session=db_session,
    )
    repo = RecommendationJournalRepository(db_session=db_session)
    result = repo.get_journal_entry(journal_id=journal_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "journal_not_found", "message": f"Journal {journal_id} not found."},
        )
    entry = result.entry
    return _build_journal_summary(
        entry=entry,
        lines=result.lines,
        postings=result.postings,
    )


@router.get(
    "/journals/{journal_id}/postings/{posting_id}/download",
    summary="Download one external posting package for a journal",
)
def download_journal_posting_package(
    entity_id: UUID,
    close_run_id: UUID,
    journal_id: UUID,
    posting_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    db_session: DbSessionDep,
) -> Response:
    """Stream one generated journal posting package through the authenticated API surface."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    _require_close_run_access(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
        db_session=db_session,
    )
    repo = RecommendationJournalRepository(db_session=db_session)
    result = repo.get_journal_entry(journal_id=journal_id)
    if (
        result is None
        or result.entry.close_run_id != close_run_id
        or result.entry.entity_id != entity_id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "journal_not_found", "message": f"Journal {journal_id} not found."},
        )
    posting = next((item for item in result.postings if item.id == posting_id), None)
    if posting is None or posting.artifact_storage_key is None or posting.artifact_filename is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "posting_artifact_not_found",
                "message": "The requested journal posting package does not exist.",
            },
        )

    payload = StorageClient.from_settings(settings).download_bytes(
        bucket_kind=StorageBucketKind.ARTIFACTS,
        object_key=posting.artifact_storage_key,
    )
    return Response(
        content=payload,
        media_type=_infer_posting_content_type(
            artifact_type=posting.artifact_type,
            filename=posting.artifact_filename,
        ),
        headers={
            "Content-Disposition": f'attachment; filename="{posting.artifact_filename}"',
        },
    )


@router.post(
    "/journals/{journal_id}/approve",
    response_model=JournalActionResponse,
    summary="Approve one journal entry",
)
def approve_journal(
    entity_id: UUID,
    close_run_id: UUID,
    journal_id: UUID,
    payload: ApproveJournalRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    recommendation_service: RecommendationServiceDependency,
    db_session: DbSessionDep,
    auth_context: RequestAuthDependency,
) -> JournalActionResponse:
    """Approve a draft or pending journal entry."""
    session_result = auth_context
    _require_close_run_access(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
        db_session=db_session,
    )
    require_active_close_run_phase(
        actor_user=_to_entity_user(session_result),
        entity_id=entity_id,
        close_run_id=close_run_id,
        required_phase=WorkflowPhase.PROCESSING,
        action_label="Journal approval",
        db_session=db_session,
    )
    actor = ActorContext(
        user_id=session_result.user.id,
        full_name=session_result.user.full_name,
        email=session_result.user.email,
    )
    try:
        result = recommendation_service.approve_journal(
            journal_id=journal_id,
            entity_id=entity_id,
            close_run_id=close_run_id,
            actor=actor,
            reason=payload.reason,
            trace_id=_resolve_trace_id(request),
            source_surface=AuditSourceSurface.DESKTOP,
        )
        db_session.commit()
        repo = RecommendationJournalRepository(db_session=db_session)
        journal_result = repo.get_journal_entry(journal_id=journal_id)
        if journal_result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "journal_not_found", "message": "Journal not found after action."},
            )
        return _build_journal_action_response(journal_result, result)
    except RecommendationApplyError as error:
        raise _build_recommendation_http_exception(error) from error


@router.post(
    "/journals/{journal_id}/apply",
    response_model=JournalActionResponse,
    summary="Post one approved journal through the chosen target",
)
def apply_journal(
    entity_id: UUID,
    close_run_id: UUID,
    journal_id: UUID,
    payload: ApplyJournalRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    recommendation_service: RecommendationServiceDependency,
    db_session: DbSessionDep,
    auth_context: RequestAuthDependency,
) -> JournalActionResponse:
    """Post an approved journal entry to the selected ledger target."""
    session_result = auth_context
    _require_close_run_access(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
        db_session=db_session,
    )
    require_active_close_run_phase(
        actor_user=_to_entity_user(session_result),
        entity_id=entity_id,
        close_run_id=close_run_id,
        required_phase=WorkflowPhase.PROCESSING,
        action_label="Journal application",
        db_session=db_session,
    )
    actor = ActorContext(
        user_id=session_result.user.id,
        full_name=session_result.user.full_name,
        email=session_result.user.email,
    )
    try:
        result = recommendation_service.apply_journal(
            journal_id=journal_id,
            entity_id=entity_id,
            close_run_id=close_run_id,
            actor=actor,
            posting_target=payload.posting_target,
            reason=payload.reason,
            trace_id=_resolve_trace_id(request),
            source_surface=AuditSourceSurface.DESKTOP,
        )
        db_session.commit()
        repo = RecommendationJournalRepository(db_session=db_session)
        journal_result = repo.get_journal_entry(journal_id=journal_id)
        if journal_result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "journal_not_found", "message": "Journal not found after action."},
            )
        return _build_journal_action_response(journal_result, result)
    except RecommendationApplyError as error:
        raise _build_recommendation_http_exception(error) from error


@router.post(
    "/journals/{journal_id}/reject",
    response_model=JournalActionResponse,
    summary="Reject one journal entry",
)
def reject_journal(
    entity_id: UUID,
    close_run_id: UUID,
    journal_id: UUID,
    payload: RejectJournalRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    recommendation_service: RecommendationServiceDependency,
    db_session: DbSessionDep,
    auth_context: RequestAuthDependency,
) -> JournalActionResponse:
    """Reject a draft or pending journal entry."""
    session_result = auth_context
    _require_close_run_access(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
        db_session=db_session,
    )
    require_active_close_run_phase(
        actor_user=_to_entity_user(session_result),
        entity_id=entity_id,
        close_run_id=close_run_id,
        required_phase=WorkflowPhase.PROCESSING,
        action_label="Journal rejection",
        db_session=db_session,
    )
    actor = ActorContext(
        user_id=session_result.user.id,
        full_name=session_result.user.full_name,
        email=session_result.user.email,
    )
    try:
        result = recommendation_service.reject_journal(
            journal_id=journal_id,
            entity_id=entity_id,
            close_run_id=close_run_id,
            actor=actor,
            reason=payload.reason,
            trace_id=_resolve_trace_id(request),
            source_surface=AuditSourceSurface.DESKTOP,
        )
        db_session.commit()
        repo = RecommendationJournalRepository(db_session=db_session)
        journal_result = repo.get_journal_entry(journal_id=journal_id)
        if journal_result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "journal_not_found", "message": "Journal not found after action."},
            )
        return _build_journal_action_response(journal_result, result)
    except RecommendationApplyError as error:
        raise _build_recommendation_http_exception(error) from error


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_journal_action_response(
    journal_result: Any,
    action_result: Any,
) -> JournalActionResponse:
    """Assemble a JournalActionResponse from a journal query result and action result."""
    entry = journal_result.entry
    return JournalActionResponse(
        journal=_build_journal_summary(
            entry=entry,
            lines=journal_result.lines,
            postings=journal_result.postings,
        ),
        action=action_result.action,
        autonomy_mode=action_result.autonomy_mode,
    )


def _build_journal_summary(
    *,
    entry: Any,
    lines: Any,
    postings: Any,
) -> JournalSummary:
    """Assemble one journal summary with lines and posting outcomes."""

    return JournalSummary(
        id=str(entry.id),
        entity_id=str(entry.entity_id),
        close_run_id=str(entry.close_run_id),
        recommendation_id=str(entry.recommendation_id) if entry.recommendation_id else None,
        journal_number=entry.journal_number,
        posting_date=entry.posting_date,
        status=ReviewStatus(entry.status),
        description=entry.description,
        total_debits=str(entry.total_debits),
        total_credits=str(entry.total_credits),
        line_count=entry.line_count,
        source_surface=entry.source_surface,
        autonomy_mode=entry.autonomy_mode,
        reasoning_summary=entry.reasoning_summary,
        approved_by_user_id=str(entry.approved_by_user_id) if entry.approved_by_user_id else None,
        applied_by_user_id=str(entry.applied_by_user_id) if entry.applied_by_user_id else None,
        postings=[_build_journal_posting_summary(posting) for posting in postings],
        lines=[
            JournalLineSummary(
                id=str(line.id),
                line_no=line.line_no,
                account_code=line.account_code,
                line_type=line.line_type,
                amount=str(line.amount),
                description=line.description,
                dimensions=line.dimensions,
                reference=line.reference,
            )
            for line in lines
        ],
        created_at=entry.created_at.isoformat(),
        updated_at=entry.updated_at.isoformat(),
    )


def _build_journal_posting_summary(posting: Any) -> JournalPostingSummary:
    """Translate one posting record into the strict API contract."""

    return JournalPostingSummary(
        id=str(posting.id),
        posting_target=posting.posting_target,
        provider=posting.provider,
        status=posting.status,
        artifact_id=str(posting.artifact_id) if posting.artifact_id else None,
        artifact_type=posting.artifact_type,
        artifact_filename=posting.artifact_filename,
        artifact_storage_key=posting.artifact_storage_key,
        note=posting.note,
        posted_by_user_id=str(posting.posted_by_user_id) if posting.posted_by_user_id else None,
        posted_at=posting.posted_at.isoformat(),
        posting_metadata=dict(posting.posting_metadata),
    )


def _infer_posting_content_type(*, artifact_type: str | None, filename: str) -> str:
    """Infer the content type for a stored journal posting package."""

    if artifact_type == "gl_posting_package" or filename.endswith(".csv"):
        return "text/csv; charset=utf-8"
    if filename.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return "application/octet-stream"


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


def _resolve_trace_id(request: Request) -> str | None:
    """Return the request ID bound by middleware so timeline events can link to logs."""

    request_id = getattr(request.state, "request_id", None)
    return str(request_id) if request_id is not None else None


def _build_recommendation_http_exception(error: RecommendationApplyError) -> HTTPException:
    """Convert a recommendation apply error into a structured HTTP response."""
    status_codes: dict[str, int] = {
        RecommendationApplyErrorCode.RECOMMENDATION_NOT_FOUND.value: status.HTTP_404_NOT_FOUND,
        RecommendationApplyErrorCode.JOURNAL_NOT_FOUND.value: status.HTTP_404_NOT_FOUND,
        RecommendationApplyErrorCode.INVALID_TRANSITION.value: status.HTTP_409_CONFLICT,
        RecommendationApplyErrorCode.JOURNAL_NOT_BALANCED.value: status.HTTP_400_BAD_REQUEST,
        RecommendationApplyErrorCode.APPROVAL_NOT_ALLOWED.value: status.HTTP_409_CONFLICT,
        RecommendationApplyErrorCode.REJECTION_NOT_ALLOWED.value: status.HTTP_409_CONFLICT,
        RecommendationApplyErrorCode.APPLY_NOT_ALLOWED.value: status.HTTP_409_CONFLICT,
        RecommendationApplyErrorCode.EDIT_NOT_ALLOWED.value: status.HTTP_409_CONFLICT,
        RecommendationApplyErrorCode.SUPERSEDED.value: status.HTTP_409_CONFLICT,
    }
    return HTTPException(
        status_code=status_codes.get(error.code, status.HTTP_400_BAD_REQUEST),
        detail={"code": error.code, "message": error.message},
    )


__all__ = ["router"]
