"""
Purpose: Define strict internal contracts for LLM-assisted document parsing.
Scope: Structured classification and bounded field-candidate outputs used by the
document parser to improve low-structure PDF and OCR extraction while keeping
all model output schema-validated.
Dependencies: Canonical document enums and the shared strict contract base model.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from services.common.enums import DocumentType
from services.contracts.api_models import ContractModel

type SupportedDocumentFieldName = Literal[
    "vendor_name",
    "vendor_address",
    "vendor_tax_id",
    "customer_name",
    "customer_tax_id",
    "invoice_number",
    "invoice_date",
    "due_date",
    "currency",
    "subtotal",
    "tax_amount",
    "tax_rate",
    "discount_amount",
    "total",
    "payment_terms",
    "notes",
    "bank_name",
    "account_number",
    "account_name",
    "statement_start_date",
    "statement_end_date",
    "opening_balance",
    "closing_balance",
    "total_credits",
    "total_debits",
    "employee_name",
    "employee_id",
    "employer_name",
    "pay_period_start",
    "pay_period_end",
    "pay_date",
    "basic_salary",
    "allowances",
    "deductions",
    "gross_pay",
    "net_pay",
    "paye_tax",
    "pension_contribution",
    "receipt_number",
    "receipt_date",
    "payment_method",
    "contract_number",
    "contract_date",
    "effective_date",
    "expiration_date",
    "party_a_name",
    "party_b_name",
    "contract_value",
    "contract_type",
    "terms",
    "renewal_terms",
    "termination_terms",
]


class DocumentFieldAssistCandidate(ContractModel):
    """Describe one model-suggested top-level field candidate for a parsed document."""

    field_name: SupportedDocumentFieldName = Field(
        description="Canonical document field name supported by the parser.",
    )
    value: str = Field(
        min_length=1,
        max_length=5_000,
        description="Normalized candidate field value as a string.",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Model confidence in this field candidate.",
    )
    evidence_quote: str | None = Field(
        default=None,
        max_length=280,
        description="Short quoted evidence snippet from the parsed text.",
    )


class DocumentParseAssistOutput(ContractModel):
    """Return bounded classification and field-candidate improvements for one document."""

    predicted_type: DocumentType = Field(description="Best document type from the model.")
    classification_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Model confidence in the predicted document type.",
    )
    classification_reasoning: str = Field(
        min_length=1,
        max_length=2_000,
        description="Short explanation grounded in the provided parser evidence.",
    )
    field_candidates: tuple[DocumentFieldAssistCandidate, ...] = Field(
        default=(),
        description=(
            "Top-level field candidates the parser may merge into deterministic output when "
            "they are missing or replaceable."
        ),
    )


__all__ = [
    "DocumentFieldAssistCandidate",
    "DocumentParseAssistOutput",
    "SupportedDocumentFieldName",
]
