"""
Purpose: Persist and query close runs, phase states, lifecycle review records,
and close-run activity timeline events.
Scope: Entity-scoped close-run CRUD, phase-state mutation, duplicate-period
checks, and gate-signal reads used by the close-run service.
Dependencies: SQLAlchemy ORM sessions plus auth, entity, close-run, audit,
and review persistence models.
"""

from __future__ import annotations

import re
from copy import deepcopy
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
    JobStatus,
    SupportingScheduleStatus,
    SupportingScheduleType,
    WorkflowPhase,
)
from services.common.types import JsonObject, utc_now
from services.db.models.audit import AuditSourceSurface
from services.db.models.close_run import CloseRun, CloseRunPhaseState
from services.db.models.documents import Document, DocumentIssue, DocumentVersion
from services.db.models.entity import Entity, EntityMembership, EntityStatus
from services.db.models.exports import Artifact, ExportDistribution, ExportRun
from services.db.models.extractions import DocumentExtraction, DocumentLineItem, ExtractedField
from services.db.models.jobs import Job
from services.db.models.journals import JournalEntry, JournalLine, JournalPosting
from services.db.models.recommendations import Recommendation
from services.db.models.reconciliation import (
    Reconciliation,
    ReconciliationAnomaly,
    ReconciliationItem,
    TrialBalanceSnapshot,
)
from services.db.models.reporting import CommentaryStatus, ReportCommentary, ReportRun
from services.db.models.supporting_schedules import SupportingSchedule, SupportingScheduleRow
from services.jobs.task_names import TaskName
from sqlalchemy import asc, delete, desc, func, select
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


@dataclass(frozen=True, slots=True)
class ReopenedCloseRunCarryForwardSummary:
    """Describe the mutable workflow state copied into a reopened close run."""

    document_count: int = 0
    recommendation_count: int = 0
    journal_count: int = 0
    reconciliation_count: int = 0
    supporting_schedule_count: int = 0
    report_run_count: int = 0


