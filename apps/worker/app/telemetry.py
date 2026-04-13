"""
Purpose: Provide the canonical worker-side telemetry wrapper for Celery task execution.
Scope: Child-span creation, duration measurement, result/exception outcome classification,
and operational event emission for document, recommendation, reconciliation, and reporting tasks.
Dependencies: Celery retry exceptions, shared job status enums, observability event emitters,
and OpenTelemetry tracer helpers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from time import perf_counter

from celery.exceptions import Retry  # type: ignore[import-untyped]
from opentelemetry.trace import SpanKind, Status, StatusCode
from services.common.enums import JobStatus
from services.observability.events import (
    OperationalEventName,
    OperationalEventOutcome,
    emit_operational_event,
)
from services.observability.otel import get_tracer


def observe_worker_task_execution[TReturn](
    *,
    task_name: str,
    task_id: str | None,
    retries: int,
    routing_key: str | None,
    runner: Callable[[], TReturn],
) -> TReturn:
    """Execute one worker task under canonical telemetry spans and event emission."""

    event_name = infer_worker_event_name(task_name)
    attributes = {
        "task_id": task_id or "",
        "task_name": task_name,
        "retries": retries,
        "routing_key": routing_key or "",
        "workflow_area": event_name.value.split(".")[0],
    }
    tracer = get_tracer(__name__)
    start_time = perf_counter()

    with tracer.start_as_current_span(
        f"worker.task.execute.{task_name}",
        kind=SpanKind.INTERNAL,
        attributes=attributes,
    ) as span:
        try:
            result = runner()
        except Retry as error:
            span.set_status(Status(StatusCode.ERROR, "Retry scheduled."))
            span.record_exception(error)
            emit_operational_event(
                event_name=event_name,
                outcome=OperationalEventOutcome.RETRY_SCHEDULED,
                duration_ms=(perf_counter() - start_time) * 1000,
                error=error,
                attributes=attributes,
            )
            raise
        except Exception as error:
            span.set_status(Status(StatusCode.ERROR, str(error)))
            span.record_exception(error)
            emit_operational_event(
                event_name=event_name,
                outcome=OperationalEventOutcome.FAILED,
                duration_ms=(perf_counter() - start_time) * 1000,
                error=error,
                attributes=attributes,
            )
            raise

        outcome = _infer_task_outcome_from_result(result)
        if outcome is not OperationalEventOutcome.SUCCEEDED:
            span.set_status(Status(StatusCode.ERROR, f"Task outcome={outcome.value}"))
        emit_operational_event(
            event_name=event_name,
            outcome=outcome,
            duration_ms=(perf_counter() - start_time) * 1000,
            attributes=attributes,
        )
        return result


def infer_worker_event_name(task_name: str) -> OperationalEventName:
    """Map a canonical Celery task name into its operational dashboard family."""

    if task_name.startswith("documents."):
        return OperationalEventName.DOCUMENT_PIPELINE
    if task_name.startswith("accounting."):
        return OperationalEventName.RECOMMENDATION_PIPELINE
    if task_name.startswith("reconciliation."):
        return OperationalEventName.RECONCILIATION_PIPELINE
    if task_name.startswith("reporting."):
        return OperationalEventName.REPORTING_PIPELINE
    if task_name.startswith("exports."):
        return OperationalEventName.EXPORT_PIPELINE

    return OperationalEventName.WORKER_TASK


def _infer_task_outcome_from_result[TReturn](result: TReturn) -> OperationalEventOutcome:
    """Infer a task outcome from canonical job payloads returned by tracked worker tasks."""

    if not isinstance(result, Mapping):
        return OperationalEventOutcome.SUCCEEDED

    raw_status = result.get("status")
    if not isinstance(raw_status, str):
        return OperationalEventOutcome.SUCCEEDED

    if raw_status == JobStatus.BLOCKED.value:
        return OperationalEventOutcome.BLOCKED
    if raw_status == JobStatus.CANCELED.value:
        return OperationalEventOutcome.CANCELED
    if raw_status == JobStatus.FAILED.value:
        return OperationalEventOutcome.FAILED

    return OperationalEventOutcome.SUCCEEDED


__all__ = ["infer_worker_event_name", "observe_worker_task_execution"]
