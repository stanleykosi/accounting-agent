"""Purpose: Guard canonical export route registration against dynamic-path shadowing.
Scope: FastAPI router registration order for literal evidence-pack paths versus UUID export paths.
Dependencies: Export API router and a lightweight FastAPI app only.
"""

from __future__ import annotations

from apps.api.app.routes.exports import router
from fastapi import FastAPI


def test_evidence_pack_routes_register_before_export_id_route() -> None:
    """Literal evidence-pack paths must win over the dynamic export-id matcher."""

    app = FastAPI()
    app.include_router(router)

    export_paths = [
        route.path
        for route in app.routes
        if "/entities/{entity_id}/close-runs/{close_run_id}/exports" in getattr(route, "path", "")
    ]

    evidence_pack_index = export_paths.index(
        "/entities/{entity_id}/close-runs/{close_run_id}/exports/evidence-pack"
    )
    export_id_index = export_paths.index(
        "/entities/{entity_id}/close-runs/{close_run_id}/exports/{export_id}"
    )

    assert evidence_pack_index < export_id_index
