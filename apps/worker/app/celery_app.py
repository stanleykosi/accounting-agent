"""
Purpose: Configure the canonical Celery worker application
and seed task set for the local demo stack.
Scope: Worker bootstrap hooks, trace-aware task execution,
retry behavior, and the runtime probe task.
Dependencies: Celery, shared job configuration, shared observability helpers, and backend settings.
"""

from __future__ import annotations

from typing import Any

from celery import Task, signals
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import BaseModel, ConfigDict, Field, field_validator
from services.common.logging import configure_logging, get_logger
from services.common.settings import get_settings
from services.jobs.celery import create_celery_app
from services.jobs.task_names import TaskName, resolve_task_route, task_queue_names
from services.observability.context import (
    activate_incoming_context,
    bind_runtime_log_context,
    current_request_id,
    current_trace_metadata,
    normalize_headers,
    release_context,
)
from services.observability.otel import configure_observability, get_tracer

SETTINGS = get_settings()
LOGGER = get_logger(__name__)
celery_app = create_celery_app(settings=SETTINGS)


class TraceProbePayload(BaseModel):
    """Validate the runtime probe task payload used for end-to-end async verification."""

    model_config = ConfigDict(extra="forbid")

    probe_name: str = Field(min_length=1, max_length=120)
    attributes: dict[str, str] = Field(default_factory=dict)
    fail_until_attempt: int = Field(default=0, ge=0, le=10)

    @field_validator("attributes")
    @classmethod
    def validate_attribute_keys_and_values(cls, value: dict[str, str]) -> dict[str, str]:
        """Reject empty attribute keys and normalize surrounding whitespace in the probe payload."""

        normalized: dict[str, str] = {}
        for key, item_value in value.items():
            stripped_key = key.strip()
            if not stripped_key:
                raise ValueError("Probe attribute keys cannot be empty.")

            normalized[stripped_key] = item_value.strip()

        return normalized


class ObservedTask(Task):  # type: ignore[misc]
    """Wrap Celery task execution in canonical trace propagation and structured log binding."""

    abstract = True

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Execute the task under the propagated parent trace and fail-fast logging context."""

        headers = normalize_headers(getattr(self.request, "headers", None))
        activation = activate_incoming_context(
            headers=headers,
            bind_values={
                "celery_task_id": self.request.id,
                "celery_task_name": self.name,
                "source_surface": "worker",
            },
        )
        tracer = get_tracer(__name__)
        delivery_info = getattr(self.request, "delivery_info", {}) or {}

        with tracer.start_as_current_span(
            f"celery.consume.{self.name}",
            kind=SpanKind.CONSUMER,
            attributes={
                "messaging.system": "redis",
                "messaging.destination_kind": "queue",
                "messaging.destination.name": str(delivery_info.get("exchange", "")),
                "messaging.message.id": str(self.request.id),
                "messaging.operation": "process",
                "messaging.rabbitmq.routing_key": str(delivery_info.get("routing_key", "")),
                "messaging.celery.task_name": self.name,
            },
        ) as span:
            bind_runtime_log_context(
                celery_task_id=self.request.id,
                celery_task_name=self.name,
                source_surface="worker",
            )
            try:
                return self.run(*args, **kwargs)
            except Exception as error:
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR, str(error)))
                LOGGER.exception(
                    "Celery task execution failed.",
                    task_id=self.request.id,
                    task_name=self.name,
                    retries=self.request.retries,
                )
                raise
            finally:
                release_context(activation)


def configure_worker_process_observability(**_: Any) -> None:
    """Initialize logging and OpenTelemetry inside each worker process after fork."""

    configure_logging(SETTINGS, service_name="worker")
    configure_observability(SETTINGS, service_name="worker")
    LOGGER.info(
        "Worker process observability configured.",
        concurrency=SETTINGS.worker.concurrency,
        queues=task_queue_names(include_dead_letter=False),
    )


def log_task_retry(*, request: Any, reason: BaseException, **__: Any) -> None:
    """Emit structured retry diagnostics for tasks that will be rescheduled by Celery."""

    LOGGER.warning(
        "Celery task scheduled for retry.",
        task_id=request.id,
        task_name=request.task,
        retries=request.retries,
        reason=str(reason),
    )


def _run_trace_probe(
    self: ObservedTask,
    *,
    probe_name: str,
    attributes: dict[str, str] | None = None,
    fail_until_attempt: int = 0,
) -> dict[str, Any]:
    """Execute a trace-aware runtime probe so operators can verify API-to-worker propagation."""

    payload = TraceProbePayload.model_validate(
        {
            "probe_name": probe_name,
            "attributes": attributes or {},
            "fail_until_attempt": fail_until_attempt,
        }
    )
    if self.request.retries < payload.fail_until_attempt:
        raise RuntimeError(
            "Trace probe requested a retry before success to validate retry instrumentation."
        )

    trace_metadata = current_trace_metadata()
    LOGGER.info(
        "Trace probe task completed.",
        task_id=self.request.id,
        task_name=self.name,
        probe_name=payload.probe_name,
        retries=self.request.retries,
        request_id=current_request_id(),
        trace_id=trace_metadata.trace_id,
    )
    return {
        "attributes": payload.attributes,
        "probe_name": payload.probe_name,
        "request_id": current_request_id(),
        "retries": self.request.retries,
        "task_id": self.request.id,
        "task_name": self.name,
        "trace_id": trace_metadata.trace_id,
    }


signals.worker_process_init.connect(configure_worker_process_observability)
signals.task_retry.connect(log_task_retry)
run_trace_probe = celery_app.task(
    bind=True,
    base=ObservedTask,
    name=TaskName.SYSTEM_TRACE_PROBE.value,
    autoretry_for=(RuntimeError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=resolve_task_route(TaskName.SYSTEM_TRACE_PROBE).max_retries,
)(_run_trace_probe)

from apps.worker.app.tasks import extract_documents as _extract_documents  # noqa: E402,F401
from apps.worker.app.tasks import parse_documents as _parse_documents  # noqa: E402,F401

__all__ = ["ObservedTask", "celery_app", "run_trace_probe"]
