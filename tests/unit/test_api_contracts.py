"""
Purpose: Verify the canonical API contract surface exposed by the FastAPI application.
Scope: OpenAPI path coverage and metadata-route behavior
for the shared contract generation pipeline.
Dependencies: apps/api/app/main.py, services/contracts/api_models.py,
and FastAPI's TestClient.
"""

from __future__ import annotations

from apps.api.app.main import API_VERSION, app
from fastapi.testclient import TestClient


def test_openapi_schema_exposes_seed_contract_routes() -> None:
    """Ensure the generated OpenAPI schema includes the seed health and metadata paths."""

    schema = app.openapi()

    assert schema["info"]["version"] == API_VERSION
    assert "/api/health" in schema["paths"]
    assert "/api/metadata" in schema["paths"]


def test_metadata_endpoint_returns_contract_catalog() -> None:
    """Ensure the metadata endpoint publishes route descriptors for the generated SDK."""

    client = TestClient(app)

    response = client.get("/api/metadata")

    assert response.status_code == 200
    payload = response.json()
    route_names = {route["name"] for route in payload["routes"]}
    assert payload["version"] == API_VERSION
    assert payload["openapi_url"] == "/api/openapi.json"
    assert {"read_api_metadata", "read_health_status"} <= route_names
