"""
Purpose: Define the reusable workspace-context builder interface for agent
planners that need live system state snapshots.
Scope: Snapshot construction contracts for workspace-scoped agent runtimes.
Dependencies: Python typing only.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID


class WorkspaceContextBuilder(Protocol):
    """Build one live workspace snapshot for the planner."""

    def build_snapshot(
        self,
        *,
        actor: Any,
        entity_id: UUID,
        close_run_id: UUID | None,
        thread_id: UUID | None,
    ) -> dict[str, Any]:
        """Return a compact JSON-safe workspace snapshot for planning."""

