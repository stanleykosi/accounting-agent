"""
Purpose: Verify API requests produce active trace metadata that Grafana-style backends can ingest.
Scope: FastAPI health route execution under the canonical request telemetry middleware.
Dependencies: apps/api/app/main.py, apps/api/app/middleware/telemetry.py, and FastAPI TestClient.
"""

from __future__ import annotations

from apps.api.app import main as api_main
from apps.api.app.middleware import telemetry as telemetry_module
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from services.common.settings import AppSettings


def test_health_request_emits_operational_event_with_trace_id(monkeypatch) -> None:
    """Ensure API requests create an active span before operational events are emitted."""

    receipts: list[object] = []
    original_emit_operational_event = telemetry_module.emit_operational_event

    def capture_operational_event(**kwargs: object) -> object:
        receipt = original_emit_operational_event(**kwargs)
        receipts.append(receipt)
        return receipt

    monkeypatch.setattr(api_main, "run_backend_dependency_healthcheck", lambda settings: None)
    monkeypatch.setattr(api_main, "configure_logging", lambda *args, **kwargs: None)
    local_tracer_provider = TracerProvider()
    monkeypatch.setattr(
        api_main,
        "configure_observability",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        telemetry_module,
        "get_tracer",
        lambda name: local_tracer_provider.get_tracer(name),
    )
    monkeypatch.setattr(telemetry_module, "emit_operational_event", capture_operational_event)

    app = api_main.create_app(settings=AppSettings())
    client = TestClient(app)

    with client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert receipts, "Expected at least one operational event receipt."
    request_receipt = receipts[-1]
    assert getattr(request_receipt, "event_name") == "api.request"
    assert getattr(request_receipt, "trace_id") is not None
