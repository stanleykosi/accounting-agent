"""
Purpose: Verify deterministic document-to-transaction auto-linking.
Scope: Exact match, no-match, and not-applicable transaction-linking outcomes plus payload helpers.
Dependencies: Canonical document enums and the transaction-matching service helpers.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from services.common.enums import DocumentType
from services.documents.transaction_matching import (
    AutoTransactionMatchStatus,
    StatementLineCandidate,
    _deduplicate_statement_candidates,
    evaluate_auto_transaction_match,
    extract_auto_review_metadata,
    extract_auto_transaction_match_metadata,
    update_extraction_auto_review_payload,
    update_extraction_transaction_match_payload,
)


def test_invoice_auto_transaction_match_prefers_exact_amount_date_and_reference() -> None:
    """Invoices should match the bank-statement line with the strongest deterministic signals."""

    result = evaluate_auto_transaction_match(
        document_type=DocumentType.INVOICE,
        original_filename="vendor-invoice-001.pdf",
        field_values={
            "invoice_number": "INV-001",
            "invoice_date": "2026-03-14",
            "total": "1500.00",
            "vendor_name": "Acme Supplies",
        },
        statement_candidates=(
            StatementLineCandidate(
                account_number=None,
                document_id=uuid4(),
                original_filename="march-bank-statement.pdf",
                line_no=3,
                amount=Decimal("1500.00"),
                date=date(2026, 3, 14),
                reference="INV-001",
                description="ACME SUPPLIES PAYMENT",
            ),
            StatementLineCandidate(
                account_number=None,
                document_id=uuid4(),
                original_filename="march-bank-statement.pdf",
                line_no=7,
                amount=Decimal("1499.99"),
                date=date(2026, 3, 18),
                reference="OTHER",
                description="OTHER PAYMENT",
            ),
        ),
    )

    assert result.status is AutoTransactionMatchStatus.MATCHED
    assert result.matched_line_no == 3
    assert result.matched_document_filename == "march-bank-statement.pdf"
    assert result.score is not None and result.score >= 0.72


def test_receipt_auto_transaction_match_waits_for_bank_evidence_when_no_lines_exist() -> None:
    """Receipts should wait for bank evidence instead of becoming blocked immediately."""

    result = evaluate_auto_transaction_match(
        document_type=DocumentType.RECEIPT,
        original_filename="receipt-001.pdf",
        field_values={
            "receipt_number": "RCT-001",
            "receipt_date": "2026-03-10",
            "total": "250.00",
        },
        statement_candidates=(),
    )

    assert result.status is AutoTransactionMatchStatus.PENDING_EVIDENCE
    assert "Bank-statement evidence has not been uploaded yet" in result.primary_reason


def test_bank_statement_auto_transaction_match_is_not_applicable() -> None:
    """Bank-statement uploads should not require a second transaction-linking pass."""

    result = evaluate_auto_transaction_match(
        document_type=DocumentType.BANK_STATEMENT,
        original_filename="bank-statement.pdf",
        field_values={},
        statement_candidates=(),
    )

    assert result.status is AutoTransactionMatchStatus.NOT_APPLICABLE


def test_mirrored_bank_statement_uploads_are_deduplicated_before_matching() -> None:
    """PDF/XLSX twins of the same statement line should not create fake ambiguity."""

    candidates = _deduplicate_statement_candidates(
        [
            StatementLineCandidate(
                account_number="3000149827",
                document_id=uuid4(),
                original_filename="bank-statement-operating-account-2026-03.pdf",
                line_no=6,
                amount=Decimal("5940000.00"),
                date=date(2026, 3, 11),
                reference="SGS-1103",
                description="SIGNAL GUARD SERVICES SGS-1103",
            ),
            StatementLineCandidate(
                account_number="3000149827",
                document_id=uuid4(),
                original_filename="bank-statement-operating-account-2026-03.xlsx",
                line_no=6,
                amount=Decimal("5940000.00"),
                date=date(2026, 3, 11),
                reference="SGS-1103",
                description="SIGNAL GUARD SERVICES SGS-1103",
            ),
        ]
    )

    result = evaluate_auto_transaction_match(
        document_type=DocumentType.INVOICE,
        original_filename="invoice-signal-security-services-2026-03.pdf",
        field_values={
            "invoice_number": "SGS-1103",
            "invoice_date": "2026-03-10",
            "total": "5940000.00",
            "vendor_name": "Signal Guard Services Limited",
        },
        statement_candidates=candidates,
    )

    assert len(candidates) == 1
    assert result.status is AutoTransactionMatchStatus.MATCHED
    assert result.matched_document_filename == "bank-statement-operating-account-2026-03.pdf"


def test_transaction_matching_payload_helpers_round_trip_metadata() -> None:
    """Persisted extraction metadata should preserve match and auto-review state cleanly."""

    match_result = evaluate_auto_transaction_match(
        document_type=DocumentType.PAYSLIP,
        original_filename="payslip.pdf",
        field_values={
            "employee_id": "EMP-01",
            "employee_name": "Jane Doe",
            "net_pay": "5000.00",
            "pay_date": "2026-03-31",
        },
        statement_candidates=(
            StatementLineCandidate(
                account_number=None,
                document_id=uuid4(),
                original_filename="march-bank-statement.pdf",
                line_no=12,
                amount=Decimal("5000.00"),
                date=date(2026, 3, 31),
                reference="EMP-01",
                description="JANE DOE SALARY",
            ),
        ),
    )
    payload = update_extraction_transaction_match_payload(
        extracted_payload={},
        match_result=match_result,
    )
    payload = update_extraction_auto_review_payload(
        extracted_payload=payload,
        auto_approved=True,
        autonomy_mode="reduced_interruption",
        reasons=("All deterministic collection checks passed.",),
    )

    assert extract_auto_transaction_match_metadata(payload) is not None
    auto_review_metadata = extract_auto_review_metadata(payload)
    assert auto_review_metadata == {
        "auto_approved": True,
        "autonomy_mode": "reduced_interruption",
        "reasons": ["All deterministic collection checks passed."],
        "evaluated_at": auto_review_metadata["evaluated_at"],
    }
