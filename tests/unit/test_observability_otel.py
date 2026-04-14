"""
Purpose: Verify OTLP exporter configuration supports both bare collector endpoints and
authenticated hosted vendors.
Scope: Shared OpenTelemetry bootstrap and OTLP exporter argument wiring.
Dependencies: services/observability/otel.py and services/common/settings.py.
"""

from __future__ import annotations

from dataclasses import dataclass

import services.observability.otel as otel_module
from services.common.settings import AppSettings


def test_configure_observability_passes_optional_otlp_headers(monkeypatch) -> None:
    """Ensure hosted OTLP headers are forwarded to both trace and metric exporters."""

    captured_arguments: dict[str, dict[str, object]] = {}

    @dataclass
    class FakeBatchSpanProcessor:
        exporter: object

    @dataclass
    class FakePeriodicExportingMetricReader:
        exporter: object
        export_interval_millis: int

    class FakeTracerProvider:
        def __init__(self, *, resource: object) -> None:
            self.resource = resource

        def add_span_processor(self, processor: object) -> None:
            captured_arguments["span_processor"] = {"processor": processor}

    class FakeMeterProvider:
        def __init__(self, *, resource: object, metric_readers: list[object]) -> None:
            captured_arguments["meter_provider"] = {
                "resource": resource,
                "metric_readers": metric_readers,
            }

    class FakeLoggingInstrumentor:
        def instrument(self, *, set_logging_format: bool) -> None:
            captured_arguments["logging_instrumentor"] = {
                "set_logging_format": set_logging_format
            }

    def fake_span_exporter(**kwargs: object) -> object:
        captured_arguments["span_exporter"] = dict(kwargs)
        return {"kind": "span_exporter", "kwargs": kwargs}

    def fake_metric_exporter(**kwargs: object) -> object:
        captured_arguments["metric_exporter"] = dict(kwargs)
        return {"kind": "metric_exporter", "kwargs": kwargs}

    monkeypatch.setattr(otel_module, "_CONFIGURED_PID", None)
    monkeypatch.setattr(otel_module, "OTLPSpanExporter", fake_span_exporter)
    monkeypatch.setattr(otel_module, "OTLPMetricExporter", fake_metric_exporter)
    monkeypatch.setattr(otel_module, "BatchSpanProcessor", FakeBatchSpanProcessor)
    monkeypatch.setattr(
        otel_module,
        "PeriodicExportingMetricReader",
        FakePeriodicExportingMetricReader,
    )
    monkeypatch.setattr(otel_module, "TracerProvider", FakeTracerProvider)
    monkeypatch.setattr(otel_module, "MeterProvider", FakeMeterProvider)
    monkeypatch.setattr(otel_module, "LoggingInstrumentor", FakeLoggingInstrumentor)
    monkeypatch.setattr(otel_module.trace, "set_tracer_provider", lambda provider: None)
    monkeypatch.setattr(otel_module.metrics, "set_meter_provider", lambda provider: None)

    settings = AppSettings(
        observability=AppSettings().observability.model_copy(
            update={
                "otlp_endpoint": "https://otlp-gateway.example.com/otlp",
                "otlp_headers": {
                    "authorization": "Basic abc123",
                    "x-scope-orgid": "tenant-42",
                },
            }
        )
    )

    otel_module.configure_observability(settings, service_name="pytest")

    assert captured_arguments["span_exporter"] == {
        "endpoint": "https://otlp-gateway.example.com/otlp",
        "headers": {
            "authorization": "Basic abc123",
            "x-scope-orgid": "tenant-42",
        },
        "insecure": False,
    }
    assert captured_arguments["metric_exporter"] == {
        "endpoint": "https://otlp-gateway.example.com/otlp",
        "headers": {
            "authorization": "Basic abc123",
            "x-scope-orgid": "tenant-42",
        },
        "insecure": False,
    }


def test_configure_observability_skips_otlp_exporters_without_endpoint(monkeypatch) -> None:
    """Ensure blank OTLP endpoint leaves local tracing enabled without export attempts."""

    captured_calls: dict[str, object] = {}

    class FakeTracerProvider:
        def __init__(self, *, resource: object) -> None:
            captured_calls["trace_provider_resource"] = resource

        def add_span_processor(self, processor: object) -> None:
            captured_calls["span_processor"] = processor

    class FakeMeterProvider:
        def __init__(self, *, resource: object, metric_readers: list[object] | None = None) -> None:
            captured_calls["meter_provider"] = {
                "resource": resource,
                "metric_readers": metric_readers or [],
            }

    class FakeLoggingInstrumentor:
        def instrument(self, *, set_logging_format: bool) -> None:
            captured_calls["logging_instrumented"] = set_logging_format

    monkeypatch.setattr(otel_module, "_CONFIGURED_PID", None)
    monkeypatch.setattr(otel_module, "TracerProvider", FakeTracerProvider)
    monkeypatch.setattr(otel_module, "MeterProvider", FakeMeterProvider)
    monkeypatch.setattr(otel_module, "LoggingInstrumentor", FakeLoggingInstrumentor)
    monkeypatch.setattr(
        otel_module,
        "OTLPSpanExporter",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("span exporter should not be created")),
    )
    monkeypatch.setattr(
        otel_module,
        "OTLPMetricExporter",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("metric exporter should not be created")
        ),
    )
    monkeypatch.setattr(otel_module.trace, "set_tracer_provider", lambda provider: None)
    monkeypatch.setattr(otel_module.metrics, "set_meter_provider", lambda provider: None)

    settings = AppSettings()
    otel_module.configure_observability(settings, service_name="pytest")

    assert "span_processor" not in captured_calls
    assert captured_calls["meter_provider"] == {
        "resource": captured_calls["trace_provider_resource"],
        "metric_readers": [],
    }
