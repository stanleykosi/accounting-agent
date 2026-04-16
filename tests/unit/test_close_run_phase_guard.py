"""
Purpose: Verify worker-side close-run phase guards for stale background jobs.
Scope: Active-phase success and cancellation behavior when workflow scope changes.
Dependencies: close_run phase guard helper and job cancellation error contract.
"""

from __future__ import annotations

from services.common.enums import WorkflowPhase
from services.jobs.retry_policy import JobCancellationRequestedError
from apps.worker.app.tasks.close_run_phase_guard import ensure_close_run_active_phase


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, *, close_run_exists: bool, phase_rows):
        self._close_run_exists = close_run_exists
        self._phase_rows = phase_rows

    def get(self, model, close_run_id):
        del model, close_run_id
        return object() if self._close_run_exists else None

    def execute(self, statement):
        del statement
        return _FakeResult(self._phase_rows)


def test_phase_guard_allows_expected_active_phase() -> None:
    """Workers should proceed when the close run is still in the required phase."""

    session = _FakeSession(
        close_run_exists=True,
        phase_rows=(
            ("collection", "completed"),
            ("processing", "completed"),
            ("reconciliation", "in_progress"),
            ("reporting", "not_started"),
            ("review_signoff", "not_started"),
        ),
    )

    ensure_close_run_active_phase(
        session=session,
        close_run_id="close-run-id",
        required_phase=WorkflowPhase.RECONCILIATION,
    )


def test_phase_guard_cancels_when_active_phase_has_moved() -> None:
    """Workers should stop before persisting if the close run rewound to another phase."""

    session = _FakeSession(
        close_run_exists=True,
        phase_rows=(
            ("collection", "completed"),
            ("processing", "in_progress"),
            ("reconciliation", "not_started"),
            ("reporting", "not_started"),
            ("review_signoff", "not_started"),
        ),
    )

    try:
        ensure_close_run_active_phase(
            session=session,
            close_run_id="close-run-id",
            required_phase=WorkflowPhase.REPORTING,
        )
    except JobCancellationRequestedError as error:
        assert error.details["required_phase"] == WorkflowPhase.REPORTING.value
        assert error.details["active_phase"] == WorkflowPhase.PROCESSING.value
    else:
        raise AssertionError("Expected the worker phase guard to cancel the stale job.")
