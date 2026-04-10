"""
Purpose: Verify the canonical domain-language layer introduced for workflow,
lifecycle, review, autonomy, and artifact vocabulary.
Scope: Enum catalogs and validation rules for close-run phase primitives.
Dependencies: services/common/enums.py, services/contracts/domain_models.py,
and pytest for assertion helpers.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from services.common.enums import (
    CANONICAL_WORKFLOW_PHASES,
    ArtifactType,
    AutonomyMode,
    CloseRunPhaseStatus,
    CloseRunStatus,
    JobStatus,
    ReviewStatus,
)
from services.contracts.domain_models import (
    CloseRunPhaseState,
    CloseRunWorkflowState,
    build_domain_language_catalog,
)


def test_domain_language_catalog_exposes_all_canonical_vocabularies() -> None:
    """Ensure the shared catalog includes every Step 5 enum family in canonical order."""

    catalog = build_domain_language_catalog()

    assert tuple(item.phase for item in catalog.workflow_phases) == CANONICAL_WORKFLOW_PHASES
    assert tuple(item.value for item in catalog.close_run_statuses) == CloseRunStatus.values()
    assert tuple(item.value for item in catalog.close_run_phase_statuses) == (
        CloseRunPhaseStatus.values()
    )
    assert tuple(item.value for item in catalog.job_statuses) == JobStatus.values()
    assert tuple(item.value for item in catalog.autonomy_modes) == AutonomyMode.values()
    assert tuple(item.value for item in catalog.review_statuses) == ReviewStatus.values()
    assert tuple(item.value for item in catalog.artifact_types) == ArtifactType.values()


def test_blocked_phase_state_requires_blocking_reason() -> None:
    """Ensure blocked phases fail fast when a recovery explanation is missing."""

    with pytest.raises(ValueError, match="blocking_reason"):
        CloseRunPhaseState(
            phase=CANONICAL_WORKFLOW_PHASES[0],
            status=CloseRunPhaseStatus.BLOCKED,
        )


def test_close_run_workflow_state_requires_full_phase_order() -> None:
    """Ensure workflow state snapshots cannot omit or reorder canonical phases."""

    started_at = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)
    out_of_order_states = (
        CloseRunPhaseState(
            phase=CANONICAL_WORKFLOW_PHASES[1],
            status=CloseRunPhaseStatus.IN_PROGRESS,
        ),
        CloseRunPhaseState(
            phase=CANONICAL_WORKFLOW_PHASES[0],
            status=CloseRunPhaseStatus.COMPLETED,
            completed_at=started_at,
        ),
        *(
            CloseRunPhaseState(phase=phase, status=CloseRunPhaseStatus.NOT_STARTED)
            for phase in CANONICAL_WORKFLOW_PHASES[2:]
        ),
    )

    with pytest.raises(ValueError, match="canonical workflow phase"):
        CloseRunWorkflowState(
            status=CloseRunStatus.IN_REVIEW,
            active_phase=CANONICAL_WORKFLOW_PHASES[1],
            phase_states=out_of_order_states,
        )
