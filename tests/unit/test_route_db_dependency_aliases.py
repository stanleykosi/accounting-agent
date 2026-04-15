"""
Purpose: Guard API route session aliases against accidental double-wrapping with Depends().
Scope: Ensures route-local DB dependency aliases reuse the canonical request-scoped session.
Dependencies: API route modules and the shared database dependency alias.
"""

from __future__ import annotations

from apps.api.app.dependencies.db import DatabaseSessionDependency
from apps.api.app.routes import documents, recommendations, reconciliation, supporting_schedules


def test_route_db_session_aliases_reuse_canonical_dependency() -> None:
    """Route DB aliases should point directly at the canonical session dependency."""

    assert documents.DbSessionDep == DatabaseSessionDependency
    assert recommendations.DbSessionDep == DatabaseSessionDependency
    assert reconciliation.DbSessionDep == DatabaseSessionDependency
    assert supporting_schedules.DbSessionDep == DatabaseSessionDependency
