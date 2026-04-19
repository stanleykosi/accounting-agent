"""
Purpose: Orchestrate imported ledger baseline uploads and close-run bindings.
Scope: Entity access checks, GL/TB upload parsing, import persistence, safe auto-binding
to matching close runs, and immutable activity-event emission.
Dependencies: Ledger importer, SQLAlchemy persistence models, and shared audit helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from services.audit.service import AuditService
from services.auth.service import serialize_uuid
from services.common.enums import CloseRunStatus, ReviewStatus
from services.common.types import JsonObject
from services.contracts.ledger_models import (
    CloseRunLedgerBindingSummary,
    GeneralLedgerImportSummary,
    GeneralLedgerImportUploadResponse,
    LedgerWorkspaceResponse,
    TrialBalanceImportSummary,
    TrialBalanceImportUploadResponse,
)
from services.db.models.audit import AuditSourceSurface
from services.db.models.close_run import CloseRun
from services.db.models.documents import Document
from services.db.models.entity import Entity, EntityMembership, EntityStatus
from services.db.models.journals import JournalEntry
from services.db.models.ledger import (
    CloseRunLedgerBinding,
    GeneralLedgerImportBatch,
    GeneralLedgerImportLine,
    TrialBalanceImportBatch,
    TrialBalanceImportLine,
)
from services.db.models.recommendations import Recommendation
from services.db.models.reconciliation import Reconciliation, TrialBalanceSnapshot
from services.db.repositories.entity_repo import EntityUserRecord
from services.documents.imported_ledger_representation import (
    evaluate_documents_imported_gl_representation,
)
from services.ledger.importer import (
    ImportedGeneralLedgerLineSeed,
    ImportedTrialBalanceLineSeed,
    LedgerImportError,
    LedgerImportErrorCode,
    import_general_ledger_file,
    import_trial_balance_file,
)
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class LedgerEntityRecord:
    """Describe the subset of entity fields required by ledger-import workflows."""

    id: UUID
    name: str
    status: EntityStatus


@dataclass(frozen=True, slots=True)
class LedgerCloseRunRecord:
    """Describe one open close run relevant to import auto-binding."""

    id: UUID
    entity_id: UUID
    period_start: date
    period_end: date
    status: CloseRunStatus


@dataclass(frozen=True, slots=True)
class GeneralLedgerImportBatchRecord:
    """Describe one persisted general-ledger import batch."""

    id: UUID
    entity_id: UUID
    period_start: date
    period_end: date
    source_format: str
    uploaded_filename: str
    row_count: int
    imported_by_user_id: UUID | None
    import_metadata: JsonObject
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TrialBalanceImportBatchRecord:
    """Describe one persisted trial-balance import batch."""

    id: UUID
    entity_id: UUID
    period_start: date
    period_end: date
    source_format: str
    uploaded_filename: str
    row_count: int
    imported_by_user_id: UUID | None
    import_metadata: JsonObject
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CloseRunLedgerBindingRecord:
    """Describe one persisted close-run baseline binding."""

    id: UUID
    close_run_id: UUID
    general_ledger_import_batch_id: UUID | None
    trial_balance_import_batch_id: UUID | None
    binding_source: str
    bound_by_user_id: UUID | None
    created_at: datetime
    updated_at: datetime


class LedgerImportServiceErrorCode(StrEnum):
    """Enumerate stable error codes surfaced by ledger-import workflows."""

    ENTITY_ARCHIVED = "entity_archived"
    ENTITY_NOT_FOUND = "entity_not_found"
    INTEGRITY_CONFLICT = "integrity_conflict"
    INVALID_GL_FILE = "invalid_gl_file"
    INVALID_TB_FILE = "invalid_tb_file"
    UNSUPPORTED_FILE_TYPE = "unsupported_file_type"


class LedgerImportServiceError(Exception):
    """Represent an expected ledger-import failure for API translation."""

    def __init__(
        self,
        *,
        status_code: int,
        code: LedgerImportServiceErrorCode,
        message: str,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class LedgerRepositoryProtocol(Protocol):
    """Describe persistence operations required by ledger-import workflows."""

    def get_entity_for_user(self, *, entity_id: UUID, user_id: UUID) -> LedgerEntityRecord | None:
        """Return one accessible entity or None."""

    def list_general_ledger_imports(
        self,
        *,
        entity_id: UUID,
    ) -> tuple[GeneralLedgerImportBatchRecord, ...]:
        """Return GL import batches for one entity."""

    def list_trial_balance_imports(
        self,
        *,
        entity_id: UUID,
    ) -> tuple[TrialBalanceImportBatchRecord, ...]:
        """Return TB import batches for one entity."""

    def list_close_run_bindings_for_entity(
        self,
        *,
        entity_id: UUID,
    ) -> tuple[CloseRunLedgerBindingRecord, ...]:
        """Return close-run ledger bindings for one entity."""

    def create_general_ledger_import_batch(
        self,
        *,
        entity_id: UUID,
        period_start: date,
        period_end: date,
        source_format: str,
        uploaded_filename: str,
        row_count: int,
        imported_by_user_id: UUID | None,
        import_metadata: JsonObject,
    ) -> GeneralLedgerImportBatchRecord:
        """Persist one new GL import batch."""

    def create_general_ledger_import_lines(
        self,
        *,
        batch_id: UUID,
        lines: tuple[ImportedGeneralLedgerLineSeed, ...],
    ) -> int:
        """Persist GL line rows for a batch."""

    def create_trial_balance_import_batch(
        self,
        *,
        entity_id: UUID,
        period_start: date,
        period_end: date,
        source_format: str,
        uploaded_filename: str,
        row_count: int,
        imported_by_user_id: UUID | None,
        import_metadata: JsonObject,
    ) -> TrialBalanceImportBatchRecord:
        """Persist one new TB import batch."""

    def create_trial_balance_import_lines(
        self,
        *,
        batch_id: UUID,
        lines: tuple[ImportedTrialBalanceLineSeed, ...],
    ) -> int:
        """Persist TB line rows for a batch."""

    def list_open_close_runs_for_period(
        self,
        *,
        entity_id: UUID,
        period_start: date,
        period_end: date,
    ) -> tuple[LedgerCloseRunRecord, ...]:
        """Return open close runs matching the import period."""

    def close_run_has_ledger_activity(self, *, close_run_id: UUID) -> bool:
        """Return whether the close run already has ledger-sensitive downstream state."""

    def upsert_close_run_binding(
        self,
        *,
        close_run_id: UUID,
        general_ledger_import_batch_id: UUID | None,
        trial_balance_import_batch_id: UUID | None,
        binding_source: str,
        bound_by_user_id: UUID | None,
    ) -> CloseRunLedgerBindingRecord:
        """Create or replace the baseline binding for one close run."""

    def supersede_imported_gl_processing_state(
        self,
        *,
        close_run_id: UUID,
    ) -> tuple[int, int]:
        """Supersede stale recommendation and journal drafts now covered by imported GL."""

    def create_activity_event(
        self,
        *,
        entity_id: UUID,
        actor_user_id: UUID | None,
        event_type: str,
        source_surface: AuditSourceSurface,
        payload: JsonObject,
        trace_id: str | None,
    ) -> None:
        """Persist one immutable entity-scoped activity event."""

    def commit(self) -> None:
        """Commit the current unit of work."""

    def rollback(self) -> None:
        """Rollback the current unit of work."""

    def is_integrity_error(self, error: Exception) -> bool:
        """Return whether the exception originated from DB integrity checks."""


class LedgerRepository:
    """Execute canonical ledger-import persistence operations in one DB session."""

    def __init__(self, *, db_session: Session) -> None:
        self._db_session = db_session

    def get_entity_for_user(self, *, entity_id: UUID, user_id: UUID) -> LedgerEntityRecord | None:
        statement = (
            select(Entity)
            .join(EntityMembership, EntityMembership.entity_id == Entity.id)
            .where(Entity.id == entity_id, EntityMembership.user_id == user_id)
        )
        entity = self._db_session.execute(statement).scalar_one_or_none()
        return _map_entity(entity) if entity is not None else None

    def list_general_ledger_imports(
        self,
        *,
        entity_id: UUID,
    ) -> tuple[GeneralLedgerImportBatchRecord, ...]:
        statement = (
            select(GeneralLedgerImportBatch)
            .where(GeneralLedgerImportBatch.entity_id == entity_id)
            .order_by(desc(GeneralLedgerImportBatch.created_at), desc(GeneralLedgerImportBatch.id))
        )
        return tuple(_map_gl_batch(row) for row in self._db_session.scalars(statement))

    def list_trial_balance_imports(
        self,
        *,
        entity_id: UUID,
    ) -> tuple[TrialBalanceImportBatchRecord, ...]:
        statement = (
            select(TrialBalanceImportBatch)
            .where(TrialBalanceImportBatch.entity_id == entity_id)
            .order_by(desc(TrialBalanceImportBatch.created_at), desc(TrialBalanceImportBatch.id))
        )
        return tuple(_map_tb_batch(row) for row in self._db_session.scalars(statement))

    def list_close_run_bindings_for_entity(
        self,
        *,
        entity_id: UUID,
    ) -> tuple[CloseRunLedgerBindingRecord, ...]:
        statement = (
            select(CloseRunLedgerBinding)
            .join(CloseRun, CloseRun.id == CloseRunLedgerBinding.close_run_id)
            .where(CloseRun.entity_id == entity_id)
            .order_by(desc(CloseRunLedgerBinding.updated_at), desc(CloseRunLedgerBinding.id))
        )
        return tuple(_map_binding(row) for row in self._db_session.scalars(statement))

    def create_general_ledger_import_batch(
        self,
        *,
        entity_id: UUID,
        period_start: date,
        period_end: date,
        source_format: str,
        uploaded_filename: str,
        row_count: int,
        imported_by_user_id: UUID | None,
        import_metadata: JsonObject,
    ) -> GeneralLedgerImportBatchRecord:
        batch = GeneralLedgerImportBatch(
            entity_id=entity_id,
            period_start=period_start,
            period_end=period_end,
            source_format=source_format,
            uploaded_filename=uploaded_filename,
            row_count=row_count,
            imported_by_user_id=imported_by_user_id,
            import_metadata=dict(import_metadata),
        )
        self._db_session.add(batch)
        self._db_session.flush()
        return _map_gl_batch(batch)

    def create_general_ledger_import_lines(
        self,
        *,
        batch_id: UUID,
        lines: tuple[ImportedGeneralLedgerLineSeed, ...],
    ) -> int:
        rows = [
            GeneralLedgerImportLine(
                batch_id=batch_id,
                line_no=line.line_no,
                posting_date=line.posting_date,
                account_code=line.account_code,
                account_name=line.account_name,
                reference=line.reference,
                description=line.description,
                debit_amount=line.debit_amount,
                credit_amount=line.credit_amount,
                dimensions=dict(line.dimensions),
                external_ref=line.external_ref,
                transaction_group_key=line.transaction_group_key,
            )
            for line in lines
        ]
        self._db_session.add_all(rows)
        self._db_session.flush()
        return len(rows)

    def create_trial_balance_import_batch(
        self,
        *,
        entity_id: UUID,
        period_start: date,
        period_end: date,
        source_format: str,
        uploaded_filename: str,
        row_count: int,
        imported_by_user_id: UUID | None,
        import_metadata: JsonObject,
    ) -> TrialBalanceImportBatchRecord:
        batch = TrialBalanceImportBatch(
            entity_id=entity_id,
            period_start=period_start,
            period_end=period_end,
            source_format=source_format,
            uploaded_filename=uploaded_filename,
            row_count=row_count,
            imported_by_user_id=imported_by_user_id,
            import_metadata=dict(import_metadata),
        )
        self._db_session.add(batch)
        self._db_session.flush()
        return _map_tb_batch(batch)

    def create_trial_balance_import_lines(
        self,
        *,
        batch_id: UUID,
        lines: tuple[ImportedTrialBalanceLineSeed, ...],
    ) -> int:
        rows = [
            TrialBalanceImportLine(
                batch_id=batch_id,
                line_no=line.line_no,
                account_code=line.account_code,
                account_name=line.account_name,
                account_type=line.account_type,
                debit_balance=line.debit_balance,
                credit_balance=line.credit_balance,
                is_active=line.is_active,
            )
            for line in lines
        ]
        self._db_session.add_all(rows)
        self._db_session.flush()
        return len(rows)

    def list_open_close_runs_for_period(
        self,
        *,
        entity_id: UUID,
        period_start: date,
        period_end: date,
    ) -> tuple[LedgerCloseRunRecord, ...]:
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
            .order_by(desc(CloseRun.created_at), desc(CloseRun.id))
        )
        return tuple(_map_close_run(row) for row in self._db_session.scalars(statement))

    def close_run_has_ledger_activity(self, *, close_run_id: UUID) -> bool:
        journal_count = self._db_session.execute(
            select(func.count(JournalEntry.id)).where(
                JournalEntry.close_run_id == close_run_id,
                JournalEntry.status.in_(("approved", "applied")),
            )
        ).scalar_one()
        if int(journal_count or 0) > 0:
            return True

        reconciliation_count = self._db_session.execute(
            select(func.count(Reconciliation.id)).where(Reconciliation.close_run_id == close_run_id)
        ).scalar_one()
        if int(reconciliation_count or 0) > 0:
            return True

        trial_balance_count = self._db_session.execute(
            select(func.count(TrialBalanceSnapshot.id)).where(
                TrialBalanceSnapshot.close_run_id == close_run_id
            )
        ).scalar_one()
        return int(trial_balance_count or 0) > 0

    def upsert_close_run_binding(
        self,
        *,
        close_run_id: UUID,
        general_ledger_import_batch_id: UUID | None,
        trial_balance_import_batch_id: UUID | None,
        binding_source: str,
        bound_by_user_id: UUID | None,
    ) -> CloseRunLedgerBindingRecord:
        binding = self._db_session.execute(
            select(CloseRunLedgerBinding).where(CloseRunLedgerBinding.close_run_id == close_run_id)
        ).scalar_one_or_none()
        if binding is None:
            binding = CloseRunLedgerBinding(
                close_run_id=close_run_id,
                general_ledger_import_batch_id=general_ledger_import_batch_id,
                trial_balance_import_batch_id=trial_balance_import_batch_id,
                binding_source=binding_source,
                bound_by_user_id=bound_by_user_id,
            )
            self._db_session.add(binding)
        else:
            if general_ledger_import_batch_id is not None:
                binding.general_ledger_import_batch_id = general_ledger_import_batch_id
            if trial_balance_import_batch_id is not None:
                binding.trial_balance_import_batch_id = trial_balance_import_batch_id
            binding.binding_source = binding_source
            binding.bound_by_user_id = bound_by_user_id
        self._db_session.flush()
        return _map_binding(binding)

    def supersede_imported_gl_processing_state(
        self,
        *,
        close_run_id: UUID,
    ) -> tuple[int, int]:
        candidate_document_ids = tuple(
            dict.fromkeys(
                self._db_session.execute(
                    select(Document.id)
                    .join(Recommendation, Recommendation.document_id == Document.id)
                    .where(
                        Document.close_run_id == close_run_id,
                        Recommendation.close_run_id == close_run_id,
                        Recommendation.superseded_by_id.is_(None),
                        Recommendation.status.in_(
                            (
                                ReviewStatus.DRAFT.value,
                                ReviewStatus.PENDING_REVIEW.value,
                                ReviewStatus.APPROVED.value,
                            )
                        ),
                    )
                ).scalars().all()
            )
        )
        if not candidate_document_ids:
            return 0, 0

        representation = evaluate_documents_imported_gl_representation(
            session=self._db_session,
            close_run_id=close_run_id,
            document_ids=candidate_document_ids,
        )
        represented_document_ids = tuple(
            document_id
            for document_id, result in representation.items()
            if result.represented_in_imported_gl
        )
        if not represented_document_ids:
            return 0, 0

        recommendation_ids = tuple(
            self._db_session.execute(
                select(Recommendation.id).where(
                    Recommendation.close_run_id == close_run_id,
                    Recommendation.document_id.in_(represented_document_ids),
                    Recommendation.superseded_by_id.is_(None),
                    Recommendation.status.in_(
                        (
                            ReviewStatus.DRAFT.value,
                            ReviewStatus.PENDING_REVIEW.value,
                            ReviewStatus.APPROVED.value,
                        )
                    ),
                )
            ).scalars().all()
        )
        if not recommendation_ids:
            return 0, 0

        superseded_journal_count = int(
            self._db_session.query(JournalEntry)
            .filter(
                JournalEntry.close_run_id == close_run_id,
                JournalEntry.recommendation_id.in_(recommendation_ids),
                JournalEntry.superseded_by_id.is_(None),
                JournalEntry.status.in_(
                    (
                        ReviewStatus.DRAFT.value,
                        ReviewStatus.PENDING_REVIEW.value,
                    )
                ),
            )
            .update(
                {JournalEntry.status: ReviewStatus.SUPERSEDED.value},
                synchronize_session=False,
            )
        )
        superseded_recommendation_count = int(
            self._db_session.query(Recommendation)
            .filter(
                Recommendation.id.in_(recommendation_ids),
                Recommendation.superseded_by_id.is_(None),
            )
            .update(
                {Recommendation.status: ReviewStatus.SUPERSEDED.value},
                synchronize_session=False,
            )
        )
        self._db_session.flush()
        return superseded_recommendation_count, superseded_journal_count

    def create_activity_event(
        self,
        *,
        entity_id: UUID,
        actor_user_id: UUID | None,
        event_type: str,
        source_surface: AuditSourceSurface,
        payload: JsonObject,
        trace_id: str | None,
    ) -> None:
        AuditService(db_session=self._db_session).emit_audit_event(
            entity_id=entity_id,
            close_run_id=None,
            event_type=event_type,
            actor_user_id=actor_user_id,
            source_surface=source_surface,
            payload=dict(payload),
            trace_id=trace_id,
        )

    def commit(self) -> None:
        self._db_session.commit()

    def rollback(self) -> None:
        self._db_session.rollback()

    @staticmethod
    def is_integrity_error(error: Exception) -> bool:
        return isinstance(error, IntegrityError)


class LedgerImportService:
    """Provide the canonical entity-level imported-ledger workflows."""

    def __init__(self, *, repository: LedgerRepositoryProtocol) -> None:
        self._repository = repository

    def read_workspace(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
    ) -> LedgerWorkspaceResponse:
        self._require_active_entity(entity_id=entity_id, user_id=actor_user.id)
        return self._build_workspace(entity_id=entity_id)

    def upload_general_ledger(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        period_start: date,
        period_end: date,
        filename: str,
        payload: bytes,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> GeneralLedgerImportUploadResponse:
        entity = self._require_active_entity(entity_id=entity_id, user_id=actor_user.id)
        try:
            imported_file = import_general_ledger_file(filename=filename, payload=payload)
            batch = self._repository.create_general_ledger_import_batch(
                entity_id=entity_id,
                period_start=period_start,
                period_end=period_end,
                source_format=str(imported_file.import_metadata["format"]),
                uploaded_filename=filename,
                row_count=len(imported_file.lines),
                imported_by_user_id=actor_user.id,
                import_metadata=imported_file.import_metadata,
            )
            self._repository.create_general_ledger_import_lines(
                batch_id=batch.id,
                lines=imported_file.lines,
            )
            auto_bound_close_run_ids, skipped_close_run_ids = self._auto_bind_import(
                entity_id=entity_id,
                period_start=period_start,
                period_end=period_end,
                general_ledger_import_batch_id=batch.id,
                trial_balance_import_batch_id=None,
                bound_by_user_id=actor_user.id,
            )
            self._repository.create_activity_event(
                entity_id=entity_id,
                actor_user_id=actor_user.id,
                event_type="ledger.general_ledger_imported",
                source_surface=source_surface,
                payload={
                    "entity_name": entity.name,
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "row_count": batch.row_count,
                    "general_ledger_import_batch_id": serialize_uuid(batch.id),
                    "auto_bound_close_run_ids": [
                        serialize_uuid(close_run_id) for close_run_id in auto_bound_close_run_ids
                    ],
                    "skipped_close_run_ids": [
                        serialize_uuid(close_run_id) for close_run_id in skipped_close_run_ids
                    ],
                    "summary": (
                        f"{actor_user.full_name} imported {batch.row_count} general-ledger line(s) "
                        f"for {period_start.isoformat()} to {period_end.isoformat()}."
                    ),
                },
                trace_id=trace_id,
            )
            workspace = self._build_workspace(entity_id=entity_id)
            self._repository.commit()
        except LedgerImportError as error:
            self._repository.rollback()
            raise _translate_gl_import_error(error) from error
        except Exception as error:
            self._repository.rollback()
            if self._repository.is_integrity_error(error):
                raise LedgerImportServiceError(
                    status_code=409,
                    code=LedgerImportServiceErrorCode.INTEGRITY_CONFLICT,
                    message=(
                        "The general-ledger import could not be persisted because "
                        "the data conflicted with current state."
                    ),
                ) from error
            raise

        return GeneralLedgerImportUploadResponse(
            imported_batch=_build_gl_summary(batch),
            auto_bound_close_run_ids=tuple(
                serialize_uuid(close_run_id) for close_run_id in auto_bound_close_run_ids
            ),
            skipped_close_run_ids=tuple(
                serialize_uuid(close_run_id) for close_run_id in skipped_close_run_ids
            ),
            workspace=workspace,
        )

    def upload_trial_balance(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        period_start: date,
        period_end: date,
        filename: str,
        payload: bytes,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> TrialBalanceImportUploadResponse:
        entity = self._require_active_entity(entity_id=entity_id, user_id=actor_user.id)
        try:
            imported_file = import_trial_balance_file(filename=filename, payload=payload)
            batch = self._repository.create_trial_balance_import_batch(
                entity_id=entity_id,
                period_start=period_start,
                period_end=period_end,
                source_format=str(imported_file.import_metadata["format"]),
                uploaded_filename=filename,
                row_count=len(imported_file.lines),
                imported_by_user_id=actor_user.id,
                import_metadata=imported_file.import_metadata,
            )
            self._repository.create_trial_balance_import_lines(
                batch_id=batch.id,
                lines=imported_file.lines,
            )
            auto_bound_close_run_ids, skipped_close_run_ids = self._auto_bind_import(
                entity_id=entity_id,
                period_start=period_start,
                period_end=period_end,
                general_ledger_import_batch_id=None,
                trial_balance_import_batch_id=batch.id,
                bound_by_user_id=actor_user.id,
            )
            self._repository.create_activity_event(
                entity_id=entity_id,
                actor_user_id=actor_user.id,
                event_type="ledger.trial_balance_imported",
                source_surface=source_surface,
                payload={
                    "entity_name": entity.name,
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "row_count": batch.row_count,
                    "trial_balance_import_batch_id": serialize_uuid(batch.id),
                    "auto_bound_close_run_ids": [
                        serialize_uuid(close_run_id) for close_run_id in auto_bound_close_run_ids
                    ],
                    "skipped_close_run_ids": [
                        serialize_uuid(close_run_id) for close_run_id in skipped_close_run_ids
                    ],
                    "summary": (
                        f"{actor_user.full_name} imported {batch.row_count} trial-balance row(s) "
                        f"for {period_start.isoformat()} to {period_end.isoformat()}."
                    ),
                },
                trace_id=trace_id,
            )
            workspace = self._build_workspace(entity_id=entity_id)
            self._repository.commit()
        except LedgerImportError as error:
            self._repository.rollback()
            raise _translate_tb_import_error(error) from error
        except Exception as error:
            self._repository.rollback()
            if self._repository.is_integrity_error(error):
                raise LedgerImportServiceError(
                    status_code=409,
                    code=LedgerImportServiceErrorCode.INTEGRITY_CONFLICT,
                    message=(
                        "The trial-balance import could not be persisted because "
                        "the data conflicted with current state."
                    ),
                ) from error
            raise

        return TrialBalanceImportUploadResponse(
            imported_batch=_build_tb_summary(batch),
            auto_bound_close_run_ids=tuple(
                serialize_uuid(close_run_id) for close_run_id in auto_bound_close_run_ids
            ),
            skipped_close_run_ids=tuple(
                serialize_uuid(close_run_id) for close_run_id in skipped_close_run_ids
            ),
            workspace=workspace,
        )

    def _auto_bind_import(
        self,
        *,
        entity_id: UUID,
        period_start: date,
        period_end: date,
        general_ledger_import_batch_id: UUID | None,
        trial_balance_import_batch_id: UUID | None,
        bound_by_user_id: UUID | None,
    ) -> tuple[tuple[UUID, ...], tuple[UUID, ...]]:
        """Bind an import to matching open close runs when they have no ledger activity yet."""

        auto_bound: list[UUID] = []
        skipped: list[UUID] = []
        for close_run in self._repository.list_open_close_runs_for_period(
            entity_id=entity_id,
            period_start=period_start,
            period_end=period_end,
        ):
            if self._repository.close_run_has_ledger_activity(close_run_id=close_run.id):
                skipped.append(close_run.id)
                continue
            self._repository.upsert_close_run_binding(
                close_run_id=close_run.id,
                general_ledger_import_batch_id=general_ledger_import_batch_id,
                trial_balance_import_batch_id=trial_balance_import_batch_id,
                binding_source="auto",
                bound_by_user_id=bound_by_user_id,
            )
            if general_ledger_import_batch_id is not None:
                self._repository.supersede_imported_gl_processing_state(
                    close_run_id=close_run.id
                )
            auto_bound.append(close_run.id)
        return tuple(auto_bound), tuple(skipped)

    def _build_workspace(self, *, entity_id: UUID) -> LedgerWorkspaceResponse:
        return LedgerWorkspaceResponse(
            general_ledger_imports=tuple(
                _build_gl_summary(record)
                for record in self._repository.list_general_ledger_imports(entity_id=entity_id)
            ),
            trial_balance_imports=tuple(
                _build_tb_summary(record)
                for record in self._repository.list_trial_balance_imports(entity_id=entity_id)
            ),
            close_run_bindings=tuple(
                _build_binding_summary(record)
                for record in self._repository.list_close_run_bindings_for_entity(
                    entity_id=entity_id
                )
            ),
        )

    def _require_active_entity(self, *, entity_id: UUID, user_id: UUID) -> LedgerEntityRecord:
        entity = self._repository.get_entity_for_user(entity_id=entity_id, user_id=user_id)
        if entity is None:
            raise LedgerImportServiceError(
                status_code=404,
                code=LedgerImportServiceErrorCode.ENTITY_NOT_FOUND,
                message="That entity does not exist or is not accessible to the current user.",
            )
        if entity.status is EntityStatus.ARCHIVED:
            raise LedgerImportServiceError(
                status_code=409,
                code=LedgerImportServiceErrorCode.ENTITY_ARCHIVED,
                message="Archived entities cannot accept imported ledger baselines.",
            )
        return entity


def _translate_gl_import_error(error: LedgerImportError) -> LedgerImportServiceError:
    """Convert a raw importer failure into the public GL upload error surface."""

    if error.code is LedgerImportErrorCode.UNSUPPORTED_FILE_TYPE:
        code = LedgerImportServiceErrorCode.UNSUPPORTED_FILE_TYPE
    else:
        code = LedgerImportServiceErrorCode.INVALID_GL_FILE
    return LedgerImportServiceError(status_code=400, code=code, message=error.message)


def _translate_tb_import_error(error: LedgerImportError) -> LedgerImportServiceError:
    """Convert a raw importer failure into the public TB upload error surface."""

    if error.code is LedgerImportErrorCode.UNSUPPORTED_FILE_TYPE:
        code = LedgerImportServiceErrorCode.UNSUPPORTED_FILE_TYPE
    else:
        code = LedgerImportServiceErrorCode.INVALID_TB_FILE
    return LedgerImportServiceError(status_code=400, code=code, message=error.message)


def _map_entity(entity: Entity) -> LedgerEntityRecord:
    """Convert an entity ORM row into the immutable record used by ledger workflows."""

    return LedgerEntityRecord(id=entity.id, name=entity.name, status=EntityStatus(entity.status))


def _map_close_run(close_run: CloseRun) -> LedgerCloseRunRecord:
    """Convert a close-run ORM row into the minimal auto-binding record."""

    return LedgerCloseRunRecord(
        id=close_run.id,
        entity_id=close_run.entity_id,
        period_start=close_run.period_start,
        period_end=close_run.period_end,
        status=CloseRunStatus(close_run.status),
    )


def _map_gl_batch(batch: GeneralLedgerImportBatch) -> GeneralLedgerImportBatchRecord:
    """Convert one GL batch ORM row into an immutable service-layer record."""

    return GeneralLedgerImportBatchRecord(
        id=batch.id,
        entity_id=batch.entity_id,
        period_start=batch.period_start,
        period_end=batch.period_end,
        source_format=batch.source_format,
        uploaded_filename=batch.uploaded_filename,
        row_count=batch.row_count,
        imported_by_user_id=batch.imported_by_user_id,
        import_metadata=dict(batch.import_metadata or {}),
        created_at=batch.created_at,
        updated_at=batch.updated_at,
    )


def _map_tb_batch(batch: TrialBalanceImportBatch) -> TrialBalanceImportBatchRecord:
    """Convert one TB batch ORM row into an immutable service-layer record."""

    return TrialBalanceImportBatchRecord(
        id=batch.id,
        entity_id=batch.entity_id,
        period_start=batch.period_start,
        period_end=batch.period_end,
        source_format=batch.source_format,
        uploaded_filename=batch.uploaded_filename,
        row_count=batch.row_count,
        imported_by_user_id=batch.imported_by_user_id,
        import_metadata=dict(batch.import_metadata or {}),
        created_at=batch.created_at,
        updated_at=batch.updated_at,
    )


def _map_binding(binding: CloseRunLedgerBinding) -> CloseRunLedgerBindingRecord:
    """Convert one baseline-binding ORM row into an immutable service-layer record."""

    return CloseRunLedgerBindingRecord(
        id=binding.id,
        close_run_id=binding.close_run_id,
        general_ledger_import_batch_id=binding.general_ledger_import_batch_id,
        trial_balance_import_batch_id=binding.trial_balance_import_batch_id,
        binding_source=binding.binding_source,
        bound_by_user_id=binding.bound_by_user_id,
        created_at=binding.created_at,
        updated_at=binding.updated_at,
    )


def _build_binding_summary(binding: CloseRunLedgerBindingRecord) -> CloseRunLedgerBindingSummary:
    """Convert one service-layer binding record into the public contract."""

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


def _build_gl_summary(batch: GeneralLedgerImportBatchRecord) -> GeneralLedgerImportSummary:
    """Convert one GL batch record into the public contract."""

    return GeneralLedgerImportSummary(
        id=serialize_uuid(batch.id),
        entity_id=serialize_uuid(batch.entity_id),
        period_start=batch.period_start,
        period_end=batch.period_end,
        source_format=batch.source_format,
        uploaded_filename=batch.uploaded_filename,
        row_count=batch.row_count,
        imported_by_user_id=(
            serialize_uuid(batch.imported_by_user_id)
            if batch.imported_by_user_id is not None
            else None
        ),
        import_metadata=batch.import_metadata,
        created_at=batch.created_at,
        updated_at=batch.updated_at,
    )


def _build_tb_summary(batch: TrialBalanceImportBatchRecord) -> TrialBalanceImportSummary:
    """Convert one TB batch record into the public contract."""

    return TrialBalanceImportSummary(
        id=serialize_uuid(batch.id),
        entity_id=serialize_uuid(batch.entity_id),
        period_start=batch.period_start,
        period_end=batch.period_end,
        source_format=batch.source_format,
        uploaded_filename=batch.uploaded_filename,
        row_count=batch.row_count,
        imported_by_user_id=(
            serialize_uuid(batch.imported_by_user_id)
            if batch.imported_by_user_id is not None
            else None
        ),
        import_metadata=batch.import_metadata,
        created_at=batch.created_at,
        updated_at=batch.updated_at,
    )


__all__ = [
    "CloseRunLedgerBindingRecord",
    "GeneralLedgerImportBatchRecord",
    "LedgerImportService",
    "LedgerImportServiceError",
    "LedgerImportServiceErrorCode",
    "LedgerRepository",
    "LedgerRepositoryProtocol",
    "TrialBalanceImportBatchRecord",
]
