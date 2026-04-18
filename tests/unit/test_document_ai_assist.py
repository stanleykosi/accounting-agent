"""
Purpose: Verify the bounded LLM-assisted document parsing helper.
Scope: Invocation gating and structured response handling for parser-side model assists.
Dependencies: Document AI assist service, strict output contracts, and lightweight fakes only.
"""

from __future__ import annotations

from types import SimpleNamespace

from services.common.enums import DocumentType
from services.contracts.document_ai_models import (
    DocumentFieldAssistCandidate,
    DocumentParseAssistOutput,
)
from services.documents import ai_assist


def test_should_invoke_document_parse_assist_skips_when_model_gateway_is_unconfigured(
    monkeypatch,
) -> None:
    """The assist pass should not run when no model API key is configured."""

    monkeypatch.setattr(
        ai_assist,
        "get_settings",
        lambda: SimpleNamespace(model_gateway=SimpleNamespace(api_key=None)),
    )

    should_invoke = ai_assist.should_invoke_document_parse_assist(
        raw_parse_payload={"text": "Document Type: Invoice", "pages": [{"text": "Invoice"}]},
        document_type=DocumentType.INVOICE,
        classification_confidence=0.96,
    )

    assert should_invoke is False


def test_run_document_parse_assist_returns_validated_output(monkeypatch) -> None:
    """The assist helper should return the schema-validated gateway response."""

    monkeypatch.setattr(
        ai_assist,
        "get_settings",
        lambda: SimpleNamespace(model_gateway=SimpleNamespace(api_key="test-key")),
    )

    expected_output = DocumentParseAssistOutput(
        predicted_type=DocumentType.BANK_STATEMENT,
        classification_confidence=0.95,
        classification_reasoning="The document explicitly states it is a bank statement.",
        field_candidates=(
            DocumentFieldAssistCandidate(
                field_name="statement_start_date",
                value="2026-03-01",
                confidence=0.94,
                evidence_quote="Statement Start Date: 2026-03-01",
            ),
            DocumentFieldAssistCandidate(
                field_name="statement_end_date",
                value="2026-03-31",
                confidence=0.94,
                evidence_quote="Statement End Date: 2026-03-31",
            ),
        ),
    )

    class FakeGateway:
        def complete_structured(self, *, messages, response_model):  # type: ignore[no-untyped-def]
            assert len(messages) == 2
            assert response_model is DocumentParseAssistOutput
            return expected_output

    monkeypatch.setattr(ai_assist, "ModelGateway", FakeGateway)

    result = ai_assist.run_document_parse_assist(
        filename="bank-statement.pdf",
        raw_parse_payload={
            "text": (
                "Document Type: Bank Statement\n"
                "Statement Start Date: 2026-03-01\n"
                "Statement End Date: 2026-03-31\n"
            ),
            "pages": [{"text": "Document Type: Bank Statement"}],
        },
        deterministic_document_type=DocumentType.BANK_STATEMENT,
        deterministic_classification_confidence=0.65,
        close_run_period_start="2026-03-01",
        close_run_period_end="2026-03-31",
        current_field_hints={"statement_start_date": "2026-03-01"},
    )

    assert result == expected_output


def test_should_invoke_document_parse_assist_skips_high_confidence_non_ocr_page_documents(
    monkeypatch,
) -> None:
    """High-confidence page-based parses should stay on the deterministic path."""

    monkeypatch.setattr(
        ai_assist,
        "get_settings",
        lambda: SimpleNamespace(model_gateway=SimpleNamespace(api_key="test-key")),
    )

    should_invoke = ai_assist.should_invoke_document_parse_assist(
        raw_parse_payload={
            "text": "Invoice Number: INV-1048\nTotal: 2450.00",
            "pages": [{"text": "Invoice Number: INV-1048", "extraction_method": "pdf_text"}],
            "metadata": {"requires_ocr": False},
        },
        document_type=DocumentType.INVOICE,
        classification_confidence=0.96,
    )

    assert should_invoke is False


def test_should_invoke_document_parse_assist_force_overrides_high_confidence_gate(
    monkeypatch,
) -> None:
    """Forced retries should bypass the confidence gate when the model gateway is configured."""

    monkeypatch.setattr(
        ai_assist,
        "get_settings",
        lambda: SimpleNamespace(model_gateway=SimpleNamespace(api_key="test-key")),
    )

    should_invoke = ai_assist.should_invoke_document_parse_assist(
        raw_parse_payload={
            "text": "Invoice Number: INV-1048\nTotal: 2450.00",
            "pages": [{"text": "Invoice Number: INV-1048", "extraction_method": "pdf_text"}],
            "metadata": {"requires_ocr": False},
        },
        document_type=DocumentType.INVOICE,
        classification_confidence=0.96,
        force=True,
    )

    assert should_invoke is True
