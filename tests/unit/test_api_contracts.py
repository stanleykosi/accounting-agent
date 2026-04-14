"""
Purpose: Verify the canonical API contract surface exposed by the FastAPI application.
Scope: OpenAPI path coverage and metadata-route behavior
for the shared contract generation pipeline.
Dependencies: apps/api/app/main.py, services/contracts/api_models.py,
and FastAPI's TestClient.
"""

from __future__ import annotations

from apps.api.app import main as api_main
from fastapi.testclient import TestClient


def test_openapi_schema_exposes_seed_contract_routes() -> None:
    """Ensure the generated OpenAPI schema includes the seed health and metadata paths."""

    schema = api_main.app.openapi()

    assert schema["info"]["version"] == api_main.API_VERSION
    assert "/api/auth/login" in schema["paths"]
    assert "/api/auth/register" in schema["paths"]
    assert "/api/auth/session" in schema["paths"]
    assert "/api/health" in schema["paths"]
    assert "/api/metadata" in schema["paths"]


def test_metadata_endpoint_returns_contract_catalog(monkeypatch) -> None:
    """Ensure the metadata endpoint publishes route descriptors for the generated SDK."""

    startup_checks: list[object] = []

    def capture_startup_healthcheck(settings: object) -> None:
        startup_checks.append(settings)

    monkeypatch.setattr(api_main, "run_backend_dependency_healthcheck", capture_startup_healthcheck)
    monkeypatch.setattr(api_main, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(api_main, "configure_observability", lambda *args, **kwargs: None)

    client = TestClient(api_main.app)

    with client:
        response = client.get("/api/metadata")

    assert response.status_code == 200
    payload = response.json()
    route_names = {route["name"] for route in payload["routes"]}
    assert payload["version"] == api_main.API_VERSION
    assert payload["openapi_url"] == "/api/openapi.json"
    assert {
        "login_user",
        "logout_user",
        "read_api_metadata",
        "read_current_session",
        "read_health_status",
        "register_user",
    } <= route_names
    assert len(startup_checks) == 1
