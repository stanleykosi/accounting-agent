"""
Purpose: Define strict API contracts for background-job inspection, cancellation,
and resume flows.
Scope: Job summaries, detail payloads, filtering responses, and operator-issued
control requests across API and worker surfaces.
Dependencies: Shared API contract defaults, canonical job status enum, and JSON-safe types.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field
from services.common.enums import JobStatus
from services.common.types import JsonObject
from services.contracts.api_models import ContractModel


class CancelJobRequest(ContractModel):
    """Capture an operator-issued cancel request for one background job."""

    reason: str = Field(
        min_length=1,
        max_length=500,
        description="Operator-facing reason recorded with the cancellation request.",
    )


class ResumeJobRequest(ContractModel):
    """Capture an operator-issued resume request for one resumable background job."""

    reason: str = Field(
        min_length=1,
        max_length=500,
        description="Operator-facing reason recorded when resuming from a checkpoint.",
    )


class JobSummary(ContractModel):
    """Describe one durable background job at list-response granularity."""

    id: str = Field(description="Stable UUID for the job and its Celery task identifier.")
    entity_id: str | None = Field(default=None, description="Owning entity workspace UUID.")
    close_run_id: str | None = Field(default=None, description="Owning close run UUID.")
    document_id: str | None = Field(default=None, description="Related document UUID if any.")
    task_name: str = Field(min_length=1, description="Canonical background task name.")
    queue_name: str = Field(min_length=1, description="Queue lane used for execution.")
    routing_key: str = Field(min_length=1, description="Routing key used for execution.")
    status: JobStatus = Field(description="Current durable lifecycle status.")
    retry_count: int = Field(ge=0, description="Number of retries already consumed.")
    max_retries: int = Field(ge=0, description="Configured retry budget for this job.")
    attempt_count: int = Field(ge=0, description="Total execution attempts started so far.")
    failure_reason: str | None = Field(
        default=None,
        description="Current failure summary when the job is blocked or failed.",
    )
    blocking_reason: str | None = Field(
        default=None,
        description="Recovery-oriented reason when the job is blocked.",
    )
    cancellation_requested_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when a cancel request was issued, if any.",
    )
    dead_lettered_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when the job exhausted retries and entered dead-letter state.",
    )
    resumed_from_job_id: str | None = Field(
        default=None,
        description="Prior job UUID when this row is a resumed execution.",
    )
    started_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when the current job began running.",
    )
    completed_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when the job reached a terminal state.",
    )
    created_at: datetime = Field(description="UTC timestamp when the job row was created.")
    updated_at: datetime = Field(description="UTC timestamp when the job row was last updated.")


class JobDetail(JobSummary):
    """Describe one durable background job with payloads, checkpoints, and results."""

    actor_user_id: str | None = Field(default=None, description="User who triggered the job.")
    canceled_by_user_id: str | None = Field(
        default=None,
        description="User who requested or completed cancellation, if applicable.",
    )
    payload: JsonObject = Field(
        description="JSON-safe task payload recorded at dispatch time.",
    )
    checkpoint_payload: JsonObject = Field(
        description="Latest resumable checkpoint persisted by the worker.",
    )
    result_payload: JsonObject | None = Field(
        default=None,
        description=(
            "JSON-safe completion payload when the job succeeded or returned a "
            "terminal state."
        ),
    )
    failure_details: JsonObject | None = Field(
        default=None,
        description="Structured failure metadata recorded for retries or terminal failures.",
    )
    trace_id: str | None = Field(
        default=None,
        description="Trace identifier linked to the dispatch and worker execution.",
    )


class JobListResponse(ContractModel):
    """Return jobs for one entity scope in deterministic recent-first order."""

    jobs: tuple[JobSummary, ...] = Field(
        default=(),
        description="Jobs visible to the caller for the selected scope.",
    )


class ResumeJobResponse(ContractModel):
    """Return the new queued job created from a resumable failed or blocked execution."""

    resumed_job: JobSummary = Field(description="New queued job created from a saved checkpoint.")


__all__ = [
    "CancelJobRequest",
    "JobDetail",
    "JobListResponse",
    "JobSummary",
    "ResumeJobRequest",
    "ResumeJobResponse",
]
