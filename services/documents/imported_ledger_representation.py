"""
Purpose: Detect when a source document is already represented in a bound imported general ledger.
Scope: Conservative duplicate-post suppression for imported-GL close runs so recommendation and
journal flows focus on period adjustments rather than re-booking production ledger activity.
Dependencies: Close-run ledger bindings, imported GL lines, document metadata, and latest
extraction payloads only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from services.common.enums import DocumentType
from services.db.models.documents import Document
from services.db.models.extractions import DocumentExtraction
from services.db.models.ledger import GeneralLedgerImportLine
from services.documents.recommendation_eligibility import is_gl_coding_recommendation_eligible
from services.ledger.effective_ledger import load_close_run_ledger_binding
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

_REFERENCE_FIELD_NAMES_BY_TYPE: dict[DocumentType, tuple[str, ...]] = {
    DocumentType.INVOICE: ("invoice_number",),
    DocumentType.RECEIPT: ("receipt_number",),
    DocumentType.PAYSLIP: ("employee_id",),
}
_NAME_FIELD_NAMES_BY_TYPE: dict[DocumentType, tuple[str, ...]] = {
    DocumentType.INVOICE: ("vendor_name", "customer_name"),
    DocumentType.RECEIPT: ("vendor_name", "customer_name"),
    DocumentType.PAYSLIP: ("employee_name", "employer_name"),
}
_DATE_FIELD_NAMES_BY_TYPE: dict[DocumentType, tuple[str, ...]] = {
    DocumentType.INVOICE: ("invoice_date", "due_date"),
    DocumentType.RECEIPT: ("receipt_date",),
    DocumentType.PAYSLIP: ("pay_date", "pay_period_end", "pay_period_start"),
}
_AMOUNT_FIELD_NAMES_BY_TYPE: dict[DocumentType, tuple[str, ...]] = {
    DocumentType.INVOICE: ("total", "amount_due", "subtotal"),
    DocumentType.RECEIPT: ("total", "subtotal"),
    DocumentType.PAYSLIP: ("net_pay", "gross_pay", "basic_salary"),
}
_STOP_WORD_TOKENS = frozenset(
    {
        "and",
        "company",
        "inc",
        "invoice",
        "limited",
        "llc",
        "ltd",
        "payroll",
        "payment",
        "receipt",
        "salary",
        "service",
        "services",
        "the",
    }
)
_AMOUNT_TOLERANCE = Decimal("0.01")
_DATE_WINDOW_DAYS = 15


@dataclass(frozen=True, slots=True)
class ImportedGeneralLedgerLineCandidate:
    """Describe one imported GL line available for duplicate-post suppression checks."""

    batch_id: UUID
    line_no: int
    posting_date: date
    amount: Decimal
    account_code: str
    account_name: str
    reference: str | None
    external_ref: str | None
    description: str | None

    @property
    def compact_search_text(self) -> str:
        """Return a punctuation-free lowercase search surface for reference matching."""

        return _compact_text(
            " ".join(
                part
                for part in (
                    self.account_code,
                    self.account_name,
                    self.reference,
                    self.external_ref,
                    self.description,
                )
                if part
            )
        )

    @property
    def normalized_search_text(self) -> str:
        """Return a normalized text surface for looser token matching."""

        return _normalize_text(
            " ".join(
                part
                for part in (
                    self.account_code,
                    self.account_name,
                    self.reference,
                    self.external_ref,
                    self.description,
                )
                if part
            )
        )


@dataclass(frozen=True, slots=True)
class ImportedLedgerRepresentationResult:
    """Describe whether one document appears to already exist in the imported GL baseline."""

    document_id: UUID
    represented_in_imported_gl: bool
    status: str
    reason: str
    matched_line_no: int | None = None
    matched_reference: str | None = None
    matched_description: str | None = None
    matched_posting_date: date | None = None


def evaluate_document_imported_gl_representation(
    *,
    session: Session,
    close_run_id: UUID,
    document_id: UUID,
    imported_gl_candidates: tuple[ImportedGeneralLedgerLineCandidate, ...] | None = None,
) -> ImportedLedgerRepresentationResult:
    """Return whether one document is already represented in the bound imported GL baseline."""

    document = session.get(Document, document_id)
    if document is None:
        return ImportedLedgerRepresentationResult(
            document_id=document_id,
            represented_in_imported_gl=False,
            status="document_missing",
            reason="The source document no longer exists.",
        )
    if not is_gl_coding_recommendation_eligible(document.document_type):
        return ImportedLedgerRepresentationResult(
            document_id=document_id,
            represented_in_imported_gl=False,
            status="not_applicable",
            reason="This document type does not use GL-coding recommendation flows.",
        )

    resolved_candidates = (
        imported_gl_candidates
        if imported_gl_candidates is not None
        else load_imported_gl_line_candidates(session=session, close_run_id=close_run_id)
    )
    if not resolved_candidates:
        return ImportedLedgerRepresentationResult(
            document_id=document_id,
            represented_in_imported_gl=False,
            status="no_imported_general_ledger",
            reason="No imported general-ledger baseline is bound to this close run.",
        )

    latest_extraction = _load_latest_extraction(session=session, document_id=document_id)
    if latest_extraction is None:
        return ImportedLedgerRepresentationResult(
            document_id=document_id,
            represented_in_imported_gl=False,
            status="extraction_missing",
            reason="Structured extraction is not available yet for imported-ledger comparison.",
        )

    field_values = _load_extracted_field_values(latest_extraction=latest_extraction)
    payroll_batch_result = _evaluate_payroll_batch_representation(
        session=session,
        close_run_id=close_run_id,
        document_id=document_id,
        document_type=DocumentType(document.document_type),
        field_values=field_values,
        imported_gl_candidates=resolved_candidates,
    )
    if payroll_batch_result is not None:
        return payroll_batch_result
    return _evaluate_document_fields_against_imported_gl(
        document_id=document_id,
        document_type=DocumentType(document.document_type),
        field_values=field_values,
        imported_gl_candidates=resolved_candidates,
    )


def evaluate_documents_imported_gl_representation(
    *,
    session: Session,
    close_run_id: UUID,
    document_ids: tuple[UUID, ...],
) -> dict[UUID, ImportedLedgerRepresentationResult]:
    """Evaluate a batch of documents against the bound imported GL baseline once."""

    if not document_ids:
        return {}

    imported_gl_candidates = load_imported_gl_line_candidates(
        session=session,
        close_run_id=close_run_id,
    )
    return {
        document_id: evaluate_document_imported_gl_representation(
            session=session,
            close_run_id=close_run_id,
            document_id=document_id,
            imported_gl_candidates=imported_gl_candidates,
        )
        for document_id in document_ids
    }


def load_imported_gl_line_candidates(
    *,
    session: Session,
    close_run_id: UUID,
) -> tuple[ImportedGeneralLedgerLineCandidate, ...]:
    """Load the imported GL line candidates bound to one close run."""

    binding = load_close_run_ledger_binding(session, close_run_id)
    if binding is None or binding.general_ledger_import_batch_id is None:
        return ()

    rows = (
        session.execute(
            select(GeneralLedgerImportLine).where(
                GeneralLedgerImportLine.batch_id == binding.general_ledger_import_batch_id
            )
            .order_by(
                GeneralLedgerImportLine.posting_date.asc(),
                GeneralLedgerImportLine.line_no.asc(),
            )
        )
        .scalars()
        .all()
    )
    return tuple(
        ImportedGeneralLedgerLineCandidate(
            batch_id=row.batch_id,
            line_no=row.line_no,
            posting_date=row.posting_date,
            amount=_resolve_line_amount(row.debit_amount, row.credit_amount),
            account_code=row.account_code,
            account_name=row.account_name,
            reference=row.reference,
            external_ref=row.external_ref,
            description=row.description,
        )
        for row in rows
    )


def _evaluate_document_fields_against_imported_gl(
    *,
    document_id: UUID,
    document_type: DocumentType,
    field_values: dict[str, Any],
    imported_gl_candidates: tuple[ImportedGeneralLedgerLineCandidate, ...],
) -> ImportedLedgerRepresentationResult:
    """Evaluate one document field set against imported GL line candidates."""

    amount_candidates = _collect_amount_candidates(
        document_type=document_type,
        field_values=field_values,
    )
    if not amount_candidates:
        return ImportedLedgerRepresentationResult(
            document_id=document_id,
            represented_in_imported_gl=False,
            status="missing_amount_signal",
            reason="No reliable amount signal was available for imported-ledger comparison.",
        )

    compact_reference_candidates = tuple(
        candidate
        for candidate in (
            _compact_text(field_values.get(field_name))
            for field_name in _REFERENCE_FIELD_NAMES_BY_TYPE.get(document_type, ())
        )
        if candidate
    )
    name_tokens = tuple(
        dict.fromkeys(
            token
            for field_name in _NAME_FIELD_NAMES_BY_TYPE.get(document_type, ())
            for token in _name_tokens(field_values.get(field_name))
        )
    )
    date_candidates = tuple(
        candidate
        for candidate in (
            _coerce_date(field_values.get(field_name))
            for field_name in _DATE_FIELD_NAMES_BY_TYPE.get(document_type, ())
        )
        if candidate is not None
    )

    best_match: tuple[ImportedGeneralLedgerLineCandidate, str] | None = None
    for candidate in imported_gl_candidates:
        if not any(
            abs(candidate.amount - amount) <= _AMOUNT_TOLERANCE
            for amount in amount_candidates
        ):
            continue

        has_reference_match = bool(
            compact_reference_candidates
            and any(
                reference_candidate and reference_candidate in candidate.compact_search_text
                for reference_candidate in compact_reference_candidates
            )
        )
        has_name_match = bool(
            name_tokens
            and any(token in candidate.normalized_search_text.split() for token in name_tokens)
        )
        has_date_match = bool(
            date_candidates
            and any(
                abs((candidate.posting_date - document_date).days) <= _DATE_WINDOW_DAYS
                for document_date in date_candidates
            )
        )

        if has_reference_match:
            best_match = (
                candidate,
                (
                    "The imported general ledger already contains a line with the same "
                    "amount and reference."
                ),
            )
            break
        if has_name_match and has_date_match:
            best_match = (
                candidate,
                (
                    "The imported general ledger already contains a line with the same "
                    "amount, date window, and counterparty signal."
                ),
            )
            break

    if best_match is None:
        return ImportedLedgerRepresentationResult(
            document_id=document_id,
            represented_in_imported_gl=False,
            status="not_represented_in_imported_gl",
            reason="No sufficiently grounded imported-ledger match was found for this document.",
        )

    matched_line, reason = best_match
    return ImportedLedgerRepresentationResult(
        document_id=document_id,
        represented_in_imported_gl=True,
        status="represented_in_imported_gl",
        reason=reason,
        matched_line_no=matched_line.line_no,
        matched_reference=matched_line.reference or matched_line.external_ref,
        matched_description=matched_line.description,
        matched_posting_date=matched_line.posting_date,
    )


def _evaluate_payroll_batch_representation(
    *,
    session: Session,
    close_run_id: UUID,
    document_id: UUID,
    document_type: DocumentType,
    field_values: dict[str, Any],
    imported_gl_candidates: tuple[ImportedGeneralLedgerLineCandidate, ...],
) -> ImportedLedgerRepresentationResult | None:
    """Suppress approved payslips when a bound imported GL already carries a payroll batch."""

    if document_type is not DocumentType.PAYSLIP:
        return None

    target_pay_date = _coerce_date(field_values.get("pay_date"))
    target_net_pay = _coerce_decimal(field_values.get("net_pay"))
    if target_pay_date is None or target_net_pay is None or target_net_pay <= Decimal("0.00"):
        return None

    approved_payslip_ids = tuple(
        row.id
        for row in session.execute(
            select(Document.id).where(
                Document.close_run_id == close_run_id,
                Document.status == "approved",
                Document.document_type == DocumentType.PAYSLIP.value,
            )
        ).all()
    )
    if len(approved_payslip_ids) < 2:
        return None

    batch_document_ids: list[UUID] = []
    batch_total = Decimal("0.00")
    for approved_payslip_id in approved_payslip_ids:
        extraction = _load_latest_extraction(
            session=session,
            document_id=approved_payslip_id,
        )
        if extraction is None:
            continue
        extraction_fields = _load_extracted_field_values(latest_extraction=extraction)
        pay_date = _coerce_date(extraction_fields.get("pay_date"))
        net_pay = _coerce_decimal(extraction_fields.get("net_pay"))
        if pay_date != target_pay_date or net_pay is None or net_pay <= Decimal("0.00"):
            continue
        batch_document_ids.append(approved_payslip_id)
        batch_total += net_pay.quantize(_AMOUNT_TOLERANCE)

    if document_id not in batch_document_ids or len(batch_document_ids) < 2:
        return None

    for candidate in imported_gl_candidates:
        if abs(candidate.amount - batch_total) > _AMOUNT_TOLERANCE:
            continue
        if not _looks_like_payroll_batch_line(candidate):
            continue
        if abs((candidate.posting_date - target_pay_date).days) > _DATE_WINDOW_DAYS:
            continue
        return ImportedLedgerRepresentationResult(
            document_id=document_id,
            represented_in_imported_gl=True,
            status="represented_in_imported_gl",
            reason=(
                "The imported general ledger already contains the payroll batch that "
                "covers approved payslips for this pay date."
            ),
            matched_line_no=candidate.line_no,
            matched_reference=candidate.reference or candidate.external_ref,
            matched_description=candidate.description,
            matched_posting_date=candidate.posting_date,
        )

    return None


def _load_latest_extraction(
    *,
    session: Session,
    document_id: UUID,
) -> DocumentExtraction | None:
    """Return the latest extraction row for one document."""

    return session.execute(
        select(DocumentExtraction)
        .where(DocumentExtraction.document_id == document_id)
        .order_by(desc(DocumentExtraction.version_no), desc(DocumentExtraction.created_at))
        .limit(1)
    ).scalar_one_or_none()


def _load_extracted_field_values(*, latest_extraction: DocumentExtraction) -> dict[str, Any]:
    """Return simple field-value pairs from the latest extraction payload."""

    payload = latest_extraction.extracted_payload
    if not isinstance(payload, dict):
        return {}
    fields_list = payload.get("fields")
    if not isinstance(fields_list, list):
        return {}
    field_values: dict[str, Any] = {}
    for field_item in fields_list:
        if not isinstance(field_item, dict):
            continue
        field_name = field_item.get("field_name")
        if not isinstance(field_name, str) or not field_name:
            continue
        field_values[field_name] = field_item.get("field_value")
    return field_values


def _collect_amount_candidates(
    *,
    document_type: DocumentType,
    field_values: dict[str, Any],
) -> tuple[Decimal, ...]:
    """Return positive amount candidates suitable for imported-ledger matching."""

    values: list[Decimal] = []
    for field_name in _AMOUNT_FIELD_NAMES_BY_TYPE.get(document_type, ()):
        candidate = _coerce_decimal(field_values.get(field_name))
        if candidate is None or candidate <= Decimal("0.00"):
            continue
        values.append(candidate.quantize(_AMOUNT_TOLERANCE))
    return tuple(dict.fromkeys(values))


def _resolve_line_amount(debit_amount: Any, credit_amount: Any) -> Decimal:
    """Return the absolute non-zero amount for one imported GL line."""

    debit = _coerce_decimal(debit_amount) or Decimal("0.00")
    credit = _coerce_decimal(credit_amount) or Decimal("0.00")
    if debit > Decimal("0.00"):
        return debit.quantize(_AMOUNT_TOLERANCE)
    return credit.quantize(_AMOUNT_TOLERANCE)


def _coerce_decimal(value: Any) -> Decimal | None:
    """Convert one JSON-like value into a positive-or-zero Decimal when possible."""

    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _coerce_date(value: Any) -> date | None:
    """Coerce one JSON-like value into a date."""

    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _normalize_text(value: Any) -> str:
    """Normalize one value into lowercase alphanumeric space-separated text."""

    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


def _compact_text(value: Any) -> str:
    """Normalize one value into lowercase alphanumeric text without separators."""

    return re.sub(r"[^a-z0-9]+", "", _normalize_text(value))


def _name_tokens(value: Any) -> tuple[str, ...]:
    """Return conservative matching tokens for one counterparty or employee name."""

    normalized = _normalize_text(value)
    if not normalized:
        return ()
    return tuple(
        token
        for token in dict.fromkeys(normalized.split())
        if len(token) >= 4 and token not in _STOP_WORD_TOKENS
    )


def _looks_like_payroll_batch_line(candidate: ImportedGeneralLedgerLineCandidate) -> bool:
    """Return whether one imported GL line clearly looks like a payroll batch posting."""

    search_text = candidate.normalized_search_text
    return any(
        signal in search_text
        for signal in (
            "salary batch",
            "march payroll",
            "payroll",
            "salaries and wages",
            "salary",
        )
    )


__all__ = [
    "ImportedGeneralLedgerLineCandidate",
    "ImportedLedgerRepresentationResult",
    "evaluate_document_imported_gl_representation",
    "evaluate_documents_imported_gl_representation",
    "load_imported_gl_line_candidates",
]
