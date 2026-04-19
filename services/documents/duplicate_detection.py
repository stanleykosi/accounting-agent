"""
Purpose: Implement duplicate document detection for document intake workflows.
Scope: Exact-hash duplicate detection plus semantic duplicate detection from
normalized extraction facts within one close run.
Dependencies: Document repository, storage layer, and document models.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any, Protocol
from uuid import UUID

from services.common.enums import DocumentStatus, DocumentType
from services.db.repositories.document_repo import DocumentRepository, DocumentWithExtractionRecord
from services.storage.repository import StorageRepository


@dataclass(frozen=True, slots=True)
class DuplicateDetectionResult:
    """Result of duplicate detection check."""

    is_duplicate: bool
    existing_document_id: str | None = None
    existing_document_filename: str | None = None
    similarity_score: float = 0.0
    detection_method: str = "sha256_exact"
    matched_fields: tuple[str, ...] = ()


class DuplicateDetectionProtocol(Protocol):
    """Protocol for duplicate detection service."""

    def check_duplicate(
        self,
        *,
        document_hash: str,
        close_run_id: str,
        entity_id: str,
        current_document_id: str | None = None,
    ) -> DuplicateDetectionResult:
        """Check if a document with the given hash already exists."""
        ...


class DuplicateDetectionService:
    """Service for detecting duplicate documents in close runs."""

    def __init__(
        self,
        *,
        document_repo: DocumentRepository,
        storage_repo: StorageRepository,
    ) -> None:
        """Initialize with required dependencies."""
        self._document_repo = document_repo
        self._storage_repo = storage_repo

    def check_duplicate(
        self,
        *,
        document_hash: str,
        close_run_id: str,
        entity_id: str,
        current_document_id: str | None = None,
    ) -> DuplicateDetectionResult:
        """Check whether the current document is an exact or semantic duplicate."""
        del entity_id

        try:
            close_run_uuid = UUID(close_run_id)
        except ValueError:
            return DuplicateDetectionResult(is_duplicate=False)

        current_document_uuid: UUID | None = None
        if current_document_id is not None:
            try:
                current_document_uuid = UUID(current_document_id)
            except ValueError:
                current_document_uuid = None

        close_run_documents = (
            self._document_repo.list_documents_for_close_run_with_latest_extraction(
                close_run_id=close_run_uuid
            )
        )
        candidate_rows = [
            row
            for row in close_run_documents
            if row.document.id != current_document_uuid and _document_is_duplicate_eligible(row)
        ]
        exact_matches = [
            row for row in candidate_rows if row.document.sha256_hash == document_hash
        ]
        if exact_matches:
            existing_row = sorted(exact_matches, key=_duplicate_sort_key)[0]
            return DuplicateDetectionResult(
                is_duplicate=True,
                existing_document_id=str(existing_row.document.id),
                existing_document_filename=existing_row.document.original_filename,
                similarity_score=1.0,
                detection_method="sha256_exact",
                matched_fields=("sha256_hash",),
            )

        if current_document_uuid is None:
            return DuplicateDetectionResult(is_duplicate=False)

        current_row = next(
            (
                row
                for row in close_run_documents
                if row.document.id == current_document_uuid and row.latest_extraction is not None
            ),
            None,
        )
        if current_row is None:
            return DuplicateDetectionResult(is_duplicate=False)

        semantic_matches = [
            (match, row)
            for row in candidate_rows
            if (match := _match_semantic_duplicate(current_row=current_row, candidate_row=row))
            is not None
        ]
        if not semantic_matches:
            return DuplicateDetectionResult(is_duplicate=False)

        return sorted(
            semantic_matches,
            key=lambda item: (
                -item[0].similarity_score,
                1 if item[0].detection_method.endswith("_summary") else 0,
                _duplicate_sort_key(item[1]),
            ),
        )[0][0]

    def refresh_close_run_duplicates(
        self,
        *,
        close_run_id: str,
    ) -> dict[UUID, DuplicateDetectionResult]:
        """Recompute duplicate state deterministically for every eligible document."""

        try:
            close_run_uuid = UUID(close_run_id)
        except ValueError:
            return {}

        close_run_documents = (
            self._document_repo.list_documents_for_close_run_with_latest_extraction(
                close_run_id=close_run_uuid
            )
        )
        ordered_rows = sorted(
            (
                row
                for row in close_run_documents
                if _document_is_duplicate_eligible(row)
            ),
            key=_duplicate_sort_key,
        )

        canonical_rows: list[DocumentWithExtractionRecord] = []
        duplicate_results: dict[UUID, DuplicateDetectionResult] = {}

        for row in ordered_rows:
            duplicate_result = _detect_duplicate_against_canonical_rows(
                current_row=row,
                canonical_rows=tuple(canonical_rows),
            )
            duplicate_results[row.document.id] = duplicate_result
            if not duplicate_result.is_duplicate:
                canonical_rows.append(row)

        return duplicate_results


def _document_is_duplicate_eligible(row: DocumentWithExtractionRecord) -> bool:
    """Return whether one document row can be considered a duplicate source candidate."""

    return row.document.status not in {
        DocumentStatus.REJECTED,
        DocumentStatus.FAILED,
    }


def _duplicate_sort_key(row: DocumentWithExtractionRecord) -> tuple[int, Any, Any]:
    """Prefer canonical earlier documents over already-flagged duplicates."""

    return (
        1 if row.document.status is DocumentStatus.DUPLICATE else 0,
        row.document.created_at,
        row.document.id,
    )

def _match_semantic_duplicate(
    *,
    current_row: DocumentWithExtractionRecord,
    candidate_row: DocumentWithExtractionRecord,
) -> DuplicateDetectionResult | None:
    """Return one semantic duplicate result when two parsed documents represent the same source."""

    current_type = current_row.document.document_type
    if (
        current_type is DocumentType.UNKNOWN
        or current_type is not candidate_row.document.document_type
        or current_row.latest_extraction is None
        or candidate_row.latest_extraction is None
    ):
        return None

    current_fields = _field_values(current_row)
    candidate_fields = _field_values(candidate_row)
    if current_type is DocumentType.INVOICE:
        return _match_invoice_duplicate(
            current_row=current_row,
            candidate_row=candidate_row,
            current_fields=current_fields,
            candidate_fields=candidate_fields,
        )
    if current_type is DocumentType.RECEIPT:
        return _match_receipt_duplicate(
            current_row=current_row,
            candidate_row=candidate_row,
            current_fields=current_fields,
            candidate_fields=candidate_fields,
        )
    if current_type is DocumentType.PAYSLIP:
        return _match_payslip_duplicate(
            current_row=current_row,
            candidate_row=candidate_row,
            current_fields=current_fields,
            candidate_fields=candidate_fields,
        )
    if current_type is DocumentType.BANK_STATEMENT:
        return _match_bank_statement_duplicate(
            current_row=current_row,
            candidate_row=candidate_row,
            current_fields=current_fields,
            candidate_fields=candidate_fields,
        )
    if current_type is DocumentType.CONTRACT:
        return _match_contract_duplicate(
            current_row=current_row,
            candidate_row=candidate_row,
            current_fields=current_fields,
            candidate_fields=candidate_fields,
        )
    return None


def _detect_duplicate_against_canonical_rows(
    *,
    current_row: DocumentWithExtractionRecord,
    canonical_rows: tuple[DocumentWithExtractionRecord, ...],
) -> DuplicateDetectionResult:
    """Return the best duplicate result for one row against previously accepted canonical rows."""

    exact_matches = [
        row
        for row in canonical_rows
        if row.document.sha256_hash == current_row.document.sha256_hash
    ]
    if exact_matches:
        existing_row = sorted(exact_matches, key=_duplicate_sort_key)[0]
        return DuplicateDetectionResult(
            is_duplicate=True,
            existing_document_id=str(existing_row.document.id),
            existing_document_filename=existing_row.document.original_filename,
            similarity_score=1.0,
            detection_method="sha256_exact",
            matched_fields=("sha256_hash",),
        )

    semantic_matches = [
        (match, row)
        for row in canonical_rows
        if (match := _match_semantic_duplicate(current_row=current_row, candidate_row=row))
        is not None
    ]
    if not semantic_matches:
        return DuplicateDetectionResult(is_duplicate=False)

    return sorted(
        semantic_matches,
        key=lambda item: (
            -item[0].similarity_score,
            1 if item[0].detection_method.endswith("_summary") else 0,
            _duplicate_sort_key(item[1]),
        ),
    )[0][0]


def _match_invoice_duplicate(
    *,
    current_row: DocumentWithExtractionRecord,
    candidate_row: DocumentWithExtractionRecord,
    current_fields: dict[str, Any],
    candidate_fields: dict[str, Any],
) -> DuplicateDetectionResult | None:
    """Detect duplicate invoices from vendor/reference/date/amount facts."""

    shared_total = _shared_decimal_value(current_fields, candidate_fields, "total", "subtotal")
    shared_date = _shared_date_value(current_fields, candidate_fields, "invoice_date", "due_date")
    current_reference = _preferred_text(current_fields, "invoice_number")
    candidate_reference = _preferred_text(candidate_fields, "invoice_number")
    if (
        current_reference is not None
        and current_reference == candidate_reference
        and shared_total is not None
        and shared_date is not None
        and _shared_optional_text(current_fields, candidate_fields, "currency")
    ):
        return _semantic_result(
            candidate_row=candidate_row,
            similarity_score=0.98,
            detection_method="semantic_invoice_reference",
            matched_fields=("invoice_number", shared_date, shared_total, "currency"),
        )

    vendor_field = _shared_counterparty_field(
        current_fields,
        candidate_fields,
        exact_fields=("vendor_tax_id",),
        fuzzy_fields=("vendor_name",),
        similarity_threshold=0.94,
    )
    if shared_total is not None and shared_date is not None and vendor_field is not None:
        matched_fields = [vendor_field, shared_date, shared_total]
        if _shared_optional_text(current_fields, candidate_fields, "currency"):
            matched_fields.append("currency")
        return _semantic_result(
            candidate_row=candidate_row,
            similarity_score=0.91,
            detection_method="semantic_invoice_summary",
            matched_fields=tuple(matched_fields),
        )
    return None


def _match_receipt_duplicate(
    *,
    current_row: DocumentWithExtractionRecord,
    candidate_row: DocumentWithExtractionRecord,
    current_fields: dict[str, Any],
    candidate_fields: dict[str, Any],
) -> DuplicateDetectionResult | None:
    """Detect duplicate receipts from receipt/vendor/date/amount facts."""

    shared_total = _shared_decimal_value(current_fields, candidate_fields, "total", "subtotal")
    shared_date = _shared_date_value(current_fields, candidate_fields, "receipt_date")
    current_reference = _preferred_text(current_fields, "receipt_number")
    candidate_reference = _preferred_text(candidate_fields, "receipt_number")
    if (
        current_reference is not None
        and current_reference == candidate_reference
        and shared_total is not None
        and shared_date is not None
    ):
        matched_fields = ["receipt_number", shared_date, shared_total]
        if _shared_optional_text(current_fields, candidate_fields, "currency"):
            matched_fields.append("currency")
        return _semantic_result(
            candidate_row=candidate_row,
            similarity_score=0.97,
            detection_method="semantic_receipt_reference",
            matched_fields=tuple(matched_fields),
        )

    vendor_field = _shared_counterparty_field(
        current_fields,
        candidate_fields,
        exact_fields=("vendor_tax_id",),
        fuzzy_fields=("vendor_name",),
        similarity_threshold=0.94,
    )
    if shared_total is not None and shared_date is not None and vendor_field is not None:
        matched_fields = [vendor_field, shared_date, shared_total]
        if _shared_optional_text(current_fields, candidate_fields, "currency"):
            matched_fields.append("currency")
        return _semantic_result(
            candidate_row=candidate_row,
            similarity_score=0.90,
            detection_method="semantic_receipt_summary",
            matched_fields=tuple(matched_fields),
        )
    return None


def _match_payslip_duplicate(
    *,
    current_row: DocumentWithExtractionRecord,
    candidate_row: DocumentWithExtractionRecord,
    current_fields: dict[str, Any],
    candidate_fields: dict[str, Any],
) -> DuplicateDetectionResult | None:
    """Detect duplicate payslips from employee/pay-period/amount facts."""

    employee_field = _shared_counterparty_field(
        current_fields,
        candidate_fields,
        exact_fields=("employee_id",),
        fuzzy_fields=("employee_name",),
        similarity_threshold=0.96,
    )
    shared_pay_date = _shared_date_value(
        current_fields,
        candidate_fields,
        "pay_date",
        "pay_period_end",
        "pay_period_start",
    )
    shared_net_pay = _shared_decimal_value(current_fields, candidate_fields, "net_pay", "gross_pay")
    if employee_field is None or shared_pay_date is None or shared_net_pay is None:
        return None

    matched_fields = [employee_field, shared_pay_date, shared_net_pay]
    employer_field = _shared_counterparty_field(
        current_fields,
        candidate_fields,
        exact_fields=(),
        fuzzy_fields=("employer_name",),
        similarity_threshold=0.96,
    )
    if employer_field is not None:
        matched_fields.append(employer_field)
    if _shared_optional_text(current_fields, candidate_fields, "currency"):
        matched_fields.append("currency")
    return _semantic_result(
        candidate_row=candidate_row,
        similarity_score=0.94,
        detection_method="semantic_payslip_compensation",
        matched_fields=tuple(matched_fields),
    )


def _match_bank_statement_duplicate(
    *,
    current_row: DocumentWithExtractionRecord,
    candidate_row: DocumentWithExtractionRecord,
    current_fields: dict[str, Any],
    candidate_fields: dict[str, Any],
) -> DuplicateDetectionResult | None:
    """Detect duplicate bank statements from account/period/balance facts."""

    account_field = _shared_counterparty_field(
        current_fields,
        candidate_fields,
        exact_fields=("account_number",),
        fuzzy_fields=("account_name",),
        similarity_threshold=0.98,
    )
    start_date = _shared_date_value(current_fields, candidate_fields, "statement_start_date")
    end_date = _shared_date_value(current_fields, candidate_fields, "statement_end_date")
    closing_balance = _shared_decimal_value(current_fields, candidate_fields, "closing_balance")
    if account_field is None or start_date is None or end_date is None or closing_balance is None:
        return None

    matched_fields = [account_field, start_date, end_date, closing_balance]
    opening_balance = _shared_decimal_value(current_fields, candidate_fields, "opening_balance")
    if opening_balance is not None:
        matched_fields.append(opening_balance)
    line_count = _shared_statement_line_count(current_row, candidate_row)
    if line_count is not None:
        matched_fields.append("statement_line_count")
    if _shared_optional_text(current_fields, candidate_fields, "currency"):
        matched_fields.append("currency")
    return _semantic_result(
        candidate_row=candidate_row,
        similarity_score=0.96,
        detection_method="semantic_bank_statement_period",
        matched_fields=tuple(matched_fields),
    )


def _match_contract_duplicate(
    *,
    current_row: DocumentWithExtractionRecord,
    candidate_row: DocumentWithExtractionRecord,
    current_fields: dict[str, Any],
    candidate_fields: dict[str, Any],
) -> DuplicateDetectionResult | None:
    """Detect duplicate contracts from contract identifiers or party/date/value facts."""

    contract_number = _shared_optional_text(current_fields, candidate_fields, "contract_number")
    contract_date = _shared_date_value(
        current_fields,
        candidate_fields,
        "effective_date",
        "contract_date",
    )
    contract_value = _shared_decimal_value(current_fields, candidate_fields, "contract_value")
    if contract_number and contract_date is not None and contract_value is not None:
        matched_fields = ["contract_number", contract_date, contract_value]
        if _shared_optional_text(current_fields, candidate_fields, "currency"):
            matched_fields.append("currency")
        return _semantic_result(
            candidate_row=candidate_row,
            similarity_score=0.96,
            detection_method="semantic_contract_reference",
            matched_fields=tuple(matched_fields),
        )

    party_a_field = _shared_counterparty_field(
        current_fields,
        candidate_fields,
        exact_fields=(),
        fuzzy_fields=("party_a_name",),
        similarity_threshold=0.96,
    )
    party_b_field = _shared_counterparty_field(
        current_fields,
        candidate_fields,
        exact_fields=(),
        fuzzy_fields=("party_b_name",),
        similarity_threshold=0.96,
    )
    if (
        party_a_field is None
        or party_b_field is None
        or contract_date is None
        or contract_value is None
    ):
        return None
    matched_fields = [party_a_field, party_b_field, contract_date, contract_value]
    if _shared_optional_text(current_fields, candidate_fields, "currency"):
        matched_fields.append("currency")
    return _semantic_result(
        candidate_row=candidate_row,
        similarity_score=0.89,
        detection_method="semantic_contract_summary",
        matched_fields=tuple(matched_fields),
    )


def _semantic_result(
    *,
    candidate_row: DocumentWithExtractionRecord,
    similarity_score: float,
    detection_method: str,
    matched_fields: tuple[str, ...],
) -> DuplicateDetectionResult:
    """Build one semantic duplicate result payload."""

    return DuplicateDetectionResult(
        is_duplicate=True,
        existing_document_id=str(candidate_row.document.id),
        existing_document_filename=candidate_row.document.original_filename,
        similarity_score=similarity_score,
        detection_method=detection_method,
        matched_fields=matched_fields,
    )


def _field_values(row: DocumentWithExtractionRecord) -> dict[str, Any]:
    """Return the latest extracted field values for one document row."""

    extraction = row.latest_extraction
    if extraction is None:
        return {}
    values: dict[str, Any] = {}
    for field in extraction.fields:
        values.setdefault(field.field_name, field.field_value)
    return values


def _shared_counterparty_field(
    current_fields: dict[str, Any],
    candidate_fields: dict[str, Any],
    *,
    exact_fields: tuple[str, ...],
    fuzzy_fields: tuple[str, ...],
    similarity_threshold: float,
) -> str | None:
    """Return the field name that produced a shared counterparty identity."""

    for field_name in exact_fields:
        if _shared_optional_text(current_fields, candidate_fields, field_name):
            return field_name
    for field_name in fuzzy_fields:
        current_value = _preferred_text(current_fields, field_name)
        candidate_value = _preferred_text(candidate_fields, field_name)
        if current_value is None or candidate_value is None:
            continue
        if _text_similarity(current_value, candidate_value) >= similarity_threshold:
            return field_name
    return None


def _shared_optional_text(
    current_fields: dict[str, Any],
    candidate_fields: dict[str, Any],
    *field_names: str,
) -> str | None:
    """Return the field name when both documents share the same normalized text value."""

    for field_name in field_names:
        current_value = _preferred_text(current_fields, field_name)
        candidate_value = _preferred_text(candidate_fields, field_name)
        if current_value is not None and current_value == candidate_value:
            return field_name
    return None


def _shared_decimal_value(
    current_fields: dict[str, Any],
    candidate_fields: dict[str, Any],
    *field_names: str,
) -> str | None:
    """Return the field name when both documents share the same normalized amount."""

    for field_name in field_names:
        current_value = _preferred_decimal(current_fields, field_name)
        candidate_value = _preferred_decimal(candidate_fields, field_name)
        if current_value is not None and current_value == candidate_value:
            return field_name
    return None


def _shared_date_value(
    current_fields: dict[str, Any],
    candidate_fields: dict[str, Any],
    *field_names: str,
) -> str | None:
    """Return the field name when both documents share the same normalized date."""

    for field_name in field_names:
        current_value = _preferred_date(current_fields, field_name)
        candidate_value = _preferred_date(candidate_fields, field_name)
        if current_value is not None and current_value == candidate_value:
            return field_name
    return None


def _shared_statement_line_count(
    current_row: DocumentWithExtractionRecord,
    candidate_row: DocumentWithExtractionRecord,
) -> int | None:
    """Return the shared statement-line count when both extractions expose the same number."""

    current_count = _statement_line_count(current_row)
    candidate_count = _statement_line_count(candidate_row)
    if current_count is not None and current_count == candidate_count:
        return current_count
    return None


def _statement_line_count(row: DocumentWithExtractionRecord) -> int | None:
    """Read the extracted bank-statement line count from the known payload shapes."""

    extraction = row.latest_extraction
    if extraction is None:
        return None
    payload = extraction.extracted_payload
    if not isinstance(payload, dict):
        return None
    parser_output = payload.get("parser_output")
    if not isinstance(parser_output, dict):
        return None
    candidate = parser_output.get("statement_lines")
    if isinstance(candidate, list):
        return len([item for item in candidate if isinstance(item, dict)])
    return None


def _preferred_text(field_values: dict[str, Any], field_name: str) -> str | None:
    """Return one normalized text field value."""

    return _clean_text(field_values.get(field_name))


def _preferred_decimal(field_values: dict[str, Any], field_name: str) -> str | None:
    """Return one normalized decimal field value."""

    return _decimal_to_string(field_values.get(field_name))


def _preferred_date(field_values: dict[str, Any], field_name: str) -> str | None:
    """Return one normalized date field value."""

    return _date_to_string(field_values.get(field_name))


def _clean_text(value: Any) -> str | None:
    """Normalize one possible text value into a stable uppercase comparison key."""

    if value is None:
        return None
    cleaned = " ".join(str(value).strip().upper().replace("_", " ").split())
    return cleaned or None


def _text_similarity(left: str, right: str) -> float:
    """Return a deterministic similarity score for two normalized text fragments."""

    if left in right or right in left:
        return 1.0
    return SequenceMatcher(a=left, b=right).ratio()


def _decimal_to_string(value: Any) -> str | None:
    """Serialize one numeric value into a stable two-decimal string."""

    if value is None:
        return None
    try:
        if isinstance(value, Decimal):
            numeric = value
        else:
            numeric = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return format(numeric.quantize(Decimal("0.01")), "f")


def _date_to_string(value: Any) -> str | None:
    """Serialize one date-like value into ISO form for stable comparisons."""

    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        try:
            return date.fromisoformat(normalized).isoformat()
        except ValueError:
            return normalized
    return None


__all__ = [
    "DuplicateDetectionProtocol",
    "DuplicateDetectionResult",
    "DuplicateDetectionService",
]
