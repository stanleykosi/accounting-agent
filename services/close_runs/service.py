"""
Purpose: Orchestrate close-run creation, workflow phase transitions, approval,
archival, and reopening.
Scope: Entity access checks, duplicate-period controls, deterministic phase-gate
evaluation, lifecycle state mutation, and timeline/review event emission.
Dependencies: Close-run contracts, gate evaluator, repository records, audit
source surfaces, and UUID serialization helpers.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from services.auth.service import serialize_uuid
from services.close_runs.gates import (
    EvaluatedPhaseState,
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
    AutonomyMode,
    CloseRunPhaseStatus,
    CloseRunStatus,
    WorkflowPhase,
)
from services.common.types import JsonObject, utc_now
from services.contracts.close_run_models import (
    CloseRunListResponse,
    CloseRunOperatingModeSummary,
    CloseRunReopenResponse,
    CloseRunRewindResponse,
    CloseRunSummary,
    CloseRunTransitionResponse,
)
from services.contracts.domain_models import CloseRunPhaseState, CloseRunWorkflowState
from services.contracts.ledger_models import CloseRunLedgerBindingSummary
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


class CloseRunServiceErrorCode(StrEnum):
    """Enumerate stable error codes surfaced by close-run workflows."""

    CLOSE_RUN_NOT_FOUND = "close_run_not_found"
    DUPLICATE_PERIOD = "duplicate_period"
    ENTITY_ARCHIVED = "entity_archived"
    ENTITY_NOT_FOUND = "entity_not_found"
    DELETE_NOT_ALLOWED = "delete_not_allowed"
    INTEGRITY_CONFLICT = "integrity_conflict"
    INVALID_TRANSITION = "invalid_transition"
    PHASE_BLOCKED = "phase_blocked"
    APPROVAL_BLOCKED = "approval_blocked"
    ARCHIVE_NOT_ALLOWED = "archive_not_allowed"
    REOPEN_NOT_ALLOWED = "reopen_not_allowed"


class CloseRunServiceError(Exception):
    """Represent an expected close-run-domain failure for API translation."""

    def __init__(self, *, status_code: int, code: CloseRunServiceErrorCode, message: str) -> None:
        """Capture HTTP status, stable code, and operator-facing recovery message."""

        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class CloseRunRepositoryProtocol(Protocol):
    """Describe the persistence operations required by close-run workflows."""

    def get_entity_for_user(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> CloseRunEntityRecord | None:
        """Return an entity when the user has access."""

    def list_close_runs_for_entity(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> tuple[CloseRunRecord, ...]:
        """Return close runs visible to the user for an entity."""

    def get_close_run_for_user(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        user_id: UUID,
    ) -> CloseRunAccessRecord | None:
        """Return one close run and entity when accessible."""

    def find_open_close_run_for_period(
        self,
        *,
        entity_id: UUID,
        period_start: date,
        period_end: date,
    ) -> CloseRunRecord | None:
        """Return an existing open close run for an exact period."""

    def next_version_no_for_period(
        self,
        *,
        entity_id: UUID,
        period_start: date,
        period_end: date,
    ) -> int:
        """Return the next close-run version for an entity-period pair."""

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
        """Persist a new close-run row."""

    def create_phase_states(
        self,
        *,
        close_run_id: UUID,
        phase_states: tuple[EvaluatedPhaseState, ...],
    ) -> tuple[CloseRunPhaseStateRecord, ...]:
        """Persist the five canonical phase states for a close run."""

    def bind_latest_imported_ledger_baseline(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        period_start: date,
        period_end: date,
        bound_by_user_id: UUID | None = None,
    ) -> CloseRunLedgerBindingRecord | None:
        """Bind the newest exact-period GL/TB imports to a fresh close run when present."""

    def carry_forward_working_state_for_reopened_close_run(
        self,
        *,
        source_close_run_id: UUID,
        target_close_run_id: UUID,
    ) -> ReopenedCloseRunCarryForwardSummary:
        """Clone current-state workflow artifacts into a reopened close run."""

    def clear_state_after_phase_rewind(
        self,
        *,
        close_run_id: UUID,
        target_phase: WorkflowPhase,
        canceled_by_user_id: UUID | None = None,
    ) -> CloseRunStateResetSummary:
        """Delete later-phase derived state after rewinding workflow."""

    def list_phase_states(self, *, close_run_id: UUID) -> tuple[CloseRunPhaseStateRecord, ...]:
        """Return all phase-state rows for one close run."""

    def replace_phase_states(
        self,
        *,
        close_run_id: UUID,
        phase_states: tuple[EvaluatedPhaseState, ...],
    ) -> tuple[CloseRunPhaseStateRecord, ...]:
        """Persist recalculated phase-state rows."""

    def update_close_run_status(
        self,
        *,
        close_run_id: UUID,
        status: CloseRunStatus,
        approved_by_user_id: UUID | None = None,
        approved_at: datetime | None = None,
        archived_at: datetime | None = None,
    ) -> CloseRunRecord:
        """Persist a lifecycle status update."""

    def get_phase_gate_signals(self, *, close_run_id: UUID) -> PhaseGateSignals:
        """Return the deterministic readiness signals for one close run."""

    def get_close_run_ledger_binding(
        self,
        *,
        close_run_id: UUID,
    ) -> CloseRunLedgerBindingRecord | None:
        """Return the imported ledger baseline bound to one close run."""

    def create_review_action(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        target_type: str,
        target_id: UUID,
        actor_user_id: UUID,
        autonomy_mode: AutonomyMode,
        source_surface: AuditSourceSurface,
        action: str,
        reason: str | None,
        before_payload: JsonObject | None,
        after_payload: JsonObject | None,
        trace_id: str | None,
        audit_payload: JsonObject | None = None,
    ) -> None:
        """Persist an immutable review action for a close-run decision."""

    def create_activity_event(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        actor_user_id: UUID | None,
        event_type: str,
        source_surface: AuditSourceSurface,
        payload: JsonObject,
        trace_id: str | None,
    ) -> None:
        """Persist one close-run timeline event."""

    def commit(self) -> None:
        """Commit the current unit of work."""

    def rollback(self) -> None:
        """Rollback the current unit of work."""

    def is_integrity_error(self, error: Exception) -> bool:
        """Return whether the provided exception originated from the database."""


class CloseRunService:
    """Provide the canonical close-run lifecycle workflow used by API routes."""

    def __init__(self, *, repository: CloseRunRepositoryProtocol) -> None:
        """Capture the close-run persistence boundary."""

        self._repository = repository

    def list_close_runs_for_entity(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
    ) -> CloseRunListResponse:
        """Return accessible close runs for one entity with calculated phase states."""

        self._require_entity_access(entity_id=entity_id, user_id=actor_user.id)
        close_runs = self._repository.list_close_runs_for_entity(
            entity_id=entity_id,
            user_id=actor_user.id,
        )
        return CloseRunListResponse(
            close_runs=tuple(self._build_close_run_summary(close_run) for close_run in close_runs)
        )

    def create_close_run(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        period_start: date,
        period_end: date,
        reporting_currency: str | None,
        allow_duplicate_period: bool,
        duplicate_period_reason: str | None,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> CloseRunSummary:
        """Create a close run, seed phase gates, and emit the opening timeline event."""

        entity = self._require_active_entity(entity_id=entity_id, user_id=actor_user.id)
        duplicate = self._repository.find_open_close_run_for_period(
            entity_id=entity_id,
            period_start=period_start,
            period_end=period_end,
        )
        if duplicate is not None and not allow_duplicate_period:
            raise CloseRunServiceError(
                status_code=409,
                code=CloseRunServiceErrorCode.DUPLICATE_PERIOD,
                message=(
                    "An open close run already exists for that entity and period. "
                    "Reopen or finish the existing run, or explicitly provide a duplicate reason."
                ),
            )

        version_no = self._repository.next_version_no_for_period(
            entity_id=entity_id,
            period_start=period_start,
            period_end=period_end,
        )
        resolved_currency = reporting_currency or entity.base_currency

        try:
            close_run = self._repository.create_close_run(
                entity_id=entity_id,
                period_start=period_start,
                period_end=period_end,
                reporting_currency=resolved_currency,
                current_version_no=version_no,
                opened_by_user_id=actor_user.id,
                status=CloseRunStatus.DRAFT,
            )
            self._repository.create_phase_states(
                close_run_id=close_run.id,
                phase_states=build_initial_phase_states(),
            )
            self._repository.bind_latest_imported_ledger_baseline(
                entity_id=entity_id,
                close_run_id=close_run.id,
                period_start=period_start,
                period_end=period_end,
                bound_by_user_id=actor_user.id,
            )
            self._repository.create_activity_event(
                entity_id=entity_id,
                close_run_id=close_run.id,
                actor_user_id=actor_user.id,
                event_type="close_run.created",
                source_surface=source_surface,
                payload={
                    "summary": (
                        f"{actor_user.full_name} opened close run "
                        f"{period_start.isoformat()} to {period_end.isoformat()}."
                    ),
                    "duplicate_period_reason": duplicate_period_reason,
                },
                trace_id=trace_id,
            )
            self._repository.commit()
        except Exception as error:
            self._repository.rollback()
            if self._repository.is_integrity_error(error):
                raise CloseRunServiceError(
                    status_code=409,
                    code=CloseRunServiceErrorCode.INTEGRITY_CONFLICT,
                    message=(
                        "The close run could not be created because the period version conflicts."
                    ),
                ) from error
            raise

        return self._build_close_run_summary(close_run)

    def get_close_run(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
    ) -> CloseRunSummary:
        """Return one accessible close run with recalculated phase gates."""

        access_record = self._require_close_run_access(
            entity_id=entity_id,
            close_run_id=close_run_id,
            user_id=actor_user.id,
        )
        return self._build_close_run_summary(access_record.close_run)

    def transition_close_run(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
        target_phase: WorkflowPhase,
        reason: str | None,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> CloseRunTransitionResponse:
        """Advance the active phase into its immediate successor after gate evaluation."""

        access_record = self._require_close_run_access(
            entity_id=entity_id,
            close_run_id=close_run_id,
            user_id=actor_user.id,
        )
        self._require_mutable_status(access_record.close_run)

        try:
            transition = transition_to_next_phase(
                phase_states=self._existing_phase_states(close_run_id=close_run_id),
                target_phase=target_phase,
                signals=self._repository.get_phase_gate_signals(close_run_id=close_run_id),
            )
        except PhaseGateError as error:
            raise CloseRunServiceError(
                status_code=409,
                code=CloseRunServiceErrorCode.PHASE_BLOCKED,
                message=str(error),
            ) from error

        next_status = (
            CloseRunStatus.IN_REVIEW
            if access_record.close_run.status is CloseRunStatus.DRAFT
            else access_record.close_run.status
        )

        try:
            self._repository.replace_phase_states(
                close_run_id=close_run_id,
                phase_states=transition.phase_states,
            )
            close_run = self._repository.update_close_run_status(
                close_run_id=close_run_id,
                status=next_status,
            )
            self._repository.create_activity_event(
                entity_id=entity_id,
                close_run_id=close_run_id,
                actor_user_id=actor_user.id,
                event_type="close_run.phase_transitioned",
                source_surface=source_surface,
                payload={
                    "summary": (
                        f"{actor_user.full_name} completed {transition.completed_phase.label} "
                        f"and opened {transition.active_phase.label}."
                    ),
                    "completed_phase": transition.completed_phase.value,
                    "active_phase": transition.active_phase.value,
                    "reason": reason,
                },
                trace_id=trace_id,
            )
            self._repository.commit()
        except Exception:
            self._repository.rollback()
            raise

        return CloseRunTransitionResponse(
            close_run=self._build_close_run_summary(close_run),
            completed_phase=transition.completed_phase,
            active_phase=transition.active_phase,
        )

    def rewind_close_run(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
        target_phase: WorkflowPhase,
        reason: str | None,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> CloseRunRewindResponse:
        """Reopen an earlier workflow phase on a mutable close run."""

        access_record = self._require_close_run_access(
            entity_id=entity_id,
            close_run_id=close_run_id,
            user_id=actor_user.id,
        )
        self._require_mutable_status(access_record.close_run)

        try:
            rewind = rewind_to_phase(
                phase_states=self._existing_phase_states(close_run_id=close_run_id),
                target_phase=target_phase,
                signals=self._repository.get_phase_gate_signals(close_run_id=close_run_id),
            )
        except PhaseGateError as error:
            raise CloseRunServiceError(
                status_code=409,
                code=CloseRunServiceErrorCode.INVALID_TRANSITION,
                message=str(error),
            ) from error

        try:
            self._repository.replace_phase_states(
                close_run_id=close_run_id,
                phase_states=rewind.phase_states,
            )
            reset_summary = self._repository.clear_state_after_phase_rewind(
                close_run_id=close_run_id,
                target_phase=target_phase,
                canceled_by_user_id=actor_user.id,
            )
            self._repository.create_activity_event(
                entity_id=entity_id,
                close_run_id=close_run_id,
                actor_user_id=actor_user.id,
                event_type="close_run.phase_rewound",
                source_surface=source_surface,
                payload={
                    "summary": (
                        f"{actor_user.full_name} moved the close run from "
                        f"{rewind.previous_active_phase.label} back to {rewind.active_phase.label}."
                    ),
                    "previous_active_phase": rewind.previous_active_phase.value,
                    "active_phase": rewind.active_phase.value,
                    "reset_summary": _build_reset_summary_payload(reset_summary),
                    "reason": reason,
                },
                trace_id=trace_id,
            )
            self._repository.commit()
        except Exception:
            self._repository.rollback()
            raise

        return CloseRunRewindResponse(
            close_run=self._build_close_run_summary(access_record.close_run),
            previous_active_phase=rewind.previous_active_phase,
            active_phase=rewind.active_phase,
        )

    def approve_close_run(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
        reason: str | None,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> CloseRunSummary:
        """Sign off a close run after the final gate proves ready."""

        access_record = self._require_close_run_access(
            entity_id=entity_id,
            close_run_id=close_run_id,
            user_id=actor_user.id,
        )
        self._require_mutable_status(access_record.close_run)

        existing_states = self._existing_phase_states(close_run_id=close_run_id)
        try:
            review_state = evaluate_signoff_readiness(
                phase_states=existing_states,
                signals=self._repository.get_phase_gate_signals(close_run_id=close_run_id),
            )
        except PhaseGateError as error:
            raise CloseRunServiceError(
                status_code=409,
                code=CloseRunServiceErrorCode.APPROVAL_BLOCKED,
                message=str(error),
            ) from error

        if review_state.status is CloseRunPhaseStatus.BLOCKED:
            raise CloseRunServiceError(
                status_code=409,
                code=CloseRunServiceErrorCode.APPROVAL_BLOCKED,
                message=review_state.blocking_reason or "Review / Sign-off is blocked.",
            )
        if review_state.status is not CloseRunPhaseStatus.READY:
            raise CloseRunServiceError(
                status_code=409,
                code=CloseRunServiceErrorCode.APPROVAL_BLOCKED,
                message="Review / Sign-off is not ready. Complete the prior workflow phases first.",
            )

        approved_at = utc_now()
        approved_states = tuple(
            EvaluatedPhaseState(
                phase=phase_state.phase,
                status=(
                    CloseRunPhaseStatus.COMPLETED
                    if phase_state.phase is WorkflowPhase.REVIEW_SIGNOFF
                    else phase_state.status
                ),
                blocking_reason=None,
                completed_at=(
                    approved_at
                    if phase_state.phase is WorkflowPhase.REVIEW_SIGNOFF
                    else phase_state.completed_at
                ),
            )
            for phase_state in evaluate_phase_gates(
                phase_states=existing_states,
                signals=self._repository.get_phase_gate_signals(close_run_id=close_run_id),
            )
        )

        before_payload = _build_close_run_payload(access_record.close_run)
        try:
            self._repository.replace_phase_states(
                close_run_id=close_run_id,
                phase_states=approved_states,
            )
            close_run = self._repository.update_close_run_status(
                close_run_id=close_run_id,
                status=CloseRunStatus.APPROVED,
                approved_by_user_id=actor_user.id,
                approved_at=approved_at,
            )
            self._repository.create_review_action(
                entity_id=entity_id,
                close_run_id=close_run_id,
                target_type="close_run",
                target_id=close_run_id,
                actor_user_id=actor_user.id,
                autonomy_mode=access_record.entity.autonomy_mode,
                source_surface=source_surface,
                action="approve",
                reason=reason,
                before_payload=before_payload,
                after_payload=_build_close_run_payload(close_run),
                trace_id=trace_id,
                audit_payload={"summary": f"{actor_user.full_name} approved the close run."},
            )
            self._repository.create_activity_event(
                entity_id=entity_id,
                close_run_id=close_run_id,
                actor_user_id=actor_user.id,
                event_type="close_run.approved",
                source_surface=source_surface,
                payload={
                    "summary": f"{actor_user.full_name} approved the close run.",
                    "reason": reason,
                },
                trace_id=trace_id,
            )
            self._repository.commit()
        except Exception:
            self._repository.rollback()
            raise

        return self._build_close_run_summary(close_run)

    def archive_close_run(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
        reason: str | None,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> CloseRunSummary:
        """Archive a signed-off or exported close run while preserving its history."""

        access_record = self._require_close_run_access(
            entity_id=entity_id,
            close_run_id=close_run_id,
            user_id=actor_user.id,
        )
        if access_record.close_run.status not in {CloseRunStatus.APPROVED, CloseRunStatus.EXPORTED}:
            raise CloseRunServiceError(
                status_code=409,
                code=CloseRunServiceErrorCode.ARCHIVE_NOT_ALLOWED,
                message="Only approved or exported close runs can be archived.",
            )

        archived_at = utc_now()
        before_payload = _build_close_run_payload(access_record.close_run)
        try:
            close_run = self._repository.update_close_run_status(
                close_run_id=close_run_id,
                status=CloseRunStatus.ARCHIVED,
                archived_at=archived_at,
            )
            self._repository.create_review_action(
                entity_id=entity_id,
                close_run_id=close_run_id,
                target_type="close_run",
                target_id=close_run_id,
                actor_user_id=actor_user.id,
                autonomy_mode=access_record.entity.autonomy_mode,
                source_surface=source_surface,
                action="archive",
                reason=reason,
                before_payload=before_payload,
                after_payload=_build_close_run_payload(close_run),
                trace_id=trace_id,
                audit_payload={"summary": f"{actor_user.full_name} archived the close run."},
            )
            self._repository.create_activity_event(
                entity_id=entity_id,
                close_run_id=close_run_id,
                actor_user_id=actor_user.id,
                event_type="close_run.archived",
                source_surface=source_surface,
                payload={
                    "summary": f"{actor_user.full_name} archived the close run.",
                    "reason": reason,
                },
                trace_id=trace_id,
            )
            self._repository.commit()
        except Exception:
            self._repository.rollback()
            raise

        return self._build_close_run_summary(close_run)

    def reopen_close_run(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
        reason: str | None,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> CloseRunReopenResponse:
        """Create a new reopened working version from a signed-off or released close run."""

        access_record = self._require_close_run_access(
            entity_id=entity_id,
            close_run_id=close_run_id,
            user_id=actor_user.id,
        )
        if access_record.close_run.status not in {
            CloseRunStatus.APPROVED,
            CloseRunStatus.EXPORTED,
            CloseRunStatus.ARCHIVED,
        }:
            raise CloseRunServiceError(
                status_code=409,
                code=CloseRunServiceErrorCode.REOPEN_NOT_ALLOWED,
                message="Only approved, exported, or archived close runs can be reopened.",
            )

        version_no = self._repository.next_version_no_for_period(
            entity_id=entity_id,
            period_start=access_record.close_run.period_start,
            period_end=access_record.close_run.period_end,
        )

        try:
            reopened_close_run = self._repository.create_close_run(
                entity_id=entity_id,
                period_start=access_record.close_run.period_start,
                period_end=access_record.close_run.period_end,
                reporting_currency=access_record.close_run.reporting_currency,
                current_version_no=version_no,
                opened_by_user_id=actor_user.id,
                status=CloseRunStatus.REOPENED,
                reopened_from_close_run_id=close_run_id,
            )
            self._repository.create_phase_states(
                close_run_id=reopened_close_run.id,
                phase_states=build_reopened_phase_states(),
            )
            carry_forward_summary = (
                self._repository.carry_forward_working_state_for_reopened_close_run(
                    source_close_run_id=close_run_id,
                    target_close_run_id=reopened_close_run.id,
                )
            )
            self._repository.create_review_action(
                entity_id=entity_id,
                close_run_id=close_run_id,
                target_type="close_run",
                target_id=close_run_id,
                actor_user_id=actor_user.id,
                autonomy_mode=access_record.entity.autonomy_mode,
                source_surface=source_surface,
                action="reopen",
                reason=reason,
                before_payload=_build_close_run_payload(access_record.close_run),
                after_payload=_build_close_run_payload(reopened_close_run),
                trace_id=trace_id,
                audit_payload={
                    "summary": (
                        f"{actor_user.full_name} reopened the close run as version {version_no}."
                    ),
                    "reopened_close_run_id": serialize_uuid(reopened_close_run.id),
                    "carry_forward_summary": _build_carry_forward_summary_payload(
                        carry_forward_summary
                    ),
                },
            )
            self._repository.create_activity_event(
                entity_id=entity_id,
                close_run_id=reopened_close_run.id,
                actor_user_id=actor_user.id,
                event_type="close_run.reopened",
                source_surface=source_surface,
                payload={
                    "summary": (
                        f"{actor_user.full_name} reopened close run "
                        f"version {access_record.close_run.current_version_no} "
                        f"as version {version_no} and carried forward "
                        f"{_describe_carry_forward_summary(carry_forward_summary)}."
                    ),
                    "source_close_run_id": serialize_uuid(close_run_id),
                    "carry_forward_summary": _build_carry_forward_summary_payload(
                        carry_forward_summary
                    ),
                    "reason": reason,
                },
                trace_id=trace_id,
            )
            self._repository.commit()
        except Exception as error:
            self._repository.rollback()
            if self._repository.is_integrity_error(error):
                raise CloseRunServiceError(
                    status_code=409,
                    code=CloseRunServiceErrorCode.INTEGRITY_CONFLICT,
                    message="The reopened close run conflicts with an existing period version.",
                ) from error
            raise

        return CloseRunReopenResponse(
            close_run=self._build_close_run_summary(reopened_close_run),
            source_close_run_id=serialize_uuid(close_run_id),
            status="reopened",
        )

    def _require_entity_access(self, *, entity_id: UUID, user_id: UUID) -> CloseRunEntityRecord:
        """Load an accessible entity or raise the canonical not-found error."""

        entity = self._repository.get_entity_for_user(entity_id=entity_id, user_id=user_id)
        if entity is None:
            raise CloseRunServiceError(
                status_code=404,
                code=CloseRunServiceErrorCode.ENTITY_NOT_FOUND,
                message="That workspace does not exist or is not accessible to the current user.",
            )

        return entity

    def _require_active_entity(self, *, entity_id: UUID, user_id: UUID) -> CloseRunEntityRecord:
        """Load an accessible active entity before creating new close-run state."""

        entity = self._require_entity_access(entity_id=entity_id, user_id=user_id)
        if entity.status is EntityStatus.ARCHIVED:
            raise CloseRunServiceError(
                status_code=409,
                code=CloseRunServiceErrorCode.ENTITY_ARCHIVED,
                message="Archived workspaces cannot open new close runs.",
            )

        return entity

    def _require_close_run_access(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        user_id: UUID,
    ) -> CloseRunAccessRecord:
        """Load one accessible close run or raise the canonical not-found error."""

        access_record = self._repository.get_close_run_for_user(
            entity_id=entity_id,
            close_run_id=close_run_id,
            user_id=user_id,
        )
        if access_record is None:
            raise CloseRunServiceError(
                status_code=404,
                code=CloseRunServiceErrorCode.CLOSE_RUN_NOT_FOUND,
                message="That close run does not exist or is not accessible to the current user.",
            )

        return access_record

    def _require_mutable_status(self, close_run: CloseRunRecord) -> None:
        """Ensure workflow mutations only touch open working close runs."""

        if close_run.status not in {
            CloseRunStatus.DRAFT,
            CloseRunStatus.IN_REVIEW,
            CloseRunStatus.REOPENED,
        }:
            raise CloseRunServiceError(
                status_code=409,
                code=CloseRunServiceErrorCode.INVALID_TRANSITION,
                message="Only draft, in-review, or reopened close runs can be changed.",
            )

    def _existing_phase_states(self, *, close_run_id: UUID) -> tuple[ExistingPhaseState, ...]:
        """Project repository phase rows into gate-evaluator input."""

        return tuple(
            ExistingPhaseState(
                phase=phase_state.phase,
                status=phase_state.status,
                blocking_reason=phase_state.blocking_reason,
                completed_at=phase_state.completed_at,
            )
            for phase_state in self._repository.list_phase_states(close_run_id=close_run_id)
        )

    def _build_close_run_summary(self, close_run: CloseRunRecord) -> CloseRunSummary:
        """Convert one close-run record into the public API response contract."""

        existing_states = self._existing_phase_states(close_run_id=close_run.id)
        signals = self._repository.get_phase_gate_signals(close_run_id=close_run.id)
        phase_states = evaluate_phase_gates(
            phase_states=existing_states,
            signals=signals,
        )
        workflow_state = CloseRunWorkflowState(
            status=close_run.status,
            active_phase=_resolve_active_phase(phase_states=phase_states),
            phase_states=tuple(_build_contract_phase_state(state) for state in phase_states),
        )
        return CloseRunSummary(
            id=serialize_uuid(close_run.id),
            entity_id=serialize_uuid(close_run.entity_id),
            period_start=close_run.period_start,
            period_end=close_run.period_end,
            status=close_run.status,
            reporting_currency=close_run.reporting_currency,
            current_version_no=close_run.current_version_no,
            opened_by_user_id=serialize_uuid(close_run.opened_by_user_id),
            approved_by_user_id=(
                serialize_uuid(close_run.approved_by_user_id)
                if close_run.approved_by_user_id is not None
                else None
            ),
            approved_at=close_run.approved_at,
            archived_at=close_run.archived_at,
            reopened_from_close_run_id=(
                serialize_uuid(close_run.reopened_from_close_run_id)
                if close_run.reopened_from_close_run_id is not None
                else None
            ),
            ledger_binding=_build_ledger_binding_summary(
                self._repository.get_close_run_ledger_binding(close_run_id=close_run.id)
            ),
            operating_mode=_build_operating_mode_summary(signals=signals),
            workflow_state=workflow_state,
            created_at=close_run.created_at,
            updated_at=close_run.updated_at,
        )


def _build_contract_phase_state(phase_state: EvaluatedPhaseState) -> CloseRunPhaseState:
    """Convert one evaluated gate state into the shared domain contract."""

    return CloseRunPhaseState(
        phase=phase_state.phase,
        status=phase_state.status,
        blocking_reason=phase_state.blocking_reason,
        completed_at=phase_state.completed_at,
    )


def _build_ledger_binding_summary(
    binding: CloseRunLedgerBindingRecord | None,
) -> CloseRunLedgerBindingSummary | None:
    """Convert one repository binding record into the public close-run contract."""

    if binding is None:
        return None

    return CloseRunLedgerBindingSummary(
        close_run_id=serialize_uuid(binding.close_run_id),
        general_ledger_import_batch_id=(
            serialize_uuid(binding.general_ledger_import_batch_id)
            if binding.general_ledger_import_batch_id is not None
            else None
        ),
        trial_balance_import_batch_id=(
            serialize_uuid(binding.trial_balance_import_batch_id)
            if binding.trial_balance_import_batch_id is not None
            else None
        ),
        binding_source=binding.binding_source,
        bound_by_user_id=(
            serialize_uuid(binding.bound_by_user_id)
            if binding.bound_by_user_id is not None
            else None
        ),
        created_at=binding.created_at,
        updated_at=binding.updated_at,
    )


def _build_operating_mode_summary(*, signals: PhaseGateSignals) -> CloseRunOperatingModeSummary:
    """Convert phase-gate operating signals into the public close-run mode contract."""

    description = signals.operating_mode_description or signals.operating_mode.description
    return CloseRunOperatingModeSummary(
        mode=signals.operating_mode,
        description=description,
        has_general_ledger_baseline=signals.has_general_ledger_baseline,
        has_trial_balance_baseline=signals.has_trial_balance_baseline,
        has_working_ledger_entries=signals.has_working_ledger_entries,
        bank_reconciliation_available=signals.bank_reconciliation_available,
        trial_balance_review_available=signals.trial_balance_review_available,
        journal_posting_available=signals.journal_posting_available,
        general_ledger_export_available=signals.general_ledger_export_available,
    )


def _resolve_active_phase(
    *,
    phase_states: tuple[EvaluatedPhaseState, ...],
) -> WorkflowPhase | None:
    """Return the first incomplete phase, or null when the workflow is complete."""

    for phase_state in phase_states:
        if phase_state.status is not CloseRunPhaseStatus.COMPLETED:
            return phase_state.phase

    return None


def _build_close_run_payload(close_run: CloseRunRecord) -> JsonObject:
    """Build a compact JSON-safe lifecycle snapshot for review records."""

    return {
        "id": serialize_uuid(close_run.id),
        "entity_id": serialize_uuid(close_run.entity_id),
        "status": close_run.status.value,
        "period_start": close_run.period_start.isoformat(),
        "period_end": close_run.period_end.isoformat(),
        "reporting_currency": close_run.reporting_currency,
        "current_version_no": close_run.current_version_no,
        "approved_by_user_id": (
            serialize_uuid(close_run.approved_by_user_id)
            if close_run.approved_by_user_id is not None
            else None
        ),
        "reopened_from_close_run_id": (
            serialize_uuid(close_run.reopened_from_close_run_id)
            if close_run.reopened_from_close_run_id is not None
            else None
        ),
    }


def _build_carry_forward_summary_payload(
    carry_forward_summary: ReopenedCloseRunCarryForwardSummary,
) -> JsonObject:
    """Return a stable audit payload for reopened close-run carry-forward state."""

    return {
        "document_count": carry_forward_summary.document_count,
        "recommendation_count": carry_forward_summary.recommendation_count,
        "journal_count": carry_forward_summary.journal_count,
        "reconciliation_count": carry_forward_summary.reconciliation_count,
        "supporting_schedule_count": carry_forward_summary.supporting_schedule_count,
        "report_run_count": carry_forward_summary.report_run_count,
    }


def _build_reset_summary_payload(reset_summary: CloseRunStateResetSummary) -> JsonObject:
    """Return a stable audit payload for later-phase state removed during rewind."""

    return {
        "recommendation_count": reset_summary.recommendation_count,
        "journal_count": reset_summary.journal_count,
        "reconciliation_count": reset_summary.reconciliation_count,
        "supporting_schedule_count": reset_summary.supporting_schedule_count,
        "report_run_count": reset_summary.report_run_count,
        "export_run_count": reset_summary.export_run_count,
        "evidence_pack_count": reset_summary.evidence_pack_count,
        "canceled_job_count": reset_summary.canceled_job_count,
    }


def _describe_carry_forward_summary(
    carry_forward_summary: ReopenedCloseRunCarryForwardSummary,
) -> str:
    """Render a compact operator-facing summary for reopen timeline events."""

    parts = [
        f"{carry_forward_summary.document_count} document(s)",
        f"{carry_forward_summary.recommendation_count} recommendation(s)",
        f"{carry_forward_summary.journal_count} journal(s)",
        f"{carry_forward_summary.reconciliation_count} reconciliation run(s)",
        f"{carry_forward_summary.supporting_schedule_count} supporting schedule(s)",
        f"{carry_forward_summary.report_run_count} report run(s)",
    ]
    return ", ".join(parts)


__all__ = [
    "CloseRunService",
    "CloseRunServiceError",
    "CloseRunServiceErrorCode",
]
