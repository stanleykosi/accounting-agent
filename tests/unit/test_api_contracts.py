"""
Purpose: Verify the canonical API contract surface exposed by the FastAPI application.
Scope: OpenAPI path coverage and metadata-route behavior
for the shared contract generation pipeline.
Dependencies: apps/api/app/main.py, services/contracts/api_models.py,
and FastAPI's TestClient.
"""

from __future__ import annotations

import time

import pytest
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
    assert "/api/ready" in schema["paths"]
    assert "/api/metadata" in schema["paths"]


def test_openapi_operation_ids_are_unique() -> None:
    """Generated SDK operation IDs should remain unique across the OpenAPI surface."""

    schema = api_main.app.openapi()
    operation_ids = [
        operation["operationId"]
        for path_item in schema["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]

    assert len(operation_ids) == len(set(operation_ids))


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
        "read_readiness_status",
        "register_user",
    } <= route_names
    assert len(startup_checks) == 1


def test_dependency_backed_routes_return_503_after_retryable_startup_failure(
    monkeypatch,
) -> None:
    """Dependency-backed routes should fail closed during retryable startup warmup."""

    attempt_count = 0

    def retryable_startup_healthcheck(settings: object) -> None:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            raise api_main.TransientDependencyCheckError(
                "Database dependency is not reachable yet: connection refused"
            )
        time.sleep(0.2)

    monkeypatch.setattr(
        api_main,
        "run_backend_dependency_healthcheck",
        retryable_startup_healthcheck,
    )
    monkeypatch.setattr(api_main, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(api_main, "configure_observability", lambda *args, **kwargs: None)

    client = TestClient(api_main.create_app())

    with client:
        blocked_response = client.get("/api/auth/session")
        readiness_response = client.get("/api/ready")

    assert blocked_response.status_code == 503
    assert blocked_response.json()["detail"]["code"] == "backend_not_ready"
    assert readiness_response.status_code == 503
    assert readiness_response.json()["status"] == "retrying"


def test_permanent_dependency_startup_failure_aborts_startup(monkeypatch) -> None:
    """Non-retryable dependency failures should still fail fast during API startup."""

    attempt_count = 0

    def permanent_startup_healthcheck(settings: object) -> None:
        nonlocal attempt_count
        attempt_count += 1
        raise RuntimeError(
            "Object-storage validation failed. Missing required buckets: derivatives."
        )

    monkeypatch.setattr(
        api_main,
        "run_backend_dependency_healthcheck",
        permanent_startup_healthcheck,
    )
    monkeypatch.setattr(api_main, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(api_main, "configure_observability", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="Missing required buckets: derivatives"):
        with TestClient(api_main.create_app()):
            pass

    assert attempt_count == 1
