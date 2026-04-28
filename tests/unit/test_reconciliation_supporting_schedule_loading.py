# ruff: noqa: E402

"""
Purpose: Verify Step 6 supporting schedules feed reconciliation counterparts.
Scope: Focused unit coverage over the reconciliation worker helper functions
that translate standalone workpapers into matcher inputs.
Dependencies: run_reconciliation helper functions only.
"""

from __future__ import annotations

import sys
import types
from datetime import date
from decimal import Decimal
from uuid import uuid4

from services.common.enums import DocumentSourceChannel, DocumentStatus
from services.db.base import Base
from services.db.models.close_run import CloseRun
from services.db.models.documents import Document
from services.db.models.extractions import DocumentExtraction
from services.db.models.journals import JournalEntry, JournalLine
from services.db.models.ledger import (
    CloseRunLedgerBinding,
    GeneralLedgerImportBatch,
    GeneralLedgerImportLine,
    TrialBalanceImportBatch,
    TrialBalanceImportLine,
)
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker


class _DummyCeleryApp:
    """Provide the minimal Celery decorator surface needed for unit imports."""

    @staticmethod
    def task(*args, **kwargs):
        def _decorator(function):
            return function

        return _decorator


dummy_celery_module = types.SimpleNamespace(
    ObservedTask=object,
    celery_app=_DummyCeleryApp(),
)
sys.modules.setdefault("apps.worker.app.celery_app", dummy_celery_module)
sys.modules.setdefault(
    "apps.worker.app.tasks.base",
    types.SimpleNamespace(JobRuntimeContext=object, TrackedJobTask=object),
)

