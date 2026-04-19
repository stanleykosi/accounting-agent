"""
Purpose: Verify typed extraction coercion for parser outputs that resemble real documents.
Scope: Focused regression coverage for decimal parsing used by extraction persistence.
Dependencies: field extractors and canonical document enums only.
"""

from __future__ import annotations

from decimal import Decimal

from services.common.enums import DocumentType
from services.extraction.field_extractors import (
    compute_confidence_summary,
    extract_fields_by_document_type,
)


def test_bank_statement_fields_accept_comma_formatted_summary_amounts() -> None:
    """Comma-formatted statement summary values should parse into Decimal fields."""

    fields = extract_fields_by_document_type(
        DocumentType.BANK_STATEMENT,
        {
            "source_type": "parser",
            "fields": {
                "opening_balance": "68,500.00",
                "closing_balance": "71,105.00",
                "total_credits": "26,600.00",
                "total_debits": "23,995.00",
            },
            "field_locations": {},
        },
    )

    field_values = {field.field_name: field.field_value for field in fields}

    assert field_values["opening_balance"] == Decimal("68500.00")
    assert field_values["closing_balance"] == Decimal("71105.00")
    assert field_values["total_credits"] == Decimal("26600.00")
    assert field_values["total_debits"] == Decimal("23995.00")


def test_contract_missing_narrative_clauses_do_not_force_review() -> None:
    """Missing long-form contract clauses should not create a fake low-confidence review state."""

    fields = extract_fields_by_document_type(
        DocumentType.CONTRACT,
        {
            "source_type": "parser",
            "fields": {
                "contract_number": "CTR-ERP-2026-021",
                "contract_date": "2026-02-15",
                "effective_date": "2026-03-01",
                "expiration_date": "2027-02-28",
                "party_a_name": "Transfa Logistics Limited",
                "party_b_name": "CloudCore Technology Services Limited",
                "contract_value": "51360000.00",
                "currency": "NGN",
                "contract_type": "ERP and Warehouse Systems Subscription",
                "terms": None,
                "renewal_terms": None,
                "termination_terms": None,
            },
            "field_locations": {},
        },
    )

    confidence_summary = compute_confidence_summary(fields)

    assert confidence_summary.low_confidence_fields == 0
