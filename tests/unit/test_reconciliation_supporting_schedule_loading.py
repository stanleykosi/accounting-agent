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
                        "statement_lines": [
                            {
                                "line_no": 1,
                                "date": "2026-03-01",
                                "credit": "1000.00",
                                "description": "superseded extraction",
                            }
                        ]
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
                        "statement_lines": [
                            {
                                "line_no": 1,
                                "date": "2026-03-01",
                                "credit": "2500.00",
                                "description": "latest extraction",
                            }
                        ]
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
