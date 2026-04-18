"""
Purpose: Expose the canonical FastAPI application and OpenAPI schema for the accounting API.
Scope: Application construction, lifecycle logging, stable operation IDs,
and seed contract routes for health and metadata discovery.
Dependencies: FastAPI, shared runtime settings, structured logging,
and the seed API contract models.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

from apps.api.app.middleware.telemetry import install_request_telemetry_middleware
from apps.api.app.routes.api_tokens import router as api_tokens_router
from apps.api.app.routes.auth import router as auth_router
from apps.api.app.routes.chat import router as chat_router
from apps.api.app.routes.close_runs import router as close_runs_router
from apps.api.app.routes.coa import router as coa_router
from apps.api.app.routes.documents import router as documents_router
from apps.api.app.routes.entities import router as entities_router
from apps.api.app.routes.exports import router as exports_router
from apps.api.app.routes.jobs import router as jobs_router
from apps.api.app.routes.ledger import close_run_router as close_run_ledger_router
from apps.api.app.routes.ledger import router as ledger_router
from apps.api.app.routes.ownership import router as ownership_router
from apps.api.app.routes.quickbooks import router as quickbooks_router
from apps.api.app.routes.recommendations import router as recommendations_router
from apps.api.app.routes.reconciliation import router as reconciliation_router
from apps.api.app.routes.report_templates import router as report_templates_router
from apps.api.app.routes.reports import router as reports_router
from apps.api.app.routes.supporting_schedules import router as supporting_schedules_router
from fastapi import APIRouter, FastAPI, Request
from fastapi.routing import APIRoute
from services.common.logging import configure_logging, get_logger
from services.common.runtime_checks import run_backend_dependency_healthcheck
from services.common.settings import AppSettings, get_settings
from services.common.types import utc_now
from services.contracts.api_models import (
    ApiContractMetadata,
    ApiHealthStatus,
    ApiRouteDescriptor,
)
from services.observability.otel import configure_observability

API_VERSION = "0.1.0"


def create_app(*, settings: AppSettings | None = None) -> FastAPI:
    """Create the canonical FastAPI application for the API service."""

    resolved_settings = settings or get_settings()
    api_base_path = resolved_settings.runtime.api_base_path
    api_router = APIRouter(prefix=_resolve_router_prefix(api_base_path))

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """Configure runtime logging and emit deterministic startup and shutdown events."""

        configure_logging(resolved_settings, service_name="api")
        configure_observability(
            resolved_settings,
            service_name="api",
            service_version=API_VERSION,
            app=application,
        )
        logger = get_logger(__name__)
        logger.info(
            "API service starting.",
            api_base_path=api_base_path,
            docs_url=_join_api_path(api_base_path, "/docs"),
            openapi_url=_join_api_path(api_base_path, "/openapi.json"),
        )
        run_backend_dependency_healthcheck(resolved_settings)
        logger.info("API backend dependency healthcheck passed.")
        yield
        logger.info("API service stopping.")

    app = FastAPI(
        title="Accounting AI Agent API",
        summary="Canonical API for the Accounting AI Agent workflow platform.",
        description=(
            "API surface for the Accounting AI Agent workflow platform. Pydantic models are the "
            "source of truth for request and response contracts, and the OpenAPI schema drives "
            "generated TypeScript clients."
        ),
        version=API_VERSION,
        openapi_url=_join_api_path(api_base_path, "/openapi.json"),
        docs_url=_join_api_path(api_base_path, "/docs"),
        redoc_url=_join_api_path(api_base_path, "/redoc"),
        lifespan=lifespan,
        generate_unique_id_function=_build_operation_id,
    )
    install_request_telemetry_middleware(app)

    @api_router.get(
        "/health",
        response_model=ApiHealthStatus,
        summary="Read API health",
        tags=["platform"],
    )
    async def read_health_status() -> ApiHealthStatus:
        """Return a deterministic health payload for operators and generated clients."""

        return ApiHealthStatus(
            status="ok",
            service_name="api",
            environment=resolved_settings.runtime.environment,
            version=API_VERSION,
            api_base_path=api_base_path,
            generated_at=utc_now(),
        )

    @api_router.get(
        "/metadata",
        response_model=ApiContractMetadata,
        summary="Read API contract metadata",
        tags=["contracts"],
    )
    async def read_api_metadata(request: Request) -> ApiContractMetadata:
        """Return contract metadata that frontend tooling can inspect without parsing the spec."""

        return ApiContractMetadata(
            service_name="api",
            version=API_VERSION,
            api_base_path=api_base_path,
            openapi_url=_join_api_path(api_base_path, "/openapi.json"),
            docs_url=_join_api_path(api_base_path, "/docs"),
            routes=_collect_route_descriptors(request.app.routes),
        )

    api_router.include_router(auth_router)
    api_router.include_router(api_tokens_router)
    api_router.include_router(chat_router)
    api_router.include_router(entities_router)
    api_router.include_router(jobs_router)
    api_router.include_router(ledger_router)
    api_router.include_router(close_run_ledger_router)
    api_router.include_router(close_runs_router)
    api_router.include_router(coa_router)
    api_router.include_router(documents_router)
    api_router.include_router(ownership_router)
    api_router.include_router(quickbooks_router)
    api_router.include_router(recommendations_router)
    api_router.include_router(reconciliation_router)
    api_router.include_router(supporting_schedules_router)
    api_router.include_router(report_templates_router)
    api_router.include_router(reports_router)
    api_router.include_router(exports_router)
    app.include_router(api_router)
    return app


def _resolve_router_prefix(api_base_path: str) -> str:
    """Resolve the router prefix from the configured API base path."""

    if api_base_path == "/":
        return ""

    return api_base_path


def _join_api_path(base_path: str, suffix: str) -> str:
    """Join the API base path with a route suffix without introducing double slashes."""

    if base_path == "/":
        return suffix

    return f"{base_path}{suffix}"


def _build_operation_id(route: APIRoute) -> str:
    """Build a stable OpenAPI operation ID that will not drift with method ordering."""

    if route.name:
        return str(route.name)

    methods = "_".join(method.lower() for method in sorted(route.methods or {"get"}))
    normalized_path = route.path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
    return f"{methods}_{normalized_path or 'root'}"


def _collect_route_descriptors(routes: Iterable[Any]) -> tuple[ApiRouteDescriptor, ...]:
    """Collect public API route metadata for contract introspection responses."""

    descriptors: list[ApiRouteDescriptor] = []
    for route in routes:
        if not isinstance(route, APIRoute):
            continue

        if route.path in {"/openapi.json", "/docs", "/redoc"}:
            continue

        methods = tuple(sorted(method for method in route.methods or set() if method != "HEAD"))
        descriptors.append(
            ApiRouteDescriptor(
                name=route.name,
                path=route.path,
                methods=methods,
                summary=route.summary,
                tags=tuple(str(tag) for tag in route.tags or ()),
            )
        )

    return tuple(sorted(descriptors, key=lambda descriptor: (descriptor.path, descriptor.name)))


app = create_app()

__all__ = ["API_VERSION", "app", "create_app"]
