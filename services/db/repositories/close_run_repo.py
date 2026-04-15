"""
Purpose: Persist and query close runs, phase states, lifecycle review records,
and close-run activity timeline events.
Scope: Entity-scoped close-run CRUD, phase-state mutation, duplicate-period
checks, and gate-signal reads used by the close-run service.
Dependencies: SQLAlchemy ORM sessions plus auth, entity, close-run, audit,
and review persistence models.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from services.audit.service import AuditService
from services.close_runs.gates import EvaluatedPhaseState, PhaseGateSignals
from services.common.enums import (
    CANONICAL_WORKFLOW_PHASES,
    ArtifactType,
    AutonomyMode,
    CloseRunPhaseStatus,
    CloseRunStatus,
    DocumentType,
    SupportingScheduleStatus,
    SupportingScheduleType,
    WorkflowPhase,
)
from services.common.types import JsonObject
from services.db.models.audit import AuditSourceSurface
from services.db.models.close_run import CloseRun, CloseRunPhaseState
from services.db.models.documents import Document, DocumentIssue
from services.db.models.entity import Entity, EntityMembership, EntityStatus
from services.db.models.exports import Artifact, ExportDistribution, ExportRun
from services.db.models.journals import JournalEntry
from services.db.models.recommendations import Recommendation
from services.db.models.reconciliation import (
    Reconciliation,
    ReconciliationAnomaly,
    ReconciliationItem,
)
from services.db.models.reporting import CommentaryStatus, ReportCommentary, ReportRun
from services.db.models.supporting_schedules import SupportingSchedule, SupportingScheduleRow
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class CloseRunEntityRecord:
    """Describe the owning entity fields required by close-run workflows."""

    id: UUID
    name: str
    base_currency: str
    autonomy_mode: AutonomyMode
    status: EntityStatus


@dataclass(frozen=True, slots=True)
class CloseRunRecord:
    """Describe one close-run row as an immutable service-layer record."""

    id: UUID
    entity_id: UUID
    period_start: date
    period_end: date
    status: CloseRunStatus
    reporting_currency: str
    current_version_no: int
    opened_by_user_id: UUID
    approved_by_user_id: UUID | None
    approved_at: datetime | None
    archived_at: datetime | None
    reopened_from_close_run_id: UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CloseRunPhaseStateRecord:
    """Describe one persisted phase-state row for service-layer calculation."""

    id: UUID
    close_run_id: UUID
    phase: WorkflowPhase
    status: CloseRunPhaseStatus
    blocking_reason: str | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CloseRunAccessRecord:
    """Describe a close run together with its accessible owning entity."""

    close_run: CloseRunRecord
    entity: CloseRunEntityRecord


class CloseRunRepository:
    """Execute canonical close-run persistence in one request-scoped DB session."""

    def __init__(self, *, db_session: Session) -> None:
        """Capture the SQLAlchemy session used by close-run workflows."""

        self._db_session = db_session

    def get_entity_for_user(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> CloseRunEntityRecord | None:
        """Return an entity when the user has a membership that grants access."""

        statement = (
            select(Entity)
            .join(EntityMembership, EntityMembership.entity_id == Entity.id)
            .where(Entity.id == entity_id, EntityMembership.user_id == user_id)
        )
        entity = self._db_session.execute(statement).scalar_one_or_none()
        return _map_entity(entity) if entity is not None else None

    def list_close_runs_for_entity(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> tuple[CloseRunRecord, ...]:
        """Return close runs for an accessible entity in newest period/version order."""

        if self.get_entity_for_user(entity_id=entity_id, user_id=user_id) is None:
            return ()

        statement = (
            select(CloseRun)
            .where(CloseRun.entity_id == entity_id)
            .order_by(desc(CloseRun.period_start), desc(CloseRun.current_version_no))
        )
        return tuple(_map_close_run(close_run) for close_run in self._db_session.scalars(statement))

    def get_close_run_for_user(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        user_id: UUID,
    ) -> CloseRunAccessRecord | None:
        """Return one close run and entity when the user can access the workspace."""

        statement = (
            select(CloseRun, Entity)
            .join(Entity, Entity.id == CloseRun.entity_id)
            .join(EntityMembership, EntityMembership.entity_id == Entity.id)
            .where(
                CloseRun.id == close_run_id,
                CloseRun.entity_id == entity_id,
                EntityMembership.user_id == user_id,
            )
        )
        row = self._db_session.execute(statement).one_or_none()
        if row is None:
            return None

        close_run, entity = row
        return CloseRunAccessRecord(close_run=_map_close_run(close_run), entity=_map_entity(entity))

    def find_open_close_run_for_period(
        self,
        *,
        entity_id: UUID,
        period_start: date,
        period_end: date,
    ) -> CloseRunRecord | None:
        """Return an existing open close run for an exact entity-period match."""

        statement = (
            select(CloseRun)
            .where(
                CloseRun.entity_id == entity_id,
                CloseRun.period_start == period_start,
                CloseRun.period_end == period_end,
                CloseRun.status.in_(
                    (
                        CloseRunStatus.DRAFT.value,
                        CloseRunStatus.IN_REVIEW.value,
                        CloseRunStatus.REOPENED.value,
                    )
                ),
            )
            .order_by(desc(CloseRun.current_version_no))
            .limit(1)
        )
        close_run = self._db_session.execute(statement).scalar_one_or_none()
        return _map_close_run(close_run) if close_run is not None else None

    def next_version_no_for_period(
        self,
        *,
        entity_id: UUID,
        period_start: date,
        period_end: date,
    ) -> int:
        """Return the next close-run version number for an entity-period pair."""

        statement = select(func.max(CloseRun.current_version_no)).where(
            CloseRun.entity_id == entity_id,
            CloseRun.period_start == period_start,
            CloseRun.period_end == period_end,
        )
        current_max = self._db_session.execute(statement).scalar_one_or_none()
        return int(current_max or 0) + 1

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
        """Stage a new close-run row and flush it for dependent phase-state writes."""

        close_run = CloseRun(
            entity_id=entity_id,
            period_start=period_start,
            period_end=period_end,
            status=status.value,
            reporting_currency=reporting_currency,
            current_version_no=current_version_no,
            opened_by_user_id=opened_by_user_id,
            reopened_from_close_run_id=reopened_from_close_run_id,
        )
        self._db_session.add(close_run)
        self._db_session.flush()
        return _map_close_run(close_run)

    def create_phase_states(
        self,
        *,
        close_run_id: UUID,
        phase_states: tuple[EvaluatedPhaseState, ...],
    ) -> tuple[CloseRunPhaseStateRecord, ...]:
        """Stage the five canonical phase-state rows for a close run."""

        rows = [
            CloseRunPhaseState(
                close_run_id=close_run_id,
                phase=phase_state.phase.value,
                status=phase_state.status.value,
                blocking_reason=phase_state.blocking_reason,
                completed_at=phase_state.completed_at,
            )
            for phase_state in phase_states
        ]
        self._db_session.add_all(rows)
        self._db_session.flush()
        return tuple(_map_phase_state(row) for row in rows)

    def list_phase_states(self, *, close_run_id: UUID) -> tuple[CloseRunPhaseStateRecord, ...]:
        """Return all five phase-state rows in canonical workflow order."""

        statement = select(CloseRunPhaseState).where(
            CloseRunPhaseState.close_run_id == close_run_id
        )
        rows_by_phase = {
            _resolve_workflow_phase(row.phase): row for row in self._db_session.scalars(statement)
        }
        return tuple(_map_phase_state(rows_by_phase[phase]) for phase in CANONICAL_WORKFLOW_PHASES)

    def replace_phase_states(
        self,
        *,
        close_run_id: UUID,
        phase_states: tuple[EvaluatedPhaseState, ...],
    ) -> tuple[CloseRunPhaseStateRecord, ...]:
        """Persist recalculated statuses for every phase state on a close run."""

        statement = select(CloseRunPhaseState).where(
            CloseRunPhaseState.close_run_id == close_run_id
        )
        rows_by_phase = {
            _resolve_workflow_phase(row.phase): row for row in self._db_session.scalars(statement)
        }
        updated_rows: list[CloseRunPhaseState] = []
        for phase_state in phase_states:
            row = rows_by_phase[phase_state.phase]
            row.status = phase_state.status.value
            row.blocking_reason = phase_state.blocking_reason
            row.completed_at = phase_state.completed_at
            updated_rows.append(row)

        self._db_session.flush()
        return tuple(_map_phase_state(row) for row in updated_rows)

    def update_close_run_status(
        self,
        *,
        close_run_id: UUID,
        status: CloseRunStatus,
        approved_by_user_id: UUID | None = None,
        approved_at: datetime | None = None,
        archived_at: datetime | None = None,
    ) -> CloseRunRecord:
        """Persist a close-run lifecycle status update and return the refreshed row."""

        close_run = self._load_close_run(close_run_id=close_run_id)
        close_run.status = status.value
        if approved_by_user_id is not None:
            close_run.approved_by_user_id = approved_by_user_id
        if approved_at is not None:
            close_run.approved_at = approved_at
        if archived_at is not None:
            close_run.archived_at = archived_at

        self._db_session.flush()
        return _map_close_run(close_run)

    def get_phase_gate_signals(self, *, close_run_id: UUID) -> PhaseGateSignals:
        """Return current gate-blocking signals for deterministic phase evaluation."""

        close_run = self._load_close_run(close_run_id=close_run_id)
        document_rows = self._db_session.execute(
            select(
                Document.id,
                Document.document_type,
                Document.status,
            ).where(Document.close_run_id == close_run_id)
        ).all()
        document_ids = tuple(row.id for row in document_rows)

        required_document_types = {
            DocumentType.INVOICE,
            DocumentType.BANK_STATEMENT,
            DocumentType.RECEIPT,
        }
        present_document_types = {
            DocumentType(row.document_type)
            for row in document_rows
            if row.document_type != DocumentType.UNKNOWN.value
            and row.status not in {"rejected", "duplicate"}
        }
        missing_required_documents = tuple(
            sorted(
                document_type.value
                for document_type in (required_document_types - present_document_types)
            )
        )
        pending_document_review_count = sum(
            1
            for row in document_rows
            if row.status
            in {
                "uploaded",
                "processing",
                "parsed",
                "needs_review",
                "failed",
                "blocked",
            }
        )
        approved_document_ids = {row.id for row in document_rows if row.status == "approved"}

        open_issue_rows = (
            self._db_session.execute(
                select(
                    DocumentIssue.document_id,
                    DocumentIssue.issue_type,
                ).where(
                    DocumentIssue.document_id.in_(document_ids) if document_ids else False,
                    DocumentIssue.status == "open",
                )
            ).all()
            if document_ids
            else []
        )
        unauthorized_document_ids = {
            row.document_id for row in open_issue_rows if row.issue_type == "unauthorized_document"
        }
        unmatched_transaction_document_ids = {
            row.document_id for row in open_issue_rows if row.issue_type == "transaction_mismatch"
        }
        wrong_period_document_ids = {
            row.document_id for row in open_issue_rows if row.issue_type == "wrong_period_document"
        }

        recommendation_rows = self._db_session.execute(
            select(
                Recommendation.id,
                Recommendation.document_id,
            ).where(
                Recommendation.close_run_id == close_run_id,
                Recommendation.superseded_by_id.is_(None),
                Recommendation.document_id.isnot(None),
            )
        ).all()
        recommendation_ids_by_document_id: dict[UUID, set[UUID]] = {}
        for row in recommendation_rows:
            if row.document_id is None:
                continue
            recommendation_ids_by_document_id.setdefault(row.document_id, set()).add(row.id)

        applied_journal_recommendation_ids = {
            row.recommendation_id
            for row in self._db_session.execute(
                select(JournalEntry.recommendation_id).where(
                    JournalEntry.close_run_id == close_run_id,
                    JournalEntry.superseded_by_id.is_(None),
                    JournalEntry.status == "applied",
                    JournalEntry.recommendation_id.isnot(None),
                )
            ).all()
            if row.recommendation_id is not None
        }
        unresolved_processing_item_count = 0
        for document_id in approved_document_ids:
            recommendation_ids = recommendation_ids_by_document_id.get(document_id, set())
            if not recommendation_ids:
                unresolved_processing_item_count += 1
                continue
            if recommendation_ids.isdisjoint(applied_journal_recommendation_ids):
                unresolved_processing_item_count += 1

        reconciliation_rows = self._db_session.execute(
            select(Reconciliation.id, Reconciliation.status).where(
                Reconciliation.close_run_id == close_run_id
            )
        ).all()
        schedule_rows = self._db_session.execute(
            select(
                SupportingSchedule.id,
                SupportingSchedule.schedule_type,
                SupportingSchedule.status,
            ).where(SupportingSchedule.close_run_id == close_run_id)
        ).all()
        schedule_row_counts = {
            row.supporting_schedule_id: int(row.row_count)
            for row in self._db_session.execute(
                select(
                    SupportingScheduleRow.supporting_schedule_id,
                    func.count(SupportingScheduleRow.id).label("row_count"),
                )
                .join(
                    SupportingSchedule,
                    SupportingSchedule.id == SupportingScheduleRow.supporting_schedule_id,
                )
                .where(SupportingSchedule.close_run_id == close_run_id)
                .group_by(SupportingScheduleRow.supporting_schedule_id)
            ).all()
        }
        schedule_by_type = {
            SupportingScheduleType(row.schedule_type): (
                SupportingScheduleStatus(row.status),
                schedule_row_counts.get(row.id, 0),
            )
            for row in schedule_rows
        }
        missing_supporting_schedules: list[str] = []
        pending_supporting_schedule_review_count = 0
        for schedule_type in (
            SupportingScheduleType.FIXED_ASSETS,
            SupportingScheduleType.LOAN_AMORTISATION,
            SupportingScheduleType.ACCRUAL_TRACKER,
            SupportingScheduleType.BUDGET_VS_ACTUAL,
        ):
            schedule_state = schedule_by_type.get(schedule_type)
            if schedule_state is None:
                missing_supporting_schedules.append(schedule_type.value)
                pending_supporting_schedule_review_count += 1
                continue
            schedule_status, row_count = schedule_state
            if schedule_status is SupportingScheduleStatus.NOT_APPLICABLE:
                continue
            if row_count == 0:
                missing_supporting_schedules.append(schedule_type.value)
            if schedule_status is not SupportingScheduleStatus.APPROVED:
                pending_supporting_schedule_review_count += 1

        unresolved_reconciliation_exception_count = 0
        if not reconciliation_rows:
            unresolved_reconciliation_exception_count = 1
        else:
            reconciliation_ids = tuple(row.id for row in reconciliation_rows)
            unresolved_reconciliation_exception_count += self._db_session.execute(
                select(func.count(ReconciliationItem.id)).where(
                    ReconciliationItem.reconciliation_id.in_(reconciliation_ids),
                    ReconciliationItem.requires_disposition.is_(True),
                    ReconciliationItem.disposition.is_(None),
                )
            ).scalar_one()
            unresolved_reconciliation_exception_count += self._db_session.execute(
                select(func.count(ReconciliationAnomaly.id)).where(
                    ReconciliationAnomaly.close_run_id == close_run_id,
                    ReconciliationAnomaly.resolved.is_(False),
                )
            ).scalar_one()
            unresolved_reconciliation_exception_count += sum(
                1 for row in reconciliation_rows if row.status != "approved"
            )

        latest_completed_report_run = (
            self._db_session.execute(
                select(ReportRun)
                .where(
                    ReportRun.close_run_id == close_run_id,
                    ReportRun.status == "completed",
                )
                .order_by(desc(ReportRun.version_no), desc(ReportRun.created_at))
            )
            .scalars()
            .first()
        )
        missing_required_reports: list[str] = []
        if latest_completed_report_run is None:
            missing_required_reports.extend(("report_excel", "report_pdf", "approved_commentary"))
        else:
            artifact_types = {
                str(artifact_ref.get("type") or "").strip()
                for artifact_ref in (
                    latest_completed_report_run.artifact_refs
                    if isinstance(latest_completed_report_run.artifact_refs, list)
                    else []
                )
                if isinstance(artifact_ref, dict)
            }
            if "report_excel" not in artifact_types:
                missing_required_reports.append("report_excel")
            if "report_pdf" not in artifact_types:
                missing_required_reports.append("report_pdf")

            approved_commentary_sections = {
                row.section_key
                for row in self._db_session.execute(
                    select(ReportCommentary.section_key).where(
                        ReportCommentary.report_run_id == latest_completed_report_run.id,
                        ReportCommentary.status == CommentaryStatus.APPROVED.value,
                    )
                ).all()
            }
            for section_key in (
                "profit_and_loss",
                "balance_sheet",
                "cash_flow",
                "budget_variance",
                "kpi_dashboard",
            ):
                if section_key not in approved_commentary_sections:
                    missing_required_reports.append(f"commentary:{section_key}")

        completed_exports = tuple(
            self._db_session.execute(
                select(ExportRun).where(
                    ExportRun.close_run_id == close_run_id,
                    ExportRun.version_no == close_run.current_version_no,
                    ExportRun.status == "completed",
                )
            ).scalars()
        )
        completed_export_exists = bool(completed_exports)
        evidence_pack_exists = (
            self._db_session.execute(
                select(Artifact.id)
                .where(
                    Artifact.close_run_id == close_run_id,
                    Artifact.version_no == close_run.current_version_no,
                    Artifact.artifact_type == ArtifactType.EVIDENCE_PACK.value,
                )
                .limit(1)
            ).scalar_one_or_none()
            is not None
        )
        distribution_exists = (
            self._db_session.execute(
                select(ExportDistribution.id)
                .where(
                    ExportDistribution.close_run_id == close_run_id,
                    ExportDistribution.version_no == close_run.current_version_no,
                )
                .limit(1)
            ).scalar_one_or_none()
            is not None
        )
        missing_signoff_requirements: list[str] = []
        if not completed_export_exists:
            missing_signoff_requirements.append("completed export package")
        if not evidence_pack_exists:
            missing_signoff_requirements.append("evidence pack release")
        if not distribution_exists:
            missing_signoff_requirements.append("management distribution record")
        unresolved_signoff_item_count = len(missing_signoff_requirements)

        return PhaseGateSignals(
            missing_required_documents=missing_required_documents,
            unauthorized_document_count=len(unauthorized_document_ids),
            pending_document_review_count=pending_document_review_count,
            unmatched_transaction_count=len(unmatched_transaction_document_ids),
            wrong_period_document_count=len(wrong_period_document_ids),
            unresolved_processing_item_count=unresolved_processing_item_count,
            unresolved_reconciliation_exception_count=unresolved_reconciliation_exception_count,
            missing_supporting_schedules=tuple(sorted(missing_supporting_schedules)),
            pending_supporting_schedule_review_count=pending_supporting_schedule_review_count,
            missing_required_reports=tuple(missing_required_reports),
            missing_signoff_requirements=tuple(missing_signoff_requirements),
            unresolved_signoff_item_count=unresolved_signoff_item_count,
        )

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
        """Persist one immutable close-run review action for a lifecycle decision."""

        AuditService(db_session=self._db_session).record_review_action(
            entity_id=entity_id,
            close_run_id=close_run_id,
            target_type=target_type,
            target_id=target_id,
            action=action,
            actor_user_id=actor_user_id,
            autonomy_mode=autonomy_mode,
            source_surface=source_surface,
            reason=reason,
            before_payload=before_payload,
            after_payload=after_payload,
            trace_id=trace_id,
            audit_payload=audit_payload,
        )

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
        """Persist one close-run-scoped activity event for the workspace timeline."""

        AuditService(db_session=self._db_session).emit_audit_event(
            entity_id=entity_id,
            close_run_id=close_run_id,
            event_type=event_type,
            actor_user_id=actor_user_id,
            source_surface=source_surface,
            payload=dict(payload),
            trace_id=trace_id,
        )

    def commit(self) -> None:
        """Commit the current close-run transaction after a successful mutation."""

        self._db_session.commit()

    def rollback(self) -> None:
        """Rollback the current close-run transaction after a failed mutation."""

        self._db_session.rollback()

    @staticmethod
    def is_integrity_error(error: Exception) -> bool:
        """Return whether the provided exception originated from a DB integrity failure."""

        return isinstance(error, IntegrityError)

    def _load_close_run(self, *, close_run_id: UUID) -> CloseRun:
        """Load one close run by UUID or fail fast when service state is inconsistent."""

        statement = select(CloseRun).where(CloseRun.id == close_run_id)
        close_run = self._db_session.execute(statement).scalar_one_or_none()
        if close_run is None:
            raise LookupError(f"Close run {close_run_id} does not exist.")

        return close_run


def _map_entity(entity: Entity) -> CloseRunEntityRecord:
    """Convert an ORM entity row into the immutable close-run entity record."""

    return CloseRunEntityRecord(
        id=entity.id,
        name=entity.name,
        base_currency=entity.base_currency,
        autonomy_mode=_resolve_autonomy_mode(entity.autonomy_mode),
        status=EntityStatus(entity.status),
    )


def _map_close_run(close_run: CloseRun) -> CloseRunRecord:
    """Convert an ORM close-run row into the immutable repository record."""

    return CloseRunRecord(
        id=close_run.id,
        entity_id=close_run.entity_id,
        period_start=close_run.period_start,
        period_end=close_run.period_end,
        status=_resolve_close_run_status(close_run.status),
        reporting_currency=close_run.reporting_currency,
        current_version_no=close_run.current_version_no,
        opened_by_user_id=close_run.opened_by_user_id,
        approved_by_user_id=close_run.approved_by_user_id,
        approved_at=close_run.approved_at,
        archived_at=close_run.archived_at,
        reopened_from_close_run_id=close_run.reopened_from_close_run_id,
        created_at=close_run.created_at,
        updated_at=close_run.updated_at,
    )


def _map_phase_state(phase_state: CloseRunPhaseState) -> CloseRunPhaseStateRecord:
    """Convert an ORM phase-state row into the immutable repository record."""

    return CloseRunPhaseStateRecord(
        id=phase_state.id,
        close_run_id=phase_state.close_run_id,
        phase=_resolve_workflow_phase(phase_state.phase),
        status=_resolve_phase_status(phase_state.status),
        blocking_reason=phase_state.blocking_reason,
        completed_at=phase_state.completed_at,
        created_at=phase_state.created_at,
        updated_at=phase_state.updated_at,
    )


def _resolve_autonomy_mode(value: str) -> AutonomyMode:
    """Resolve a stored autonomy-mode value or fail fast on schema drift."""

    for autonomy_mode in AutonomyMode:
        if autonomy_mode.value == value:
            return autonomy_mode

    raise ValueError(f"Unsupported autonomy mode value: {value}")


def _resolve_close_run_status(value: str) -> CloseRunStatus:
    """Resolve a stored close-run status value or fail fast on schema drift."""

    for status in CloseRunStatus:
        if status.value == value:
            return status

    raise ValueError(f"Unsupported close-run status value: {value}")


def _resolve_phase_status(value: str) -> CloseRunPhaseStatus:
    """Resolve a stored phase status value or fail fast on schema drift."""

    for status in CloseRunPhaseStatus:
        if status.value == value:
            return status

    raise ValueError(f"Unsupported close-run phase status value: {value}")


def _resolve_workflow_phase(value: str) -> WorkflowPhase:
    """Resolve a stored workflow phase value or fail fast on schema drift."""

    for phase in WorkflowPhase:
        if phase.value == value:
            return phase

    raise ValueError(f"Unsupported workflow phase value: {value}")


__all__ = [
    "CloseRunAccessRecord",
    "CloseRunEntityRecord",
    "CloseRunPhaseStateRecord",
    "CloseRunRecord",
    "CloseRunRepository",
]
