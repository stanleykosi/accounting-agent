"""
Purpose: Enforce mutation-time workflow phase boundaries for close-run actions.
Scope: Active-phase validation shared by API routes, agent tools, and other
write-capable entrypoints.
Dependencies: Canonical workflow enums plus the close-run detail contract.
"""

from __future__ import annotations

from services.common.enums import WorkflowPhase
from services.contracts.close_run_models import CloseRunSummary


class WorkflowPhaseLockedError(ValueError):
    """Represent an attempt to mutate the close run outside the active workflow phase."""

    def __init__(
        self,
        *,
        action_label: str,
        required_phase: WorkflowPhase,
        active_phase: WorkflowPhase | None,
        message: str,
    ) -> None:
        super().__init__(message)
        self.action_label = action_label
        self.required_phase = required_phase
        self.active_phase = active_phase
        self.message = message


def require_active_phase(
    close_run: CloseRunSummary,
    *,
    required_phase: WorkflowPhase,
    action_label: str,
) -> None:
    """Require the close run's active workflow phase to match the action's phase."""

    active_phase = close_run.workflow_state.active_phase
    if active_phase is required_phase:
        return

    current_phase_label = active_phase.label if active_phase is not None else "No active phase"
    required_phase_label = required_phase.label
    message = (
        f"{action_label} is only available during {required_phase_label}. "
        f"The current active phase is {current_phase_label}."
    )
    if active_phase is None:
        message += " Reopen or create a working close run version before making further changes."
    else:
        message += (
            " Finish the current phase's required work or move into the correct phase before "
            "trying again."
        )

    raise WorkflowPhaseLockedError(
        action_label=action_label,
        required_phase=required_phase,
        active_phase=active_phase,
        message=message,
    )


__all__ = ["WorkflowPhaseLockedError", "require_active_phase"]
