"""
Purpose: Define extraction schemas for supported document types with field-level
validation, evidence references, and confidence summaries.
Scope: Pydantic models for invoices, bank statements, payslips, receipts,
and contracts that downstream services use to validate and persist extraction
outputs.
Dependencies: pydantic, parser output models, and contract types defined in earlier
steps.
"""

from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


class EvidenceRef(BaseModel):
    """Reference to the source location of an extracted field value."""

    page: int | None = Field(
        default=None,
        description="One-indexed page number where the field was found.",
    )
    row: int | None = Field(
        default=None,
        description="One-indexed row number in table or structured content.",
    )
    cell: str | None = Field(
        default=None,
        description="Cell coordinate (e.g., 'A1', 'B3') for spreadsheet cells.",
    )
    x_coordinate: float | None = Field(
        default=None,
        description="X coordinate in PDF points for visual document evidence.",
    )
    y_coordinate: float | None = Field(
        default=None,
        description="Y coordinate in PDF points for visual document evidence.",
    )
    snippet: str | None = Field(
        default=None,
        description="Text snippet surrounding the extracted value for verification.",
    )


class ConfidenceSummary(BaseModel):
    """Aggregate confidence scores for an extraction result."""

    overall_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Weighted overall confidence score for the extraction.",
    )
    field_count: int = Field(
        ge=0,
        description="Total number of fields extracted.",
    )
    low_confidence_fields: int = Field(
        ge=0,
        description="Number of fields below the confidence threshold.",
    )
    missing_fields: int = Field(
        ge=0,
        description="Number of expected fields that could not be extracted.",
    )


class ExtractedField(BaseModel):
    """One extracted field from a document with its evidence and confidence."""

    field_name: str = Field(description="Canonical name of the extracted field.")
    field_value: Any = Field(description="Extracted value, typed according to field_type.")
    field_type: Literal[
        "string",
        "integer",
        "decimal",
        "date",
        "boolean",
    ] = Field(description="Pydantic type annotation for the field value.")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence score for this specific field extraction.",
    )
    evidence_ref: EvidenceRef = Field(
        description="Source location reference for the extracted value.",
    )
    is_human_corrected: bool = Field(
        default=False,
        description="Whether a human reviewer corrected this field value.",
    )


class DocumentLineItem(BaseModel):
    """One line item extracted from a table or structured section of a document."""

    line_no: int = Field(ge=1, description="One-indexed line item sequence.")
    description: str | None = Field(
        default=None,
        description="Line item description or narrative.",
    )
    quantity: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        description="Quantity for invoice line items.",
    )
    unit_price: Decimal | None = Field(
        default=None,
        description="Unit price for invoice line items.",
    )
    amount: Decimal | None = Field(
        default=None,
        description="Total amount for the line item.",
    )
    tax_amount: Decimal | None = Field(
        default=None,
        description="Tax amount applicable to the line item.",
    )
    dimensions: dict[str, Any] = Field(
        default_factory=dict,
        description="Cost centre, department, project, or other dimension tags.",
    )
    evidence_ref: EvidenceRef = Field(
        description="Source location reference for the line item.",
    )


class InvoiceExtraction(BaseModel):
    """Extracted fields from an invoice document."""

    vendor_name: str | None = Field(
        default=None,
        description="Name of the vendor or supplier.",
    )
    vendor_address: str | None = Field(
        default=None,
        description="Vendor address from the invoice header.",
    )
    vendor_tax_id: str | None = Field(
        default=None,
        description="Vendor tax identification number.",
    )
    customer_name: str | None = Field(
        default=None,
        description="Name of the customer or buyer.",
    )
    customer_tax_id: str | None = Field(
        default=None,
        description="Customer tax identification number.",
    )
    invoice_number: str | None = Field(
        default=None,
        description="Invoice number or reference.",
    )
    invoice_date: date_type | None = Field(
        default=None,
        description="Date emitted on the invoice.",
    )
    due_date: date_type | None = Field(
        default=None,
        description="Payment due date.",
    )
    currency: str | None = Field(
        default=None,
        max_length=3,
        description="ISO 4217 currency code.",
    )
    subtotal: Decimal | None = Field(
        default=None,
        description="Subtotal before tax.",
    )
    tax_amount: Decimal | None = Field(
        default=None,
        description="Total tax amount.",
    )
    tax_rate: Decimal | None = Field(
        default=None,
        description="Tax rate applied (e.g., 0.075 for 7.5%).",
    )
    discount_amount: Decimal | None = Field(
        default=None,
        description="Discount applied before tax.",
    )
    total: Decimal | None = Field(
        default=None,
        description="Grand total due.",
    )
    line_items: list[DocumentLineItem] = Field(
        default_factory=list,
        description="Extracted line items from the invoice.",
    )
    payment_terms: str | None = Field(
        default=None,
        description="Payment terms or terms of delivery.",
    )
    notes: str | None = Field(
        default=None,
        description="Notes or special instructions.",
    )


