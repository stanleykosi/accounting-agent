"""
Purpose: Regression coverage for recommendation-task persistence phase guards.
Scope: Ensures stale recommendation writes are blocked in the same session that persists them.
Dependencies: Recommendation task helpers plus lightweight session doubles.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from apps.worker.app.tasks import generate_recommendations as recommendation_task_module
from services.common.enums import ReviewStatus, WorkflowPhase
from services.contracts.recommendation_models import RecommendationContext
from services.db.base import Base
from services.db.models.close_run import CloseRun
from services.db.models.journals import JournalEntry
from services.db.models.recommendations import Recommendation
from services.documents.imported_ledger_representation import (
    ImportedLedgerRepresentationResult,
)
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(
    _type_: JSONB,
    _compiler: object,
    **_compiler_kwargs: object,
) -> str:
    """Allow recommendation-task helpers to run against in-memory SQLite."""

    return "JSON"


class _FakeQuery:
    def filter(self, *args, **kwargs):
        del args, kwargs
        return self

    def first(self):
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.commit_count = 0
        self.refreshed: list[object] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        del exc_type, exc, tb
        return False

    def add(self, value: object) -> None:
        self.added.append(value)

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.commit_count += 1

    def refresh(self, value: object) -> None:
        self.refreshed.append(value)

    def query(self, model):
        del model
        return _FakeQuery()


class _FakeJobContext:
    def checkpoint(self, **kwargs):
        del kwargs
        return {}

    def ensure_not_canceled(self) -> None:
        return None


def test_persist_recommendation_checks_processing_phase_in_persistence_session(monkeypatch) -> None:
    """Recommendation persistence should validate phase with the same session that writes state."""

    fake_session = _FakeSession()
    guard_calls: list[tuple[object, object, object]] = []

    monkeypatch.setattr(
        recommendation_task_module,
        "get_session_factory",
        lambda: (lambda: fake_session),
    )
    monkeypatch.setattr(
        recommendation_task_module,
        "ensure_close_run_active_phase",
        lambda *, session, close_run_id, required_phase: guard_calls.append(
            (session, close_run_id, required_phase)
        ),
    )

    context = RecommendationContext(
        close_run_id=uuid4(),
        document_id=uuid4(),
        entity_id=uuid4(),
        period_start="2026-03-01",
        period_end="2026-03-31",
        document_type=None,
        extracted_fields={},
        line_items=[],
        coa_accounts=[],
        coa_source="uploaded",
        autonomy_mode="human_review",
        confidence_threshold=0.7,
    )

    receipt = recommendation_task_module._persist_recommendation(
        recommendation_data={
            "close_run_id": context.close_run_id,
            "document_id": context.document_id,
            "recommendation_type": "gl_coding",
            "payload": {"account_code": "6100"},
            "confidence": 0.93,
            "reasoning_summary": "Deterministic coding matched the expense account.",
            "evidence_links": [],
            "prompt_version": "test-prompt",
            "rule_version": "test-rules",
            "schema_version": "1.0.0",
        },
        routed_status="draft",
        context=context,
        actor_user_id=uuid4(),
        force=False,
        trace_id="trace-123",
    )

    assert receipt.status == "draft"
    assert guard_calls == [
        (fake_session, context.close_run_id, WorkflowPhase.PROCESSING)
    ]
    assert fake_session.commit_count == 1


def test_run_recommendation_task_suppresses_when_imported_gl_already_represents_document(
    monkeypatch,
) -> None:
    """The worker should no-op before graph execution when the imported GL already has the doc."""

    fake_session = _FakeSession()
    entity_id = uuid4()
    close_run_id = uuid4()
    document_id = uuid4()
    actor_user_id = uuid4()

    monkeypatch.setattr(
        recommendation_task_module,
        "get_session_factory",
        lambda: (lambda: fake_session),
    )
    monkeypatch.setattr(
        recommendation_task_module,
        "evaluate_document_imported_gl_representation",
        lambda **kwargs: ImportedLedgerRepresentationResult(
            document_id=kwargs["document_id"],
            represented_in_imported_gl=True,
            status="represented_in_imported_gl",
            reason="The imported general ledger already contains this transaction.",
            matched_line_no=7,
            matched_reference="INV-1048",
            matched_description="Imported baseline entry",
            matched_posting_date=date(2026, 3, 15),
        ),
    )
    monkeypatch.setattr(
        recommendation_task_module,
        "_load_recommendation_context",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("context load should not run")),
    )

    payload = recommendation_task_module._run_recommendation_task(
        entity_id=str(entity_id),
        close_run_id=str(close_run_id),
        document_id=str(document_id),
        actor_user_id=str(actor_user_id),
        force=False,
        job_context=_FakeJobContext(),
    )

    assert payload == {
        "recommendation_id": None,
        "status": "suppressed_existing_imported_gl",
        "confidence": 1.0,
        "model_used": False,
        "errors": ["The imported general ledger already contains this transaction."],
        "document_id": str(document_id),
        "close_run_id": str(close_run_id),
    }


def test_force_regeneration_supersedes_prior_recommendation_state() -> None:
    """A replacement recommendation should supersede older active state for the document."""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    tables = [
        CloseRun.__table__,
        Recommendation.__table__,
        JournalEntry.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    session_factory = sessionmaker(bind=engine)

    close_run_id = uuid4()
    document_id = uuid4()
    entity_id = uuid4()
    old_recommendation_id = uuid4()
    replacement_recommendation_id = uuid4()
    journal_id = uuid4()

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
        session.add_all(
            (
                Recommendation(
                    id=old_recommendation_id,
                    close_run_id=close_run_id,
                    document_id=document_id,
                    recommendation_type="gl_coding",
                    status=ReviewStatus.DRAFT.value,
                    payload={"account_code": "6100"},
                    confidence=0.61,
                    reasoning_summary="Original recommendation",
                    evidence_links=[],
                    prompt_version="prompt-v1",
                    rule_version="rules-v1",
                    schema_version="1.0.0",
                ),
                Recommendation(
                    id=replacement_recommendation_id,
                    close_run_id=close_run_id,
                    document_id=document_id,
                    recommendation_type="gl_coding",
                    status=ReviewStatus.DRAFT.value,
                    payload={"account_code": "6200"},
                    confidence=0.83,
                    reasoning_summary="Replacement recommendation",
                    evidence_links=[],
                    prompt_version="prompt-v2",
                    rule_version="rules-v2",
                    schema_version="1.0.0",
                ),
                JournalEntry(
                    id=journal_id,
                    entity_id=entity_id,
                    close_run_id=close_run_id,
                    recommendation_id=old_recommendation_id,
                    journal_number="JE-2026-00001",
                    posting_date=date(2026, 3, 31),
                    status=ReviewStatus.APPROVED.value,
                    description="Original generated journal",
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
                ),
            )
        )
        session.commit()

        counts = recommendation_task_module._supersede_existing_recommendation_state_for_document(
            db_session=session,
            close_run_id=close_run_id,
            document_id=document_id,
            replacement_recommendation_id=replacement_recommendation_id,
        )
        session.commit()

        refreshed_old_recommendation = session.get(Recommendation, old_recommendation_id)
        refreshed_replacement = session.get(Recommendation, replacement_recommendation_id)
        refreshed_journal = session.get(JournalEntry, journal_id)

    assert counts == (1, 1)
    assert refreshed_old_recommendation is not None
    assert refreshed_old_recommendation.status == ReviewStatus.SUPERSEDED.value
    assert refreshed_old_recommendation.superseded_by_id == replacement_recommendation_id
    assert refreshed_replacement is not None
    assert refreshed_replacement.superseded_by_id is None
    assert refreshed_journal is not None
    assert refreshed_journal.status == ReviewStatus.SUPERSEDED.value


def test_force_regeneration_blocks_when_prior_applied_journal_exists() -> None:
    """A late applied journal should still block worker-side regeneration superseding."""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    tables = [
        CloseRun.__table__,
        Recommendation.__table__,
        JournalEntry.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    session_factory = sessionmaker(bind=engine)

    close_run_id = uuid4()
    document_id = uuid4()
    entity_id = uuid4()
    old_recommendation_id = uuid4()
    replacement_recommendation_id = uuid4()
    journal_id = uuid4()

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
        session.add_all(
            (
                Recommendation(
                    id=old_recommendation_id,
                    close_run_id=close_run_id,
                    document_id=document_id,
                    recommendation_type="gl_coding",
                    status=ReviewStatus.DRAFT.value,
                    payload={"account_code": "6100"},
                    confidence=0.61,
                    reasoning_summary="Original recommendation",
                    evidence_links=[],
                    prompt_version="prompt-v1",
                    rule_version="rules-v1",
                    schema_version="1.0.0",
                ),
                Recommendation(
                    id=replacement_recommendation_id,
                    close_run_id=close_run_id,
                    document_id=document_id,
                    recommendation_type="gl_coding",
                    status=ReviewStatus.DRAFT.value,
                    payload={"account_code": "6200"},
                    confidence=0.83,
                    reasoning_summary="Replacement recommendation",
                    evidence_links=[],
                    prompt_version="prompt-v2",
                    rule_version="rules-v2",
                    schema_version="1.0.0",
                ),
                JournalEntry(
                    id=journal_id,
                    entity_id=entity_id,
                    close_run_id=close_run_id,
                    recommendation_id=old_recommendation_id,
                    journal_number="JE-2026-00002",
                    posting_date=date(2026, 3, 31),
                    status=ReviewStatus.APPLIED.value,
                    description="Already applied journal",
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
                ),
            )
        )
        session.commit()

        try:
            recommendation_task_module._supersede_existing_recommendation_state_for_document(
                db_session=session,
                close_run_id=close_run_id,
                document_id=document_id,
                replacement_recommendation_id=replacement_recommendation_id,
            )
        except recommendation_task_module.RecommendationRegenerationBlockedError:
            session.rollback()
        else:
            raise AssertionError("Expected applied-journal regeneration guard to raise.")

        refreshed_old_recommendation = session.get(Recommendation, old_recommendation_id)
        refreshed_journal = session.get(JournalEntry, journal_id)

    assert refreshed_old_recommendation is not None
    assert refreshed_old_recommendation.status == ReviewStatus.DRAFT.value
    assert refreshed_old_recommendation.superseded_by_id is None
    assert refreshed_journal is not None
    assert refreshed_journal.status == ReviewStatus.APPLIED.value
