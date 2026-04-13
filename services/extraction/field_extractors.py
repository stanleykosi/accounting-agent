"""
Purpose: Map parser outputs into structured extraction fields with confidence scores.
Scope: Field extraction logic that transforms parser results (PDF, OCR, Excel, CSV) into
validated ExtractedField instances for persistence and downstream accounting.
Dependencies: Extraction schemas, evidence reference builders, and confidence
thresholds from settings.
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, cast

from services.extraction.evidence_refs import normalize_parser_output_to_evidence_ref
from services.extraction.schemas import (
    CONFIDENCE_THRESHOLD_DEFAULT,
    ConfidenceSummary,
    ExtractedField,
)

type FieldType = Literal["string", "integer", "decimal", "date", "boolean"]


def parse_field_value(
    raw_value: Any,
    field_type: FieldType,
) -> Any:
    """Cast a raw parser value to its target type with safe fallbacks.

    Args:
        raw_value: The raw value from parser output.
        field_type: Target pydantic type for the field.

    Returns:
        The value cast to field_type, or None if casting fails.
    """

    if raw_value is None:
        return None

    if field_type == "string":
        return str(raw_value).strip() if raw_value else None

    if field_type == "integer":
        try:
            return int(raw_value)
        except (ValueError, TypeError):
            return None

    if field_type == "decimal":
        try:
            return Decimal(str(raw_value))
        except (ValueError, TypeError):
            return None

    if field_type == "date":
        if isinstance(raw_value, date_type):
            return raw_value
        if isinstance(raw_value, str):
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
                try:
                    return datetime.strptime(raw_value.strip(), fmt).date()
                except ValueError:
                    continue
        return None

    if field_type == "boolean":
        if isinstance(raw_value, bool):
            return raw_value
        if isinstance(raw_value, str):
            return raw_value.lower() in ("true", "yes", "1", "y")
        return None

    return raw_value


def estimate_field_confidence(
    raw_value: Any,
    parser_confidence: float | None,
    is_ocr: bool,
    field_name: str,
) -> float:
    """Estimate the confidence score for a single extracted field.

    The confidence algorithm uses parser-reported confidence when available,
    then applies heuristic adjustments for field characteristics and OCR.

    Args:
        raw_value: The extracted value before type casting.
        parser_confidence: Parser-reported confidence (0-1) or None.
        is_ocr: Whether the field came from OCR processing.
        field_name: Name of the field for heuristic adjustments.

    Returns:
        Estimated confidence score between 0.0 and 1.0.
    """

    base_confidence = parser_confidence if parser_confidence is not None else 0.8

    if raw_value is None or raw_value == "":
        return 0.0

    if is_ocr:
        base_confidence *= 0.85

    high_confidence_keywords = ("total", "amount", "date", "number", "balance")
    if any(kw in field_name.lower() for kw in high_confidence_keywords):
        base_confidence = min(1.0, base_confidence * 1.05)

    low_confidence_keywords = ("notes", "description", "memo", "terms")
    if any(kw in field_name.lower() for kw in low_confidence_keywords):
        base_confidence *= 0.9

    return round(base_confidence, 4)


def extract_invoice_fields(
    parser_output: dict[str, Any],
) -> list[ExtractedField]:
    """Extract structured fields from a parsed invoice document.

    Args:
        parser_output: Raw parser output containing extracted text,
            tables, and metadata.

    Returns:
        List of ExtractedField instances with typed values and evidence.
    """

    fields: list[ExtractedField] = []
    raw_fields = parser_output.get("fields", {})
    is_ocr = parser_output.get("source_type") == "ocr"

    for field_name, field_config in (
        ("vendor_name", "string"),
        ("vendor_address", "string"),
        ("vendor_tax_id", "string"),
        ("customer_name", "string"),
        ("customer_tax_id", "string"),
        ("invoice_number", "string"),
        ("invoice_date", "date"),
        ("due_date", "date"),
        ("currency", "string"),
        ("subtotal", "decimal"),
        ("tax_amount", "decimal"),
        ("tax_rate", "decimal"),
        ("discount_amount", "decimal"),
        ("total", "decimal"),
        ("payment_terms", "string"),
        ("notes", "string"),
    ):
        raw_value = raw_fields.get(field_name)
        field_type = cast(FieldType, field_config)
        parser_confidence = raw_fields.get(f"{field_name}_confidence")

        parsed_value = parse_field_value(raw_value, field_type)
        confidence = estimate_field_confidence(raw_value, parser_confidence, is_ocr, field_name)

        evidence = normalize_parser_output_to_evidence_ref(
            parser_output.get("field_locations", {}).get(field_name, {})
        )

        fields.append(
            ExtractedField(
                field_name=field_name,
                field_value=parsed_value,
                field_type=field_type,
                confidence=confidence,
                evidence_ref=evidence,
            )
        )

    return fields


def extract_bank_statement_fields(
    parser_output: dict[str, Any],
) -> list[ExtractedField]:
    """Extract structured fields from a parsed bank statement.

    Args:
        parser_output: Raw parser output containing extracted text,
            tables, and metadata.

    Returns:
        List of ExtractedField instances with typed values and evidence.
    """

    fields: list[ExtractedField] = []
    raw_fields = parser_output.get("fields", {})
    is_ocr = parser_output.get("source_type") == "ocr"

    for field_name, field_type in (
        ("bank_name", "string"),
        ("account_number", "string"),
        ("account_name", "string"),
        ("statement_start_date", "date"),
        ("statement_end_date", "date"),
        ("opening_balance", "decimal"),
        ("closing_balance", "decimal"),
        ("total_credits", "decimal"),
        ("total_debits", "decimal"),
        ("currency", "string"),
    ):
        field_type = cast(FieldType, field_type)
        raw_value = raw_fields.get(field_name)
        parser_confidence = raw_fields.get(f"{field_name}_confidence")

        parsed_value = parse_field_value(raw_value, field_type)
        confidence = estimate_field_confidence(raw_value, parser_confidence, is_ocr, field_name)

        evidence = normalize_parser_output_to_evidence_ref(
            parser_output.get("field_locations", {}).get(field_name, {})
        )

        fields.append(
            ExtractedField(
                field_name=field_name,
                field_value=parsed_value,
                field_type=field_type,
                confidence=confidence,
                evidence_ref=evidence,
            )
        )

    return fields


def extract_payslip_fields(
    parser_output: dict[str, Any],
) -> list[ExtractedField]:
    """Extract structured fields from a parsed payslip.

    Args:
        parser_output: Raw parser output containing extracted text,
            tables, and metadata.

    Returns:
        List of ExtractedField instances with typed values and evidence.
    """

    fields: list[ExtractedField] = []
    raw_fields = parser_output.get("fields", {})
    is_ocr = parser_output.get("source_type") == "ocr"

    for field_name, field_type in (
        ("employee_name", "string"),
        ("employee_id", "string"),
        ("employer_name", "string"),
        ("pay_period_start", "date"),
        ("pay_period_end", "date"),
        ("pay_date", "date"),
        ("basic_salary", "decimal"),
        ("allowances", "decimal"),
        ("deductions", "decimal"),
        ("gross_pay", "decimal"),
        ("net_pay", "decimal"),
        ("currency", "string"),
        ("paye_tax", "decimal"),
        ("pension_contribution", "decimal"),
    ):
        field_type = cast(FieldType, field_type)
        raw_value = raw_fields.get(field_name)
        parser_confidence = raw_fields.get(f"{field_name}_confidence")

        parsed_value = parse_field_value(raw_value, field_type)
        confidence = estimate_field_confidence(raw_value, parser_confidence, is_ocr, field_name)

        evidence = normalize_parser_output_to_evidence_ref(
            parser_output.get("field_locations", {}).get(field_name, {})
        )

        fields.append(
            ExtractedField(
                field_name=field_name,
                field_value=parsed_value,
                field_type=field_type,
                confidence=confidence,
                evidence_ref=evidence,
            )
        )

    return fields


def extract_receipt_fields(
    parser_output: dict[str, Any],
) -> list[ExtractedField]:
    """Extract structured fields from a parsed receipt.

    Args:
        parser_output: Raw parser output containing extracted text,
            tables, and metadata.

    Returns:
        List of ExtractedField instances with typed values and evidence.
    """

    fields: list[ExtractedField] = []
    raw_fields = parser_output.get("fields", {})
    is_ocr = parser_output.get("source_type") == "ocr"

    for field_name, field_type in (
        ("receipt_number", "string"),
        ("receipt_date", "date"),
        ("vendor_name", "string"),
        ("vendor_tax_id", "string"),
        ("customer_name", "string"),
        ("currency", "string"),
        ("subtotal", "decimal"),
        ("tax_amount", "decimal"),
        ("total", "decimal"),
        ("payment_method", "string"),
    ):
        field_type = cast(FieldType, field_type)
        raw_value = raw_fields.get(field_name)
        parser_confidence = raw_fields.get(f"{field_name}_confidence")

        parsed_value = parse_field_value(raw_value, field_type)
        confidence = estimate_field_confidence(raw_value, parser_confidence, is_ocr, field_name)

        evidence = normalize_parser_output_to_evidence_ref(
            parser_output.get("field_locations", {}).get(field_name, {})
        )

        fields.append(
            ExtractedField(
                field_name=field_name,
                field_value=parsed_value,
                field_type=field_type,
                confidence=confidence,
                evidence_ref=evidence,
            )
        )

    return fields


def extract_contract_fields(
    parser_output: dict[str, Any],
) -> list[ExtractedField]:
    """Extract structured fields from a parsed contract.

    Args:
        parser_output: Raw parser output containing extracted text,
            tables, and metadata.

    Returns:
        List of ExtractedField instances with typed values and evidence.
    """

    fields: list[ExtractedField] = []
    raw_fields = parser_output.get("fields", {})
    is_ocr = parser_output.get("source_type") == "ocr"

    for field_name, field_type in (
        ("contract_number", "string"),
        ("contract_date", "date"),
        ("effective_date", "date"),
        ("expiration_date", "date"),
        ("party_a_name", "string"),
        ("party_b_name", "string"),
        ("contract_value", "decimal"),
        ("currency", "string"),
        ("contract_type", "string"),
        ("terms", "string"),
        ("renewal_terms", "string"),
        ("termination_terms", "string"),
    ):
        field_type = cast(FieldType, field_type)
        raw_value = raw_fields.get(field_name)
        parser_confidence = raw_fields.get(f"{field_name}_confidence")

        parsed_value = parse_field_value(raw_value, field_type)
        confidence = estimate_field_confidence(raw_value, parser_confidence, is_ocr, field_name)

        evidence = normalize_parser_output_to_evidence_ref(
            parser_output.get("field_locations", {}).get(field_name, {})
        )

        fields.append(
            ExtractedField(
                field_name=field_name,
                field_value=parsed_value,
                field_type=field_type,
                confidence=confidence,
                evidence_ref=evidence,
            )
        )

    return fields


EXTRACTOR_REGISTRY: dict[str, Any] = {
    "invoice": extract_invoice_fields,
    "bank_statement": extract_bank_statement_fields,
    "payslip": extract_payslip_fields,
    "receipt": extract_receipt_fields,
    "contract": extract_contract_fields,
}


def extract_fields_by_document_type(
    document_type: str,
    parser_output: dict[str, Any],
) -> list[ExtractedField]:
    """Extract structured fields based on document type.

    Args:
        document_type: The classified document type.
        parser_output: Raw parser output dictionary.

    Returns:
        List of ExtractedField instances, or empty list if type unknown.
    """

    extractor = EXTRACTOR_REGISTRY.get(document_type)
    if extractor is None:
        return []

    return list(extractor(parser_output))


def compute_confidence_summary(
    fields: list[ExtractedField],
) -> ConfidenceSummary:
    """Compute aggregate confidence metrics from extracted fields.

    Args:
        fields: List of extracted fields.

    Returns:
        ConfidenceSummary with aggregate scores.
    """

    threshold_low = CONFIDENCE_THRESHOLD_DEFAULT

    field_count = len(fields)
    if field_count == 0:
        return ConfidenceSummary(
            overall_confidence=0.0,
            field_count=0,
            low_confidence_fields=0,
            missing_fields=0,
        )

    total_confidence = sum(f.confidence for f in fields)
    overall = total_confidence / field_count

    low_confidence_count = sum(1 for f in fields if f.confidence < threshold_low)

    return ConfidenceSummary(
        overall_confidence=round(overall, 4),
        field_count=field_count,
        low_confidence_fields=low_confidence_count,
        missing_fields=0,
    )


__all__ = [
    "EXTRACTOR_REGISTRY",
    "compute_confidence_summary",
    "estimate_field_confidence",
    "extract_bank_statement_fields",
    "extract_contract_fields",
    "extract_fields_by_document_type",
    "extract_invoice_fields",
    "extract_payslip_fields",
    "extract_receipt_fields",
    "parse_field_value",
]
