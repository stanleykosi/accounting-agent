"""
Purpose: Install the canonical API middleware for request-bound telemetry and safe logging.
Scope: Inbound trace activation, request correlation headers, span enrichment, operational
event emission, and guaranteed context cleanup for every FastAPI request.
Dependencies: FastAPI request/response objects, shared observability context helpers,
and operational event emitters.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from time import perf_counter

from fastapi import FastAPI, Request
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from services.observability.context import (
    REQUEST_ID_HEADER,
    activate_incoming_context,
    bind_runtime_log_context,
    release_context,
)
from services.observability.events import (
    OperationalEventName,
    OperationalEventOutcome,
    emit_operational_event,
)
from services.observability.otel import get_tracer
from starlette.responses import Response


def install_request_telemetry_middleware(app: FastAPI) -> None:
    """Attach the canonical telemetry middleware to the provided FastAPI application."""

    @app.middleware("http")
    async def request_telemetry_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Bind request context, emit telemetry, and propagate correlation headers."""

        route_group = infer_api_route_group(request.url.path)
        activation = activate_incoming_context(
            headers=dict(request.headers),
            bind_values={
                "http_method": request.method,
                "http_path": request.url.path,
                "route_group": route_group,
                "source_surface": "api",
            },
        )
        request.state.request_id = activation.request_id
        request.state.route_group = route_group
        bind_runtime_log_context(
            request_id=activation.request_id,
            http_method=request.method,
            http_path=request.url.path,
            route_group=route_group,
            source_surface="api",
        )

        start_time = perf_counter()
        response: Response | None = None
        tracer = get_tracer(__name__)
        span_name = f"{request.method} {request.url.path}"
        with tracer.start_as_current_span(span_name, kind=SpanKind.SERVER) as span:
            bind_runtime_log_context(
                request_id=activation.request_id,
                http_method=request.method,
                http_path=request.url.path,
                route_group=route_group,
                source_surface="api",
            )
            span.set_attribute("http.request.method", request.method)
            span.set_attribute("url.path", request.url.path)
            span.set_attribute("accounting_agent.request_id", activation.request_id)
            span.set_attribute("accounting_agent.route_group", route_group)
            try:
                response = await call_next(request)
                outcome = _resolve_request_outcome(response.status_code)
                _finalize_request_span(
                    request=request,
                    status_code=response.status_code,
                )
                if response.status_code >= 500:
                    span.set_status(Status(status_code=StatusCode.ERROR))
                emit_operational_event(
                    event_name=OperationalEventName.API_REQUEST,
                    outcome=outcome,
                    duration_ms=(perf_counter() - start_time) * 1000,
                    attributes={
                        "http_method": request.method,
                        "http_path": request.url.path,
                        "route_group": route_group,
                        "status_code": response.status_code,
                    },
                )
                return response
            except Exception as error:
                span.set_status(Status(status_code=StatusCode.ERROR, description=str(error)))
                _finalize_request_span(
                    request=request,
                    status_code=500,
                    error=error,
                )
                emit_operational_event(
                    event_name=OperationalEventName.API_REQUEST,
                    outcome=OperationalEventOutcome.FAILED,
                    duration_ms=(perf_counter() - start_time) * 1000,
                    error=error,
                    attributes={
                        "http_method": request.method,
                        "http_path": request.url.path,
                        "route_group": route_group,
                        "status_code": 500,
                    },
                )
                raise
            finally:
                if response is not None:
                    response.headers[REQUEST_ID_HEADER] = activation.request_id
                release_context(activation)


def infer_api_route_group(path: str) -> str:
    """Collapse dynamic API paths into stable operational route groups."""

    normalized_path = path.casefold()
    for route_group in (
        "exports",
        "reports",
        "reconciliations",
        "recommendations",
        "documents",
        "chat",
        "jobs",
        "quickbooks",
        "coa",
        "ownership",
        "close-runs",
        "entities",
        "auth",
    ):
        if route_group in normalized_path:
            return route_group.replace("-", "_")

    return "platform"


def _finalize_request_span(
    *,
    request: Request,
    status_code: int,
    error: Exception | None = None,
) -> None:
    """Stamp stable request attributes and optional exception details onto the active span."""

    route = request.scope.get("route")
    route_path = getattr(route, "path", request.url.path)
    span = trace.get_current_span()
    span.set_attribute("http.route", str(route_path))
    span.set_attribute("http.response.status_code", status_code)
    span.set_attribute("accounting_agent.route_group", infer_api_route_group(str(route_path)))
    if error is not None:
        span.record_exception(error)


def _resolve_request_outcome(status_code: int) -> OperationalEventOutcome:
    """Map HTTP response codes into stable operational outcomes."""

    if status_code >= 500:
        return OperationalEventOutcome.FAILED
    if status_code >= 400:
        return OperationalEventOutcome.BLOCKED

    return OperationalEventOutcome.SUCCEEDED


__all__ = ["infer_api_route_group", "install_request_telemetry_middleware"]
