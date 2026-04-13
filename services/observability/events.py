"""
Purpose: Emit canonical operational telemetry events across API and worker execution paths.
Scope: Operational event naming, metrics emission, current-span annotations, and audit-safe
structured log records for request and task lifecycles.
Dependencies: Shared trace context, OpenTelemetry meter access, and observability redaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from opentelemetry import trace
from services.observability.context import current_trace_metadata
from services.observability.otel import get_meter
from services.observability.redaction import redact_log_payload

_METER = get_meter(__name__)
_EVENT_COUNTER = _METER.create_counter(
    name="accounting_agent_operational_events_total",
    description="Count of canonical API and worker operational events.",
)
_FAILURE_COUNTER = _METER.create_counter(
    name="accounting_agent_operational_failures_total",
    description="Count of failed, blocked, canceled, or retried operational events.",
)
_DURATION_HISTOGRAM = _METER.create_histogram(
    name="accounting_agent_operational_duration_ms",
    description="Execution duration for canonical operational events in milliseconds.",
    unit="ms",
)
_METRIC_ATTRIBUTE_ALLOWLIST = frozenset(
    {
        "error_type",
        "event_name",
        "http_method",
        "outcome",
        "retries",
        "route_group",
        "routing_key",
        "source_surface",
        "status_code",
        "task_name",
        "workflow_area",
    }
)


class OperationalEventName(StrEnum):
    """Enumerate the stable operational event families used in logs and dashboards."""

    API_REQUEST = "api.request"
    WORKER_TASK = "worker.task"
    DOCUMENT_PIPELINE = "documents.pipeline"
    RECOMMENDATION_PIPELINE = "recommendations.pipeline"
    RECONCILIATION_PIPELINE = "reconciliation.pipeline"
    REPORTING_PIPELINE = "reporting.pipeline"
    EXPORT_PIPELINE = "exports.pipeline"


class OperationalEventOutcome(StrEnum):
    """Enumerate lifecycle outcomes emitted for requests and worker tasks."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELED = "canceled"
    RETRY_SCHEDULED = "retry_scheduled"


@dataclass(frozen=True, slots=True)
class OperationalEventReceipt:
    """Describe one emitted operational event for tests and higher-level callers."""

    event_name: str
    outcome: str
    duration_ms: float | None
    request_id: str | None
    trace_id: str | None
    attributes: dict[str, str | int | float | bool]


def emit_operational_event(
    *,
    event_name: OperationalEventName | str,
    outcome: OperationalEventOutcome | str,
    attributes: dict[str, Any] | None = None,
    duration_ms: float | None = None,
    error: BaseException | None = None,
) -> OperationalEventReceipt:
    """Emit one canonical operational event to logs, metrics, and the active trace span."""

    trace_metadata = current_trace_metadata()
    safe_attributes = _normalize_attributes(attributes or {})
    safe_attributes.setdefault("event_name", str(event_name))
    safe_attributes.setdefault("outcome", str(outcome))
    if trace_metadata.request_id is not None:
        safe_attributes.setdefault("request_id", trace_metadata.request_id)
    if trace_metadata.trace_id is not None:
        safe_attributes.setdefault("trace_id", trace_metadata.trace_id)
    if error is not None:
        safe_attributes["error_type"] = type(error).__name__
        safe_attributes["error_message"] = str(redact_log_payload(str(error)))

    if duration_ms is not None:
        safe_attributes["duration_ms"] = round(duration_ms, 2)

    current_span = trace.get_current_span()
    if current_span.is_recording():
        current_span.add_event(
            name="accounting_agent.operational_event",
            attributes=safe_attributes,
        )

    metric_attributes = _build_metric_attributes(safe_attributes)
    _EVENT_COUNTER.add(1, metric_attributes)
    if duration_ms is not None:
        _DURATION_HISTOGRAM.record(duration_ms, metric_attributes)
    if str(outcome) != OperationalEventOutcome.SUCCEEDED.value:
        _FAILURE_COUNTER.add(1, metric_attributes)

    _get_logger().info("operational_event_emitted", **safe_attributes)
    return OperationalEventReceipt(
        event_name=str(event_name),
        outcome=str(outcome),
        duration_ms=duration_ms,
        request_id=trace_metadata.request_id,
        trace_id=trace_metadata.trace_id,
        attributes=metric_attributes,
    )


def _normalize_attributes(attributes: dict[str, Any]) -> dict[str, str | int | float | bool]:
    """Convert caller-provided event attributes into OpenTelemetry-safe primitive values."""

    redacted_attributes = redact_log_payload(attributes)
    normalized: dict[str, str | int | float | bool] = {}
    for key, value in redacted_attributes.items():
        normalized_value = _normalize_attribute_value(value)
        if normalized_value is None:
            continue
        normalized[str(key)] = normalized_value

    return normalized


def _normalize_attribute_value(value: Any) -> str | int | float | bool | None:
    """Normalize a metric/log attribute value or drop it if it cannot be represented safely."""

    if value is None:
        return None

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return value

    if isinstance(value, str):
        return value

    return str(value)


def _build_metric_attributes(
    attributes: dict[str, str | int | float | bool],
) -> dict[str, str | int | float | bool]:
    """Keep only bounded-cardinality attributes on metrics while leaving logs fully detailed."""

    return {
        key: value
        for key, value in attributes.items()
        if key in _METRIC_ATTRIBUTE_ALLOWLIST
    }


def _get_logger() -> Any:
    """Resolve the structured logger lazily so events stay safe during logging bootstrap."""

    from services.common.logging import get_logger

    return get_logger(__name__)


__all__ = [
    "OperationalEventName",
    "OperationalEventOutcome",
    "OperationalEventReceipt",
    "emit_operational_event",
]
