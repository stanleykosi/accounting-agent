"""
Purpose: Define the canonical retry, blocking, cancellation, and dead-letter policy
used by long-running worker tasks.
Scope: Classify job failures into retryable, blocked, canceled, or terminal outcomes
without letting task-specific code invent parallel lifecycle rules.
Dependencies: Shared job status enum, JSON-safe type aliases, and canonical task routing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from services.common.enums import JobStatus
from services.common.types import JsonObject
from services.jobs.task_names import TaskQueue


class JobControlAction(StrEnum):
    """Enumerate the canonical lifecycle actions chosen after one task failure."""

    RETRY = "retry"
    FAIL = "fail"
    BLOCK = "block"
    CANCEL = "cancel"


class JobExecutionError(Exception):
    """Describe a structured worker-side failure with a safe operator-facing summary."""

    code = "job_execution_error"

    def __init__(
        self,
        message: str,
        *,
        details: JsonObject | None = None,
    ) -> None:
        """Capture a safe failure message and optional JSON-safe details."""

        super().__init__(message)
        self.message = message
        self.details = details or {}


class RetryableJobError(JobExecutionError):
    """Mark a failure as retryable within the configured retry budget."""

    code = "retryable_job_error"


class BlockedJobError(JobExecutionError):
    """Mark a failure as blocked pending explicit operator recovery."""

    code = "blocked_job_error"


class PermanentJobError(JobExecutionError):
    """Mark a failure as terminal and immediately eligible for dead-letter handling."""

    code = "permanent_job_error"


class JobCancellationRequestedError(JobExecutionError):
    """Raise when the worker observes an operator-issued cancellation request."""

    code = "job_cancellation_requested"


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """Describe the canonical lifecycle decision chosen after one worker failure."""

    action: JobControlAction
    status: JobStatus
    failure_reason: str
    failure_details: JsonObject
    countdown_seconds: int | None
    dead_letter: bool
    dead_letter_queue_name: str | None


def classify_job_failure(
    *,
    error: BaseException,
    current_retry_count: int,
    max_retries: int,
) -> RetryDecision:
    """Translate one task failure into the platform's canonical lifecycle decision."""

    base_details: JsonObject = {
        "exception_type": error.__class__.__name__,
        "message": str(error),
    }

    if isinstance(error, JobCancellationRequestedError):
        return RetryDecision(
            action=JobControlAction.CANCEL,
            status=JobStatus.CANCELED,
            failure_reason=error.message,
            failure_details={**base_details, **error.details},
            countdown_seconds=None,
            dead_letter=False,
            dead_letter_queue_name=None,
        )

    if isinstance(error, BlockedJobError):
        return RetryDecision(
            action=JobControlAction.BLOCK,
            status=JobStatus.BLOCKED,
            failure_reason=error.message,
            failure_details={**base_details, **error.details},
            countdown_seconds=None,
            dead_letter=False,
            dead_letter_queue_name=None,
        )

    if isinstance(error, PermanentJobError):
        return RetryDecision(
            action=JobControlAction.FAIL,
            status=JobStatus.FAILED,
            failure_reason=error.message,
            failure_details={**base_details, **error.details},
            countdown_seconds=None,
            dead_letter=True,
            dead_letter_queue_name=TaskQueue.DEAD_LETTER.value,
        )

    if current_retry_count < max_retries:
        return RetryDecision(
            action=JobControlAction.RETRY,
            status=JobStatus.QUEUED,
            failure_reason=str(error),
            failure_details=base_details,
            countdown_seconds=compute_retry_delay_seconds(current_retry_count=current_retry_count),
            dead_letter=False,
            dead_letter_queue_name=None,
        )

    return RetryDecision(
        action=JobControlAction.FAIL,
        status=JobStatus.FAILED,
        failure_reason=str(error),
        failure_details=base_details,
        countdown_seconds=None,
        dead_letter=True,
        dead_letter_queue_name=TaskQueue.DEAD_LETTER.value,
    )


def compute_retry_delay_seconds(*, current_retry_count: int) -> int:
    """Return the canonical exponential retry delay with a sane local-demo cap."""

    capped_attempt = min(current_retry_count + 1, 6)
    delay_seconds = 2 ** int(capped_attempt)
    return int(min(delay_seconds, 120))


__all__ = [
    "BlockedJobError",
    "JobCancellationRequestedError",
    "JobControlAction",
    "JobExecutionError",
    "PermanentJobError",
    "RetryDecision",
    "RetryableJobError",
    "classify_job_failure",
    "compute_retry_delay_seconds",
]
