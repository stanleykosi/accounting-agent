"""
Purpose: Verify parser payload normalization before inline extraction persistence.
Scope: Focused unit coverage over the helper that maps raw parser output into the
field and line-item shape consumed by extraction, matching, and recommendations.
Dependencies: parse_documents helpers and canonical document enums only.
"""

from __future__ import annotations

from apps.worker.app.tasks.parse_documents import _build_extraction_parser_output
from services.common.enums import DocumentType


def test_invoice_parser_output_normalizes_fields_and_line_items() -> None:
    """Invoice tables should yield extractor-friendly fields plus line items."""

    parser_output = _build_extraction_parser_output(
        raw_parse_payload={
            "text": "Invoice INV-001 dated 2026-03-14 for Acme Supplies",
            "tables": [
                {
                    "name": "Invoices",
                    "rows": [
                        {
                            "Vendor": "Acme Supplies",
                            "Reference": "INV-001",
                            "Date": "2026-03-14",
                            "Amount": "1500.00",
                            "Description": "Office chairs",
                            "Quantity": "2",
                        }
                    ],
                }
            ],
        },
        document_type=DocumentType.INVOICE,
    )

    assert parser_output["source_type"] == "parser"
    assert parser_output["fields"]["vendor_name"] == "Acme Supplies"
    assert parser_output["fields"]["invoice_number"] == "INV-001"
    assert parser_output["fields"]["invoice_date"] == "2026-03-14"
    assert parser_output["fields"]["total"] == "1500.00"
    assert parser_output["line_items"] == [
        {
            "line_no": 1,
            "evidence_ref": {},
            "dimensions": {},
            "description": "Office chairs",
            "quantity": "2",
            "amount": "1500.00",
        }
    ]


def test_bank_statement_parser_output_normalizes_nested_statement_lines() -> None:
    """Bank-statement tables should yield statement lines and derived date totals."""

    parser_output = _build_extraction_parser_output(
        raw_parse_payload={
            "text": "Bank statement\nBank Name: Demo Bank\nAccount Number: 1234567890",
            "tables": [
                {
                    "name": "Statement",
                    "rows": [
                        {
                            "Date": "2026-03-01",
                            "Description": "Opening balance",
                            "Credit": "1000.00",
                            "Balance": "1000.00",
                        },
                        {
                            "Date": "2026-03-02",
                            "Description": "Vendor payment",
                            "Reference": "INV-001",
                            "Debit": "250.00",
                            "Balance": "750.00",
                        },
                    ],
                }
            ],
        },
        document_type=DocumentType.BANK_STATEMENT,
    )

    assert parser_output["fields"]["bank_name"] == "Demo Bank"
    assert parser_output["fields"]["account_number"] == "1234567890"
    assert parser_output["fields"]["statement_start_date"] == "2026-03-01"
    assert parser_output["fields"]["statement_end_date"] == "2026-03-02"
    assert parser_output["fields"]["total_credits"] == "1000.00"
    assert parser_output["fields"]["total_debits"] == "250.00"
    assert parser_output["statement_lines"] == [
        {
            "line_no": 1,
            "evidence_ref": {},
            "date": "2026-03-01",
            "description": "Opening balance",
            "credit": "1000.00",
            "balance": "1000.00",
        },
        {
            "line_no": 2,
            "evidence_ref": {},
            "date": "2026-03-02",
            "description": "Vendor payment",
            "reference": "INV-001",
            "debit": "250.00",
            "balance": "750.00",
        },
    ]


def test_bank_statement_parser_output_normalizes_pdf_delimited_statement_lines() -> None:
    """PDF-delimited statement tables should map their header row into canonical line keys."""

    parser_output = _build_extraction_parser_output(
        raw_parse_payload={
            "text": (
                "Bank Statement\n"
                "Bank Name: Demo Bank\n"
                "Account Name: Operating Account\n"
                "Account Number: 1234567890\n"
                "Statement Start Date: 2026-03-01\n"
                "Statement End Date: 2026-03-31\n"
                "Opening Balance: 5000.00\n"
                "Closing Balance: 5750.00\n"
                "Credits Total: 1000.00\n"
                "Debits Total: 250.00\n"
            ),
            "tables": [
                {
                    "name": "pdf_delimited_text",
                    "rows": [
                        {
                            "source_line_number": "5",
                            "column_1": "Date",
                            "column_2": "Description",
                            "column_3": "Reference",
                            "column_4": "Debit",
                            "column_5": "Credit",
                            "column_6": "Balance",
                        },
                        {
                            "source_line_number": "6",
                            "column_1": "2026-03-01",
                            "column_2": "Opening balance",
                            "column_3": "",
                            "column_4": "0.00",
                            "column_5": "1000.00",
                            "column_6": "1000.00",
                        },
                        {
                            "source_line_number": "7",
                            "column_1": "2026-03-02",
                            "column_2": "Vendor payment",
                            "column_3": "INV-001",
                            "column_4": "250.00",
                            "column_5": "0.00",
                            "column_6": "750.00",
                        },
                    ],
                }
            ],
        },
        document_type=DocumentType.BANK_STATEMENT,
    )

    assert parser_output["fields"]["bank_name"] == "Demo Bank"
    assert parser_output["fields"]["account_name"] == "Operating Account"
    assert parser_output["fields"]["account_number"] == "1234567890"
    assert parser_output["fields"]["statement_start_date"] == "2026-03-01"
    assert parser_output["fields"]["statement_end_date"] == "2026-03-31"
    assert parser_output["fields"]["opening_balance"] == "5000.00"
    assert parser_output["fields"]["closing_balance"] == "5750.00"
    assert parser_output["fields"]["total_credits"] == "1000.00"
    assert parser_output["fields"]["total_debits"] == "250.00"
    assert parser_output["statement_lines"] == [
        {
            "line_no": 1,
            "evidence_ref": {},
            "date": "2026-03-01",
            "description": "Opening balance",
            "debit": "0.00",
            "credit": "1000.00",
            "balance": "1000.00",
        },
        {
            "line_no": 2,
            "evidence_ref": {},
            "date": "2026-03-02",
            "description": "Vendor payment",
            "reference": "INV-001",
            "debit": "250.00",
            "credit": "0.00",
            "balance": "750.00",
        },
    ]


def test_bank_statement_parser_output_preserves_blank_pdf_header_columns() -> None:
    """Blank spacer columns in PDF headers should not shift later statement fields."""

    parser_output = _build_extraction_parser_output(
        raw_parse_payload={
            "text": "Bank Statement\nBank Name: Demo Bank\nAccount Number: 1234567890",
            "tables": [
                {
                    "name": "pdf_delimited_text",
                    "rows": [
                        {
                            "source_line_number": "5",
                            "column_1": "Date",
                            "column_2": "Description",
                            "column_3": "",
                            "column_4": "Debit",
                            "column_5": "Credit",
                            "column_6": "Balance",
                        },
                        {
                            "source_line_number": "6",
                            "column_1": "2026-03-02",
                            "column_2": "Vendor payment",
                            "column_3": "INV-001",
                            "column_4": "250.00",
                            "column_5": "0.00",
                            "column_6": "750.00",
                        },
                    ],
                }
            ],
        },
        document_type=DocumentType.BANK_STATEMENT,
    )

    assert parser_output["statement_lines"] == [
        {
            "line_no": 1,
            "evidence_ref": {},
            "date": "2026-03-02",
            "description": "Vendor payment",
            "debit": "250.00",
            "credit": "0.00",
            "balance": "750.00",
        }
    ]
