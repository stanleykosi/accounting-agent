"""
Purpose: Expose authenticated background-job inspection, cancellation, and resume routes.
Scope: Entity-scoped list/detail reads plus operator-issued cancel and resume controls
for checkpointed worker tasks.
Dependencies: FastAPI, shared request authentication, durable job service, and
strict job API contracts.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from apps.api.app.dependencies.db import DatabaseSessionDependency
from apps.api.app.dependencies.tasks import TaskDispatcherDependency
from apps.api.app.routes.request_auth import RequestAuthDependency
from fastapi import APIRouter, Depends, HTTPException, Query, status
from services.auth.service import serialize_uuid
from services.common.enums import JobStatus
from services.contracts.job_models import (
    CancelJobRequest,
    JobDetail,
    JobListResponse,
    JobSummary,
    ResumeJobRequest,
    ResumeJobResponse,
)
from services.jobs.service import JobRecord, JobService, JobServiceError

router = APIRouter(prefix="/entities/{entity_id}/jobs", tags=["jobs"])


def get_job_service(db_session: DatabaseSessionDependency) -> JobService:
    """Construct the canonical durable job service from a request-scoped DB session."""

    return JobService(db_session=db_session)


JobServiceDependency = Annotated[JobService, Depends(get_job_service)]
StatusQuery = Annotated[list[JobStatus] | None, Query(description="Optional job-status filters.")]


@router.get(
    "",
    response_model=JobListResponse,
    summary="List background jobs for one entity",
)
def list_jobs(
    entity_id: UUID,
    auth_context: RequestAuthDependency,
    job_service: JobServiceDependency,
    close_run_id: Annotated[UUID | None, Query(description="Optional close-run filter.")] = None,
    status_filter: StatusQuery = None,
) -> JobListResponse:
    """Return recent jobs visible to the current user for one entity workspace."""

    try:
        jobs = job_service.list_jobs_for_user(
            entity_id=entity_id,
            user_id=auth_context.user.id,
            close_run_id=close_run_id,
            statuses=tuple(status_filter) if status_filter else None,
        )
    except JobServiceError as error:
        raise _build_job_http_exception(error) from error

    return JobListResponse(jobs=tuple(_to_job_summary(job) for job in jobs))


@router.get(
    "/{job_id}",
    response_model=JobDetail,
    summary="Read one background job",
)
def read_job_detail(
    entity_id: UUID,
    job_id: UUID,
    auth_context: RequestAuthDependency,
    job_service: JobServiceDependency,
) -> JobDetail:
    """Return one durable background-job record with payloads and checkpoint state."""

    try:
        job = job_service.get_job_for_user(
            entity_id=entity_id,
            job_id=job_id,
            user_id=auth_context.user.id,
        )
    except JobServiceError as error:
        raise _build_job_http_exception(error) from error

    return _to_job_detail(job)


@router.post(
    "/{job_id}/cancel",
    response_model=JobDetail,
    summary="Request cancellation for one background job",
)
def cancel_job(
    entity_id: UUID,
    job_id: UUID,
    payload: CancelJobRequest,
    auth_context: RequestAuthDependency,
    job_service: JobServiceDependency,
) -> JobDetail:
    """Request cancellation and return the updated durable job state."""

    try:
        job = job_service.request_cancellation(
            entity_id=entity_id,
            job_id=job_id,
            actor_user_id=auth_context.user.id,
            reason=payload.reason,
        )
    except JobServiceError as error:
        raise _build_job_http_exception(error) from error

    return _to_job_detail(job)


@router.post(
    "/{job_id}/resume",
    response_model=ResumeJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Resume one failed, blocked, or canceled background job from its checkpoint",
)
def resume_job(
    entity_id: UUID,
    job_id: UUID,
    payload: ResumeJobRequest,
    auth_context: RequestAuthDependency,
    job_service: JobServiceDependency,
    task_dispatcher: TaskDispatcherDependency,
) -> ResumeJobResponse:
    """Create a fresh queued job from the saved checkpoint of a prior execution."""

    try:
        resumed_job = job_service.resume_job(
            dispatcher=task_dispatcher,
            entity_id=entity_id,
            job_id=job_id,
            actor_user_id=auth_context.user.id,
            reason=payload.reason,
        )
    except JobServiceError as error:
        raise _build_job_http_exception(error) from error

    return ResumeJobResponse(resumed_job=_to_job_summary(resumed_job))


def _build_job_http_exception(error: JobServiceError) -> HTTPException:
    """Convert a job-domain error into the API's structured HTTP shape."""

    return HTTPException(
        status_code=error.status_code,
        detail={
            "code": str(error.code),
            "message": error.message,
        },
    )


def _to_job_summary(job: JobRecord) -> JobSummary:
    """Translate a durable job record into the public summary contract."""

    return JobSummary(
        id=serialize_uuid(job.id),
        entity_id=serialize_uuid(job.entity_id) if job.entity_id is not None else None,
        close_run_id=serialize_uuid(job.close_run_id) if job.close_run_id is not None else None,
        document_id=serialize_uuid(job.document_id) if job.document_id is not None else None,
        task_name=job.task_name,
        queue_name=job.queue_name,
        routing_key=job.routing_key,
        status=job.status,
        retry_count=job.retry_count,
        max_retries=job.max_retries,
        attempt_count=job.attempt_count,
        failure_reason=job.failure_reason,
        blocking_reason=job.blocking_reason,
        cancellation_requested_at=job.cancellation_requested_at,
        dead_lettered_at=job.dead_lettered_at,
        resumed_from_job_id=(
            serialize_uuid(job.resumed_from_job_id)
            if job.resumed_from_job_id is not None
            else None
        ),
        started_at=job.started_at,
        completed_at=job.completed_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _to_job_detail(job: JobRecord) -> JobDetail:
    """Translate a durable job record into the public detail contract."""

    summary = _to_job_summary(job)
    return JobDetail(
        **summary.model_dump(),
        actor_user_id=serialize_uuid(job.actor_user_id) if job.actor_user_id is not None else None,
        canceled_by_user_id=(
            serialize_uuid(job.canceled_by_user_id)
            if job.canceled_by_user_id is not None
            else None
        ),
        payload=job.payload,
        checkpoint_payload=job.checkpoint_payload,
        result_payload=job.result_payload,
        failure_details=job.failure_details,
        trace_id=job.trace_id,
    )


__all__ = ["router"]