@dataclass(frozen=True, slots=True)
class CloseRunStateResetSummary:
    """Describe the later-phase state removed after rewinding workflow."""

    recommendation_count: int = 0
    journal_count: int = 0
    reconciliation_count: int = 0
    supporting_schedule_count: int = 0
    report_run_count: int = 0
    export_run_count: int = 0
    evidence_pack_count: int = 0
    canceled_job_count: int = 0


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

    def carry_forward_working_state_for_reopened_close_run(
        self,
        *,
        source_close_run_id: UUID,
        target_close_run_id: UUID,
    ) -> ReopenedCloseRunCarryForwardSummary:
        """Clone the canonical current-state workflow artifacts into a reopened version."""

        target_close_run = self._load_close_run(close_run_id=target_close_run_id)
        target_version_no = target_close_run.current_version_no
        source_documents = self._db_session.scalars(
            select(Document)
            .where(Document.close_run_id == source_close_run_id)
            .order_by(asc(Document.created_at), asc(Document.original_filename), asc(Document.id))
        ).all()

        cloned_documents_by_source_id: dict[UUID, Document] = {}
        for source_document in source_documents:
            clone = Document(
                close_run_id=target_close_run_id,
                parent_document_id=None,
                document_type=source_document.document_type,
                source_channel=source_document.source_channel,
                storage_key=source_document.storage_key,
                original_filename=source_document.original_filename,
                mime_type=source_document.mime_type,
                file_size_bytes=source_document.file_size_bytes,
                sha256_hash=source_document.sha256_hash,
                period_start=source_document.period_start,
                period_end=source_document.period_end,
                classification_confidence=source_document.classification_confidence,
                ocr_required=source_document.ocr_required,
                status=source_document.status,
                owner_user_id=source_document.owner_user_id,
                last_touched_by_user_id=source_document.last_touched_by_user_id,
            )
            self._db_session.add(clone)
            cloned_documents_by_source_id[source_document.id] = clone
        self._db_session.flush()

        for source_document in source_documents:
            if source_document.parent_document_id is None:
                continue
            cloned_parent = cloned_documents_by_source_id.get(source_document.parent_document_id)
            if cloned_parent is None:
                continue
            cloned_documents_by_source_id[source_document.id].parent_document_id = cloned_parent.id
        self._db_session.flush()

        source_document_ids = tuple(source_document.id for source_document in source_documents)
        source_versions = self._db_session.scalars(
            select(DocumentVersion)
            .where(DocumentVersion.document_id.in_(source_document_ids))
            .order_by(asc(DocumentVersion.document_id), asc(DocumentVersion.version_no))
        ).all()
        for source_version in source_versions:
            self._db_session.add(
                DocumentVersion(
                    document_id=cloned_documents_by_source_id[source_version.document_id].id,
                    version_no=source_version.version_no,
                    normalized_storage_key=source_version.normalized_storage_key,
                    ocr_text_storage_key=source_version.ocr_text_storage_key,
                    parser_name=source_version.parser_name,
                    parser_version=source_version.parser_version,
                    raw_parse_payload=source_version.raw_parse_payload,
                    page_count=source_version.page_count,
                    checksum=source_version.checksum,
                )
            )

        source_extractions = self._db_session.scalars(
            select(DocumentExtraction)
            .where(DocumentExtraction.document_id.in_(source_document_ids))
            .order_by(asc(DocumentExtraction.document_id), asc(DocumentExtraction.version_no))
        ).all()
        cloned_extractions_by_source_id: dict[UUID, DocumentExtraction] = {}
        for source_extraction in source_extractions:
            clone = DocumentExtraction(
                document_id=cloned_documents_by_source_id[source_extraction.document_id].id,
                version_no=source_extraction.version_no,
                schema_name=source_extraction.schema_name,
                schema_version=source_extraction.schema_version,
                extracted_payload=source_extraction.extracted_payload,
                confidence_summary=source_extraction.confidence_summary,
                needs_review=source_extraction.needs_review,
                approved_version=source_extraction.approved_version,
            )
            self._db_session.add(clone)
            cloned_extractions_by_source_id[source_extraction.id] = clone
        self._db_session.flush()

        source_extraction_ids = tuple(
            source_extraction.id for source_extraction in source_extractions
        )
        if source_extraction_ids:
            source_fields = self._db_session.scalars(
                select(ExtractedField)
                .where(ExtractedField.document_extraction_id.in_(source_extraction_ids))
                .order_by(
                    asc(ExtractedField.document_extraction_id),
                    asc(ExtractedField.field_name),
                    asc(ExtractedField.created_at),
                )
            ).all()
            for source_field in source_fields:
                self._db_session.add(
                    ExtractedField(
                        document_extraction_id=cloned_extractions_by_source_id[
                            source_field.document_extraction_id
                        ].id,
                        field_name=source_field.field_name,
                        field_value=source_field.field_value,
                        field_type=source_field.field_type,
                        confidence=source_field.confidence,
                        evidence_ref=source_field.evidence_ref,
                        is_human_corrected=source_field.is_human_corrected,
                    )
                )

            source_line_items = self._db_session.scalars(
                select(DocumentLineItem)
                .where(DocumentLineItem.document_extraction_id.in_(source_extraction_ids))
                .order_by(
                    asc(DocumentLineItem.document_extraction_id),
                    asc(DocumentLineItem.line_no),
                )
            ).all()
            for source_line_item in source_line_items:
                self._db_session.add(
                    DocumentLineItem(
                        document_extraction_id=cloned_extractions_by_source_id[
                            source_line_item.document_extraction_id
                        ].id,
                        line_no=source_line_item.line_no,
                        description=source_line_item.description,
                        quantity=source_line_item.quantity,
                        unit_price=source_line_item.unit_price,
                        amount=source_line_item.amount,
                        tax_amount=source_line_item.tax_amount,
                        dimensions=source_line_item.dimensions,
                        evidence_ref=source_line_item.evidence_ref,
                    )
                )

        source_issues = self._db_session.scalars(
            select(DocumentIssue)
            .where(DocumentIssue.document_id.in_(source_document_ids))
            .order_by(asc(DocumentIssue.document_id), asc(DocumentIssue.created_at))
        ).all()
        for source_issue in source_issues:
            self._db_session.add(
                DocumentIssue(
                    document_id=cloned_documents_by_source_id[source_issue.document_id].id,
                    issue_type=source_issue.issue_type,
                    severity=source_issue.severity,
                    status=source_issue.status,
                    details=source_issue.details,
                    assigned_to_user_id=source_issue.assigned_to_user_id,
                    resolved_by_user_id=source_issue.resolved_by_user_id,
                    resolved_at=source_issue.resolved_at,
                )
            )

        self._db_session.flush()
        source_recommendations = self._db_session.scalars(
            select(Recommendation)
            .where(
                Recommendation.close_run_id == source_close_run_id,
                Recommendation.superseded_by_id.is_(None),
            )
            .order_by(asc(Recommendation.created_at), asc(Recommendation.id))
        ).all()
        cloned_recommendations_by_source_id: dict[UUID, Recommendation] = {}
        for source_recommendation in source_recommendations:
            clone = Recommendation(
                close_run_id=target_close_run_id,
                document_id=(
                    cloned_documents_by_source_id[source_recommendation.document_id].id
                    if source_recommendation.document_id in cloned_documents_by_source_id
                    else None
                ),
                recommendation_type=source_recommendation.recommendation_type,
                status=source_recommendation.status,
                payload=_clone_json_value(source_recommendation.payload),
                confidence=source_recommendation.confidence,
                reasoning_summary=source_recommendation.reasoning_summary,
                evidence_links=_clone_json_value(source_recommendation.evidence_links),
                prompt_version=source_recommendation.prompt_version,
                rule_version=source_recommendation.rule_version,
                schema_version=source_recommendation.schema_version,
                created_by_system=source_recommendation.created_by_system,
                autonomy_mode=source_recommendation.autonomy_mode,
                superseded_by_id=None,
            )
            self._db_session.add(clone)
            cloned_recommendations_by_source_id[source_recommendation.id] = clone
        self._db_session.flush()

        source_journals = self._db_session.scalars(
            select(JournalEntry)
            .where(
                JournalEntry.close_run_id == source_close_run_id,
                JournalEntry.superseded_by_id.is_(None),
            )
            .order_by(asc(JournalEntry.created_at), asc(JournalEntry.id))
        ).all()
        cloned_journals_by_source_id: dict[UUID, JournalEntry] = {}
        for source_journal in source_journals:
            clone = JournalEntry(
                entity_id=source_journal.entity_id,
                close_run_id=target_close_run_id,
                recommendation_id=(
                    cloned_recommendations_by_source_id[source_journal.recommendation_id].id
                    if source_journal.recommendation_id in cloned_recommendations_by_source_id
                    else None
                ),
                journal_number=_build_reopened_journal_number(
                    source_journal_number=source_journal.journal_number,
                    target_version_no=target_version_no,
                ),
                posting_date=source_journal.posting_date,
                status=source_journal.status,
                description=source_journal.description,
                total_debits=source_journal.total_debits,
                total_credits=source_journal.total_credits,
                line_count=source_journal.line_count,
                source_surface=source_journal.source_surface,
                autonomy_mode=source_journal.autonomy_mode,
                reasoning_summary=source_journal.reasoning_summary,
                metadata_payload=_clone_json_value(source_journal.metadata_payload),
                approved_by_user_id=source_journal.approved_by_user_id,
                applied_by_user_id=source_journal.applied_by_user_id,
                superseded_by_id=None,
            )
            self._db_session.add(clone)
            cloned_journals_by_source_id[source_journal.id] = clone
        self._db_session.flush()

        source_journal_ids = tuple(source_journal.id for source_journal in source_journals)
        if source_journal_ids:
            source_journal_lines = self._db_session.scalars(
                select(JournalLine)
                .where(JournalLine.journal_entry_id.in_(source_journal_ids))
                .order_by(asc(JournalLine.journal_entry_id), asc(JournalLine.line_no))
            ).all()
            for source_line in source_journal_lines:
                self._db_session.add(
                    JournalLine(
                        journal_entry_id=cloned_journals_by_source_id[source_line.journal_entry_id].id,
                        line_no=source_line.line_no,
                        account_code=source_line.account_code,
                        line_type=source_line.line_type,
                        amount=source_line.amount,
                        description=source_line.description,
                        dimensions=_clone_json_value(source_line.dimensions),
                        reference=source_line.reference,
                    )
                )

            source_postings = self._db_session.scalars(
                select(JournalPosting)
                .where(JournalPosting.journal_entry_id.in_(source_journal_ids))
                .order_by(asc(JournalPosting.posted_at), asc(JournalPosting.created_at))
            ).all()
            source_posting_artifact_ids = tuple(
                source_posting.artifact_id
                for source_posting in source_postings
                if source_posting.artifact_id is not None
            )
            source_artifacts_by_id = {
                artifact.id: artifact
                for artifact in self._db_session.scalars(
                    select(Artifact).where(Artifact.id.in_(source_posting_artifact_ids))
                )
            }
            cloned_artifacts_by_source_id: dict[UUID, Artifact] = {}
            for source_posting in source_postings:
                cloned_artifact_id: UUID | None = None
                if source_posting.artifact_id is not None:
                    source_artifact = source_artifacts_by_id.get(source_posting.artifact_id)
                    if source_artifact is None:
                        raise LookupError(
                            "A journal posting referenced a missing artifact while reopening "
                            "the close run."
                        )
                    cloned_artifact = cloned_artifacts_by_source_id.get(source_artifact.id)
                    if cloned_artifact is None:
                        cloned_artifact = Artifact(
                            close_run_id=target_close_run_id,
                            report_run_id=None,
                            artifact_type=source_artifact.artifact_type,
                            storage_key=source_artifact.storage_key,
                            mime_type=source_artifact.mime_type,
                            checksum=source_artifact.checksum,
                            idempotency_key=_build_carried_forward_artifact_idempotency_key(
                                source_idempotency_key=source_artifact.idempotency_key,
                                target_close_run_id=target_close_run_id,
                                target_version_no=target_version_no,
                            ),
                            version_no=target_version_no,
                            released_at=source_artifact.released_at,
                            artifact_metadata=_clone_json_value(source_artifact.artifact_metadata),
                        )
                        self._db_session.add(cloned_artifact)
                        self._db_session.flush()
                        cloned_artifacts_by_source_id[source_artifact.id] = cloned_artifact
                    cloned_artifact_id = cloned_artifact.id
                self._db_session.add(
                    JournalPosting(
                        journal_entry_id=cloned_journals_by_source_id[
                            source_posting.journal_entry_id
                        ].id,
                        entity_id=source_posting.entity_id,
                        close_run_id=target_close_run_id,
                        version_no=target_version_no,
                        posting_target=source_posting.posting_target,
                        provider=source_posting.provider,
                        status=source_posting.status,
                        artifact_id=cloned_artifact_id,
                        artifact_type=source_posting.artifact_type,
                        note=source_posting.note,
                        posting_metadata=_clone_json_value(source_posting.posting_metadata),
                        posted_by_user_id=source_posting.posted_by_user_id,
                        posted_at=source_posting.posted_at,
                    )
                )

        source_reconciliations = self._db_session.scalars(
            select(Reconciliation)
            .where(Reconciliation.close_run_id == source_close_run_id)
            .order_by(asc(Reconciliation.created_at), asc(Reconciliation.id))
        ).all()
        cloned_reconciliations_by_source_id: dict[UUID, Reconciliation] = {}
        for source_reconciliation in source_reconciliations:
            clone = Reconciliation(
                close_run_id=target_close_run_id,
                reconciliation_type=source_reconciliation.reconciliation_type,
                status=source_reconciliation.status,
                summary=_clone_json_value(source_reconciliation.summary),
                blocking_reason=source_reconciliation.blocking_reason,
                approved_by_user_id=source_reconciliation.approved_by_user_id,
                created_by_user_id=source_reconciliation.created_by_user_id,
            )
            self._db_session.add(clone)
            cloned_reconciliations_by_source_id[source_reconciliation.id] = clone
        self._db_session.flush()

        source_trial_balance_snapshots = self._db_session.scalars(
            select(TrialBalanceSnapshot)
            .where(TrialBalanceSnapshot.close_run_id == source_close_run_id)
            .order_by(asc(TrialBalanceSnapshot.snapshot_no), asc(TrialBalanceSnapshot.id))
        ).all()
        cloned_trial_balance_snapshots_by_source_id: dict[UUID, TrialBalanceSnapshot] = {}
        for source_snapshot in source_trial_balance_snapshots:
            clone = TrialBalanceSnapshot(
                close_run_id=target_close_run_id,
                snapshot_no=source_snapshot.snapshot_no,
                total_debits=source_snapshot.total_debits,
                total_credits=source_snapshot.total_credits,
                is_balanced=source_snapshot.is_balanced,
                account_balances=_clone_json_value(source_snapshot.account_balances),
                generated_by_user_id=source_snapshot.generated_by_user_id,
                metadata_payload=_clone_json_value(source_snapshot.metadata_payload),
            )
            self._db_session.add(clone)
            cloned_trial_balance_snapshots_by_source_id[source_snapshot.id] = clone
        self._db_session.flush()

        source_reconciliation_ids = tuple(
            source_reconciliation.id for source_reconciliation in source_reconciliations
        )
        if source_reconciliation_ids:
            source_reconciliation_items = self._db_session.scalars(
                select(ReconciliationItem)
                .where(ReconciliationItem.reconciliation_id.in_(source_reconciliation_ids))
                .order_by(
                    asc(ReconciliationItem.reconciliation_id),
                    asc(ReconciliationItem.created_at),
                    asc(ReconciliationItem.id),
                )
            ).all()
            for source_item in source_reconciliation_items:
                self._db_session.add(
                    ReconciliationItem(
                        reconciliation_id=cloned_reconciliations_by_source_id[
                            source_item.reconciliation_id
                        ].id,
                        source_type=source_item.source_type,
                        source_ref=source_item.source_ref,
                        match_status=source_item.match_status,
                        amount=source_item.amount,
                        matched_to=_clone_json_value(source_item.matched_to),
                        difference_amount=source_item.difference_amount,
                        explanation=source_item.explanation,
                        requires_disposition=source_item.requires_disposition,
                        disposition=source_item.disposition,
                        disposition_reason=source_item.disposition_reason,
                        disposition_by_user_id=source_item.disposition_by_user_id,
                        dimensions=_clone_json_value(source_item.dimensions),
                        period_date=source_item.period_date,
                    )
                )

        source_anomalies = self._db_session.scalars(
            select(ReconciliationAnomaly)
            .where(ReconciliationAnomaly.close_run_id == source_close_run_id)
            .order_by(asc(ReconciliationAnomaly.created_at), asc(ReconciliationAnomaly.id))
        ).all()
        for source_anomaly in source_anomalies:
            self._db_session.add(
                ReconciliationAnomaly(
                    close_run_id=target_close_run_id,
                    trial_balance_snapshot_id=(
                        cloned_trial_balance_snapshots_by_source_id[
                            source_anomaly.trial_balance_snapshot_id
                        ].id
                        if source_anomaly.trial_balance_snapshot_id
                        in cloned_trial_balance_snapshots_by_source_id
                        else None
                    ),
                    anomaly_type=source_anomaly.anomaly_type,
                    severity=source_anomaly.severity,
                    account_code=source_anomaly.account_code,
                    description=source_anomaly.description,
                    details=_clone_json_value(source_anomaly.details),
                    resolved=source_anomaly.resolved,
                    resolved_by_user_id=source_anomaly.resolved_by_user_id,
                    resolution_note=source_anomaly.resolution_note,
                )
            )

        source_supporting_schedules = self._db_session.scalars(
            select(SupportingSchedule)
            .where(SupportingSchedule.close_run_id == source_close_run_id)
            .order_by(asc(SupportingSchedule.schedule_type), asc(SupportingSchedule.created_at))
        ).all()
        cloned_supporting_schedules_by_source_id: dict[UUID, SupportingSchedule] = {}
        for source_schedule in source_supporting_schedules:
            clone = SupportingSchedule(
                close_run_id=target_close_run_id,
                schedule_type=source_schedule.schedule_type,
                status=source_schedule.status,
                note=source_schedule.note,
                reviewed_by_user_id=source_schedule.reviewed_by_user_id,
                reviewed_at=source_schedule.reviewed_at,
            )
            self._db_session.add(clone)
            cloned_supporting_schedules_by_source_id[source_schedule.id] = clone
        self._db_session.flush()

        source_supporting_schedule_ids = tuple(
            source_schedule.id for source_schedule in source_supporting_schedules
        )
        if source_supporting_schedule_ids:
            source_supporting_schedule_rows = self._db_session.scalars(
                select(SupportingScheduleRow)
                .where(SupportingScheduleRow.supporting_schedule_id.in_(source_supporting_schedule_ids))
                .order_by(
                    asc(SupportingScheduleRow.supporting_schedule_id),
                    asc(SupportingScheduleRow.line_no),
                    asc(SupportingScheduleRow.created_at),
                )
            ).all()
            for source_row in source_supporting_schedule_rows:
                self._db_session.add(
                    SupportingScheduleRow(
                        supporting_schedule_id=cloned_supporting_schedules_by_source_id[
                            source_row.supporting_schedule_id
                        ].id,
                        row_ref=source_row.row_ref,
                        line_no=source_row.line_no,
                        payload=_clone_json_value(source_row.payload),
                    )
                )

        source_report_runs = self._db_session.scalars(
            select(ReportRun)
            .where(ReportRun.close_run_id == source_close_run_id)
            .order_by(asc(ReportRun.version_no), asc(ReportRun.created_at))
        ).all()
        cloned_report_runs_by_source_id: dict[UUID, ReportRun] = {}
        for source_report_run in source_report_runs:
            clone = ReportRun(
                close_run_id=target_close_run_id,
                template_id=source_report_run.template_id,
                version_no=source_report_run.version_no,
                status=source_report_run.status,
                failure_reason=source_report_run.failure_reason,
                generation_config=_clone_json_value(source_report_run.generation_config),
                artifact_refs=_clone_json_value(source_report_run.artifact_refs),
                generated_by_user_id=source_report_run.generated_by_user_id,
                completed_at=source_report_run.completed_at,
            )
            self._db_session.add(clone)
            cloned_report_runs_by_source_id[source_report_run.id] = clone
        self._db_session.flush()

        source_report_run_ids = tuple(
            source_report_run.id for source_report_run in source_report_runs
        )
        if source_report_run_ids:
            source_commentary = self._db_session.scalars(
                select(ReportCommentary)
                .where(ReportCommentary.report_run_id.in_(source_report_run_ids))
                .order_by(asc(ReportCommentary.created_at), asc(ReportCommentary.id))
            ).all()
            cloned_commentary_by_source_id: dict[UUID, ReportCommentary] = {}
            for source_commentary_row in source_commentary:
                clone = ReportCommentary(
                    report_run_id=cloned_report_runs_by_source_id[
                        source_commentary_row.report_run_id
                    ].id,
                    section_key=source_commentary_row.section_key,
                    status=source_commentary_row.status,
                    body=source_commentary_row.body,
                    authored_by_user_id=source_commentary_row.authored_by_user_id,
                    superseded_by_id=None,
                )
                self._db_session.add(clone)
                cloned_commentary_by_source_id[source_commentary_row.id] = clone
            self._db_session.flush()

            for source_commentary_row in source_commentary:
                if (
                    source_commentary_row.superseded_by_id is None
                    or source_commentary_row.id not in cloned_commentary_by_source_id
                ):
                    continue
                cloned_superseded_by = cloned_commentary_by_source_id.get(
                    source_commentary_row.superseded_by_id
                )
                if cloned_superseded_by is None:
                    continue
                cloned_commentary_by_source_id[
                    source_commentary_row.id
                ].superseded_by_id = cloned_superseded_by.id
            self._db_session.flush()

        return ReopenedCloseRunCarryForwardSummary(
            document_count=len(source_documents),
            recommendation_count=len(source_recommendations),
            journal_count=len(source_journals),
            reconciliation_count=len(source_reconciliations),
            supporting_schedule_count=len(source_supporting_schedules),
            report_run_count=len(source_report_runs),
        )

    def clear_state_after_phase_rewind(
        self,
        *,
        close_run_id: UUID,
        target_phase: WorkflowPhase,
        canceled_by_user_id: UUID | None = None,
    ) -> CloseRunStateResetSummary:
        """Delete later-phase derived state so rewound workflow remains canonical."""

        recommendation_count = 0
        journal_count = 0
        reconciliation_count = 0
        supporting_schedule_count = 0
        report_run_count = 0
        export_run_count = 0
        evidence_pack_count = 0
        canceled_job_count = self._cancel_later_phase_jobs_after_rewind(
            close_run_id=close_run_id,
            target_phase=target_phase,
            canceled_by_user_id=canceled_by_user_id,
        )

        if target_phase is WorkflowPhase.COLLECTION:
            recommendation_count, journal_count = self._clear_processing_state(
                close_run_id=close_run_id
            )
            (
                reconciliation_count,
                supporting_schedule_count,
            ) = self._clear_reconciliation_state(close_run_id=close_run_id)
            report_run_count = self._clear_reporting_state(close_run_id=close_run_id)
            export_run_count, evidence_pack_count = self._clear_signoff_state(
                close_run_id=close_run_id
            )
        elif target_phase is WorkflowPhase.PROCESSING:
            (
                reconciliation_count,
                supporting_schedule_count,
            ) = self._clear_reconciliation_state(close_run_id=close_run_id)
            report_run_count = self._clear_reporting_state(close_run_id=close_run_id)
            export_run_count, evidence_pack_count = self._clear_signoff_state(
                close_run_id=close_run_id
            )
        elif target_phase is WorkflowPhase.RECONCILIATION:
            report_run_count = self._clear_reporting_state(close_run_id=close_run_id)
            export_run_count, evidence_pack_count = self._clear_signoff_state(
                close_run_id=close_run_id
            )
        elif target_phase is WorkflowPhase.REPORTING:
            export_run_count, evidence_pack_count = self._clear_signoff_state(
                close_run_id=close_run_id
            )

        return CloseRunStateResetSummary(
            recommendation_count=recommendation_count,
            journal_count=journal_count,
            reconciliation_count=reconciliation_count,
            supporting_schedule_count=supporting_schedule_count,
            report_run_count=report_run_count,
            export_run_count=export_run_count,
            evidence_pack_count=evidence_pack_count,
            canceled_job_count=canceled_job_count,
        )

    def _cancel_later_phase_jobs_after_rewind(
        self,
        *,
        close_run_id: UUID,
        target_phase: WorkflowPhase,
        canceled_by_user_id: UUID | None,
    ) -> int:
        """Cancel queued or active later-phase jobs invalidated by a rewind."""

        task_names = _later_phase_task_names_after_rewind(target_phase=target_phase)
        if not task_names:
            return 0

        statement = (
            select(Job)
            .where(
                Job.close_run_id == close_run_id,
                Job.task_name.in_(tuple(task_name.value for task_name in task_names)),
                Job.status.in_(
                    (
                        JobStatus.QUEUED.value,
                        JobStatus.RUNNING.value,
                        JobStatus.BLOCKED.value,
                    )
                ),
            )
            .order_by(asc(Job.created_at), asc(Job.id))
        )
        jobs = self._db_session.scalars(statement).all()
        if not jobs:
            return 0

        now = utc_now()
        reason = f"Canceled because the close run was rewound to {target_phase.label}."
        for job in jobs:
            job.failure_reason = reason
            job.cancellation_requested_at = now
            job.canceled_by_user_id = canceled_by_user_id
            if job.status in {JobStatus.QUEUED.value, JobStatus.BLOCKED.value}:
                job.status = JobStatus.CANCELED.value
                job.blocking_reason = None
                job.canceled_at = now
                job.completed_at = now

        self._db_session.flush()
        return len(jobs)

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

        missing_required_documents: tuple[str, ...] = ()
        approved_document_count = sum(1 for row in document_rows if row.status == "approved")
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
            approved_document_count=approved_document_count,
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

    def _clear_processing_state(self, *, close_run_id: UUID) -> tuple[int, int]:
        """Remove processing-phase recommendations, journals, and posting artifacts."""

        journal_ids = tuple(
            self._db_session.scalars(
                select(JournalEntry.id).where(JournalEntry.close_run_id == close_run_id)
            )
        )
        recommendation_count = int(
            self._db_session.execute(
                select(func.count(Recommendation.id)).where(
                    Recommendation.close_run_id == close_run_id
                )
            ).scalar_one()
        )
        journal_count = len(journal_ids)

        if journal_ids:
            self._db_session.execute(
                delete(JournalPosting).where(JournalPosting.journal_entry_id.in_(journal_ids))
            )
            self._db_session.execute(
                delete(JournalLine).where(JournalLine.journal_entry_id.in_(journal_ids))
            )
        self._db_session.execute(
            delete(JournalEntry).where(JournalEntry.close_run_id == close_run_id)
        )
        self._db_session.execute(
            delete(Recommendation).where(Recommendation.close_run_id == close_run_id)
        )
        self._db_session.execute(
            delete(Artifact).where(
                Artifact.close_run_id == close_run_id,
                Artifact.artifact_type.in_(
                    (
                        ArtifactType.GL_POSTING_PACKAGE.value,
                        ArtifactType.QUICKBOOKS_EXPORT.value,
                    )
                ),
            )
        )
        return recommendation_count, journal_count

    def _clear_reconciliation_state(self, *, close_run_id: UUID) -> tuple[int, int]:
        """Remove reconciliation-phase runs, anomalies, and supporting schedules."""

        reconciliation_ids = tuple(
            self._db_session.scalars(
                select(Reconciliation.id).where(Reconciliation.close_run_id == close_run_id)
            )
        )
        supporting_schedule_ids = tuple(
            self._db_session.scalars(
                select(SupportingSchedule.id).where(SupportingSchedule.close_run_id == close_run_id)
            )
        )
        reconciliation_count = len(reconciliation_ids)
        supporting_schedule_count = len(supporting_schedule_ids)

        if reconciliation_ids:
            self._db_session.execute(
                delete(ReconciliationItem).where(
                    ReconciliationItem.reconciliation_id.in_(reconciliation_ids)
                )
            )
        self._db_session.execute(
            delete(ReconciliationAnomaly).where(ReconciliationAnomaly.close_run_id == close_run_id)
        )
        self._db_session.execute(
            delete(TrialBalanceSnapshot).where(TrialBalanceSnapshot.close_run_id == close_run_id)
        )
        self._db_session.execute(
            delete(Reconciliation).where(Reconciliation.close_run_id == close_run_id)
        )

        if supporting_schedule_ids:
            self._db_session.execute(
                delete(SupportingScheduleRow).where(
                    SupportingScheduleRow.supporting_schedule_id.in_(supporting_schedule_ids)
                )
            )
        self._db_session.execute(
            delete(SupportingSchedule).where(SupportingSchedule.close_run_id == close_run_id)
        )
        return reconciliation_count, supporting_schedule_count

    def _clear_reporting_state(self, *, close_run_id: UUID) -> int:
        """Remove reporting-phase runs and commentary for a close run."""

        report_run_ids = tuple(
            self._db_session.scalars(
                select(ReportRun.id).where(ReportRun.close_run_id == close_run_id)
            )
        )
        if report_run_ids:
            self._db_session.execute(
                delete(ReportCommentary).where(ReportCommentary.report_run_id.in_(report_run_ids))
            )
        self._db_session.execute(delete(ReportRun).where(ReportRun.close_run_id == close_run_id))
        return len(report_run_ids)

    def _clear_signoff_state(self, *, close_run_id: UUID) -> tuple[int, int]:
        """Remove sign-off exports and evidence packs for a close run."""

        export_run_count = int(
            self._db_session.execute(
                select(func.count(ExportRun.id)).where(ExportRun.close_run_id == close_run_id)
            ).scalar_one()
        )
        evidence_pack_count = int(
            self._db_session.execute(
                select(func.count(Artifact.id)).where(
                    Artifact.close_run_id == close_run_id,
                    Artifact.artifact_type == ArtifactType.EVIDENCE_PACK.value,
                )
            ).scalar_one()
        )
        self._db_session.execute(
            delete(ExportDistribution).where(ExportDistribution.close_run_id == close_run_id)
        )
        self._db_session.execute(delete(ExportRun).where(ExportRun.close_run_id == close_run_id))
        self._db_session.execute(
            delete(Artifact).where(
                Artifact.close_run_id == close_run_id,
                Artifact.artifact_type == ArtifactType.EVIDENCE_PACK.value,
            )
        )
        return export_run_count, evidence_pack_count


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


