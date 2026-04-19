
"""
Purpose: Verify close-run GL export generation against the canonical effective-ledger state.
Scope: Idempotent artifact release, imported-baseline plus adjustment composition,
adjustment-only exports, and explicit no-data failures without live object storage.
Dependencies: SQLAlchemy ORM models, the GL export service, and in-memory storage doubles.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

from services.common.enums import ArtifactType
from services.contracts.storage_models import (
    ArtifactStorageMetadata,
    CloseRunStorageScope,
    ObjectStorageReference,
    StorageBucketKind,
)
from services.db.base import Base
from services.db.models.close_run import CloseRun
from services.db.models.exports import Artifact
from services.db.models.journals import JournalEntry, JournalLine
from services.db.models.ledger import (
    CloseRunLedgerBinding,
    GeneralLedgerImportBatch,
    GeneralLedgerImportLine,
)
from services.db.repositories.entity_repo import EntityUserRecord
from services.ledger.export_service import (
    GeneralLedgerExportService,
    GeneralLedgerExportServiceError,
    GeneralLedgerExportServiceErrorCode,
)
from sqlalchemy import DefaultClause, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(
    _type_: JSONB,
    _compiler: object,
    **_compiler_kwargs: object,
) -> str:
    """Allow GL export helper tests to run against in-memory SQLite."""

    return "JSON"


def test_generate_export_combines_imported_gl_and_close_run_journal_adjustments() -> None:
    """The export should contain both the bound imported GL baseline and current-run journals."""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    _patch_sqlite_table_defaults()
    tables = [
        CloseRun.__table__,
        Artifact.__table__,
        GeneralLedgerImportBatch.__table__,
        GeneralLedgerImportLine.__table__,
        CloseRunLedgerBinding.__table__,
        JournalEntry.__table__,
        JournalLine.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    session_factory = sessionmaker(bind=engine)

    close_run_id = uuid4()
    entity_id = uuid4()
    gl_batch_id = uuid4()
    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    fake_storage = _FakeStorageRepository()

    with session_factory() as session:
        session.add(
            CloseRun(
                id=close_run_id,
                entity_id=entity_id,
                period_start=date(2026, 3, 1),
                period_end=date(2026, 3, 31),
                status="draft",
                reporting_currency="USD",
                current_version_no=1,
                opened_by_user_id=uuid4(),
                approved_by_user_id=None,
                approved_at=None,
                archived_at=None,
                reopened_from_close_run_id=None,
            )
        )
        session.add(
            GeneralLedgerImportBatch(
                id=gl_batch_id,
                entity_id=entity_id,
                period_start=date(2026, 3, 1),
                period_end=date(2026, 3, 31),
                source_format="csv",
                uploaded_filename="march-gl.csv",
                row_count=1,
                imported_by_user_id=None,
                import_metadata={},
            )
        )
        session.add(
            GeneralLedgerImportLine(
                id=uuid4(),
                batch_id=gl_batch_id,
                line_no=1,
                posting_date=date(2026, 3, 5),
                account_code="1000",
                account_name="Cash",
                reference="GL-001",
                description="Imported cash receipt",
                debit_amount="1200.00",
                credit_amount="0.00",
                dimensions={"department": "Ops"},
                external_ref="EXT-001",
                transaction_group_key="glgrp_import_receipt",
            )
        )
        session.add(
            CloseRunLedgerBinding(
                id=uuid4(),
                close_run_id=close_run_id,
                general_ledger_import_batch_id=gl_batch_id,
                trial_balance_import_batch_id=None,
                binding_source="auto",
                bound_by_user_id=None,
            )
        )
        journal_id = uuid4()
        session.add(
            JournalEntry(
                id=journal_id,
                entity_id=entity_id,
                close_run_id=close_run_id,
                recommendation_id=None,
                journal_number="JE-2026-00001",
                posting_date=date(2026, 3, 31),
                status="approved",
                description="Close-run adjustment",
                total_debits="50.00",
                total_credits="50.00",
                line_count=2,
                source_surface="system",
                autonomy_mode=None,
                reasoning_summary=None,
                metadata_payload={},
                approved_by_user_id=None,
                applied_by_user_id=None,
                superseded_by_id=None,
            )
        )
        session.add_all(
            (
                JournalLine(
                    id=uuid4(),
                    journal_entry_id=journal_id,
                    line_no=1,
                    account_code="6100",
                    line_type="debit",
                    amount="50.00",
                    description="Expense true-up",
                    dimensions={},
                    reference="ADJ-001",
                ),
                JournalLine(
                    id=uuid4(),
                    journal_entry_id=journal_id,
                    line_no=2,
                    account_code="1000",
                    line_type="credit",
                    amount="50.00",
                    description="Cash true-up",
                    dimensions={},
                    reference="ADJ-001",
                ),
            )
        )
        session.commit()

        service = GeneralLedgerExportService(
            db_session=session,
            storage_repository=fake_storage,
        )
        service._verify_close_run_access = lambda **kwargs: None
        service._require_close_run_context = lambda **kwargs: (
            session.get(CloseRun, close_run_id),
            SimpleNamespace(id=entity_id, name="Transfa"),
        )

        first_summary = service.generate_export(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
        second_summary = service.generate_export(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
        )

    assert first_summary.row_count == 3
    assert first_summary.imported_line_count == 1
    assert first_summary.adjustment_line_count == 2
    assert first_summary.composition_mode == "imported_gl_plus_adjustments"
    assert first_summary.includes_imported_baseline is True
    assert first_summary.artifact_id == second_summary.artifact_id
    assert len(fake_storage.calls) == 1
    assert "imported_general_ledger" in fake_storage.calls[0].payload.decode("utf-8")
    assert "close_run_journal" in fake_storage.calls[0].payload.decode("utf-8")


def test_generate_export_supports_adjustment_only_runs_without_imported_gl() -> None:
    """When no imported GL baseline is bound, the export should truthfully emit adjustments only."""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    _patch_sqlite_table_defaults()
    tables = [
        CloseRun.__table__,
        Artifact.__table__,
        CloseRunLedgerBinding.__table__,
        JournalEntry.__table__,
        JournalLine.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    session_factory = sessionmaker(bind=engine)

    close_run_id = uuid4()
    entity_id = uuid4()
    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    fake_storage = _FakeStorageRepository()

    with session_factory() as session:
        session.add(
            CloseRun(
                id=close_run_id,
                entity_id=entity_id,
                period_start=date(2026, 3, 1),
                period_end=date(2026, 3, 31),
                status="draft",
                reporting_currency="USD",
                current_version_no=1,
                opened_by_user_id=uuid4(),
                approved_by_user_id=None,
                approved_at=None,
                archived_at=None,
                reopened_from_close_run_id=None,
            )
        )
        journal_id = uuid4()
        session.add(
            JournalEntry(
                id=journal_id,
                entity_id=entity_id,
                close_run_id=close_run_id,
                recommendation_id=None,
                journal_number="JE-2026-00002",
                posting_date=date(2026, 3, 31),
                status="applied",
                description="Accrual adjustment",
                total_debits="200.00",
                total_credits="200.00",
                line_count=2,
                source_surface="system",
                autonomy_mode=None,
                reasoning_summary=None,
                metadata_payload={},
                approved_by_user_id=None,
                applied_by_user_id=None,
                superseded_by_id=None,
            )
        )
        session.add_all(
            (
                JournalLine(
                    id=uuid4(),
                    journal_entry_id=journal_id,
                    line_no=1,
                    account_code="6100",
                    line_type="debit",
                    amount="200.00",
                    description="Expense accrual",
                    dimensions={},
                    reference="ACCR-001",
                ),
                JournalLine(
                    id=uuid4(),
                    journal_entry_id=journal_id,
                    line_no=2,
                    account_code="2100",
                    line_type="credit",
                    amount="200.00",
                    description="Accrued liability",
                    dimensions={},
                    reference="ACCR-001",
                ),
            )
        )
        session.commit()

        service = GeneralLedgerExportService(
            db_session=session,
            storage_repository=fake_storage,
        )
        service._verify_close_run_access = lambda **kwargs: None
        service._require_close_run_context = lambda **kwargs: (
            session.get(CloseRun, close_run_id),
            SimpleNamespace(id=entity_id, name="Transfa"),
        )

        summary = service.generate_export(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
        )

    assert summary.row_count == 2
    assert summary.imported_line_count == 0
    assert summary.adjustment_line_count == 2
    assert summary.composition_mode == "adjustments_only"
    assert summary.includes_imported_baseline is False


def test_generate_export_fails_fast_when_no_transaction_level_ledger_data_exists() -> None:
    """TB-only or empty runs should not pretend a transaction-level GL export exists."""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    _patch_sqlite_table_defaults()
    tables = [
        CloseRun.__table__,
        Artifact.__table__,
        CloseRunLedgerBinding.__table__,
        JournalEntry.__table__,
        JournalLine.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    session_factory = sessionmaker(bind=engine)

    close_run_id = uuid4()
    entity_id = uuid4()
    actor_user = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")

    with session_factory() as session:
        session.add(
            CloseRun(
                id=close_run_id,
                entity_id=entity_id,
                period_start=date(2026, 3, 1),
                period_end=date(2026, 3, 31),
                status="draft",
                reporting_currency="USD",
                current_version_no=1,
                opened_by_user_id=uuid4(),
                approved_by_user_id=None,
                approved_at=None,
                archived_at=None,
                reopened_from_close_run_id=None,
            )
        )
        session.commit()

        service = GeneralLedgerExportService(
            db_session=session,
            storage_repository=_FakeStorageRepository(),
        )
        service._verify_close_run_access = lambda **kwargs: None
        service._require_close_run_context = lambda **kwargs: (
            session.get(CloseRun, close_run_id),
            SimpleNamespace(id=entity_id, name="Transfa"),
        )

        try:
            service.generate_export(
                actor_user=actor_user,
                entity_id=entity_id,
                close_run_id=close_run_id,
            )
        except GeneralLedgerExportServiceError as error:
            assert error.code is GeneralLedgerExportServiceErrorCode.NO_LEDGER_DATA
            assert error.status_code == 409
        else:
            raise AssertionError("Expected no-ledger-data export generation to fail.")


@dataclass(frozen=True, slots=True)
class _StoredArtifactCall:
    """Capture one artifact upload attempted by the export service."""

    artifact_type: ArtifactType
    filename: str
    idempotency_key: str
    payload: bytes
    scope: CloseRunStorageScope


class _FakeStorageRepository:
    """Provide the tiny artifact-storage surface needed by the export service tests."""

    def __init__(self) -> None:
        self.calls: list[_StoredArtifactCall] = []

    def store_artifact(
        self,
        *,
        scope: CloseRunStorageScope,
        artifact_type: ArtifactType,
        idempotency_key: str,
        filename: str,
        payload: bytes,
        content_type: str,
        expected_sha256: str | None = None,
    ) -> ArtifactStorageMetadata:
        del expected_sha256
        self.calls.append(
            _StoredArtifactCall(
                scope=scope,
                artifact_type=artifact_type,
                idempotency_key=idempotency_key,
                filename=filename,
                payload=payload,
            )
        )
        object_key = (
            f"entities/{scope.entity_id}/close-runs/{scope.close_run_id}/versions/"
            f"{scope.close_run_version_no}/artifacts/{artifact_type.value}/{idempotency_key}/{filename}"
        )
        return ArtifactStorageMetadata(
            reference=ObjectStorageReference(
                bucket_kind=StorageBucketKind.ARTIFACTS,
                bucket_name="test-artifacts",
                object_key=object_key,
            ),
            content_type=content_type,
            size_bytes=len(payload),
            sha256_checksum="a" * 64,
            etag="etag-test",
            version_id=None,
            artifact_type=artifact_type,
            close_run_version_no=scope.close_run_version_no,
            idempotency_key=idempotency_key,
        )


def _patch_sqlite_table_defaults() -> None:
    """Replace PostgreSQL-only JSONB defaults with SQLite-safe defaults for local unit tests."""

    Artifact.__table__.c["metadata"].server_default = DefaultClause(text("'{}'"))
