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
from uuid import uuid4

from services.common.enums import DocumentSourceChannel, DocumentStatus
from services.db.base import Base
from services.db.models.documents import Document
from services.db.models.extractions import DocumentExtraction
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

from apps.worker.app.tasks.run_reconciliation import (
    _build_budget_counterparts,
    _build_fixed_asset_counterparts,
    _load_bank_statement_data,
    _read_statement_lines_from_payload,
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
