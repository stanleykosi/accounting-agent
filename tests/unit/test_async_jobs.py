"""
Purpose: Verify the canonical Celery routing, task dispatch, and trace propagation added in Step 10.
Scope: Queue resolution, request-context propagation,
JSON payload validation, and eager worker execution.
Dependencies: apps/api/app/dependencies/tasks.py,
apps/worker/app/celery_app.py, and observability helpers.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from apps.api.app.dependencies.tasks import TaskDispatcher
from apps.worker.app.celery_app import celery_app, run_trace_probe
from services.common.settings import AppSettings
from services.jobs.celery import build_celery_configuration
from services.jobs.task_names import TaskName, TaskQueue, resolve_task_route, task_queue_names
from services.observability.context import (
    REQUEST_ID_HEADER,
    activate_incoming_context,
    current_request_id,
    current_trace_metadata,
    inject_trace_context,
    release_context,
)
from services.observability.otel import configure_observability, get_tracer


def _build_observability_settings() -> AppSettings:
    """Return app settings with an explicit OTLP endpoint for trace-propagation tests."""

    return AppSettings(
        observability=AppSettings().observability.model_copy(
            update={"otlp_endpoint": "http://127.0.0.1:4317"}
        )
    )


def test_build_celery_configuration_uses_canonical_queue_defaults() -> None:
    """Ensure the shared Celery config stays aligned with the documented queue topology."""

    configuration = build_celery_configuration(AppSettings())

    assert configuration["task_default_queue"] == TaskQueue.CONTROL.value
    assert configuration["task_routes"][TaskName.SYSTEM_TRACE_PROBE.value]["queue"] == (
        TaskQueue.CONTROL.value
    )
    assert configuration["task_routes"][TaskName.CHAT_RESUME_OPERATOR_TURN.value]["queue"] == (
        TaskQueue.CONTROL.value
    )
    assert configuration["task_routes"][TaskName.REPORTING_GENERATE_CLOSE_RUN_PACK.value][
        "queue"
    ] == TaskQueue.REPORTING.value
    assert configuration["task_routes"][TaskName.EXPORTS_GENERATE_CLOSE_RUN_PACKAGE.value][
        "queue"
    ] == TaskQueue.REPORTING.value
    assert configuration["task_routes"][TaskName.EXPORTS_ASSEMBLE_EVIDENCE_PACK.value][
        "queue"
    ] == TaskQueue.REPORTING.value
    assert task_queue_names(include_dead_letter=False) == (
        TaskQueue.CONTROL.value,
        TaskQueue.DOCUMENTS.value,
        TaskQueue.ACCOUNTING.value,
        TaskQueue.REPORTING.value,
        TaskQueue.INTEGRATIONS.value,
    )


def test_task_dispatcher_injects_request_id_and_route_metadata() -> None:
    """Ensure the API dispatcher uses the canonical route table and propagates trace headers."""

    configure_observability(_build_observability_settings(), service_name="pytest")
    activation = activate_incoming_context(headers={REQUEST_ID_HEADER: "req-123"})
    try:
        dispatcher = TaskDispatcher(celery_app=FakeCeleryApp())
        receipt = dispatcher.dispatch_task(
            task_name=TaskName.SYSTEM_TRACE_PROBE,
            kwargs={"probe_name": "unit-test"},
        )
    finally:
        release_context(activation)

    route = resolve_task_route(TaskName.SYSTEM_TRACE_PROBE)
    assert receipt.request_id == "req-123"
    assert receipt.queue_name == route.queue.value
    assert receipt.routing_key == route.routing_key
    assert receipt.trace_id is not None

    dispatched_call = dispatcher._celery_app.dispatched_calls[0]
    assert dispatched_call["headers"][REQUEST_ID_HEADER] == "req-123"
    assert dispatched_call["queue"] == route.queue.value
    assert dispatched_call["routing_key"] == route.routing_key


def test_task_dispatcher_rejects_non_json_payloads() -> None:
    """Ensure API dispatch fails fast when a task payload cannot cross the JSON transport safely."""

    dispatcher = TaskDispatcher(celery_app=FakeCeleryApp())

    with pytest.raises(TypeError):
        dispatcher.dispatch_task(
            task_name=TaskName.SYSTEM_TRACE_PROBE,
            kwargs={"probe_name": "bad-payload", "attributes": {"not-json": object()}},
        )


def test_trace_context_round_trips_between_publish_and_worker_activation() -> None:
    """Ensure injected trace headers preserve request IDs and trace IDs across async boundaries."""

    configure_observability(_build_observability_settings(), service_name="pytest")
    request_activation = activate_incoming_context(headers={REQUEST_ID_HEADER: "req-456"})
    try:
        with get_tracer(__name__).start_as_current_span("unit-test.publish"):
            outbound_headers = inject_trace_context(source_surface="api")
    finally:
        release_context(request_activation)

    worker_activation = activate_incoming_context(headers=outbound_headers)
    try:
        trace_metadata = current_trace_metadata()
        assert current_request_id() == "req-456"
        assert trace_metadata.request_id == "req-456"
        assert trace_metadata.trace_id is not None
    finally:
        release_context(worker_activation)


def test_trace_probe_task_runs_eagerly_and_returns_context_metadata() -> None:
    """Ensure the worker-side probe task exposes request and trace metadata when run eagerly."""

    configure_observability(_build_observability_settings(), service_name="pytest")
    request_activation = activate_incoming_context(headers={REQUEST_ID_HEADER: "req-789"})
    try:
        with get_tracer(__name__).start_as_current_span("unit-test.eager-probe"):
            headers = inject_trace_context(source_surface="api")
    finally:
        release_context(request_activation)

    original_always_eager = celery_app.conf.task_always_eager
    original_eager_propagates = celery_app.conf.task_eager_propagates
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    try:
        eager_result = run_trace_probe.apply(
            kwargs={"probe_name": "eager-test", "attributes": {"phase": "unit"}},
            headers=headers,
        )
    finally:
        celery_app.conf.task_always_eager = original_always_eager
        celery_app.conf.task_eager_propagates = original_eager_propagates

    payload = eager_result.get()
    assert payload["request_id"] == "req-789"
    assert payload["trace_id"] is not None
    assert payload["task_name"] == TaskName.SYSTEM_TRACE_PROBE.value
    assert payload["attributes"] == {"phase": "unit"}


@dataclass
class FakeAsyncResult:
    """Represent the minimal Celery async result surface needed by dispatcher tests."""

    id: str


class FakeCeleryApp:
    """Capture dispatched task calls without requiring a live Redis broker."""

    def __init__(self) -> None:
        """Initialize the in-memory call recorder."""

        self.dispatched_calls: list[dict[str, object]] = []

    def send_task(self, name: str, **kwargs: object) -> FakeAsyncResult:
        """Record one send-task invocation and return a stable fake async result."""

        self.dispatched_calls.append({"name": name, **kwargs})
        return FakeAsyncResult(id=str(kwargs["task_id"]))
