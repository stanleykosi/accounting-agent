"""
Purpose: Provide one canonical trace and request-context propagation path
across API and worker boundaries.
Scope: Request ID management, W3C trace-context injection/extraction,
and structured log-context binding.
Dependencies: OpenTelemetry context propagation plus services/common/logging.py for contextual logs.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.context import attach, detach
from opentelemetry.context.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from services.common.logging import bind_log_context, clear_log_context

REQUEST_ID_HEADER = "x-request-id"
SOURCE_SURFACE_HEADER = "x-accounting-agent-source-surface"

_REQUEST_ID_CONTEXT: ContextVar[str | None] = ContextVar(
    "accounting_agent_request_id",
    default=None,
)


@dataclass(frozen=True, slots=True)
class TraceMetadata:
    """Describe the currently active trace identifiers for logs, responses, and job receipts."""

    request_id: str | None
    trace_id: str | None
    span_id: str | None


@dataclass(frozen=True, slots=True)
class ContextActivation:
    """Capture the tokens required to unwind an activated inbound context safely."""

    request_id: str
    request_id_token: Token[str | None]
    otel_token: Token[Context]


def generate_request_id() -> str:
    """Return a deterministic UUID4 request identifier for cross-surface correlation."""

    return str(uuid4())


def current_request_id() -> str | None:
    """Return the currently bound request ID if one exists in this execution context."""

    return _REQUEST_ID_CONTEXT.get()


def current_trace_metadata() -> TraceMetadata:
    """Return trace identifiers for the active span, omitting invalid OpenTelemetry contexts."""

    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return TraceMetadata(
            request_id=current_request_id(),
            trace_id=None,
            span_id=None,
        )

    return TraceMetadata(
        request_id=current_request_id(),
        trace_id=f"{span_context.trace_id:032x}",
        span_id=f"{span_context.span_id:016x}",
    )


def inject_trace_context(
    *,
    headers: dict[str, str] | None = None,
    request_id: str | None = None,
    source_surface: str | None = None,
) -> dict[str, str]:
    """Inject the current trace context plus request metadata into message or response headers."""

    carrier = dict(headers or {})
    resolved_request_id = request_id or current_request_id() or generate_request_id()
    carrier[REQUEST_ID_HEADER] = resolved_request_id
    if source_surface is not None:
        carrier[SOURCE_SURFACE_HEADER] = source_surface

    TraceContextTextMapPropagator().inject(carrier)
    return carrier


def activate_incoming_context(
    *,
    headers: dict[str, Any] | None,
    fallback_request_id: str | None = None,
    bind_values: dict[str, Any] | None = None,
) -> ContextActivation:
    """Extract inbound trace headers, bind request metadata, and attach the parent trace context."""

    normalized_headers = normalize_headers(headers)
    request_id = (
        normalized_headers.get(REQUEST_ID_HEADER)
        or fallback_request_id
        or generate_request_id()
    )
    extracted_context = TraceContextTextMapPropagator().extract(normalized_headers)
    otel_token = attach(extracted_context)
    request_id_token = _REQUEST_ID_CONTEXT.set(request_id)

    bind_runtime_log_context(
        request_id=request_id,
        **(bind_values or {}),
    )
    return ContextActivation(
        request_id=request_id,
        request_id_token=request_id_token,
        otel_token=otel_token,
    )


def bind_runtime_log_context(*, request_id: str | None = None, **values: Any) -> TraceMetadata:
    """Bind trace metadata and caller-supplied values into the shared structured log context."""

    metadata = current_trace_metadata()
    bind_log_context(
        **values,
        request_id=request_id or metadata.request_id,
        trace_id=metadata.trace_id,
        span_id=metadata.span_id,
    )
    return metadata


def release_context(activation: ContextActivation) -> None:
    """Clear bound request/log context and detach the propagated OpenTelemetry context."""

    clear_log_context()
    _REQUEST_ID_CONTEXT.reset(activation.request_id_token)
    detach(activation.otel_token)


def normalize_headers(headers: dict[str, Any] | None) -> dict[str, str]:
    """Coerce inbound header-like mappings into a lower-level string dictionary."""

    if not headers:
        return {}

    normalized: dict[str, str] = {}
    for key, value in headers.items():
        if value is None:
            continue

        if isinstance(value, bytes):
            normalized[str(key)] = value.decode("utf-8", errors="replace")
            continue

        normalized[str(key)] = str(value)

    return normalized


__all__ = [
    "REQUEST_ID_HEADER",
    "SOURCE_SURFACE_HEADER",
    "ContextActivation",
    "TraceMetadata",
    "activate_incoming_context",
    "bind_runtime_log_context",
    "current_request_id",
    "current_trace_metadata",
    "generate_request_id",
    "inject_trace_context",
    "normalize_headers",
    "release_context",
]
