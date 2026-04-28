"""
Purpose: Provide the canonical Celery task dispatcher shared by API and worker runtimes.
Scope: Queue/routing resolution, JSON payload validation, trace propagation, and broker receipts.
Dependencies: Celery, services/jobs/*.py, and services/observability/context.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import uuid4

from celery import Celery
from opentelemetry.trace import SpanKind
from services.common.logging import get_logger
from services.jobs.task_names import TaskName, resolve_task_name, resolve_task_route
from services.observability.context import current_trace_metadata, inject_trace_context
from services.observability.otel import get_tracer

LOGGER = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TaskDispatchReceipt:
    """Describe one accepted background-job dispatch for internal workflows and API responses."""

    task_id: str
    task_name: str
    queue_name: str
    routing_key: str
    request_id: str
    trace_id: str | None


class TaskDispatcher:
    """Dispatch canonical background tasks through Celery with trace propagation and validation."""

    def __init__(self, *, celery_app: Celery, source_surface: str = "api") -> None:
        """Capture the Celery client and logical source surface used for dispatch."""

        self._celery_app = celery_app
        self._source_surface = source_surface
        self._tracer = get_tracer(__name__)

    def dispatch_task(
        self,
        *,
        task_name: TaskName | str,
        args: tuple[object, ...] | None = None,
        kwargs: dict[str, object] | None = None,
        countdown: int | None = None,
        task_id: str | None = None,
    ) -> TaskDispatchReceipt:
        """Validate and enqueue one canonical task into the correct queue lane."""

        resolved_task_name = resolve_task_name(task_name)
        route_definition = resolve_task_route(resolved_task_name)
        normalized_args = tuple(args or ())
        normalized_kwargs = dict(kwargs or {})
        _ensure_json_payload_serializable(args=normalized_args, kwargs=normalized_kwargs)
        dispatched_task_id = task_id or str(uuid4())

        with self._tracer.start_as_current_span(
            f"celery.publish.{resolved_task_name.value}",
            kind=SpanKind.PRODUCER,
            attributes={
                "messaging.system": "redis",
                "messaging.destination_kind": "queue",
                "messaging.destination.name": route_definition.queue.value,
                "messaging.message_id": dispatched_task_id,
                "messaging.operation": "publish",
                "messaging.rabbitmq.routing_key": route_definition.routing_key,
                "messaging.celery.task_name": resolved_task_name.value,
            },
        ) as span:
            headers = inject_trace_context(source_surface=self._source_surface)
            async_result = self._celery_app.send_task(
                resolved_task_name.value,
                args=normalized_args,
                kwargs=normalized_kwargs,
                countdown=countdown,
                headers=headers,
                queue=route_definition.queue.value,
                routing_key=route_definition.routing_key,
                task_id=dispatched_task_id,
            )
            trace_metadata = current_trace_metadata()
            span.set_attribute("messaging.message.conversation_id", headers["x-request-id"])
            span.set_attribute("messaging.message.id", async_result.id)

        LOGGER.info(
            "Background task dispatched.",
            task_id=async_result.id,
            task_name=resolved_task_name.value,
            queue_name=route_definition.queue.value,
            routing_key=route_definition.routing_key,
            request_id=headers["x-request-id"],
            trace_id=trace_metadata.trace_id,
            source_surface=self._source_surface,
        )
        return TaskDispatchReceipt(
            task_id=async_result.id,
            task_name=resolved_task_name.value,
            queue_name=route_definition.queue.value,
            routing_key=route_definition.routing_key,
            request_id=headers["x-request-id"],
            trace_id=trace_metadata.trace_id,
        )


def _ensure_json_payload_serializable(
    *,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    """Fail fast when a task payload cannot be serialized through the JSON-only Celery transport."""

    try:
        json.dumps({"args": args, "kwargs": kwargs})
    except TypeError as error:
        message = (
            "Background task payload must be JSON serializable. "
            "Convert complex objects into typed primitives before dispatch."
        )
        raise TypeError(message) from error


__all__ = ["TaskDispatchReceipt", "TaskDispatcher"]
