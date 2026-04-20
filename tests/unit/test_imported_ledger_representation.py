"""
Purpose: Verify conservative duplicate-post suppression against imported general-ledger baselines.
Scope: Grounded unit coverage for document-to-imported-GL representation checks only.
Dependencies: SQLAlchemy models, imported-ledger representation helpers, and in-memory SQLite.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from services.common.enums import DocumentSourceChannel, DocumentStatus, DocumentType
from services.db.base import Base
from services.db.models.close_run import CloseRun
from services.db.models.documents import Document
from services.db.models.extractions import DocumentExtraction
from services.db.models.ledger import (
    CloseRunLedgerBinding,
    GeneralLedgerImportBatch,
    GeneralLedgerImportLine,
)
from services.documents.imported_ledger_representation import (
    evaluate_document_imported_gl_representation,
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
    """Allow imported-ledger representation helpers to run against in-memory SQLite."""

    return "JSON"


def _build_document(
    *,
    close_run_id,
    document_id,
    document_type: DocumentType,
) -> Document:
    return Document(
        id=document_id,
        close_run_id=close_run_id,
        parent_document_id=None,
        document_type=document_type.value,
        source_channel=DocumentSourceChannel.UPLOAD.value,
        storage_key=f"documents/{document_id}",
        original_filename=f"{document_type.value}.pdf",
        mime_type="application/pdf",
        file_size_bytes=2048,
        sha256_hash="a" * 64,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
        classification_confidence=0.96,
        ocr_required=False,
        status=DocumentStatus.APPROVED.value,
        owner_user_id=None,
        last_touched_by_user_id=None,
    )


def _build_extraction(
    *,
    document_id,
    field_values: dict[str, object],
) -> DocumentExtraction:
    return DocumentExtraction(
        document_id=document_id,
        version_no=1,
        schema_name="document",
        schema_version="1.0.0",
        extracted_payload={
            "fields": [
                {
                    "field_name": field_name,
                    "field_value": field_value,
                }
                for field_name, field_value in field_values.items()
            ]
        },
        confidence_summary={"overall": 0.96},
        needs_review=False,
        approved_version=True,
    )


def _create_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            CloseRun.__table__,
            Document.__table__,
            DocumentExtraction.__table__,
            GeneralLedgerImportBatch.__table__,
            GeneralLedgerImportLine.__table__,
            CloseRunLedgerBinding.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def test_invoice_is_suppressed_when_imported_gl_has_same_amount_and_reference() -> None:
    """Invoices already present in the imported GL should not queue fresh coding work."""

    session = _create_session()
    close_run_id = uuid4()
    entity_id = uuid4()
    document_id = uuid4()
    batch_id = uuid4()

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
        _build_document(
            close_run_id=close_run_id,
            document_id=document_id,
            document_type=DocumentType.INVOICE,
        )
    )
    session.add(
        _build_extraction(
            document_id=document_id,
            field_values={
                "invoice_number": "INV-1048",
                "invoice_date": "2026-03-15",
                "vendor_name": "Acme Office Interiors LLC",
                "total": "2450.00",
            },
        )
    )
    session.add(
        GeneralLedgerImportBatch(
            id=batch_id,
            entity_id=entity_id,
            period_start=date(2026, 3, 1),
            period_end=date(2026, 3, 31),
            source_format="csv",
            uploaded_filename="gl.csv",
            row_count=1,
            imported_by_user_id=None,
            import_metadata={},
        )
    )
    session.add(
        GeneralLedgerImportLine(
            batch_id=batch_id,
            line_no=1,
            posting_date=date(2026, 3, 16),
            account_code="6100",
            account_name="Office Expense",
            reference="INV-1048",
            description="Acme Office Interiors fitout",
            debit_amount="2450.00",
            credit_amount="0.00",
            dimensions={},
            external_ref=None,
            transaction_group_key="glgrp_invoice_match",
        )
    )
    session.add(
        CloseRunLedgerBinding(
            close_run_id=close_run_id,
            general_ledger_import_batch_id=batch_id,
            trial_balance_import_batch_id=None,
            binding_source="auto",
            bound_by_user_id=None,
        )
    )
    session.commit()

    result = evaluate_document_imported_gl_representation(
        session=session,
        close_run_id=close_run_id,
        document_id=document_id,
    )

    assert result.represented_in_imported_gl is True
    assert result.status == "represented_in_imported_gl"
    assert result.matched_line_no == 1
    assert result.matched_reference == "INV-1048"


def test_amount_only_match_does_not_suppress_document() -> None:
    """Shared amounts alone should not be enough to suppress new accounting work."""

    session = _create_session()
    close_run_id = uuid4()
    entity_id = uuid4()
    document_id = uuid4()
    batch_id = uuid4()

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
        _build_document(
            close_run_id=close_run_id,
            document_id=document_id,
            document_type=DocumentType.INVOICE,
        )
    )
    session.add(
        _build_extraction(
            document_id=document_id,
            field_values={
                "invoice_number": "INV-2048",
                "invoice_date": "2026-03-15",
                "vendor_name": "Northwind Furnishings",
                "total": "2450.00",
            },
        )
    )
    session.add(
        GeneralLedgerImportBatch(
            id=batch_id,
            entity_id=entity_id,
            period_start=date(2026, 3, 1),
            period_end=date(2026, 3, 31),
            source_format="csv",
            uploaded_filename="gl.csv",
            row_count=1,
            imported_by_user_id=None,
            import_metadata={},
        )
    )
    session.add(
        GeneralLedgerImportLine(
            batch_id=batch_id,
            line_no=1,
            posting_date=date(2026, 1, 10),
            account_code="6100",
            account_name="Office Expense",
            reference="TXN-0001",
            description="Monthly office allocation",
            debit_amount="2450.00",
            credit_amount="0.00",
            dimensions={},
            external_ref=None,
            transaction_group_key="glgrp_allocation",
        )
    )
    session.add(
        CloseRunLedgerBinding(
            close_run_id=close_run_id,
            general_ledger_import_batch_id=batch_id,
            trial_balance_import_batch_id=None,
            binding_source="auto",
            bound_by_user_id=None,
        )
    )
    session.commit()

    result = evaluate_document_imported_gl_representation(
        session=session,
        close_run_id=close_run_id,
        document_id=document_id,
    )

    assert result.represented_in_imported_gl is False
    assert result.status == "not_represented_in_imported_gl"


def test_payslips_are_suppressed_when_imported_gl_contains_payroll_batch() -> None:
    """Approved payslips should suppress against an imported payroll batch total."""

    session = _create_session()
    close_run_id = uuid4()
    entity_id = uuid4()
    batch_id = uuid4()
    payslip_a_id = uuid4()
    payslip_b_id = uuid4()

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
            _build_document(
                close_run_id=close_run_id,
                document_id=payslip_a_id,
                document_type=DocumentType.PAYSLIP,
            ),
            _build_document(
                close_run_id=close_run_id,
                document_id=payslip_b_id,
                document_type=DocumentType.PAYSLIP,
            ),
            _build_extraction(
                document_id=payslip_a_id,
                field_values={
                    "employee_name": "Adaobi Nwosu",
                    "employee_id": "EMP-1001",
                    "pay_date": "2026-03-25",
                    "net_pay": "650000.00",
                    "gross_pay": "820000.00",
                },
            ),
            _build_extraction(
                document_id=payslip_b_id,
                field_values={
                    "employee_name": "Tunde Afolayan",
                    "employee_id": "EMP-1002",
                    "pay_date": "2026-03-25",
                    "net_pay": "850000.00",
                    "gross_pay": "1040000.00",
                },
            ),
            GeneralLedgerImportBatch(
                id=batch_id,
                entity_id=entity_id,
                period_start=date(2026, 3, 1),
                period_end=date(2026, 3, 31),
                source_format="csv",
                uploaded_filename="gl.csv",
                row_count=1,
                imported_by_user_id=None,
                import_metadata={},
            ),
            GeneralLedgerImportLine(
                batch_id=batch_id,
                line_no=1,
                posting_date=date(2026, 3, 25),
                account_code="6010",
                account_name="Salaries and Wages",
                reference="SAL-BATCH-0325",
                description="Salary batch payment for March payroll",
                debit_amount="1500000.00",
                credit_amount="0.00",
                dimensions={},
                external_ref=None,
                transaction_group_key="glgrp_payroll_batch",
            ),
            CloseRunLedgerBinding(
                close_run_id=close_run_id,
                general_ledger_import_batch_id=batch_id,
                trial_balance_import_batch_id=None,
                binding_source="auto",
                bound_by_user_id=None,
            ),
        )
    )
    session.commit()

    result = evaluate_document_imported_gl_representation(
        session=session,
        close_run_id=close_run_id,
        document_id=payslip_a_id,
    )

    assert result.represented_in_imported_gl is True
    assert result.status == "represented_in_imported_gl"
    assert result.matched_reference == "SAL-BATCH-0325"


def test_bank_statement_is_not_eligible_for_imported_gl_duplicate_suppression() -> None:
    """Bank statements remain evidence documents even when an imported GL exists."""

    session = _create_session()
    close_run_id = uuid4()
    entity_id = uuid4()
    document_id = uuid4()

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
        _build_document(
            close_run_id=close_run_id,
            document_id=document_id,
            document_type=DocumentType.BANK_STATEMENT,
        )
    )
    session.commit()

    result = evaluate_document_imported_gl_representation(
        session=session,
        close_run_id=close_run_id,
        document_id=document_id,
    )

    assert result.represented_in_imported_gl is False
    assert result.status == "not_applicable"


def test_missing_imported_gl_binding_reports_explicit_status() -> None:
    """The matcher should fail fast when the close run has no imported GL baseline."""

    session = _create_session()
    close_run_id = uuid4()
    entity_id = uuid4()
    document_id = uuid4()

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
        _build_document(
            close_run_id=close_run_id,
            document_id=document_id,
            document_type=DocumentType.RECEIPT,
        )
    )
    session.add(
        _build_extraction(
            document_id=document_id,
            field_values={
                "receipt_number": "RCT-009",
                "receipt_date": "2026-03-12",
                "vendor_name": "Stationers Hub",
                "total": "80.00",
            },
        )
    )
    session.commit()

    result = evaluate_document_imported_gl_representation(
        session=session,
        close_run_id=close_run_id,
        document_id=document_id,
    )

    assert result.represented_in_imported_gl is False
    assert result.status == "no_imported_general_ledger"