class BankStatementExtraction(BaseModel):
    """Extracted fields from a bank statement document."""

    bank_name: str | None = Field(
        default=None,
        description="Name of the bank or financial institution.",
    )
    account_number: str | None = Field(
        default=None,
        description="Account number covered by the statement.",
    )
    account_name: str | None = Field(
        default=None,
        description="Name on the account.",
    )
    statement_start_date: date_type | None = Field(
        default=None,
        description="Statement period start date.",
    )
    statement_end_date: date_type | None = Field(
        default=None,
        description="Statement period end date.",
    )
    opening_balance: Decimal | None = Field(
        default=None,
        description="Opening balance at statement start.",
    )
    closing_balance: Decimal | None = Field(
        default=None,
        description="Closing balance at statement end.",
    )
    total_credits: Decimal | None = Field(
        default=None,
        description="Total credits during the period.",
    )
    total_debits: Decimal | None = Field(
        default=None,
        description="Total debits during the period.",
    )
    currency: str | None = Field(
        default=None,
        max_length=3,
        description="ISO 4217 currency code.",
    )
    statement_lines: list[BankStatementLine] = Field(
        default_factory=list,
        description="Extracted transaction lines from the statement.",
    )


class BankStatementLine(BaseModel):
    """One transaction line extracted from a bank statement."""

    line_no: int = Field(ge=1, description="One-indexed line sequence.")
    date: date_type | None = Field(
        default=None,
        description="Transaction date.",
    )
    description: str | None = Field(
        default=None,
        description="Transaction narration or reference.",
    )
    reference: str | None = Field(
        default=None,
        description="Cheque or transaction reference number.",
    )
    debit: Decimal | None = Field(
        default=None,
        description="Debit amount if applicable.",
    )
    credit: Decimal | None = Field(
        default=None,
        description="Credit amount if applicable.",
    )
    balance: Decimal | None = Field(
        default=None,
        description="Running balance after the transaction.",
    )
    evidence_ref: EvidenceRef = Field(
        description="Source location reference for the line.",
    )


class PayslipExtraction(BaseModel):
    """Extracted fields from a payslip document."""

    employee_name: str | None = Field(
        default=None,
        description="Full name of the employee.",
    )
    employee_id: str | None = Field(
        default=None,
        description="Employee identification number.",
    )
    employer_name: str | None = Field(
        default=None,
        description="Employer or company name.",
    )
    pay_period_start: date_type | None = Field(
        default=None,
        description="Pay period start date.",
    )
    pay_period_end: date_type | None = Field(
        default=None,
        description="Pay period end date.",
    )
    pay_date: date_type | None = Field(
        default=None,
        description="Date of payment.",
    )
    basic_salary: Decimal | None = Field(
        default=None,
        description="Basic salary for the period.",
    )
    allowances: Decimal | None = Field(
        default=None,
        description="Total allowances.",
    )
    deductions: Decimal | None = Field(
        default=None,
        description="Total deductions.",
    )
    gross_pay: Decimal | None = Field(
        default=None,
        description="Gross pay before deductions.",
    )
    net_pay: Decimal | None = Field(
        default=None,
        description="Net pay after deductions.",
    )
    currency: str | None = Field(
        default=None,
        max_length=3,
        description="ISO 4217 currency code.",
    )
    paye_tax: Decimal | None = Field(
        default=None,
        description="PAYE tax deducted.",
    )
    pension_contribution: Decimal | None = Field(
        default=None,
        description="Employee pension contribution.",
    )
    other_deductions: list[PayslipDeduction] = Field(
        default_factory=list,
        description="Itemized other deductions.",
    )


