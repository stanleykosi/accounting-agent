"""
Purpose: Guard long-running worker tasks against stale close-run phase writes.
Scope: Verifies the active close-run phase before worker tasks persist results.
Dependencies: SQLAlchemy sessions, close-run phase-state models, and job retry errors.
"""

from __future__ import annotations

from uuid import UUID

from services.common.enums import CANONICAL_WORKFLOW_PHASES, CloseRunPhaseStatus, WorkflowPhase
from services.db.models.close_run import CloseRun, CloseRunPhaseState
from services.jobs.retry_policy import JobCancellationRequestedError
from sqlalchemy import select
from sqlalchemy.orm import Session


def ensure_close_run_active_phase(
    *,
    session: Session,
    close_run_id: UUID,
    required_phase: WorkflowPhase,
) -> None:
    """Cancel worker persistence when the close run is no longer in the required phase."""

    close_run = session.get(CloseRun, close_run_id)
    if close_run is None:
        raise JobCancellationRequestedError(
            "Job canceled because the close run no longer exists.",
            details={"close_run_id": str(close_run_id)},
        )

    phase_rows = session.execute(
        select(CloseRunPhaseState.phase, CloseRunPhaseState.status).where(
            CloseRunPhaseState.close_run_id == close_run_id
        )
    ).all()
    status_by_phase = {
        WorkflowPhase(phase): CloseRunPhaseStatus(status)
        for phase, status in phase_rows
    }
    active_phase = next(
        (
            phase
            for phase in CANONICAL_WORKFLOW_PHASES
            if status_by_phase.get(phase) is not CloseRunPhaseStatus.COMPLETED
        ),
        None,
    )
    if active_phase is required_phase:
        return

    raise JobCancellationRequestedError(
        f"Job canceled because the close run is no longer in {required_phase.label}.",
        details={
            "close_run_id": str(close_run_id),
            "required_phase": required_phase.value,
            "active_phase": active_phase.value if active_phase is not None else None,
        },
    )


__all__ = ["ensure_close_run_active_phase"]
