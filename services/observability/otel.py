"""
Purpose: Bootstrap OpenTelemetry traces and metrics for the canonical local runtime.
Scope: Process-local provider setup, FastAPI instrumentation, and tracer/meter access helpers.
Dependencies: OpenTelemetry SDK/exporters, FastAPI instrumentation, and services/common/settings.py.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter as GrpcOTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter as GrpcOTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter as HttpOTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter as HttpOTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import (
    DEPLOYMENT_ENVIRONMENT,
    SERVICE_NAME,
    SERVICE_NAMESPACE,
    Resource,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from services.common.settings import AppSettings
from services.common.types import OtlpExportProtocol

_CONFIGURED_PID: int | None = None
_INSTRUMENTED_APP_IDS: set[int] = set()


def configure_observability(
    settings: AppSettings,
    *,
    service_name: str,
    service_version: str = "0.1.0",
    app: FastAPI | None = None,
) -> None:
    """Configure process-local OpenTelemetry providers and optionally instrument a FastAPI app."""

    global _CONFIGURED_PID

    current_pid = os.getpid()
    if _CONFIGURED_PID != current_pid:
        otlp_endpoint = settings.observability.otlp_endpoint
        otlp_headers = settings.observability.otlp_headers or None
        otlp_protocol = settings.observability.resolve_otlp_protocol()
        resource = _build_resource(
            settings=settings,
            service_name=service_name,
            service_version=service_version,
        )
        trace_provider = TracerProvider(resource=resource)

        if otlp_endpoint is not None:
            trace_exporter = _build_trace_exporter(
                endpoint=otlp_endpoint,
                protocol=otlp_protocol,
                headers=otlp_headers,
            )
            trace_provider.add_span_processor(
                BatchSpanProcessor(trace_exporter)
            )
            metric_reader = PeriodicExportingMetricReader(
                _build_metric_exporter(
                    endpoint=otlp_endpoint,
                    protocol=otlp_protocol,
                    headers=otlp_headers,
                ),
                export_interval_millis=30_000,
            )
            meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        else:
            meter_provider = MeterProvider(resource=resource)

        metrics.set_meter_provider(meter_provider)
        trace.set_tracer_provider(trace_provider)
        LoggingInstrumentor().instrument(set_logging_format=False)

        _CONFIGURED_PID = current_pid

    if app is not None and id(app) not in _INSTRUMENTED_APP_IDS:
        FastAPIInstrumentor.instrument_app(app)
        _INSTRUMENTED_APP_IDS.add(id(app))


def get_tracer(name: str) -> Any:
    """Return a tracer bound to the given module or subsystem name."""

    return trace.get_tracer(name)


def get_meter(name: str) -> Any:
    """Return a meter bound to the given module or subsystem name."""

    return metrics.get_meter(name)


def _build_resource(
    *,
    settings: AppSettings,
    service_name: str,
    service_version: str,
) -> Resource:
    """Build the shared OpenTelemetry resource attributes for this process."""

    return Resource.create(
        {
            DEPLOYMENT_ENVIRONMENT: settings.runtime.environment.value,
            SERVICE_NAME: service_name,
            SERVICE_NAMESPACE: settings.observability.service_namespace,
            "service.version": service_version,
        }
    )


def _build_trace_exporter(
    *,
    endpoint: str,
    protocol: OtlpExportProtocol | None,
    headers: dict[str, str] | None,
) -> Any:
    """Build the OTLP trace exporter that matches the resolved transport protocol."""

    if protocol is OtlpExportProtocol.HTTP_PROTOBUF:
        return HttpOTLPSpanExporter(
            endpoint=_resolve_http_signal_endpoint(endpoint, signal_path="/v1/traces"),
            headers=headers,
        )

    return GrpcOTLPSpanExporter(
        endpoint=endpoint,
        headers=headers,
        insecure=endpoint.startswith("http://"),
    )


def _build_metric_exporter(
    *,
    endpoint: str,
    protocol: OtlpExportProtocol | None,
    headers: dict[str, str] | None,
) -> Any:
    """Build the OTLP metric exporter that matches the resolved transport protocol."""

    if protocol is OtlpExportProtocol.HTTP_PROTOBUF:
        return HttpOTLPMetricExporter(
            endpoint=_resolve_http_signal_endpoint(endpoint, signal_path="/v1/metrics"),
            headers=headers,
        )

    return GrpcOTLPMetricExporter(
        endpoint=endpoint,
        headers=headers,
        insecure=endpoint.startswith("http://"),
    )


def _resolve_http_signal_endpoint(endpoint: str, *, signal_path: str) -> str:
    """Append the required OTLP HTTP signal path unless the endpoint already targets it."""

    stripped_endpoint = endpoint.rstrip("/")
    if stripped_endpoint.endswith(signal_path):
        return stripped_endpoint

    return f"{stripped_endpoint}{signal_path}"


__all__ = ["configure_observability", "get_meter", "get_tracer"]
