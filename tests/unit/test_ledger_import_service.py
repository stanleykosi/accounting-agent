"""
Purpose: Verify ledger baseline upload orchestration and safe auto-binding behavior.
Scope: Focused unit coverage over the ledger-import service using an in-memory fake repository.
Dependencies: LedgerImportService contracts/records and EntityUserRecord fixtures.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from services.common.enums import CloseRunStatus
from services.db.models.audit import AuditSourceSurface
from services.db.models.entity import EntityStatus
from services.db.repositories.entity_repo import EntityUserRecord
from services.ledger.service import (
    CloseRunLedgerBindingRecord,
    GeneralLedgerImportBatchRecord,
    LedgerCloseRunRecord,
    LedgerEntityRecord,
    LedgerImportService,
    TrialBalanceImportBatchRecord,
)


class _FakeLedgerRepository:
    """Capture the repository interactions needed by ledger-import service tests."""

    def __init__(self) -> None:
        self.entity = LedgerEntityRecord(id=uuid4(), name="Transfa", status=EntityStatus.ACTIVE)
        self.general_ledger_imports: list[GeneralLedgerImportBatchRecord] = []
        self.trial_balance_imports: list[TrialBalanceImportBatchRecord] = []
        self.bindings_by_close_run_id: dict[UUID, CloseRunLedgerBindingRecord] = {}
        self.open_close_runs: tuple[LedgerCloseRunRecord, ...] = ()
        self.ledger_activity_by_close_run_id: dict[UUID, bool] = {}
        self.created_gl_line_counts: list[int] = []
        self.created_gl_lines: list[tuple] = []
        self.created_tb_line_counts: list[int] = []
        self.activity_events: list[dict[str, object]] = []
        self.superseded_imported_gl_close_run_ids: list[UUID] = []

    def get_entity_for_user(self, *, entity_id: UUID, user_id: UUID) -> LedgerEntityRecord | None:
        del user_id
        return self.entity if entity_id == self.entity.id else None

    def list_general_ledger_imports(
        self,
        *,
        entity_id: UUID,
    ) -> tuple[GeneralLedgerImportBatchRecord, ...]:
        if entity_id != self.entity.id:
            return ()
        return tuple(self.general_ledger_imports)

    def list_trial_balance_imports(
        self,
        *,
        entity_id: UUID,
    ) -> tuple[TrialBalanceImportBatchRecord, ...]:
        if entity_id != self.entity.id:
            return ()
        return tuple(self.trial_balance_imports)

    def list_close_run_bindings_for_entity(
        self,
        *,
        entity_id: UUID,
    ) -> tuple[CloseRunLedgerBindingRecord, ...]:
        if entity_id != self.entity.id:
            return ()
        return tuple(self.bindings_by_close_run_id.values())

    def create_general_ledger_import_batch(self, **kwargs) -> GeneralLedgerImportBatchRecord:
        created_at = datetime(2026, 4, 18, 10, 0, tzinfo=UTC)
        record = GeneralLedgerImportBatchRecord(
            id=uuid4(),
            entity_id=kwargs["entity_id"],
            period_start=kwargs["period_start"],
            period_end=kwargs["period_end"],
            source_format=kwargs["source_format"],
            uploaded_filename=kwargs["uploaded_filename"],
            row_count=kwargs["row_count"],
            imported_by_user_id=kwargs["imported_by_user_id"],
            import_metadata=kwargs["import_metadata"],
            created_at=created_at,
            updated_at=created_at,
        )
        self.general_ledger_imports.insert(0, record)
        return record

    def create_general_ledger_import_lines(self, *, batch_id: UUID, lines: tuple) -> int:
        del batch_id
        self.created_gl_line_counts.append(len(lines))
        self.created_gl_lines.append(lines)
        return len(lines)

    def create_trial_balance_import_batch(self, **kwargs) -> TrialBalanceImportBatchRecord:
        created_at = datetime(2026, 4, 18, 10, 5, tzinfo=UTC)
        record = TrialBalanceImportBatchRecord(
            id=uuid4(),
            entity_id=kwargs["entity_id"],
            period_start=kwargs["period_start"],
            period_end=kwargs["period_end"],
            source_format=kwargs["source_format"],
            uploaded_filename=kwargs["uploaded_filename"],
            row_count=kwargs["row_count"],
            imported_by_user_id=kwargs["imported_by_user_id"],
            import_metadata=kwargs["import_metadata"],
            created_at=created_at,
            updated_at=created_at,
        )
        self.trial_balance_imports.insert(0, record)
        return record

    def create_trial_balance_import_lines(self, *, batch_id: UUID, lines: tuple) -> int:
        del batch_id
        self.created_tb_line_counts.append(len(lines))
        return len(lines)

    def list_open_close_runs_for_period(
        self,
        *,
        entity_id: UUID,
        period_start: date,
        period_end: date,
    ) -> tuple[LedgerCloseRunRecord, ...]:
        if entity_id != self.entity.id:
            return ()
        return tuple(
            close_run
            for close_run in self.open_close_runs
            if close_run.period_start == period_start and close_run.period_end == period_end
        )

    def close_run_has_ledger_activity(self, *, close_run_id: UUID) -> bool:
        return self.ledger_activity_by_close_run_id.get(close_run_id, False)

    def upsert_close_run_binding(
        self,
        *,
        close_run_id: UUID,
        general_ledger_import_batch_id: UUID | None,
        trial_balance_import_batch_id: UUID | None,
        binding_source: str,
        bound_by_user_id: UUID | None,
    ) -> CloseRunLedgerBindingRecord:
        current = self.bindings_by_close_run_id.get(close_run_id)
        created_at = (
            current.created_at
            if current is not None
            else datetime(2026, 4, 18, 10, 10, tzinfo=UTC)
        )
        updated_at = datetime(2026, 4, 18, 10, 11, tzinfo=UTC)
        record = CloseRunLedgerBindingRecord(
            id=current.id if current is not None else uuid4(),
            close_run_id=close_run_id,
            general_ledger_import_batch_id=(
                general_ledger_import_batch_id
                if general_ledger_import_batch_id is not None
                else (current.general_ledger_import_batch_id if current is not None else None)
            ),
            trial_balance_import_batch_id=(
                trial_balance_import_batch_id
                if trial_balance_import_batch_id is not None
                else (current.trial_balance_import_batch_id if current is not None else None)
            ),
            binding_source=binding_source,
            bound_by_user_id=bound_by_user_id,
            created_at=created_at,
            updated_at=updated_at,
        )
        self.bindings_by_close_run_id[close_run_id] = record
        return record

    def supersede_imported_gl_processing_state(
        self,
        *,
        close_run_id: UUID,
    ) -> tuple[int, int]:
        self.superseded_imported_gl_close_run_ids.append(close_run_id)
        return (0, 0)

    def create_activity_event(self, **kwargs) -> None:
        self.activity_events.append(kwargs)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def is_integrity_error(self, error: Exception) -> bool:
        del error
        return False


def test_upload_general_ledger_auto_binds_safe_close_runs_and_skips_started_runs() -> None:
    """GL uploads should auto-bind exact-period close runs only when no ledger activity exists."""

    repository = _FakeLedgerRepository()
    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    safe_close_run = LedgerCloseRunRecord(
        id=uuid4(),
        entity_id=repository.entity.id,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
        status=CloseRunStatus.DRAFT,
    )
    started_close_run = replace(safe_close_run, id=uuid4(), status=CloseRunStatus.REOPENED)
    repository.open_close_runs = (safe_close_run, started_close_run)
    repository.ledger_activity_by_close_run_id[started_close_run.id] = True

    service = LedgerImportService(repository=repository)
    response = service.upload_general_ledger(
        actor_user=actor_user,
        entity_id=repository.entity.id,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
        filename="march-gl.csv",
        payload=(
            b"posting_date,account_code,reference,description,debit_amount,credit_amount\n"
            b"2026-03-05,1000,GL-001,Imported cash receipt,1200.00,0.00\n"
        ),
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="trace-ledger-upload",
    )

    assert response.imported_batch.row_count == 1
    assert response.auto_bound_close_run_ids == (str(safe_close_run.id),)
    assert response.skipped_close_run_ids == (str(started_close_run.id),)
    assert repository.created_gl_line_counts == [1]
    assert repository.created_gl_lines[0][0].transaction_group_key.startswith("glgrp_")
    assert (
        repository.bindings_by_close_run_id[
            safe_close_run.id
        ].general_ledger_import_batch_id
        is not None
    )
    assert repository.superseded_imported_gl_close_run_ids == [safe_close_run.id]
    assert repository.activity_events
