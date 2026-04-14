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
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
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
        resource = _build_resource(
            settings=settings,
            service_name=service_name,
            service_version=service_version,
        )
        trace_provider = TracerProvider(resource=resource)

        if otlp_endpoint is not None:
            trace_provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=otlp_endpoint,
                        headers=otlp_headers,
                        insecure=otlp_endpoint.startswith("http://"),
                    )
                )
            )
            metric_reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=otlp_endpoint,
                    headers=otlp_headers,
                    insecure=otlp_endpoint.startswith("http://"),
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


__all__ = ["configure_observability", "get_meter", "get_tracer"]