import apps.worker.app.tasks.run_reconciliation as run_reconciliation_task
from apps.worker.app.tasks.run_reconciliation import (
    _build_budget_counterparts,
    _build_fixed_asset_counterparts,
    _compute_account_balances,
    _load_bank_statement_data,
    _load_ledger_transactions,
    _read_statement_lines_from_payload,
    _resolve_statement_line_amount,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(
    _type_: JSONB,
    _compiler: object,
    **_compiler_kwargs: object,
) -> str:
    """Allow reconciliation helper tests to use in-memory SQLite."""

    return "JSON"


def test_fixed_asset_counterparts_use_asset_tags_and_account_codes() -> None:
    """Fixed-asset counterpart loading should respect asset tags and target accounts."""

    source_items = [
        {
            "asset_id": "FA-001",
            "asset_account_code": "1500",
            "accumulated_depreciation_account_code": "1510",
        }
    ]
    ledger_transactions = [
        {
            "account_code": "1500",
            "date": "2026-03-31",
            "dimensions": {"asset_id": "FA-001"},
            "reference": "",
            "signed_amount": "125000.00",
        },
        {
            "account_code": "1510",
            "date": "2026-03-31",
            "dimensions": {"asset_id": "FA-001"},
            "reference": "",
            "signed_amount": "-25000.00",
        },
        {
            "account_code": "1500",
            "date": "2026-03-31",
            "dimensions": {"asset_id": "FA-999"},
            "reference": "",
            "signed_amount": "999.00",
        },
    ]

    counterparts = _build_fixed_asset_counterparts(
        source_items=source_items,
        ledger_transactions=ledger_transactions,
    )

    assert counterparts == [
        {
            "asset_id": "FA-001",
            "ref": "ledger:asset:FA-001",
            "cost": "125000.00",
            "accumulated_depreciation": "25000.00",
        }
    ]


def test_budget_counterparts_filter_by_dimensions() -> None:
    """Budget actuals should only aggregate journal lines matching the budget dimensions."""

    source_items = [
        {
            "account_code": "6100",
            "period": "2026-03",
            "department": "Ops",
            "cost_centre": "HQ",
            "budget_amount": "20000.00",
        }
    ]
    ledger_transactions = [
        {
            "account_code": "6100",
            "period": "2026-03",
            "dimensions": {"department": "Ops", "cost_centre": "HQ"},
            "signed_amount": "7500.00",
        },
        {
            "account_code": "6100",
            "period": "2026-03",
            "dimensions": {"department": "Sales", "cost_centre": "HQ"},
            "signed_amount": "9900.00",
        },
    ]

    counterparts = _build_budget_counterparts(
        source_items=source_items,
        ledger_transactions=ledger_transactions,
    )

    assert counterparts == [
        {
            "ref": "ledger:budget:6100:2026-03:Ops:HQ",
            "account_code": "6100",
            "period": "2026-03",
            "department": "Ops",
            "cost_centre": "HQ",
            "amount": "7500.00",
        }
    ]


def test_load_bank_statement_data_uses_latest_extraction_only() -> None:
    """Reconciliation input should ignore superseded bank-statement extraction versions."""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    tables = [Document.__table__, DocumentExtraction.__table__]
    Base.metadata.create_all(engine, tables=tables)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as session:
        close_run_id = uuid4()
        document_id = uuid4()
        session.add(
            Document(
                id=document_id,
                close_run_id=close_run_id,
                parent_document_id=None,
                document_type="bank_statement",
                source_channel=DocumentSourceChannel.UPLOAD.value,
                storage_key="documents/bank-statement.pdf",
                original_filename="bank-statement.pdf",
                mime_type="application/pdf",
                file_size_bytes=1024,
                sha256_hash="a" * 64,
                period_start=None,
                period_end=None,
                classification_confidence=None,
                ocr_required=False,
                status=DocumentStatus.APPROVED.value,
                owner_user_id=None,
                last_touched_by_user_id=None,
            )
        )
        session.add_all(
            (
                DocumentExtraction(
                    id=uuid4(),
                    document_id=document_id,
                    version_no=1,
                    schema_name="bank_statement",
                    schema_version="1.0.0",
                    extracted_payload={
                        "parser_output": {
                            "statement_lines": [
                                {
                                    "line_no": 1,
                                    "date": "2026-03-01",
                                    "credit": "1000.00",
                                    "description": "superseded extraction",
                                }
                            ]
                        }
                    },
                    confidence_summary={},
                    needs_review=False,
                    approved_version=False,
                ),
                DocumentExtraction(
                    id=uuid4(),
                    document_id=document_id,
                    version_no=2,
                    schema_name="bank_statement",
                    schema_version="1.0.0",
                    extracted_payload={
                        "parser_output": {
                            "statement_lines": [
                                {
                                    "line_no": 1,
                                    "date": "2026-03-01",
                                    "credit": "2500.00",
                                    "description": "latest extraction",
                                }
                            ]
                        }
                    },
                    confidence_summary={},
                    needs_review=False,
                    approved_version=False,
                ),
            )
        )
        session.commit()

        bank_data = _load_bank_statement_data(session, close_run_id)

    assert bank_data["source_items"] == [
        {
            "ref": f"bank:{document_id}:1",
            "amount": "2500.00",
            "date": "2026-03-01",
            "reference": "",
            "description": "latest extraction",
        }
    ]


def test_read_statement_lines_supports_nested_parser_output_payloads() -> None:
    """Bank-line loading should read the normalized statement lines stored under parser_output."""

    lines = _read_statement_lines_from_payload(
        payload={
            "parser_output": {
                "statement_lines": [
                    {
                        "line_no": 1,
                        "date": "2026-03-01",
                        "credit": "1000.00",
                        "description": "Opening balance",
                    }
                ]
            }
        }
    )

    assert lines == (
        {
            "line_no": 1,
            "date": "2026-03-01",
            "credit": "1000.00",
            "description": "Opening balance",
        },
    )


def test_resolve_statement_line_amount_prefers_non_zero_credit_over_zero_debit() -> None:
    """Deposit lines should carry their credit amount into reconciliation matching."""

    assert (
        _resolve_statement_line_amount(
            {
                "debit": "0.00",
                "credit": "12000.00",
            }
        )
        == "12000.00"
    )


def test_load_ledger_transactions_includes_imported_baseline_and_close_run_journals() -> None:
    """Effective ledger loading should combine imported GL lines with current-run journals."""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    tables = [
        CloseRun.__table__,
        GeneralLedgerImportBatch.__table__,
        GeneralLedgerImportLine.__table__,
        CloseRunLedgerBinding.__table__,
        JournalEntry.__table__,
        JournalLine.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    session_factory = sessionmaker(bind=engine)

    close_run_id = uuid4()
    gl_batch_id = uuid4()
    entity_id = uuid4()
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
                dimensions={},
                external_ref=None,
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
                entity_id=session.get(CloseRun, close_run_id).entity_id,
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

        transactions = _load_ledger_transactions(session, close_run_id)

    assert [transaction["ref"] for transaction in transactions] == [
        f"gl:{gl_batch_id}:1",
        "je:JE-2026-00001:1",
        "je:JE-2026-00001:2",
    ]
    assert transactions[0]["signed_amount"] == "1200.00"
    assert transactions[2]["signed_amount"] == "-50.00"


def test_compute_account_balances_uses_trial_balance_import_plus_journal_adjustments(
    monkeypatch,
) -> None:
    """Trial balance should start from the imported TB baseline and then add run journals."""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    tables = [
        CloseRun.__table__,
        TrialBalanceImportBatch.__table__,
        TrialBalanceImportLine.__table__,
        CloseRunLedgerBinding.__table__,
        JournalEntry.__table__,
        JournalLine.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    session_factory = sessionmaker(bind=engine)

    close_run_id = uuid4()
    entity_id = uuid4()
    tb_batch_id = uuid4()
    monkeypatch.setattr(
        run_reconciliation_task,
        "_load_coa_accounts",
        lambda session, close_run_id: {
            "1000": {
                "account_code": "1000",
                "account_name": "Cash",
                "account_type": "asset",
                "is_postable": True,
            },
            "6100": {
                "account_code": "6100",
                "account_name": "Office Expense",
                "account_type": "expense",
                "is_postable": True,
            },
        },
    )
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
            TrialBalanceImportBatch(
                id=tb_batch_id,
                entity_id=entity_id,
                period_start=date(2026, 3, 1),
                period_end=date(2026, 3, 31),
                source_format="csv",
                uploaded_filename="march-tb.csv",
                row_count=2,
                imported_by_user_id=None,
                import_metadata={},
            )
        )
        session.add_all(
            (
                TrialBalanceImportLine(
                    id=uuid4(),
                    batch_id=tb_batch_id,
                    line_no=1,
                    account_code="1000",
                    account_name="Cash",
                    account_type="asset",
                    debit_balance="5000.00",
                    credit_balance="0.00",
                    is_active=True,
                ),
                TrialBalanceImportLine(
                    id=uuid4(),
                    batch_id=tb_batch_id,
                    line_no=2,
                    account_code="6100",
                    account_name="Office Expense",
                    account_type="expense",
                    debit_balance="0.00",
                    credit_balance="0.00",
                    is_active=True,
                ),
            )
        )
        session.add(
            CloseRunLedgerBinding(
                id=uuid4(),
                close_run_id=close_run_id,
                general_ledger_import_batch_id=None,
                trial_balance_import_batch_id=tb_batch_id,
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
                journal_number="JE-2026-00002",
                posting_date=date(2026, 3, 31),
                status="approved",
                description="Expense accrual",
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
                    account_code="1000",
                    line_type="credit",
                    amount="200.00",
                    description="Cash offset",
                    dimensions={},
                    reference="ACCR-001",
                ),
            )
        )
        session.commit()

        balances = _compute_account_balances(session, close_run_id)

    balances_by_code = {row["account_code"]: row for row in balances}
    assert balances_by_code["1000"]["debit_balance"] == Decimal("5000.00")
    assert balances_by_code["1000"]["credit_balance"] == Decimal("200.00")
    assert balances_by_code["6100"]["debit_balance"] == Decimal("200.00")


def test_run_reconciliation_task_raises_when_trial_balance_computation_fails(
    monkeypatch,
) -> None:
    """Worker execution should fail instead of silently completing when TB computation breaks."""

    class _DummySession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    class _DummyJobContext:
        def ensure_not_canceled(self) -> None:
            return None

        def checkpoint(self, *, step: str, state: dict | None = None) -> dict:
            return {"current_step": step, "state": state or {}}

    class _FakeService:
        def __init__(self, repository, matching_config=None) -> None:
            del repository, matching_config

        def reset_reconciliation_state(self, **kwargs) -> None:
            del kwargs
            return None

        def run_reconciliation(self, **kwargs):
            del kwargs
            return run_reconciliation_task.ReconciliationRunOutput(
                reconciliations=[],
                all_items=[],
                trial_balance=None,
                anomalies=[],
                total_items=0,
                matched_items=0,
                exception_items=0,
                unmatched_items=0,
            )

        def compute_trial_balance(self, **kwargs):
            del kwargs
            raise ValueError("snapshot write blew up")

    monkeypatch.setattr(
        run_reconciliation_task,
        "get_session_factory",
        lambda: (lambda: _DummySession()),
    )
    monkeypatch.setattr(
        run_reconciliation_task,
        "ensure_close_run_active_phase",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        run_reconciliation_task,
        "_build_reconciliation_source_data",
        lambda session, close_run_id, reconciliation_types: {
            run_reconciliation_task.ReconciliationType.TRIAL_BALANCE: {
                "source_items": [{"account_code": "1000"}],
                "counterparts": [],
            }
        },
    )
    monkeypatch.setattr(
        run_reconciliation_task,
        "filter_runnable_reconciliation_types",
        lambda **kwargs: ((run_reconciliation_task.ReconciliationType.TRIAL_BALANCE,), ()),
    )
    monkeypatch.setattr(
        run_reconciliation_task,
        "_compute_account_balances",
        lambda session, close_run_id: [
            {
                "account_code": "1000",
                "account_name": "Cash",
                "account_type": "asset",
                "debit_balance": Decimal("100.00"),
                "credit_balance": Decimal("0.00"),
                "is_active": True,
            }
        ],
    )
    monkeypatch.setattr(
        run_reconciliation_task,
        "_load_coa_accounts",
        lambda session, close_run_id: {"1000": {"account_code": "1000"}},
    )
    monkeypatch.setattr(
        run_reconciliation_task,
        "ReconciliationRepository",
        lambda session: object(),
    )
    monkeypatch.setattr(run_reconciliation_task, "ReconciliationService", _FakeService)

    try:
        run_reconciliation_task._run_reconciliation_task(
            close_run_id=str(uuid4()),
            reconciliation_types=[run_reconciliation_task.ReconciliationType.TRIAL_BALANCE.value],
            actor_user_id=None,
            matching_config=None,
            job_context=_DummyJobContext(),
        )
    except RuntimeError as exc:
        assert "Trial balance computation failed" in str(exc)
    else:
        raise AssertionError("trial-balance failures must surface as explicit job errors")


def test_run_reconciliation_task_resets_prior_state_before_rerun(
    monkeypatch,
) -> None:
    """Canonical reruns should replace prior reconciliation artifacts before rebuilding them."""

    class _DummySession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    class _DummyJobContext:
        def __init__(self) -> None:
            self.checkpoints: list[str] = []

        def ensure_not_canceled(self) -> None:
            return None

        def checkpoint(self, *, step: str, state: dict | None = None) -> dict:
            del state
            self.checkpoints.append(step)
            return {"current_step": step}

    call_log: list[tuple[str, tuple[str, ...], bool]] = []

    class _FakeService:
        def __init__(self, repository, matching_config=None) -> None:
            del repository, matching_config

        def reset_reconciliation_state(
            self,
            *,
            close_run_id,
            reconciliation_types,
            clear_trial_balance,
        ) -> None:
            del close_run_id
            call_log.append(
                (
                    "reset",
                    tuple(
                        reconciliation_type.value
                        for reconciliation_type in reconciliation_types
                    ),
                    clear_trial_balance,
                )
            )

        def run_reconciliation(self, **kwargs):
            call_log.append(
                (
                    "run",
                    tuple(
                        reconciliation_type.value
                        for reconciliation_type in kwargs["reconciliation_types"]
                    ),
                    False,
                )
            )
            return run_reconciliation_task.ReconciliationRunOutput(
                reconciliations=[],
                all_items=[],
                trial_balance=None,
                anomalies=[],
                total_items=0,
                matched_items=0,
                exception_items=0,
                unmatched_items=0,
            )

        def compute_trial_balance(self, **kwargs):
            del kwargs
            call_log.append(("trial_balance", ("trial_balance",), True))
            return types.SimpleNamespace(
                is_balanced=True,
                total_debits=Decimal("100.00"),
                total_credits=Decimal("100.00"),
            )

    monkeypatch.setattr(
        run_reconciliation_task,
        "get_session_factory",
        lambda: (lambda: _DummySession()),
    )
    monkeypatch.setattr(
        run_reconciliation_task,
        "ensure_close_run_active_phase",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        run_reconciliation_task,
        "_build_reconciliation_source_data",
        lambda session, close_run_id, reconciliation_types: {
            run_reconciliation_task.ReconciliationType.BANK_RECONCILIATION: {
                "source_items": [{"ref": "bank:1", "amount": "100.00"}],
                "counterparts": [{"ref": "ledger:1", "amount": "100.00"}],
            },
            run_reconciliation_task.ReconciliationType.TRIAL_BALANCE: {
                "source_items": [{"account_code": "1000"}],
                "counterparts": [],
            },
        },
    )
    monkeypatch.setattr(
        run_reconciliation_task,
        "filter_runnable_reconciliation_types",
        lambda **kwargs: (
            (
                run_reconciliation_task.ReconciliationType.BANK_RECONCILIATION,
                run_reconciliation_task.ReconciliationType.TRIAL_BALANCE,
            ),
            (),
        ),
    )
    monkeypatch.setattr(
        run_reconciliation_task,
        "_compute_account_balances",
        lambda session, close_run_id: [
            {
                "account_code": "1000",
                "account_name": "Cash",
                "account_type": "asset",
                "debit_balance": Decimal("100.00"),
                "credit_balance": Decimal("0.00"),
                "is_active": True,
            }
        ],
    )
    monkeypatch.setattr(
        run_reconciliation_task,
        "ReconciliationRepository",
        lambda session: object(),
    )
    monkeypatch.setattr(run_reconciliation_task, "ReconciliationService", _FakeService)

    job_context = _DummyJobContext()
    run_reconciliation_task._run_reconciliation_task(
        close_run_id=str(uuid4()),
        reconciliation_types=[
            run_reconciliation_task.ReconciliationType.BANK_RECONCILIATION.value,
            run_reconciliation_task.ReconciliationType.TRIAL_BALANCE.value,
        ],
        actor_user_id=None,
        matching_config=None,
        job_context=job_context,
    )

    assert call_log[0] == (
        "reset",
        ("bank_reconciliation", "trial_balance"),
        True,
    )
    assert call_log[1] == ("run", ("bank_reconciliation",), False)
    assert call_log[2] == ("trial_balance", ("trial_balance",), True)
    assert "reset_reconciliation_state" in job_context.checkpoints
