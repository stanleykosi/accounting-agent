"""
Purpose: Provide the canonical worker-side wrapper for checkpointed, resumable,
and cancel-aware background tasks.
Scope: Shared job lifecycle transitions, checkpoint persistence, retry policy
enforcement, and cancellation checks for all long-running Celery tasks.
Dependencies: Celery observed task base, job service, retry policy, request trace
context, and the shared DB session factory.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, TypeVar
from uuid import UUID

from apps.worker.app.celery_app import ObservedTask
from services.common.enums import JobStatus
from services.common.logging import get_logger
from services.common.types import JsonObject, JsonValue, utc_now
from services.db.session import get_session_factory
from services.jobs.retry_policy import (
    BlockedJobError,
    JobCancellationRequestedError,
    JobControlAction,
    classify_job_failure,
)
from services.jobs.service import JobRecord, JobService
from services.observability.context import current_trace_metadata

TJobServiceReturn = TypeVar("TJobServiceReturn")
LOGGER = get_logger(__name__)


@dataclass(slots=True)
class JobRuntimeContext:
    """Expose checkpoint and cancellation helpers to long-running worker tasks."""

    task: TrackedJobTask
    job_record: JobRecord

    def checkpoint(
        self,
        *,
        step: str,
        state: JsonObject | None = None,
    ) -> JsonObject:
        """Persist a resumable checkpoint for the current step and return the saved payload."""

        raw_completed_steps = self.checkpoint_payload.get("completed_steps")
        completed_steps = list(raw_completed_steps) if isinstance(raw_completed_steps, list) else []
        if step not in completed_steps:
            completed_steps.append(step)
        raw_step_states = self.checkpoint_payload.get("step_states")
        step_states = dict(raw_step_states) if isinstance(raw_step_states, dict) else {}
        if state is not None:
            step_states[step] = dict(state)

        payload: JsonObject = {
            **self.checkpoint_payload,
            "current_step": step,
            "completed_steps": completed_steps,
            "state": dict(state or self.state),
            "step_states": step_states,
            "updated_at": utc_now().isoformat(),
        }
        self.job_record = self.task._record_checkpoint(
            job_id=self.job_record.id,
            checkpoint_payload=payload,
        )
        return payload

    def ensure_not_canceled(self) -> None:
        """Fail fast when the operator requested cancellation for the current job."""

        self.task._ensure_not_canceled(job_id=self.job_record.id)

    @property
    def checkpoint_payload(self) -> JsonObject:
        """Return the latest persisted checkpoint payload for this job execution."""

        return dict(self.job_record.checkpoint_payload)

    @property
    def state(self) -> JsonObject:
        """Return the task-specific state payload nested under the checkpoint envelope."""

        raw_state = self.checkpoint_payload.get("state", {})
        if isinstance(raw_state, dict):
            return raw_state

        return {}

    def step_completed(self, step: str) -> bool:
        """Return whether the named step already appears in the persisted checkpoint history."""

        completed_steps = self.checkpoint_payload.get("completed_steps", [])
        return isinstance(completed_steps, list) and step in completed_steps

    def step_state(self, step: str) -> JsonObject:
        """Return the persisted state snapshot for one completed checkpointed step."""

        raw_step_states = self.checkpoint_payload.get("step_states", {})
        if not isinstance(raw_step_states, dict):
            return {}

        raw_state = raw_step_states.get(step)
        if not isinstance(raw_state, dict):
            return {}

        return dict(raw_state)


class TrackedJobTask(ObservedTask):
    """Wrap long-running tasks in one canonical persisted lifecycle controller."""

    abstract = True

    def run_tracked_job(
        self,
        *,
        runner: Callable[[JobRuntimeContext], dict[str, Any]],
    ) -> dict[str, Any]:
        """Execute one worker job with persisted checkpoints, retries, and cancellation checks."""

        job_id = UUID(str(self.request.id))
        job_record = self._mark_running(job_id=job_id, attempt_count=self.request.retries + 1)
        context = JobRuntimeContext(task=self, job_record=job_record)
        LOGGER.info(
            "Tracked job execution started.",
            job_id=str(job_id),
            task_name=self.name,
            attempt_count=self.request.retries + 1,
        )

        try:
            context.ensure_not_canceled()
            result = runner(context)
            result_payload = _coerce_json_object(result)
            self._mark_completed(job_id=job_id, result_payload=result_payload)
            LOGGER.info(
                "Tracked job execution completed.",
                job_id=str(job_id),
                task_name=self.name,
            )
            return result
        except JobCancellationRequestedError as error:
            self._mark_canceled(
                job_id=job_id,
                failure_reason=error.message,
                failure_details=error.details,
            )
            LOGGER.warning(
                "Tracked job execution canceled.",
                job_id=str(job_id),
                task_name=self.name,
                reason=error.message,
            )
            return {
                "job_id": str(job_id),
                "status": JobStatus.CANCELED.value,
                "message": error.message,
            }
        except BlockedJobError as error:
            self._mark_blocked(
                job_id=job_id,
                blocking_reason=error.message,
                failure_details=error.details,
            )
            LOGGER.warning(
                "Tracked job execution blocked.",
                job_id=str(job_id),
                task_name=self.name,
                reason=error.message,
            )
            return {
                "job_id": str(job_id),
                "status": JobStatus.BLOCKED.value,
                "blocking_reason": error.message,
            }
        except Exception as error:
            decision = classify_job_failure(
                error=error,
                current_retry_count=self.request.retries,
                max_retries=self.max_retries or 0,
            )
            if decision.action is JobControlAction.RETRY:
                self._mark_retry_scheduled(
                    job_id=job_id,
                    retry_count=self.request.retries + 1,
                    failure_reason=decision.failure_reason,
                    failure_details=decision.failure_details,
                )
                raise self.retry(
                    exc=error,
                    countdown=decision.countdown_seconds,
                ) from error

            if decision.action is JobControlAction.CANCEL:
                self._mark_canceled(
                    job_id=job_id,
                    failure_reason=decision.failure_reason,
                    failure_details=decision.failure_details,
                )
            elif decision.action is JobControlAction.BLOCK:
                self._mark_blocked(
                    job_id=job_id,
                    blocking_reason=decision.failure_reason,
                    failure_details=decision.failure_details,
                )
            else:
                self._mark_failed(
                    job_id=job_id,
                    failure_reason=decision.failure_reason,
                    failure_details=decision.failure_details,
                    dead_letter=decision.dead_letter,
                )
            LOGGER.exception(
                "Tracked job execution failed.",
                job_id=str(job_id),
                task_name=self.name,
                reason=decision.failure_reason,
            )
            raise

    def _mark_running(self, *, job_id: UUID, attempt_count: int) -> JobRecord:
        """Persist the running transition for the active task."""

        trace_id = current_trace_metadata().trace_id
        return self._with_job_service(
            lambda service: service.mark_running(
                job_id=job_id,
                trace_id=trace_id,
                attempt_count=attempt_count,
            )
        )

    def _record_checkpoint(self, *, job_id: UUID, checkpoint_payload: JsonObject) -> JobRecord:
        """Persist a resumable checkpoint for the active task."""

        return self._with_job_service(
            lambda service: service.record_checkpoint(
                job_id=job_id,
                checkpoint_payload=checkpoint_payload,
            )
        )

    def _ensure_not_canceled(self, *, job_id: UUID) -> None:
        """Raise a structured worker-side cancellation error when requested."""

        self._with_job_service(lambda service: service.ensure_not_canceled(job_id=job_id))

    def _mark_retry_scheduled(
        self,
        *,
        job_id: UUID,
        retry_count: int,
        failure_reason: str,
        failure_details: JsonObject,
    ) -> JobRecord:
        """Persist retry metadata before Celery reschedules the task."""

        return self._with_job_service(
            lambda service: service.mark_retry_scheduled(
                job_id=job_id,
                retry_count=retry_count,
                failure_reason=failure_reason,
                failure_details=failure_details,
            )
        )

    def _mark_blocked(
        self,
        *,
        job_id: UUID,
        blocking_reason: str,
        failure_details: JsonObject,
    ) -> JobRecord:
        """Persist the blocked lifecycle transition for the active task."""

        return self._with_job_service(
            lambda service: service.mark_blocked(
                job_id=job_id,
                blocking_reason=blocking_reason,
                failure_details=failure_details,
            )
        )

    def _mark_completed(self, *, job_id: UUID, result_payload: JsonObject) -> JobRecord:
        """Persist the successful terminal state for the active task."""

        return self._with_job_service(
            lambda service: service.mark_completed(
                job_id=job_id,
                result_payload=result_payload,
            )
        )

    def _mark_failed(
        self,
        *,
        job_id: UUID,
        failure_reason: str,
        failure_details: JsonObject,
        dead_letter: bool,
    ) -> JobRecord:
        """Persist the failed terminal state for the active task."""

        return self._with_job_service(
            lambda service: service.mark_failed(
                job_id=job_id,
                failure_reason=failure_reason,
                failure_details=failure_details,
                dead_letter=dead_letter,
            )
        )

    def _mark_canceled(
        self,
        *,
        job_id: UUID,
        failure_reason: str,
        failure_details: JsonObject,
    ) -> JobRecord:
        """Persist the canceled terminal state for the active task."""

        return self._with_job_service(
            lambda service: service.mark_canceled(
                job_id=job_id,
                failure_reason=failure_reason,
                failure_details=failure_details,
            )
        )

    def _with_job_service(
        self,
        callback: Callable[[JobService], TJobServiceReturn],
    ) -> TJobServiceReturn:
        """Execute one job-service operation in a short-lived DB session."""

        with get_session_factory()() as session:
            service = JobService(db_session=session)
            return callback(service)


def _coerce_json_object(value: dict[str, Any]) -> JsonObject:
    """Convert a task result into a strict JSON-safe object for persistence."""

    return {
        str(key): _coerce_json_value(item)
        for key, item in value.items()
    }


def _coerce_json_value(value: Any) -> JsonValue:
    """Convert supported task result values into JSON-safe primitives recursively."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, UUID):
        return str(value)

    if isinstance(value, (datetime,)):
        return value.isoformat()

    if isinstance(value, Decimal):
        return str(value)

    if isinstance(value, dict):
        return {str(key): _coerce_json_value(item) for key, item in value.items()}

    if isinstance(value, (list, tuple)):
        return [_coerce_json_value(item) for item in value]

    return str(value)


__all__ = ["JobRuntimeContext", "TrackedJobTask"]
