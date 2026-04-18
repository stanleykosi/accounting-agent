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
    rewind_to_phase,
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


def test_collection_gate_requires_at_least_one_approved_source_document() -> None:
    """Collection should stay blocked until at least one source document is approved."""

    evaluated = evaluate_phase_gates(
        phase_states=_existing_states_from_initial(),
        signals=PhaseGateSignals(),
    )

    collection = evaluated[0]

    assert collection.status is CloseRunPhaseStatus.BLOCKED
    assert collection.blocking_reason is not None
    assert "no approved source documents yet" in collection.blocking_reason


def test_collection_gate_mentions_pending_verification_and_transaction_mismatch() -> None:
    """Collection should stay blocked until verification and document matching are complete."""

    evaluated = evaluate_phase_gates(
        phase_states=_existing_states_from_initial(),
        signals=PhaseGateSignals(
            approved_document_count=1,
            pending_document_review_count=2,
            unmatched_transaction_count=1,
        ),
    )

    collection = evaluated[0]

    assert collection.status is CloseRunPhaseStatus.BLOCKED
    assert collection.blocking_reason is not None
    assert "awaiting verification" in collection.blocking_reason
    assert "matched to transactions" in collection.blocking_reason


def test_transition_opens_only_the_immediate_next_phase() -> None:
    """Ensure transitions complete the active ready phase and reject phase skipping."""

    transitioned_at = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)
    result = transition_to_next_phase(
        phase_states=_existing_states_from_initial(),
        target_phase=WorkflowPhase.PROCESSING,
        signals=PhaseGateSignals(approved_document_count=1),
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
            signals=PhaseGateSignals(approved_document_count=1),
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


def test_reconciliation_gate_blocks_missing_or_unreviewed_supporting_schedules() -> None:
    """Reconciliation should stay blocked until Step 6 workpapers are ready."""

    reconciliation_active = _existing_states(
        completed=(
            WorkflowPhase.COLLECTION,
            WorkflowPhase.PROCESSING,
        ),
        active=WorkflowPhase.RECONCILIATION,
    )

    blocked = evaluate_phase_gates(
        phase_states=reconciliation_active,
        signals=PhaseGateSignals(
            missing_supporting_schedules=("fixed_assets", "loan_amortisation"),
            pending_supporting_schedule_review_count=3,
        ),
    )

    reconciliation = blocked[2]

    assert reconciliation.phase is WorkflowPhase.RECONCILIATION
    assert reconciliation.status is CloseRunPhaseStatus.BLOCKED
    assert reconciliation.blocking_reason is not None
    assert "missing supporting schedules" in reconciliation.blocking_reason
    assert "awaiting review" in reconciliation.blocking_reason


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
        signals=PhaseGateSignals(
            missing_signoff_requirements=(
                "completed export package",
                "management distribution record",
            ),
            unresolved_signoff_item_count=2,
        ),
    )
    ready = evaluate_signoff_readiness(phase_states=review_active, signals=PhaseGateSignals())

    assert blocked.status is CloseRunPhaseStatus.BLOCKED
    assert blocked.blocking_reason is not None
    assert "management distribution" in blocked.blocking_reason
    assert ready.status is CloseRunPhaseStatus.READY


def test_reopened_phase_states_preserve_work_and_reopen_signoff() -> None:
    """Ensure reopening creates a working sign-off gate without resetting upstream phases."""

    reopened_at = datetime(2026, 4, 10, 10, 0, tzinfo=UTC)
    phase_states = build_reopened_phase_states(reopened_at=reopened_at)

    assert tuple(phase_state.phase for phase_state in phase_states) == CANONICAL_WORKFLOW_PHASES
    assert phase_states[-1].phase is WorkflowPhase.REVIEW_SIGNOFF
    assert phase_states[-1].status is CloseRunPhaseStatus.IN_PROGRESS
    assert all(
        phase_state.status is CloseRunPhaseStatus.COMPLETED for phase_state in phase_states[:-1]
    )
    assert all(phase_state.completed_at == reopened_at for phase_state in phase_states[:-1])


def test_rewind_to_phase_reopens_earlier_phase_and_clears_later_progress() -> None:
    """Rewinding should reopen the target phase and reset downstream phases."""

    phase_states = _existing_states(
        completed=(
            WorkflowPhase.COLLECTION,
            WorkflowPhase.PROCESSING,
        ),
        active=WorkflowPhase.RECONCILIATION,
    )

    result = rewind_to_phase(
        phase_states=phase_states,
        target_phase=WorkflowPhase.PROCESSING,
        signals=PhaseGateSignals(),
    )

    assert result.previous_active_phase is WorkflowPhase.RECONCILIATION
    assert result.active_phase is WorkflowPhase.PROCESSING
    assert result.phase_states[0].status is CloseRunPhaseStatus.COMPLETED
    assert result.phase_states[1].status is CloseRunPhaseStatus.IN_PROGRESS
    assert result.phase_states[1].completed_at is None
    assert all(
        phase_state.status is CloseRunPhaseStatus.NOT_STARTED
        for phase_state in result.phase_states[2:]
    )


def test_rewind_to_phase_rejects_same_or_later_target() -> None:
    """Rewinding only allows moving back into an earlier phase."""

    phase_states = _existing_states(
        completed=(WorkflowPhase.COLLECTION,),
        active=WorkflowPhase.PROCESSING,
    )

    with pytest.raises(PhaseGateError, match="rewind target must be earlier"):
        rewind_to_phase(
            phase_states=phase_states,
            target_phase=WorkflowPhase.PROCESSING,
            signals=PhaseGateSignals(),
        )


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
