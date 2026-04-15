"""
Purpose: Enforce close-run workflow phase boundaries at the API route layer.
Scope: Shared active-phase guards for mutation routes across documents,
processing, reconciliation, reporting, and sign-off surfaces.
Dependencies: Request-scoped DB sessions, close-run service access checks,
and canonical workflow guard helpers.
"""

from __future__ import annotations

from uuid import UUID

from apps.api.app.dependencies.db import DatabaseSessionDependency
from fastapi import HTTPException, status
from services.close_runs.service import CloseRunService
from services.close_runs.workflow_guards import WorkflowPhaseLockedError, require_active_phase
from services.common.enums import WorkflowPhase
from services.db.repositories.close_run_repo import CloseRunRepository
from services.db.repositories.entity_repo import EntityUserRecord


def require_active_close_run_phase(
    *,
    actor_user: EntityUserRecord,
    entity_id: UUID,
    close_run_id: UUID,
    required_phase: WorkflowPhase,
    action_label: str,
    db_session: DatabaseSessionDependency,
) -> None:
    """Require one mutation to occur only while the expected workflow phase is active."""

    close_run = CloseRunService(
        repository=CloseRunRepository(db_session=db_session)
    ).get_close_run(
        actor_user=actor_user,
        entity_id=entity_id,
        close_run_id=close_run_id,
    )
    try:
        require_active_phase(
            close_run,
            required_phase=required_phase,
            action_label=action_label,
        )
    except WorkflowPhaseLockedError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "workflow_phase_locked",
                "message": error.message,
            },
        ) from error


__all__ = ["require_active_close_run_phase"]
