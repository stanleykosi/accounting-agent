"""
Purpose: Mark the extraction package boundary for structured field extraction
workflows.
Scope: Extraction schemas, evidence reference builders, field extractors,
and extraction persistence.
Dependencies: Parser outputs, extraction database models, and confidence
thresholds from settings.
"""

from services.extraction.evidence_refs import (
    build_evidence_ref,
    build_pdf_evidence_ref,
    build_spreadsheet_evidence_ref,
    build_table_evidence_ref,
    merge_snippet_context,
    normalize_parser_output_to_evidence_ref,
)
from services.extraction.field_extractors import (
    compute_confidence_summary,
    estimate_field_confidence,
    extract_bank_statement_fields,
    extract_contract_fields,
    extract_fields_by_document_type,
    extract_invoice_fields,
    extract_payslip_fields,
    extract_receipt_fields,
    parse_field_value,
)
from services.extraction.schemas import (
    EXTRACTION_SCHEMA_VERSION,
    EXTRACTION_SCHEMAS,
    ConfidenceSummary,
    ContractExtraction,
    DocumentLineItem,
    EvidenceRef,
    ExtractedField,
    InvoiceExtraction,
    PayslipExtraction,
    ReceiptExtraction,
    get_extraction_schema,
    validate_extraction_payload,
)
from services.extraction.service import ExtractionService

__all__ = [
    "EXTRACTION_SCHEMAS",
    "EXTRACTION_SCHEMA_VERSION",
    "ConfidenceSummary",
    "ContractExtraction",
    "DocumentLineItem",
    "EvidenceRef",
    "ExtractedField",
    "ExtractionService",
    "InvoiceExtraction",
    "PayslipExtraction",
    "ReceiptExtraction",
    "build_evidence_ref",
    "build_pdf_evidence_ref",
    "build_spreadsheet_evidence_ref",
    "build_table_evidence_ref",
    "compute_confidence_summary",
    "estimate_field_confidence",
    "extract_bank_statement_fields",
    "extract_contract_fields",
    "extract_fields_by_document_type",
    "extract_invoice_fields",
    "extract_payslip_fields",
    "extract_receipt_fields",
    "get_extraction_schema",
    "merge_snippet_context",
    "normalize_parser_output_to_evidence_ref",
    "parse_field_value",
    "validate_extraction_payload",
]
