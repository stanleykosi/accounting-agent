"""
Purpose: Verify close-run phase-gate calculation and lifecycle transition primitives.
Scope: Pure unit coverage over deterministic gate rules without a database or API server.
Dependencies: close-run gate helpers plus the canonical workflow and phase-status enums.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from services.close_runs.gates import (
    ExistingPhaseState,
    PhaseGateError,
    PhaseGateSignals,
    build_initial_phase_states,
    build_reopened_phase_states,
    evaluate_phase_gates,
    evaluate_signoff_readiness,
    transition_to_next_phase,
)
from services.common.enums import (
    CANONICAL_WORKFLOW_PHASES,
    CloseRunPhaseStatus,
    WorkflowPhase,
)


def test_collection_gate_blocks_missing_and_unauthorized_documents() -> None:
    """Ensure Collection cannot advance when required document controls fail."""

    evaluated = evaluate_phase_gates(
        phase_states=_existing_states_from_initial(),
        signals=PhaseGateSignals(
            missing_required_documents=("bank statement", "payroll summary"),
            unauthorized_document_count=1,
            wrong_period_document_count=2,
        ),
    )

    collection = evaluated[0]
    processing = evaluated[1]

    assert collection.phase is WorkflowPhase.COLLECTION
    assert collection.status is CloseRunPhaseStatus.BLOCKED
    assert collection.blocking_reason is not None
    assert "missing required documents" in collection.blocking_reason
    assert "unauthorized" in collection.blocking_reason
    assert "wrong-period" in collection.blocking_reason
    assert processing.status is CloseRunPhaseStatus.NOT_STARTED


def test_transition_opens_only_the_immediate_next_phase() -> None:
    """Ensure transitions complete the active ready phase and reject phase skipping."""

    transitioned_at = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)
    result = transition_to_next_phase(
        phase_states=_existing_states_from_initial(),
        target_phase=WorkflowPhase.PROCESSING,
        signals=PhaseGateSignals(),
        transitioned_at=transitioned_at,
    )

    assert result.completed_phase is WorkflowPhase.COLLECTION
    assert result.active_phase is WorkflowPhase.PROCESSING
    assert result.phase_states[0].status is CloseRunPhaseStatus.COMPLETED
    assert result.phase_states[0].completed_at == transitioned_at
    assert result.phase_states[1].status is CloseRunPhaseStatus.IN_PROGRESS

    with pytest.raises(PhaseGateError, match="next phase"):
        transition_to_next_phase(
            phase_states=_existing_states_from_initial(),
            target_phase=WorkflowPhase.RECONCILIATION,
            signals=PhaseGateSignals(),
        )


def test_processing_gate_blocks_before_reconciliation_when_items_remain() -> None:
    """Ensure unresolved processing work prevents Reconciliation from opening."""

    processing_active = _existing_states(
        completed=(WorkflowPhase.COLLECTION,),
        active=WorkflowPhase.PROCESSING,
    )

    with pytest.raises(PhaseGateError, match="Processing is blocked"):
        transition_to_next_phase(
            phase_states=processing_active,
            target_phase=WorkflowPhase.RECONCILIATION,
            signals=PhaseGateSignals(unresolved_processing_item_count=3),
        )


def test_signoff_readiness_requires_prior_phases_and_blocks_open_review_items() -> None:
    """Ensure Review / Sign-off is only ready after upstream phases and review blockers clear."""

    review_active = _existing_states(
        completed=(
            WorkflowPhase.COLLECTION,
            WorkflowPhase.PROCESSING,
            WorkflowPhase.RECONCILIATION,
            WorkflowPhase.REPORTING,
        ),
        active=WorkflowPhase.REVIEW_SIGNOFF,
    )

    blocked = evaluate_signoff_readiness(
        phase_states=review_active,
        signals=PhaseGateSignals(unresolved_signoff_item_count=1),
    )
    ready = evaluate_signoff_readiness(phase_states=review_active, signals=PhaseGateSignals())

    assert blocked.status is CloseRunPhaseStatus.BLOCKED
    assert blocked.blocking_reason is not None
    assert "sign-off" in blocked.blocking_reason
    assert ready.status is CloseRunPhaseStatus.READY


def test_reopened_phase_states_preserve_work_and_reopen_signoff() -> None:
    """Ensure reopening creates a working sign-off gate without resetting upstream phases."""

    reopened_at = datetime(2026, 4, 10, 10, 0, tzinfo=UTC)
    phase_states = build_reopened_phase_states(reopened_at=reopened_at)

    assert tuple(phase_state.phase for phase_state in phase_states) == CANONICAL_WORKFLOW_PHASES
    assert phase_states[-1].phase is WorkflowPhase.REVIEW_SIGNOFF
    assert phase_states[-1].status is CloseRunPhaseStatus.IN_PROGRESS
    assert all(
        phase_state.status is CloseRunPhaseStatus.COMPLETED
        for phase_state in phase_states[:-1]
    )
    assert all(phase_state.completed_at == reopened_at for phase_state in phase_states[:-1])


def _existing_states_from_initial() -> tuple[ExistingPhaseState, ...]:
    """Return the initial close-run phase states projected into evaluator input."""

    return tuple(
        ExistingPhaseState(
            phase=phase_state.phase,
            status=phase_state.status,
            blocking_reason=phase_state.blocking_reason,
            completed_at=phase_state.completed_at,
        )
        for phase_state in build_initial_phase_states()
    )


def _existing_states(
    *,
    completed: tuple[WorkflowPhase, ...],
    active: WorkflowPhase,
) -> tuple[ExistingPhaseState, ...]:
    """Build a phase-state tuple with explicit completed phases and one active phase."""

    completed_at = datetime(2026, 4, 10, 8, 0, tzinfo=UTC)
    phase_states: list[ExistingPhaseState] = []
    for phase in CANONICAL_WORKFLOW_PHASES:
        if phase in completed:
            phase_states.append(
                ExistingPhaseState(
                    phase=phase,
                    status=CloseRunPhaseStatus.COMPLETED,
                    blocking_reason=None,
                    completed_at=completed_at,
                )
            )
            continue
        phase_states.append(
            ExistingPhaseState(
                phase=phase,
                status=(
                    CloseRunPhaseStatus.IN_PROGRESS
                    if phase is active
                    else CloseRunPhaseStatus.NOT_STARTED
                ),
                blocking_reason=None,
                completed_at=None,
            )
        )

    return tuple(phase_states)
