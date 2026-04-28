"""
Purpose: Provide the canonical lifecycle service for durable background jobs.
Scope: Job creation, dispatch recording, checkpoint updates, cancellation requests,
dead-letter metadata, inspection, and checkpoint-based resume support.
Dependencies: SQLAlchemy sessions, job ORM model, entity membership access checks,
task routing metadata, and shared retry-policy exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from services.auth.service import serialize_uuid
from services.common.enums import JobStatus
from services.common.types import JsonObject, utc_now
from services.db.models.entity import EntityMembership
from services.db.models.jobs import Job
from services.db.session import get_session_factory
from services.jobs.retry_policy import JobCancellationRequestedError
from services.jobs.task_names import TaskName, resolve_task_name, resolve_task_route
from sqlalchemy import desc, select
from sqlalchemy.orm import Session


class JobServiceErrorCode(StrEnum):
    """Enumerate stable error codes surfaced by job inspection and control flows."""

    DISPATCH_FAILED = "dispatch_failed"
    JOB_NOT_FOUND = "job_not_found"
    CANCEL_NOT_ALLOWED = "cancel_not_allowed"
    RESUME_NOT_ALLOWED = "resume_not_allowed"


class JobServiceError(Exception):
    """Represent an expected job-lifecycle failure for API translation."""

    def __init__(
        self,
        *,
        status_code: int,
        code: JobServiceErrorCode,
        message: str,
    ) -> None:
        """Capture HTTP status, stable code, and operator-facing recovery guidance."""

        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class JobRecord:
    """Describe one durable async job row for services, routes, and worker wrappers."""

    id: UUID
    entity_id: UUID | None
    close_run_id: UUID | None
    document_id: UUID | None
    actor_user_id: UUID | None
    canceled_by_user_id: UUID | None
    resumed_from_job_id: UUID | None
    task_name: str
    queue_name: str
    routing_key: str
    status: JobStatus
    payload: JsonObject
    checkpoint_payload: JsonObject
    result_payload: JsonObject | None
    failure_reason: str | None
    failure_details: JsonObject | None
    blocking_reason: str | None
    trace_id: str | None
    attempt_count: int
    retry_count: int
    max_retries: int
    started_at: datetime | None
    completed_at: datetime | None
    cancellation_requested_at: datetime | None
    canceled_at: datetime | None
    dead_lettered_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TaskDispatcherProtocol(Protocol):
    """Describe the broker-dispatch surface required by the job service."""

    def dispatch_task(
        self,
        *,
        task_name: TaskName | str,
        args: tuple[object, ...] | None = None,
        kwargs: dict[str, object] | None = None,
        countdown: int | None = None,
        task_id: str | None = None,
    ) -> object:
        """Dispatch a JSON-safe background task and return a broker receipt."""


class JobService:
    """Own the canonical persisted lifecycle for long-running background work."""

    def __init__(self, *, db_session: Session) -> None:
        """Capture the request- or task-scoped SQLAlchemy session used by job workflows."""

        self._db_session = db_session

    def dispatch_job(
        self,
        *,
        dispatcher: TaskDispatcherProtocol,
        task_name: TaskName | str,
        payload: JsonObject,
        entity_id: UUID | None,
        close_run_id: UUID | None,
        document_id: UUID | None,
        actor_user_id: UUID | None,
        trace_id: str | None,
        checkpoint_payload: JsonObject | None = None,
        resumed_from_job_id: UUID | None = None,
        countdown: int | None = None,
    ) -> JobRecord:
        """Persist a queued job row, dispatch the task, and return the durable job record."""

        resolved_task_name = resolve_task_name(task_name)
        route = resolve_task_route(resolved_task_name)
        job_id = uuid4()

        job = Job(
            id=job_id,
            entity_id=entity_id,
            close_run_id=close_run_id,
            document_id=document_id,
            actor_user_id=actor_user_id,
            canceled_by_user_id=None,
            resumed_from_job_id=resumed_from_job_id,
            task_name=resolved_task_name.value,
            queue_name=route.queue.value,
            routing_key=route.routing_key,
            status=JobStatus.QUEUED.value,
            payload=dict(payload),
            checkpoint_payload=dict(checkpoint_payload or {}),
            result_payload=None,
            failure_reason=None,
            failure_details=None,
            blocking_reason=None,
            trace_id=trace_id,
            attempt_count=0,
            retry_count=0,
            max_retries=route.max_retries,
            started_at=None,
            completed_at=None,
            cancellation_requested_at=None,
            canceled_at=None,
            dead_lettered_at=None,
        )
        self._db_session.add(job)
        self._db_session.commit()

        try:
            dispatcher.dispatch_task(
                task_name=resolved_task_name,
                kwargs=dict(payload),
                countdown=countdown,
                task_id=str(job_id),
            )
        except Exception as error:
            self.mark_failed(
                job_id=job_id,
                failure_reason="Task dispatch failed before the worker accepted the job.",
                failure_details={
                    "exception_type": error.__class__.__name__,
                    "message": str(error),
                },
                dead_letter=False,
            )
            raise JobServiceError(
                status_code=503,
                code=JobServiceErrorCode.DISPATCH_FAILED,
                message=(
                    "The background job could not be queued. "
                    "Check the worker and broker health, then retry."
                ),
            ) from error

        return self.get_job(job_id=job_id)

    def list_jobs_for_user(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
        close_run_id: UUID | None = None,
        statuses: tuple[JobStatus, ...] | None = None,
    ) -> tuple[JobRecord, ...]:
        """Return jobs visible to one user for an entity, optionally filtered by scope."""

        statement = (
            select(Job)
            .join(EntityMembership, EntityMembership.entity_id == Job.entity_id)
            .where(
                Job.entity_id == entity_id,
                EntityMembership.user_id == user_id,
            )
            .order_by(desc(Job.created_at), desc(Job.id))
        )
        if close_run_id is not None:
            statement = statement.where(Job.close_run_id == close_run_id)
        if statuses:
            statement = statement.where(Job.status.in_(tuple(status.value for status in statuses)))

        return tuple(_map_job(job) for job in self._db_session.scalars(statement))

    def get_job_for_user(
        self,
        *,
        entity_id: UUID,
        job_id: UUID,
        user_id: UUID,
    ) -> JobRecord:
        """Return one job when it belongs to an entity the caller can access."""

        statement = (
            select(Job)
            .join(EntityMembership, EntityMembership.entity_id == Job.entity_id)
            .where(
                Job.id == job_id,
                Job.entity_id == entity_id,
                EntityMembership.user_id == user_id,
            )
        )
        job = self._db_session.execute(statement).scalar_one_or_none()
        if job is None:
            raise JobServiceError(
                status_code=404,
                code=JobServiceErrorCode.JOB_NOT_FOUND,
                message="The requested job was not found for this entity.",
            )

        return _map_job(job)

    def request_cancellation(
        self,
        *,
        entity_id: UUID,
        job_id: UUID,
        actor_user_id: UUID,
        reason: str,
    ) -> JobRecord:
        """Request cancellation and cancel immediately when work has not started yet."""

        job = self._load_job_for_update(
            job_id=job_id,
            entity_id=entity_id,
            user_id=actor_user_id,
        )
        allowed_statuses = {
            JobStatus.QUEUED.value,
            JobStatus.RUNNING.value,
            JobStatus.BLOCKED.value,
        }
        if job.status not in allowed_statuses:
            raise JobServiceError(
                status_code=409,
                code=JobServiceErrorCode.CANCEL_NOT_ALLOWED,
                message="Only queued, running, or blocked jobs can be canceled.",
            )

        now = utc_now()
        job.failure_reason = reason
        job.cancellation_requested_at = now
        job.canceled_by_user_id = actor_user_id
        if job.status in {JobStatus.QUEUED.value, JobStatus.BLOCKED.value}:
            job.status = JobStatus.CANCELED.value
            job.blocking_reason = None
            job.canceled_at = now
            job.completed_at = now

        self._db_session.commit()
        return _map_job(job)

    def resume_job(
        self,
        *,
        dispatcher: TaskDispatcherProtocol,
        entity_id: UUID,
        job_id: UUID,
        actor_user_id: UUID,
        reason: str,
    ) -> JobRecord:
        """Create a fresh queued job from a failed, blocked, or canceled execution checkpoint."""

        source_job = self._load_job_for_update(
            job_id=job_id,
            entity_id=entity_id,
            user_id=actor_user_id,
        )
        if source_job.status not in {
            JobStatus.BLOCKED.value,
            JobStatus.CANCELED.value,
            JobStatus.FAILED.value,
        }:
            raise JobServiceError(
                status_code=409,
                code=JobServiceErrorCode.RESUME_NOT_ALLOWED,
                message="Only blocked, canceled, or failed jobs can be resumed from a checkpoint.",
            )

        resumed_checkpoint = {
            **source_job.checkpoint_payload,
            "resumed_at": utc_now().isoformat(),
            "resume_reason": reason,
            "resumed_from_job_id": serialize_uuid(source_job.id),
        }

        return self.dispatch_job(
            dispatcher=dispatcher,
            task_name=source_job.task_name,
            payload=dict(source_job.payload),
            entity_id=source_job.entity_id,
            close_run_id=source_job.close_run_id,
            document_id=source_job.document_id,
            actor_user_id=actor_user_id,
            trace_id=source_job.trace_id,
            checkpoint_payload=resumed_checkpoint,
            resumed_from_job_id=source_job.id,
        )

    def get_job(self, *, job_id: UUID) -> JobRecord:
        """Return one durable job record or fail fast when it does not exist."""

        job = self._db_session.get(Job, job_id)
        if job is None:
            raise JobServiceError(
                status_code=404,
                code=JobServiceErrorCode.JOB_NOT_FOUND,
                message="The requested job does not exist.",
            )

        return _map_job(job)

    def mark_running(
        self,
        *,
        job_id: UUID,
        trace_id: str | None,
        attempt_count: int,
    ) -> JobRecord:
        """Transition a queued or resumed job into active execution."""

        job = self._load_job(job_id=job_id)
        if job.status == JobStatus.CANCELED.value:
            return _map_job(job)

        job.status = JobStatus.RUNNING.value
        job.trace_id = trace_id
        job.started_at = utc_now()
        job.attempt_count = attempt_count
        self._db_session.commit()
        return _map_job(job)

    def record_checkpoint(
        self,
        *,
        job_id: UUID,
        checkpoint_payload: JsonObject,
    ) -> JobRecord:
        """Persist a resumable checkpoint payload for a running job."""

        job = self._load_job(job_id=job_id)
        job.checkpoint_payload = dict(checkpoint_payload)
        self._db_session.commit()
        return _map_job(job)

    def mark_retry_scheduled(
        self,
        *,
        job_id: UUID,
        retry_count: int,
        failure_reason: str,
        failure_details: JsonObject,
    ) -> JobRecord:
        """Persist retry metadata after one failed attempt that will be rescheduled."""

        job = self._load_job(job_id=job_id)
        job.status = JobStatus.QUEUED.value
        job.retry_count = retry_count
        job.failure_reason = failure_reason
        job.failure_details = dict(failure_details)
        self._db_session.commit()
        return _map_job(job)

    def mark_blocked(
        self,
        *,
        job_id: UUID,
        blocking_reason: str,
        failure_details: JsonObject,
    ) -> JobRecord:
        """Persist a blocked state that requires operator intervention before resume."""

        job = self._load_job(job_id=job_id)
        job.status = JobStatus.BLOCKED.value
        job.blocking_reason = blocking_reason
        job.failure_reason = blocking_reason
        job.failure_details = dict(failure_details)
        self._db_session.commit()
        return _map_job(job)

    def mark_completed(
        self,
        *,
        job_id: UUID,
        result_payload: JsonObject,
    ) -> JobRecord:
        """Persist a successful terminal outcome for a worker job."""

        job = self._load_job(job_id=job_id)
        job.status = JobStatus.COMPLETED.value
        job.result_payload = dict(result_payload)
        job.completed_at = utc_now()
        job.failure_reason = None
        job.failure_details = None
        job.blocking_reason = None
        self._db_session.commit()
        return _map_job(job)

    def mark_failed(
        self,
        *,
        job_id: UUID,
        failure_reason: str,
        failure_details: JsonObject,
        dead_letter: bool,
    ) -> JobRecord:
        """Persist a terminal failure, optionally marking the job as dead-lettered."""

        job = self._load_job(job_id=job_id)
        job.status = JobStatus.FAILED.value
        job.failure_reason = failure_reason
        job.failure_details = dict(failure_details)
        job.completed_at = utc_now()
        if dead_letter:
            job.dead_lettered_at = utc_now()
        self._db_session.commit()
        return _map_job(job)

    def mark_canceled(
        self,
        *,
        job_id: UUID,
        failure_reason: str,
        failure_details: JsonObject,
    ) -> JobRecord:
        """Persist a worker-observed cancellation as a terminal job outcome."""

        job = self._load_job(job_id=job_id)
        now = utc_now()
        job.status = JobStatus.CANCELED.value
        job.failure_reason = failure_reason
        job.failure_details = dict(failure_details)
        job.blocking_reason = None
        job.cancellation_requested_at = job.cancellation_requested_at or now
        job.canceled_at = now
        job.completed_at = now
        self._db_session.commit()
        return _map_job(job)

    def ensure_not_canceled(self, *, job_id: UUID) -> None:
        """Raise a structured worker-side cancellation error when an operator requested stop."""

        job = self._load_job(job_id=job_id)
        if job.cancellation_requested_at is not None or job.status == JobStatus.CANCELED.value:
            raise JobCancellationRequestedError(
                "Execution stopped because an operator requested cancellation.",
                details={"job_id": serialize_uuid(job_id)},
            )

    def _load_job(self, *, job_id: UUID) -> Job:
        """Return one job ORM row or fail fast when worker state is inconsistent."""

        job = self._db_session.get(Job, job_id)
        if job is None:
            raise RuntimeError(
                "Job lifecycle record is missing for the active worker task. "
                "Dispatch through JobService.dispatch_job before executing tracked tasks."
            )

        return job

    def _load_job_for_update(
        self,
        *,
        job_id: UUID,
        entity_id: UUID,
        user_id: UUID,
    ) -> Job:
        """Return one job only when the caller belongs to its owning entity workspace."""

        statement = (
            select(Job)
            .join(EntityMembership, EntityMembership.entity_id == Job.entity_id)
            .where(
                Job.id == job_id,
                Job.entity_id == entity_id,
                EntityMembership.user_id == user_id,
            )
        )
        job = self._db_session.execute(statement).scalar_one_or_none()
        if job is None:
            raise JobServiceError(
                status_code=404,
                code=JobServiceErrorCode.JOB_NOT_FOUND,
                message="The requested job was not found for this entity.",
            )

        return job


def build_job_service(*, db_session: Session | None = None) -> JobService:
    """Construct the canonical job service from an existing or newly created DB session."""

    if db_session is None:
        return JobService(db_session=get_session_factory()())

    return JobService(db_session=db_session)


def _map_job(job: Job) -> JobRecord:
    """Translate one ORM job row into the immutable service-layer record."""

    return JobRecord(
        id=job.id,
        entity_id=job.entity_id,
        close_run_id=job.close_run_id,
        document_id=job.document_id,
        actor_user_id=job.actor_user_id,
        canceled_by_user_id=job.canceled_by_user_id,
        resumed_from_job_id=job.resumed_from_job_id,
        task_name=job.task_name,
        queue_name=job.queue_name,
        routing_key=job.routing_key,
        status=JobStatus(job.status),
        payload=dict(job.payload),
        checkpoint_payload=dict(job.checkpoint_payload),
        result_payload=dict(job.result_payload) if job.result_payload is not None else None,
        failure_reason=job.failure_reason,
        failure_details=dict(job.failure_details) if job.failure_details is not None else None,
        blocking_reason=job.blocking_reason,
        trace_id=job.trace_id,
        attempt_count=job.attempt_count,
        retry_count=job.retry_count,
        max_retries=job.max_retries,
        started_at=job.started_at,
        completed_at=job.completed_at,
        cancellation_requested_at=job.cancellation_requested_at,
        canceled_at=job.canceled_at,
        dead_lettered_at=job.dead_lettered_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


__all__ = [
    "JobRecord",
    "JobService",
    "JobServiceError",
    "JobServiceErrorCode",
    "TaskDispatcherProtocol",
    "build_job_service",
]
