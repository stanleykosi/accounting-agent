"""
Purpose: Verify close-run service orchestration around reopened-version carry-forward
and rewind invalidation summaries.
Scope: Focused unit coverage over the service/repository contract without a database.
Dependencies: CloseRunService, close-run repository records, and canonical workflow enums.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from services.close_runs.gates import PhaseGateSignals, build_reopened_phase_states
from services.close_runs.service import CloseRunService
from services.common.enums import (
    AutonomyMode,
    CloseRunOperatingMode,
    CloseRunPhaseStatus,
    CloseRunStatus,
    WorkflowPhase,
)
from services.db.models.audit import AuditSourceSurface
from services.db.models.entity import EntityStatus
from services.db.repositories.close_run_repo import (
    CloseRunAccessRecord,
    CloseRunEntityRecord,
    CloseRunLedgerBindingRecord,
    CloseRunPhaseStateRecord,
    CloseRunRecord,
    CloseRunStateResetSummary,
    ReopenedCloseRunCarryForwardSummary,
)
from services.db.repositories.entity_repo import EntityUserRecord


class _FakeCloseRunRepository:
    """Capture the service-facing repository interactions needed by these tests."""

    def __init__(
        self,
        *,
        access_record: CloseRunAccessRecord,
        phase_states: tuple[CloseRunPhaseStateRecord, ...],
    ) -> None:
        self.access_record = access_record
        self.phase_states_by_close_run_id = {
            access_record.close_run.id: phase_states,
        }
        self.carry_forward_summary = ReopenedCloseRunCarryForwardSummary()
        self.reset_summary = CloseRunStateResetSummary()
        self.review_action_payload: dict[str, object] | None = None
        self.activity_payload: dict[str, object] | None = None
        self.created_close_run: CloseRunRecord | None = None
        self.last_rewind_canceled_by_user_id: UUID | None = None
        self.bind_calls: list[dict[str, object]] = []
        self.ledger_bindings_by_close_run_id: dict[UUID, CloseRunLedgerBindingRecord] = {}
        self.phase_gate_signals = PhaseGateSignals()

    def get_entity_for_user(self, *, entity_id: UUID, user_id: UUID) -> CloseRunEntityRecord | None:
        del entity_id, user_id
        return self.access_record.entity

    def list_close_runs_for_entity(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> tuple[CloseRunRecord, ...]:
        del entity_id, user_id
        return (self.access_record.close_run,)

    def get_close_run_for_user(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        user_id: UUID,
    ) -> CloseRunAccessRecord | None:
        del entity_id, user_id
        if close_run_id == self.access_record.close_run.id:
            return self.access_record
        if self.created_close_run is not None and close_run_id == self.created_close_run.id:
            return CloseRunAccessRecord(
                close_run=self.created_close_run,
                entity=self.access_record.entity,
            )
        return None

    def find_open_close_run_for_period(
        self,
        *,
        entity_id: UUID,
        period_start: date,
        period_end: date,
    ) -> CloseRunRecord | None:
        del entity_id, period_start, period_end
        return None

    def next_version_no_for_period(
        self,
        *,
        entity_id: UUID,
        period_start: date,
        period_end: date,
    ) -> int:
        del entity_id, period_start, period_end
        return self.access_record.close_run.current_version_no + 1

    def create_close_run(
        self,
        *,
        entity_id: UUID,
        period_start: date,
        period_end: date,
        reporting_currency: str,
        current_version_no: int,
        opened_by_user_id: UUID,
        status: CloseRunStatus,
        reopened_from_close_run_id: UUID | None = None,
    ) -> CloseRunRecord:
        created_at = datetime(2026, 4, 16, 12, 0, tzinfo=UTC)
        self.created_close_run = CloseRunRecord(
            id=uuid4(),
            entity_id=entity_id,
            period_start=period_start,
            period_end=period_end,
            status=status,
            reporting_currency=reporting_currency,
            current_version_no=current_version_no,
            opened_by_user_id=opened_by_user_id,
            approved_by_user_id=None,
            approved_at=None,
            archived_at=None,
            reopened_from_close_run_id=reopened_from_close_run_id,
            created_at=created_at,
            updated_at=created_at,
        )
        return self.created_close_run

    def create_phase_states(
        self,
        *,
        close_run_id: UUID,
        phase_states: tuple,
    ) -> tuple[CloseRunPhaseStateRecord, ...]:
        records = tuple(
            _to_phase_state_record(close_run_id=close_run_id, state=state)
            for state in phase_states
        )
        self.phase_states_by_close_run_id[close_run_id] = records
        return records

    def bind_latest_imported_ledger_baseline(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        period_start: date,
        period_end: date,
        bound_by_user_id: UUID | None = None,
    ) -> CloseRunLedgerBindingRecord | None:
        self.bind_calls.append(
            {
                "entity_id": entity_id,
                "close_run_id": close_run_id,
                "period_start": period_start,
                "period_end": period_end,
                "bound_by_user_id": bound_by_user_id,
            }
        )
        return self.ledger_bindings_by_close_run_id.get(close_run_id)

    def carry_forward_working_state_for_reopened_close_run(
        self,
        *,
        source_close_run_id: UUID,
        target_close_run_id: UUID,
    ) -> ReopenedCloseRunCarryForwardSummary:
        del source_close_run_id, target_close_run_id
        return self.carry_forward_summary

    def list_phase_states(self, *, close_run_id: UUID) -> tuple[CloseRunPhaseStateRecord, ...]:
        return self.phase_states_by_close_run_id[close_run_id]

    def replace_phase_states(
        self,
        *,
        close_run_id: UUID,
        phase_states: tuple,
    ) -> tuple[CloseRunPhaseStateRecord, ...]:
        records = tuple(
            _to_phase_state_record(close_run_id=close_run_id, state=state)
            for state in phase_states
        )
        self.phase_states_by_close_run_id[close_run_id] = records
        return records

    def clear_state_after_phase_rewind(
        self,
        *,
        close_run_id: UUID,
        target_phase: WorkflowPhase,
        canceled_by_user_id: UUID | None = None,
    ) -> CloseRunStateResetSummary:
        del close_run_id, target_phase
        self.last_rewind_canceled_by_user_id = canceled_by_user_id
        return self.reset_summary

    def update_close_run_status(
        self,
        *,
        close_run_id: UUID,
        status: CloseRunStatus,
        approved_by_user_id: UUID | None = None,
        approved_at: datetime | None = None,
        archived_at: datetime | None = None,
    ) -> CloseRunRecord:
        target = (
            self.created_close_run
            if self.created_close_run and self.created_close_run.id == close_run_id
            else self.access_record.close_run
        )
        updated = replace(
            target,
            status=status,
            approved_by_user_id=approved_by_user_id,
            approved_at=approved_at,
            archived_at=archived_at,
            updated_at=datetime(2026, 4, 16, 12, 5, tzinfo=UTC),
        )
        if self.created_close_run is not None and updated.id == self.created_close_run.id:
            self.created_close_run = updated
        return updated

    def get_phase_gate_signals(self, *, close_run_id: UUID) -> PhaseGateSignals:
        del close_run_id
        return self.phase_gate_signals

    def get_close_run_ledger_binding(
        self,
        *,
        close_run_id: UUID,
    ) -> CloseRunLedgerBindingRecord | None:
        return self.ledger_bindings_by_close_run_id.get(close_run_id)

    def create_review_action(self, **kwargs) -> None:
        self.review_action_payload = kwargs.get("audit_payload")

    def create_activity_event(self, **kwargs) -> None:
        self.activity_payload = kwargs.get("payload")

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def is_integrity_error(self, error: Exception) -> bool:
        del error
        return False


def test_reopen_close_run_records_working_state_carry_forward_summary() -> None:
    """Reopening should surface the richer carry-forward summary in audit and timeline payloads."""

    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    access_record = _build_access_record(status=CloseRunStatus.APPROVED)
    repository = _FakeCloseRunRepository(
        access_record=access_record,
        phase_states=_reopened_phase_state_records(close_run_id=access_record.close_run.id),
    )
    repository.carry_forward_summary = ReopenedCloseRunCarryForwardSummary(
        document_count=4,
        recommendation_count=3,
        journal_count=2,
        reconciliation_count=2,
        supporting_schedule_count=4,
        report_run_count=1,
    )

    service = CloseRunService(repository=repository)
    response = service.reopen_close_run(
        actor_user=actor_user,
        entity_id=access_record.close_run.entity_id,
        close_run_id=access_record.close_run.id,
        reason="Need to update the April sign-off package.",
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="trace-reopen",
    )

    assert response.close_run.current_version_no == 2
    assert repository.review_action_payload is not None
    assert repository.review_action_payload["carry_forward_summary"]["journal_count"] == 2
    assert repository.activity_payload is not None
    assert repository.activity_payload["carry_forward_summary"]["report_run_count"] == 1
    assert "3 recommendation(s)" in str(repository.activity_payload["summary"])


def test_rewind_close_run_records_reset_summary_for_later_phase_invalidation() -> None:
    """Rewinding should record which later-phase artifacts were invalidated."""

    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    access_record = _build_access_record(status=CloseRunStatus.REOPENED)
    repository = _FakeCloseRunRepository(
        access_record=access_record,
        phase_states=_reopened_phase_state_records(close_run_id=access_record.close_run.id),
    )
    repository.reset_summary = CloseRunStateResetSummary(
        report_run_count=1,
        export_run_count=2,
        evidence_pack_count=1,
        canceled_job_count=3,
    )

    service = CloseRunService(repository=repository)
    response = service.rewind_close_run(
        actor_user=actor_user,
        entity_id=access_record.close_run.entity_id,
        close_run_id=access_record.close_run.id,
        target_phase=WorkflowPhase.REPORTING,
        reason="Need to regenerate commentary before release.",
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="trace-rewind",
    )

    assert response.active_phase is WorkflowPhase.REPORTING
    assert repository.last_rewind_canceled_by_user_id == actor_user.id
    assert repository.activity_payload is not None
    assert repository.activity_payload["reset_summary"]["export_run_count"] == 2
    assert repository.activity_payload["reset_summary"]["evidence_pack_count"] == 1
    assert repository.activity_payload["reset_summary"]["canceled_job_count"] == 3


def test_create_close_run_attempts_to_bind_latest_imported_ledger_baseline() -> None:
    """Fresh close runs should auto-bind any exact-period imported GL/TB baseline."""

    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    access_record = _build_access_record(status=CloseRunStatus.DRAFT)
    repository = _FakeCloseRunRepository(
        access_record=access_record,
        phase_states=_reopened_phase_state_records(close_run_id=access_record.close_run.id),
    )

    service = CloseRunService(repository=repository)
    response = service.create_close_run(
        actor_user=actor_user,
        entity_id=access_record.close_run.entity_id,
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
        reporting_currency=None,
        allow_duplicate_period=False,
        duplicate_period_reason=None,
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="trace-create",
    )

    assert response.reporting_currency == access_record.entity.base_currency
    assert repository.bind_calls
    assert repository.bind_calls[0]["close_run_id"] == repository.created_close_run.id
    assert repository.bind_calls[0]["bound_by_user_id"] == actor_user.id


def test_get_close_run_surfaces_detected_operating_mode() -> None:
    """Close-run summaries should expose the canonical operating mode and capabilities."""

    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    access_record = _build_access_record(status=CloseRunStatus.DRAFT)
    repository = _FakeCloseRunRepository(
        access_record=access_record,
        phase_states=_reopened_phase_state_records(close_run_id=access_record.close_run.id),
    )
    repository.phase_gate_signals = PhaseGateSignals(
        operating_mode=CloseRunOperatingMode.IMPORTED_GENERAL_LEDGER,
        operating_mode_description="Imported GL baseline is active for this run.",
        has_general_ledger_baseline=True,
        bank_reconciliation_available=True,
        trial_balance_review_available=True,
        general_ledger_export_available=True,
    )

    service = CloseRunService(repository=repository)
    summary = service.get_close_run(
        actor_user=actor_user,
        entity_id=access_record.close_run.entity_id,
        close_run_id=access_record.close_run.id,
    )

    assert summary.operating_mode.mode.value == "imported_general_ledger"
    assert summary.operating_mode.description == "Imported GL baseline is active for this run."
    assert summary.operating_mode.has_general_ledger_baseline is True
    assert summary.operating_mode.bank_reconciliation_available is True
    assert summary.operating_mode.general_ledger_export_available is True


def _build_access_record(*, status: CloseRunStatus) -> CloseRunAccessRecord:
    """Return one close-run access record suitable for service orchestration tests."""

    close_run = CloseRunRecord(
        id=uuid4(),
        entity_id=uuid4(),
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
        status=status,
        reporting_currency="USD",
        current_version_no=1,
        opened_by_user_id=uuid4(),
        approved_by_user_id=uuid4() if status is CloseRunStatus.APPROVED else None,
        approved_at=(
            datetime(2026, 4, 10, 9, 0, tzinfo=UTC)
            if status is CloseRunStatus.APPROVED
            else None
        ),
        archived_at=None,
        reopened_from_close_run_id=None,
        created_at=datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
        updated_at=datetime(2026, 4, 10, 9, 0, tzinfo=UTC),
    )
    entity = CloseRunEntityRecord(
        id=close_run.entity_id,
        name="AuraTune",
        base_currency="USD",
        autonomy_mode=AutonomyMode.HUMAN_REVIEW,
        status=EntityStatus.ACTIVE,
    )
    return CloseRunAccessRecord(close_run=close_run, entity=entity)


def _reopened_phase_state_records(
    *,
    close_run_id: UUID,
) -> tuple[CloseRunPhaseStateRecord, ...]:
    """Return canonical reopened phase-state records for one close run."""

    return tuple(
        _to_phase_state_record(close_run_id=close_run_id, state=state)
        for state in build_reopened_phase_states(
            reopened_at=datetime(2026, 4, 10, 10, 0, tzinfo=UTC)
        )
    )


def _to_phase_state_record(
    *,
    close_run_id: UUID,
    state,
) -> CloseRunPhaseStateRecord:
    """Convert one evaluated phase state into the immutable repository test record."""

    created_at = datetime(2026, 4, 10, 10, 0, tzinfo=UTC)
    return CloseRunPhaseStateRecord(
        id=uuid4(),
        close_run_id=close_run_id,
        phase=state.phase,
        status=(
            state.status
            if isinstance(state.status, CloseRunPhaseStatus)
            else CloseRunPhaseStatus(state.status)
        ),
        blocking_reason=state.blocking_reason,
        completed_at=state.completed_at,
        created_at=created_at,
        updated_at=created_at,
    )
