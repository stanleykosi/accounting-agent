"""
Purpose: Evaluate close-run workflow phase gates against canonical readiness signals.
Scope: Deterministic blocking rules for Collection, Processing, Reconciliation,
Reporting, and Review / Sign-off.
Dependencies: Canonical workflow enums, UTC time helpers, and immutable dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from services.common.enums import (
    CANONICAL_WORKFLOW_PHASES,
    CloseRunPhaseStatus,
    WorkflowPhase,
)
from services.common.types import utc_now


@dataclass(frozen=True, slots=True)
class ExistingPhaseState:
    """Describe the persisted state of one workflow phase before gate recalculation."""

    phase: WorkflowPhase
    status: CloseRunPhaseStatus
    blocking_reason: str | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class PhaseGateSignals:
    """Collect deterministic signals that can block phase progression."""

    missing_required_documents: tuple[str, ...] = ()
    approved_document_count: int = 0
    unauthorized_document_count: int = 0
    pending_document_review_count: int = 0
    unmatched_transaction_count: int = 0
    wrong_period_document_count: int = 0
    unresolved_processing_item_count: int = 0
    unresolved_reconciliation_exception_count: int = 0
    missing_supporting_schedules: tuple[str, ...] = ()
    pending_supporting_schedule_review_count: int = 0
    missing_required_reports: tuple[str, ...] = ()
    missing_signoff_requirements: tuple[str, ...] = ()
    unresolved_signoff_item_count: int = 0


@dataclass(frozen=True, slots=True)
class EvaluatedPhaseState:
    """Describe a recalculated phase state and its recovery-oriented blocking reason."""

    phase: WorkflowPhase
    status: CloseRunPhaseStatus
    blocking_reason: str | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class PhaseTransitionResult:
    """Describe the recalculated phase states after a successful transition."""

    completed_phase: WorkflowPhase
    active_phase: WorkflowPhase
    phase_states: tuple[EvaluatedPhaseState, ...]


@dataclass(frozen=True, slots=True)
class PhaseRewindResult:
    """Describe the recalculated phase states after reopening an earlier phase."""

    previous_active_phase: WorkflowPhase
    active_phase: WorkflowPhase
    phase_states: tuple[EvaluatedPhaseState, ...]


class PhaseGateError(ValueError):
    """Represent an invalid or blocked phase-gate operation."""


def build_initial_phase_states() -> tuple[EvaluatedPhaseState, ...]:
    """Return the canonical phase-state seed used when a close run is created."""

    return tuple(
        EvaluatedPhaseState(
            phase=phase,
            status=(
                CloseRunPhaseStatus.IN_PROGRESS
                if phase is WorkflowPhase.COLLECTION
                else CloseRunPhaseStatus.NOT_STARTED
            ),
            blocking_reason=None,
            completed_at=None,
        )
        for phase in CANONICAL_WORKFLOW_PHASES
    )


def build_reopened_phase_states(
    *,
    reopened_at: datetime | None = None,
) -> tuple[EvaluatedPhaseState, ...]:
    """Return phase states for a reopened working version that preserves completed work."""

    resolved_reopened_at = reopened_at or utc_now()
    return tuple(
        EvaluatedPhaseState(
            phase=phase,
            status=(
                CloseRunPhaseStatus.IN_PROGRESS
                if phase is WorkflowPhase.REVIEW_SIGNOFF
                else CloseRunPhaseStatus.COMPLETED
            ),
            blocking_reason=None,
            completed_at=None if phase is WorkflowPhase.REVIEW_SIGNOFF else resolved_reopened_at,
        )
        for phase in CANONICAL_WORKFLOW_PHASES
    )


def evaluate_phase_gates(
    *,
    phase_states: tuple[ExistingPhaseState, ...],
    signals: PhaseGateSignals,
) -> tuple[EvaluatedPhaseState, ...]:
    """Recalculate phase states while preserving completed gates and enforcing blockers."""

    _validate_phase_order(phase_states=phase_states)
    evaluated_states: list[EvaluatedPhaseState] = []
    previous_completed = True

    for phase_state in phase_states:
        if not previous_completed:
            evaluated_state = EvaluatedPhaseState(
                phase=phase_state.phase,
                status=CloseRunPhaseStatus.NOT_STARTED,
                blocking_reason=None,
                completed_at=None,
            )
            evaluated_states.append(evaluated_state)
            previous_completed = False
            continue

        if phase_state.status is CloseRunPhaseStatus.COMPLETED:
            evaluated_state = EvaluatedPhaseState(
                phase=phase_state.phase,
                status=CloseRunPhaseStatus.COMPLETED,
                blocking_reason=None,
                completed_at=phase_state.completed_at or utc_now(),
            )
            evaluated_states.append(evaluated_state)
            previous_completed = True
            continue

        blocking_reason = _build_blocking_reason(phase=phase_state.phase, signals=signals)
        if blocking_reason is not None:
            evaluated_state = EvaluatedPhaseState(
                phase=phase_state.phase,
                status=CloseRunPhaseStatus.BLOCKED,
                blocking_reason=blocking_reason,
                completed_at=None,
            )
            evaluated_states.append(evaluated_state)
            previous_completed = False
            continue

        if phase_state.status in {
            CloseRunPhaseStatus.IN_PROGRESS,
            CloseRunPhaseStatus.READY,
            CloseRunPhaseStatus.BLOCKED,
        }:
            status = CloseRunPhaseStatus.READY
        else:
            status = CloseRunPhaseStatus.NOT_STARTED

        evaluated_state = EvaluatedPhaseState(
            phase=phase_state.phase,
            status=status,
            blocking_reason=None,
            completed_at=None,
        )
        evaluated_states.append(evaluated_state)
        previous_completed = status is CloseRunPhaseStatus.COMPLETED

    return tuple(evaluated_states)


def transition_to_next_phase(
    *,
    phase_states: tuple[ExistingPhaseState, ...],
    target_phase: WorkflowPhase,
    signals: PhaseGateSignals,
    transitioned_at: datetime | None = None,
) -> PhaseTransitionResult:
    """Complete the active ready phase and open its immediate successor."""

    resolved_transitioned_at = transitioned_at or utc_now()
    evaluated_states = evaluate_phase_gates(phase_states=phase_states, signals=signals)
    active_index = _find_active_phase_index(phase_states=evaluated_states)

    if active_index >= len(CANONICAL_WORKFLOW_PHASES) - 1:
        raise PhaseGateError("Review / Sign-off is the final phase and cannot transition forward.")

    expected_target_phase = CANONICAL_WORKFLOW_PHASES[active_index + 1]
    if target_phase is not expected_target_phase:
        raise PhaseGateError(
            f"The next phase must be {expected_target_phase.value}; received {target_phase.value}."
        )

    active_state = evaluated_states[active_index]
    if active_state.status is CloseRunPhaseStatus.BLOCKED:
        raise PhaseGateError(active_state.blocking_reason or "The active phase is blocked.")
    if active_state.status is not CloseRunPhaseStatus.READY:
        raise PhaseGateError(f"{active_state.phase.label} is not ready to advance.")

    transitioned_states: list[EvaluatedPhaseState] = []
    for index, phase_state in enumerate(evaluated_states):
        if index == active_index:
            transitioned_states.append(
                EvaluatedPhaseState(
                    phase=phase_state.phase,
                    status=CloseRunPhaseStatus.COMPLETED,
                    blocking_reason=None,
                    completed_at=resolved_transitioned_at,
                )
            )
            continue
        if index == active_index + 1:
            transitioned_states.append(
                EvaluatedPhaseState(
                    phase=phase_state.phase,
                    status=CloseRunPhaseStatus.IN_PROGRESS,
                    blocking_reason=None,
                    completed_at=None,
                )
            )
            continue

        transitioned_states.append(phase_state)

    return PhaseTransitionResult(
        completed_phase=active_state.phase,
        active_phase=target_phase,
        phase_states=tuple(transitioned_states),
    )


def evaluate_signoff_readiness(
    *,
    phase_states: tuple[ExistingPhaseState, ...],
    signals: PhaseGateSignals,
) -> EvaluatedPhaseState:
    """Return the Review / Sign-off gate after recalculation, or raise if it is unreachable."""

    evaluated_states = evaluate_phase_gates(phase_states=phase_states, signals=signals)
    review_state = evaluated_states[-1]
    if review_state.phase is not WorkflowPhase.REVIEW_SIGNOFF:
        raise PhaseGateError("The final canonical phase must be Review / Sign-off.")

    return review_state


def rewind_to_phase(
    *,
    phase_states: tuple[ExistingPhaseState, ...],
    target_phase: WorkflowPhase,
    signals: PhaseGateSignals,
) -> PhaseRewindResult:
    """Reopen an earlier canonical phase and clear all later phase progress."""

    evaluated_states = evaluate_phase_gates(phase_states=phase_states, signals=signals)
    active_index = _find_active_phase_index(phase_states=evaluated_states)
    target_index = CANONICAL_WORKFLOW_PHASES.index(target_phase)

    if active_index == 0:
        raise PhaseGateError("Collection is already the active phase and cannot rewind further.")
    if target_index >= active_index:
        current_phase = evaluated_states[active_index].phase
        raise PhaseGateError(
            f"The rewind target must be earlier than the current active phase "
            f"{current_phase.value}; received {target_phase.value}."
        )

    rewound_states: list[EvaluatedPhaseState] = []
    for index, phase_state in enumerate(evaluated_states):
        if index < target_index:
            rewound_states.append(phase_state)
            continue
        if index == target_index:
            rewound_states.append(
                EvaluatedPhaseState(
                    phase=phase_state.phase,
                    status=CloseRunPhaseStatus.IN_PROGRESS,
                    blocking_reason=None,
                    completed_at=None,
                )
            )
            continue

        rewound_states.append(
            EvaluatedPhaseState(
                phase=phase_state.phase,
                status=CloseRunPhaseStatus.NOT_STARTED,
                blocking_reason=None,
                completed_at=None,
            )
        )

    return PhaseRewindResult(
        previous_active_phase=evaluated_states[active_index].phase,
        active_phase=target_phase,
        phase_states=tuple(rewound_states),
    )


def _find_active_phase_index(*, phase_states: tuple[EvaluatedPhaseState, ...]) -> int:
    """Return the first incomplete phase index, failing fast when no active phase exists."""

    for index, phase_state in enumerate(phase_states):
        if phase_state.status is not CloseRunPhaseStatus.COMPLETED:
            return index

    raise PhaseGateError("All workflow phases are already complete.")


def _validate_phase_order(*, phase_states: tuple[ExistingPhaseState, ...]) -> None:
    """Ensure callers pass all five phases in the non-negotiable canonical order."""

    actual_order = tuple(phase_state.phase for phase_state in phase_states)
    if actual_order != CANONICAL_WORKFLOW_PHASES:
        raise PhaseGateError("Phase states must include every canonical workflow phase in order.")


def _build_blocking_reason(*, phase: WorkflowPhase, signals: PhaseGateSignals) -> str | None:
    """Build the deterministic recovery message for the supplied phase when blockers exist."""

    if phase is WorkflowPhase.COLLECTION:
        blockers: list[str] = []
        if signals.approved_document_count <= 0:
            blockers.append("no approved source documents yet")
        if signals.missing_required_documents:
            blockers.append(
                "missing required documents: " + ", ".join(signals.missing_required_documents)
            )
        if signals.pending_document_review_count > 0:
            blockers.append(
                f"{signals.pending_document_review_count} document(s) still awaiting verification"
            )
        if signals.unauthorized_document_count > 0:
            blockers.append(f"{signals.unauthorized_document_count} unauthorized document(s)")
        if signals.unmatched_transaction_count > 0:
            blockers.append(
                f"{signals.unmatched_transaction_count} document(s) are not matched to transactions"
            )
        if signals.wrong_period_document_count > 0:
            blockers.append(f"{signals.wrong_period_document_count} wrong-period document(s)")
        return _join_blockers(phase=phase, blockers=tuple(blockers))

    if (
        phase is WorkflowPhase.PROCESSING
        and signals.unresolved_processing_item_count > 0
    ):
        return (
            f"Processing is blocked by {signals.unresolved_processing_item_count} "
            "unresolved recommendation or extraction item(s)."
        )

    if (
        phase is WorkflowPhase.RECONCILIATION
        and (
            signals.unresolved_reconciliation_exception_count > 0
            or signals.pending_supporting_schedule_review_count > 0
            or bool(signals.missing_supporting_schedules)
        )
    ):
        blockers: list[str] = []
        if signals.unresolved_reconciliation_exception_count > 0:
            blockers.append(
                f"{signals.unresolved_reconciliation_exception_count} unresolved exception(s)"
            )
        if signals.missing_supporting_schedules:
            blockers.append(
                "missing supporting schedules: "
                + ", ".join(signals.missing_supporting_schedules)
            )
        if signals.pending_supporting_schedule_review_count > 0:
            blockers.append(
                f"{signals.pending_supporting_schedule_review_count} "
                "supporting schedule(s) awaiting review"
            )
        return _join_blockers(phase=phase, blockers=tuple(blockers))

    if phase is WorkflowPhase.REPORTING and signals.missing_required_reports:
        missing_reports = ", ".join(signals.missing_required_reports)
        return f"Reporting is blocked until required report artifacts exist: {missing_reports}."

    if (
        phase is WorkflowPhase.REVIEW_SIGNOFF
        and signals.unresolved_signoff_item_count > 0
    ):
        if signals.missing_signoff_requirements:
            return (
                "Review / Sign-off is blocked until these release controls are complete: "
                + ", ".join(signals.missing_signoff_requirements)
                + "."
            )
        return (
            "Review / Sign-off is blocked by "
            f"{signals.unresolved_signoff_item_count} unresolved sign-off item(s)."
        )

    return None


def _join_blockers(*, phase: WorkflowPhase, blockers: tuple[str, ...]) -> str | None:
    """Join phase-specific blocker fragments into one operator-facing recovery message."""

    if not blockers:
        return None

    return f"{phase.label} is blocked by " + "; ".join(blockers) + "."


__all__ = [
    "EvaluatedPhaseState",
    "ExistingPhaseState",
    "PhaseGateError",
    "PhaseGateSignals",
    "PhaseRewindResult",
    "PhaseTransitionResult",
    "build_initial_phase_states",
    "build_reopened_phase_states",
    "evaluate_phase_gates",
    "evaluate_signoff_readiness",
    "rewind_to_phase",
    "transition_to_next_phase",
]
