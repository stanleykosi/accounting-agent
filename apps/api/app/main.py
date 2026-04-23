"""
Purpose: Expose the canonical FastAPI application and OpenAPI schema for the accounting API.
Scope: Application construction, lifecycle logging, stable operation IDs,
and seed contract routes for health and metadata discovery.
Dependencies: FastAPI, shared runtime settings, structured logging,
and the seed API contract models.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager, suppress
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
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from services.common.logging import configure_logging, get_logger
from services.common.readiness import (
    BackendDependencyReadiness,
    BackendDependencyReadinessSnapshot,
)
from services.common.runtime_checks import (
    TransientDependencyCheckError,
    run_backend_dependency_healthcheck,
)
from services.common.settings import AppSettings, get_settings
from services.common.types import utc_now
from services.contracts.api_models import (
    ApiContractMetadata,
    ApiHealthStatus,
    ApiReadinessStatus,
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
        readiness = BackendDependencyReadiness()
        application.state.backend_dependency_readiness = readiness
        shutdown_event = asyncio.Event()
        readiness_task: asyncio.Task[None] | None = None
        should_continue_retrying = await _run_initial_dependency_readiness_probe(
            logger=logger,
            readiness=readiness,
            settings=resolved_settings,
        )
        if should_continue_retrying:
            readiness_task = asyncio.create_task(
                _run_dependency_readiness_probe(
                    logger=logger,
                    readiness=readiness,
                    settings=resolved_settings,
                    shutdown_event=shutdown_event,
                    initial_attempt_count=readiness.snapshot().attempt_count,
                )
            )
        try:
            yield
        finally:
            shutdown_event.set()
            if readiness_task is not None:
                readiness_task.cancel()
                with suppress(asyncio.CancelledError):
                    await readiness_task
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

    @app.middleware("http")
    async def gate_dependency_backed_routes(request: Request, call_next):
        if _is_readiness_exempt_path(request.url.path, api_base_path):
            return await call_next(request)

        readiness_snapshot = _read_dependency_readiness(request.app)
        if readiness_snapshot.ready:
            return await call_next(request)

        return _build_backend_not_ready_response(readiness_snapshot)

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
        "/ready",
        response_model=ApiReadinessStatus,
        summary="Read API dependency readiness",
        tags=["platform"],
    )
    async def read_readiness_status(request: Request) -> JSONResponse:
        """Return readiness for dependency-backed API routes without dropping the process socket."""

        readiness_payload = _build_api_readiness_status(
            api_base_path=api_base_path,
            environment=resolved_settings.runtime.environment,
            readiness_snapshot=_read_dependency_readiness(request.app),
            service_name="api",
            version=API_VERSION,
        )
        response_headers = {"cache-control": "no-store"}
        if not readiness_payload.ready:
            response_headers["retry-after"] = "1"
        return JSONResponse(
            content=readiness_payload.model_dump(mode="json"),
            headers=response_headers,
            status_code=200 if readiness_payload.ready else 503,
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


async def _run_initial_dependency_readiness_probe(
    *,
    logger,
    readiness: BackendDependencyReadiness,
    settings: AppSettings,
) -> bool:
    """Run the startup dependency probe once and decide whether background retries are safe."""

    attempt_count = 1
    try:
        await asyncio.to_thread(run_backend_dependency_healthcheck, settings)
    except TransientDependencyCheckError as error:
        error_message = _format_dependency_probe_error(error)
        readiness.mark_retrying(attempt_count=attempt_count, error_message=error_message)
        retry_delay_seconds = _compute_dependency_retry_delay_seconds(
            attempt_count=attempt_count
        )
        logger.warning(
            "API backend dependency readiness probe hit a retryable warmup error.",
            attempt_count=attempt_count,
            retry_delay_seconds=retry_delay_seconds,
            error=error_message,
        )
        return True
    except Exception as error:
        error_message = _format_dependency_probe_error(error)
        readiness.mark_failed(attempt_count=attempt_count, error_message=error_message)
        logger.error(
            "API backend dependency readiness probe failed permanently during startup.",
            attempt_count=attempt_count,
            error=error_message,
        )
        raise
    else:
        readiness.mark_ready(attempt_count=attempt_count)
        logger.info(
            "API backend dependency healthcheck passed.",
            attempt_count=attempt_count,
        )
        return False


async def _run_dependency_readiness_probe(
    *,
    logger,
    readiness: BackendDependencyReadiness,
    settings: AppSettings,
    shutdown_event: asyncio.Event,
    initial_attempt_count: int,
) -> None:
    """Continuously probe backend dependencies until the API is ready or shutting down."""

    attempt_count = initial_attempt_count
    while not shutdown_event.is_set():
        attempt_count += 1
        try:
            await asyncio.to_thread(run_backend_dependency_healthcheck, settings)
        except TransientDependencyCheckError as error:
            error_message = _format_dependency_probe_error(error)
            readiness.mark_retrying(attempt_count=attempt_count, error_message=error_message)
            retry_delay_seconds = _compute_dependency_retry_delay_seconds(
                attempt_count=attempt_count
            )
            logger.warning(
                "API backend dependency readiness probe failed.",
                attempt_count=attempt_count,
                retry_delay_seconds=retry_delay_seconds,
                error=error_message,
            )
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=retry_delay_seconds)
            except TimeoutError:
                continue
            return
        except Exception as error:
            error_message = _format_dependency_probe_error(error)
            readiness.mark_failed(attempt_count=attempt_count, error_message=error_message)
            logger.error(
                "API backend dependency readiness probe encountered a non-retryable error.",
                attempt_count=attempt_count,
                error=error_message,
            )
            return
        else:
            readiness.mark_ready(attempt_count=attempt_count)
            logger.info(
                "API backend dependency healthcheck passed.",
                attempt_count=attempt_count,
            )
            return


def _compute_dependency_retry_delay_seconds(*, attempt_count: int) -> int:
    """Return a short bounded retry delay for hosted dependency warmup probes."""

    return min(2 * max(attempt_count, 1), 10)


def _format_dependency_probe_error(error: Exception) -> str:
    """Normalize dependency probe failures into one stable operator-facing summary."""

    message = str(error).strip()
    if message:
        return message
    return error.__class__.__name__


def _read_dependency_readiness(application: FastAPI) -> BackendDependencyReadinessSnapshot:
    """Return the current backend readiness snapshot from application state."""

    readiness = getattr(application.state, "backend_dependency_readiness", None)
    if isinstance(readiness, BackendDependencyReadiness):
        return readiness.snapshot()

    return BackendDependencyReadiness().snapshot()


def _build_api_readiness_status(
    *,
    api_base_path: str,
    environment,
    readiness_snapshot: BackendDependencyReadinessSnapshot,
    service_name: str,
    version: str,
) -> ApiReadinessStatus:
    """Serialize the live readiness snapshot into the public API readiness contract."""

    return ApiReadinessStatus(
        status=readiness_snapshot.status,
        ready=readiness_snapshot.ready,
        service_name=service_name,
        environment=environment,
        version=version,
        api_base_path=api_base_path,
        attempt_count=readiness_snapshot.attempt_count,
        last_checked_at=readiness_snapshot.last_checked_at,
        last_error=readiness_snapshot.last_error,
        generated_at=utc_now(),
    )


def _build_backend_not_ready_response(
    readiness_snapshot: BackendDependencyReadinessSnapshot,
) -> JSONResponse:
    """Return the canonical 503 response while dependency-backed routes are still warming."""

    return JSONResponse(
        content={
            "detail": {
                "attempt_count": readiness_snapshot.attempt_count,
                "code": "backend_not_ready",
                "last_error": readiness_snapshot.last_error,
                "message": (
                    "The API is still validating its backend dependencies. Retry shortly."
                ),
                "status": readiness_snapshot.status,
            }
        },
        headers={
            "cache-control": "no-store",
            "retry-after": "1",
        },
        status_code=503,
    )


def _is_readiness_exempt_path(path: str, api_base_path: str) -> bool:
    """Return whether a request path should bypass dependency-readiness gating."""

    exempt_suffixes = {
        "/docs",
        "/health",
        "/metadata",
        "/openapi.json",
        "/ready",
        "/redoc",
    }
    if api_base_path == "/":
        return path in exempt_suffixes

    return path in {f"{api_base_path}{suffix}" for suffix in exempt_suffixes}


app = create_app()

__all__ = ["API_VERSION", "app", "create_app"]
