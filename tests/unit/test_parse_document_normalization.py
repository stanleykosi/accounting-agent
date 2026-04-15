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