def _clone_json_value(value: object) -> object:
    """Return a detached copy of JSON-like payloads used across workflow records."""

    if isinstance(value, (dict, list)):
        return deepcopy(value)
    return value


def _build_reopened_journal_number(*, source_journal_number: str, target_version_no: int) -> str:
    """Return the canonical journal number for a reopened close-run version."""

    base_number = re.sub(r"(?:-V\d+)+$", "", source_journal_number.strip())
    suffix = f"-V{target_version_no}"
    return f"{base_number[: max(1, 60 - len(suffix))]}{suffix}"


def _build_carried_forward_artifact_idempotency_key(
    *,
    source_idempotency_key: str,
    target_close_run_id: UUID,
    target_version_no: int,
) -> str:
    """Return a unique artifact idempotency key for carried-forward working state."""

    return (
        f"{source_idempotency_key}:reopened:{target_close_run_id}:"
        f"version:{target_version_no}"
    )


def _later_phase_task_names_after_rewind(*, target_phase: WorkflowPhase) -> tuple[TaskName, ...]:
    """Return background task families invalidated by rewinding to one phase."""

    if target_phase is WorkflowPhase.COLLECTION:
        return (
            TaskName.ACCOUNTING_RECOMMEND_CLOSE_RUN,
            TaskName.RECONCILIATION_EXECUTE_CLOSE_RUN,
            TaskName.REPORTING_GENERATE_CLOSE_RUN_PACK,
        )
    if target_phase is WorkflowPhase.PROCESSING:
        return (
            TaskName.RECONCILIATION_EXECUTE_CLOSE_RUN,
            TaskName.REPORTING_GENERATE_CLOSE_RUN_PACK,
        )
    if target_phase is WorkflowPhase.RECONCILIATION:
        return (TaskName.REPORTING_GENERATE_CLOSE_RUN_PACK,)
    return ()


__all__ = [
    "CloseRunAccessRecord",
    "CloseRunEntityRecord",
    "CloseRunPhaseStateRecord",
    "CloseRunRecord",
    "CloseRunRepository",
    "CloseRunStateResetSummary",
    "ReopenedCloseRunCarryForwardSummary",
]