class PayslipDeduction(BaseModel):
    """One deduction line item on a payslip."""

    deduction_type: str = Field(description="Type of deduction.")
    amount: Decimal | None = Field(
        default=None,
        description="Deduction amount.",
    )
    evidence_ref: EvidenceRef = Field(
        description="Source location reference.",
    )


class ReceiptExtraction(BaseModel):
    """Extracted fields from a receipt document."""

    receipt_number: str | None = Field(
        default=None,
        description="Receipt number or reference.",
    )
    receipt_date: date_type | None = Field(
        default=None,
        description="Date on the receipt.",
    )
    vendor_name: str | None = Field(
        default=None,
        description="Vendor or merchant name.",
    )
    vendor_tax_id: str | None = Field(
        default=None,
        description="Vendor tax identification number.",
    )
    customer_name: str | None = Field(
        default=None,
        description="Customer name.",
    )
    currency: str | None = Field(
        default=None,
        max_length=3,
        description="ISO 4217 currency code.",
    )
    subtotal: Decimal | None = Field(
        default=None,
        description="Subtotal before tax.",
    )
    tax_amount: Decimal | None = Field(
        default=None,
        description="Tax amount.",
    )
    total: Decimal | None = Field(
        default=None,
        description="Total amount paid.",
    )
    payment_method: str | None = Field(
        default=None,
        description="Payment method used (cash, card, transfer, etc.).",
    )
    line_items: list[DocumentLineItem] = Field(
        default_factory=list,
        description="Extracted line items from the receipt.",
    )


class ContractExtraction(BaseModel):
    """Extracted fields from a contract or agreement document."""

    contract_number: str | None = Field(
        default=None,
        description="Contract number or reference.",
    )
    contract_date: date_type | None = Field(
        default=None,
        description="Date the contract was signed or executed.",
    )
    effective_date: date_type | None = Field(
        default=None,
        description="Contract effective start date.",
    )
    expiration_date: date_type | None = Field(
        default=None,
        description="Contract expiration or end date.",
    )
    party_a_name: str | None = Field(
        default=None,
        description="First party (e.g., client or lessor).",
    )
    party_b_name: str | None = Field(
        default=None,
        description="Second party (e.g., vendor or lessee).",
    )
    contract_value: Decimal | None = Field(
        default=None,
        description="Total contract value or consideration.",
    )
    currency: str | None = Field(
        default=None,
        max_length=3,
        description="ISO 4217 currency code.",
    )
    contract_type: str | None = Field(
        default=None,
        description="Type of contract (service, lease, license, etc.).",
    )
    terms: str | None = Field(
        default=None,
        description="Key terms or special conditions.",
    )
    renewal_terms: str | None = Field(
        default=None,
        description="Renewal or extension terms.",
    )
    termination_terms: str | None = Field(
        default=None,
        description="Termination conditions.",
    )


EXTRACTION_SCHEMA_VERSION = "1.0.0"
CONFIDENCE_THRESHOLD_DEFAULT = 0.7

EXTRACTION_SCHEMAS: dict[str, type[BaseModel]] = {
    "invoice": InvoiceExtraction,
    "bank_statement": BankStatementExtraction,
    "payslip": PayslipExtraction,
    "receipt": ReceiptExtraction,
    "contract": ContractExtraction,
}


def get_extraction_schema(
    document_type: str,
) -> type[BaseModel] | None:
    """Retrieve the extraction schema class for a given document type."""

    return EXTRACTION_SCHEMAS.get(document_type)


def validate_extraction_payload(
    document_type: str,
    payload: dict[str, Any],
) -> tuple[BaseModel | None, list[str]]:
    """Validate an extraction payload against its schema.

    Returns the validated model instance and a list of validation error messages.
    """

    schema_class = get_extraction_schema(document_type)
    if schema_class is None:
        return None, [f"Unknown document type: {document_type}"]

    try:
        instance = schema_class.model_validate(payload)
        return instance, []
    except Exception as e:
        return None, [str(e)]


__all__ = [
    "EXTRACTION_SCHEMAS",
    "EXTRACTION_SCHEMA_VERSION",
    "BankStatementExtraction",
    "BankStatementLine",
    "ConfidenceSummary",
    "ContractExtraction",
    "DocumentLineItem",
    "EvidenceRef",
    "ExtractedField",
    "InvoiceExtraction",
    "PayslipDeduction",
    "PayslipExtraction",
    "ReceiptExtraction",
    "get_extraction_schema",
    "validate_extraction_payload",
]
