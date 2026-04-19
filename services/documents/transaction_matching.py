"""
Purpose: Provide deterministic auto-linking between source documents and transaction evidence.
Scope: Match invoices, receipts, and payslips against bank-statement lines already present in
the same close run, then persist the match summary into extraction metadata for UI and agents.
Dependencies: SQLAlchemy ORM models, canonical document enums, and Python standard-library
matching helpers only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Any
from uuid import UUID

from services.common.enums import DocumentType
from services.db.models.documents import Document
from services.db.models.extractions import DocumentExtraction, ExtractedField
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

AUTO_REVIEW_METADATA_KEY = "auto_review"
AUTO_TRANSACTION_MATCH_KEY = "auto_transaction_match"


class AutoTransactionMatchStatus(StrEnum):
    """Enumerate the stable states for auto transaction-linking results."""

    MATCHED = "matched"
    UNMATCHED = "unmatched"
    PENDING_EVIDENCE = "pending_evidence"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class StatementLineCandidate:
    """Describe one extracted bank-statement line available for matching."""

    document_id: UUID
    original_filename: str
    line_no: int
    amount: Decimal | None
    date: date | None
    reference: str | None
    description: str | None


@dataclass(frozen=True, slots=True)
class AutoTransactionMatchResult:
    """Describe the deterministic auto-linking result for one document."""

    status: AutoTransactionMatchStatus
    score: float | None
    match_source: str | None
    matched_document_id: UUID | None
    matched_document_filename: str | None
    matched_line_no: int | None
    matched_reference: str | None
    matched_description: str | None
    matched_date: date | None
    matched_amount: Decimal | None
    reasons: tuple[str, ...]
    extraction_available: bool = True

    @property
    def primary_reason(self) -> str:
        """Return the highest-signal operator message for this match outcome."""

        if self.reasons:
            return self.reasons[0]
        if self.status is AutoTransactionMatchStatus.MATCHED:
            return "A matching transaction was identified automatically."
        if self.status is AutoTransactionMatchStatus.PENDING_EVIDENCE:
            return "Bank-statement evidence has not been uploaded yet for this close run."
        if self.status is AutoTransactionMatchStatus.NOT_APPLICABLE:
            return "Transaction matching is not required for this document type."
        return "No matching transaction was identified automatically."

    @property
    def should_block_collection(self) -> bool:
        """Return whether this outcome should create a collection-phase blocker."""

        return False

    def to_payload(self) -> dict[str, Any]:
        """Serialize the result into JSON-safe extraction metadata."""

        return {
            "status": self.status.value,
            "score": self.score,
            "match_source": self.match_source,
            "matched_document_id": (
                str(self.matched_document_id) if self.matched_document_id is not None else None
            ),
            "matched_document_filename": self.matched_document_filename,
            "matched_line_no": self.matched_line_no,
            "matched_reference": self.matched_reference,
            "matched_description": self.matched_description,
            "matched_date": (
                self.matched_date.isoformat() if self.matched_date is not None else None
            ),
            "matched_amount": (
                _decimal_to_string(self.matched_amount) if self.matched_amount is not None else None
            ),
            "reasons": list(self.reasons),
        }


class TransactionMatchingService:
    """Load extracted evidence and persist one canonical auto transaction-linking result."""

    eligible_document_types = frozenset(
        {
            DocumentType.INVOICE.value,
            DocumentType.RECEIPT.value,
            DocumentType.PAYSLIP.value,
        }
    )

    def __init__(self, *, db_session: Session) -> None:
        self._db_session = db_session

    def evaluate_and_persist(
        self,
        *,
        close_run_id: UUID,
        document_id: UUID,
    ) -> AutoTransactionMatchResult:
        """Evaluate one document against available bank-statement lines and persist the result."""

        document = self._db_session.get(Document, document_id)
        extraction = self._get_latest_extraction(document_id=document_id)
        if document is None or extraction is None:
            return AutoTransactionMatchResult(
                status=AutoTransactionMatchStatus.UNMATCHED,
                score=None,
                match_source=None,
                matched_document_id=None,
                matched_document_filename=None,
                matched_line_no=None,
                matched_reference=None,
                matched_description=None,
                matched_date=None,
                matched_amount=None,
                reasons=("Structured extraction is not available yet for transaction matching.",),
                extraction_available=False,
            )

        field_values = self._load_field_values(extraction_id=extraction.id)
        statement_candidates = self._load_statement_line_candidates(
            close_run_id=close_run_id,
            exclude_document_id=document_id,
        )
        result = evaluate_auto_transaction_match(
            document_type=DocumentType(document.document_type),
            original_filename=document.original_filename,
            field_values=field_values,
            statement_candidates=statement_candidates,
        )
        extraction.extracted_payload = update_extraction_transaction_match_payload(
            extracted_payload=extraction.extracted_payload,
            match_result=result,
        )
        self._db_session.flush()
        return result

    def refresh_close_run_matches(
        self,
        *,
        close_run_id: UUID,
    ) -> dict[UUID, AutoTransactionMatchResult]:
        """Re-evaluate every eligible source document in one close run."""

        document_ids = tuple(
            self._db_session.scalars(
                select(Document.id)
                .where(
                    Document.close_run_id == close_run_id,
                    Document.document_type.in_(tuple(self.eligible_document_types)),
                )
                .order_by(Document.created_at.asc(), Document.id.asc())
            ).all()
        )
        results: dict[UUID, AutoTransactionMatchResult] = {}
        for document_id in document_ids:
            results[document_id] = self.evaluate_and_persist(
                close_run_id=close_run_id,
                document_id=document_id,
            )
        return results

    def _get_latest_extraction(self, *, document_id: UUID) -> DocumentExtraction | None:
        """Return the newest extraction row for one document."""

        statement = (
            select(DocumentExtraction)
            .where(DocumentExtraction.document_id == document_id)
            .order_by(desc(DocumentExtraction.version_no), desc(DocumentExtraction.created_at))
            .limit(1)
        )
        return self._db_session.execute(statement).scalar_one_or_none()

    def _load_field_values(self, *, extraction_id: UUID) -> dict[str, Any]:
        """Load the persisted extracted fields for one extraction as a simple lookup map."""

        rows = self._db_session.execute(
            select(ExtractedField.field_name, ExtractedField.field_value).where(
                ExtractedField.document_extraction_id == extraction_id
            )
        ).all()
        return {row.field_name: row.field_value for row in rows}

    def _load_statement_line_candidates(
        self,
        *,
        close_run_id: UUID,
        exclude_document_id: UUID,
    ) -> tuple[StatementLineCandidate, ...]:
        """Load the latest bank-statement transaction lines available in the close run."""

        bank_rows = self._db_session.execute(
            select(Document, DocumentExtraction)
            .join(DocumentExtraction, DocumentExtraction.document_id == Document.id)
            .where(
                Document.close_run_id == close_run_id,
                Document.id != exclude_document_id,
                Document.document_type == DocumentType.BANK_STATEMENT.value,
            )
            .order_by(
                Document.id.asc(),
                DocumentExtraction.version_no.desc(),
                DocumentExtraction.created_at.desc(),
            )
        ).all()
        latest_by_document_id: dict[UUID, tuple[Document, DocumentExtraction]] = {}
        for document, extraction in bank_rows:
            latest_by_document_id.setdefault(document.id, (document, extraction))

        candidates: list[StatementLineCandidate] = []
        for document, extraction in latest_by_document_id.values():
            for line in _read_statement_lines(extracted_payload=extraction.extracted_payload):
                candidates.append(
                    StatementLineCandidate(
                        document_id=document.id,
                        original_filename=document.original_filename,
                        line_no=line.get("line_no") or 0,
                        amount=_line_amount(line),
                        date=_coerce_date(line.get("date")),
                        reference=_clean_text(line.get("reference")),
                        description=_clean_text(line.get("description")),
                    )
                )
        return tuple(candidates)


def evaluate_auto_transaction_match(
    *,
    document_type: DocumentType,
    original_filename: str,
    field_values: Mapping[str, Any],
    statement_candidates: tuple[StatementLineCandidate, ...],
) -> AutoTransactionMatchResult:
    """Evaluate one extracted document deterministically against bank-statement evidence."""

    if document_type in {DocumentType.BANK_STATEMENT, DocumentType.CONTRACT}:
        return AutoTransactionMatchResult(
            status=AutoTransactionMatchStatus.NOT_APPLICABLE,
            score=None,
            match_source=None,
            matched_document_id=None,
            matched_document_filename=None,
            matched_line_no=None,
            matched_reference=None,
            matched_description=None,
            matched_date=None,
            matched_amount=None,
            reasons=(
                f"{document_type.label} documents do not require separate transaction linking.",
            ),
        )

    if document_type not in {
        DocumentType.INVOICE,
        DocumentType.RECEIPT,
        DocumentType.PAYSLIP,
    }:
        return AutoTransactionMatchResult(
            status=AutoTransactionMatchStatus.UNMATCHED,
            score=None,
            match_source=None,
            matched_document_id=None,
            matched_document_filename=None,
            matched_line_no=None,
            matched_reference=None,
            matched_description=None,
            matched_date=None,
            matched_amount=None,
            reasons=(
                f"{document_type.label} is not yet supported by the deterministic auto-linker.",
            ),
        )

    target_amount = _resolve_target_amount(document_type=document_type, field_values=field_values)
    target_date = _resolve_target_date(document_type=document_type, field_values=field_values)
    reference_tokens = _resolve_reference_tokens(
        document_type=document_type,
        field_values=field_values,
        original_filename=original_filename,
    )

    if target_amount is None:
        return AutoTransactionMatchResult(
            status=AutoTransactionMatchStatus.UNMATCHED,
            score=None,
            match_source=None,
            matched_document_id=None,
            matched_document_filename=None,
            matched_line_no=None,
            matched_reference=None,
            matched_description=None,
            matched_date=None,
            matched_amount=None,
            reasons=("No extracted monetary amount is available for auto transaction-linking.",),
        )

    if not statement_candidates:
        return AutoTransactionMatchResult(
            status=AutoTransactionMatchStatus.PENDING_EVIDENCE,
            score=None,
            match_source=None,
            matched_document_id=None,
            matched_document_filename=None,
            matched_line_no=None,
            matched_reference=None,
            matched_description=None,
            matched_date=None,
            matched_amount=None,
            reasons=(
                "Bank-statement evidence has not been uploaded yet, so transaction linking is "
                "deferred for now.",
            ),
        )

    ranked_candidates = sorted(
        (
            _score_statement_candidate(
                target_amount=target_amount,
                target_date=target_date,
                reference_tokens=reference_tokens,
                candidate=candidate,
            )
            for candidate in statement_candidates
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    best_score, best_reasons, best_candidate = ranked_candidates[0]
    runner_up_score = ranked_candidates[1][0] if len(ranked_candidates) > 1 else None
    if best_score < 0.72:
        return AutoTransactionMatchResult(
            status=AutoTransactionMatchStatus.UNMATCHED,
            score=round(best_score, 4),
            match_source="bank_statement_line",
            matched_document_id=None,
            matched_document_filename=None,
            matched_line_no=None,
            matched_reference=None,
            matched_description=None,
            matched_date=None,
            matched_amount=None,
            reasons=(
                "No bank-statement line met the deterministic transaction-link threshold.",
                *best_reasons,
            ),
        )
    if runner_up_score is not None and abs(best_score - runner_up_score) < 0.05:
        return AutoTransactionMatchResult(
            status=AutoTransactionMatchStatus.UNMATCHED,
            score=round(best_score, 4),
            match_source="bank_statement_line",
            matched_document_id=None,
            matched_document_filename=None,
            matched_line_no=None,
            matched_reference=None,
            matched_description=None,
            matched_date=None,
            matched_amount=None,
            reasons=(
                "Multiple bank-statement lines are similarly plausible; human review is required.",
            ),
        )

    return AutoTransactionMatchResult(
        status=AutoTransactionMatchStatus.MATCHED,
        score=round(best_score, 4),
        match_source="bank_statement_line",
        matched_document_id=best_candidate.document_id,
        matched_document_filename=best_candidate.original_filename,
        matched_line_no=best_candidate.line_no,
        matched_reference=best_candidate.reference,
        matched_description=best_candidate.description,
        matched_date=best_candidate.date,
        matched_amount=best_candidate.amount,
        reasons=tuple(best_reasons),
    )


def update_extraction_transaction_match_payload(
    *,
    extracted_payload: Mapping[str, Any] | None,
    match_result: AutoTransactionMatchResult,
) -> dict[str, Any]:
    """Persist the auto transaction-linking result into one extraction payload."""

    payload = dict(extracted_payload) if isinstance(extracted_payload, Mapping) else {}
    payload[AUTO_TRANSACTION_MATCH_KEY] = match_result.to_payload()
    return payload


def update_extraction_auto_review_payload(
    *,
    extracted_payload: Mapping[str, Any] | None,
    auto_approved: bool,
    autonomy_mode: str,
    reasons: tuple[str, ...],
) -> dict[str, Any]:
    """Persist the collection-phase auto-review outcome into one extraction payload."""

    payload = dict(extracted_payload) if isinstance(extracted_payload, Mapping) else {}
    payload[AUTO_REVIEW_METADATA_KEY] = {
        "auto_approved": auto_approved,
        "autonomy_mode": autonomy_mode,
        "reasons": list(reasons),
        "evaluated_at": datetime.now().date().isoformat(),
    }
    return payload


def extract_auto_transaction_match_metadata(
    extracted_payload: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Return the persisted auto transaction-linking payload when present."""

    if not isinstance(extracted_payload, Mapping):
        return None
    metadata = extracted_payload.get(AUTO_TRANSACTION_MATCH_KEY)
    return metadata if isinstance(metadata, Mapping) else None


def extract_auto_review_metadata(
    extracted_payload: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Return the persisted auto-review metadata when present."""

    if not isinstance(extracted_payload, Mapping):
        return None
    metadata = extracted_payload.get(AUTO_REVIEW_METADATA_KEY)
    return metadata if isinstance(metadata, Mapping) else None


def _read_statement_lines(
    *,
    extracted_payload: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], ...]:
    """Read bank-statement lines from the known extraction payload shapes."""

    if not isinstance(extracted_payload, Mapping):
        return ()
    for candidate in (
        extracted_payload.get("lines"),
        extracted_payload.get("statement_lines"),
        (extracted_payload.get("parser_output") or {}).get("lines")
        if isinstance(extracted_payload.get("parser_output"), Mapping)
        else None,
        (extracted_payload.get("parser_output") or {}).get("statement_lines")
        if isinstance(extracted_payload.get("parser_output"), Mapping)
        else None,
    ):
        if isinstance(candidate, list):
            return tuple(item for item in candidate if isinstance(item, dict))
    return ()


def _resolve_target_amount(
    *,
    document_type: DocumentType,
    field_values: Mapping[str, Any],
) -> Decimal | None:
    """Resolve the canonical amount field for one reviewable document type."""

    preferred_fields = {
        DocumentType.INVOICE: ("total", "subtotal"),
        DocumentType.RECEIPT: ("total", "subtotal"),
        DocumentType.PAYSLIP: ("net_pay", "gross_pay"),
    }.get(document_type, ())
    for field_name in preferred_fields:
        amount = _coerce_decimal(field_values.get(field_name))
        if amount is not None:
            return abs(amount)
    return None


def _resolve_target_date(
    *,
    document_type: DocumentType,
    field_values: Mapping[str, Any],
) -> date | None:
    """Resolve the canonical transaction date used for candidate ranking."""

    preferred_fields = {
        DocumentType.INVOICE: ("invoice_date", "due_date"),
        DocumentType.RECEIPT: ("receipt_date",),
        DocumentType.PAYSLIP: ("pay_date", "pay_period_end", "pay_period_start"),
    }.get(document_type, ())
    for field_name in preferred_fields:
        parsed_date = _coerce_date(field_values.get(field_name))
        if parsed_date is not None:
            return parsed_date
    return None


def _resolve_reference_tokens(
    *,
    document_type: DocumentType,
    field_values: Mapping[str, Any],
    original_filename: str,
) -> tuple[str, ...]:
    """Resolve deterministic reference/name tokens for candidate scoring."""

    raw_tokens: list[Any] = [original_filename]
    if document_type is DocumentType.INVOICE:
        raw_tokens.extend(
            [
                field_values.get("invoice_number"),
                field_values.get("vendor_name"),
                field_values.get("customer_name"),
            ]
        )
    elif document_type is DocumentType.RECEIPT:
        raw_tokens.extend(
            [
                field_values.get("receipt_number"),
                field_values.get("vendor_name"),
                field_values.get("customer_name"),
            ]
        )
    elif document_type is DocumentType.PAYSLIP:
        raw_tokens.extend(
            [
                field_values.get("employee_id"),
                field_values.get("employee_name"),
                field_values.get("employer_name"),
            ]
        )

    normalized: list[str] = []
    for token in raw_tokens:
        cleaned = _clean_text(token)
        if cleaned is None:
            continue
        if cleaned not in normalized:
            normalized.append(cleaned)
    return tuple(normalized)


def _score_statement_candidate(
    *,
    target_amount: Decimal,
    target_date: date | None,
    reference_tokens: tuple[str, ...],
    candidate: StatementLineCandidate,
) -> tuple[float, tuple[str, ...], StatementLineCandidate]:
    """Score one bank-statement candidate line against the document facts."""

    score = 0.0
    reasons: list[str] = []

    if candidate.amount is not None:
        amount_delta = abs(abs(candidate.amount) - abs(target_amount))
        if amount_delta <= Decimal("0.01"):
            score += 0.65
            reasons.append("Amount matches the extracted document total.")
        elif amount_delta <= Decimal("1.00"):
            score += 0.45
            reasons.append("Amount is within the deterministic tolerance band.")
    if target_date is not None and candidate.date is not None:
        day_delta = abs((candidate.date - target_date).days)
        if day_delta == 0:
            score += 0.2
            reasons.append("Transaction date matches exactly.")
        elif day_delta <= 3:
            score += 0.15
            reasons.append("Transaction date is within three days of the document date.")
        elif day_delta <= 7:
            score += 0.08
            reasons.append("Transaction date is within one week of the document date.")

    candidate_text = " ".join(
        value for value in (candidate.reference, candidate.description) if value
    )
    best_similarity = 0.0
    for token in reference_tokens:
        similarity = _text_similarity(token, candidate_text)
        if similarity > best_similarity:
            best_similarity = similarity
    if best_similarity >= 0.95:
        score += 0.2
        reasons.append("Reference or party name matches the bank-statement narration exactly.")
    elif best_similarity >= 0.8:
        score += 0.12
        reasons.append("Reference or party name closely matches the bank-statement narration.")
    elif best_similarity >= 0.65:
        score += 0.06
        reasons.append("Reference or party name partially matches the bank-statement narration.")

    return score, tuple(reasons), candidate


def _line_amount(line: Mapping[str, Any]) -> Decimal | None:
    """Resolve the absolute amount for one bank-statement line payload."""

    for key in ("amount", "debit", "credit"):
        amount = _coerce_decimal(line.get(key))
        if amount is not None:
            return abs(amount)
    return None


def _coerce_decimal(value: Any) -> Decimal | None:
    """Convert JSON-like numeric values into Decimal safely."""

    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _coerce_date(value: Any) -> date | None:
    """Convert extracted JSON values into date objects safely."""

    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _clean_text(value: Any) -> str | None:
    """Normalize one possible reference token into a stable uppercase key."""

    if value is None:
        return None
    cleaned = " ".join(str(value).strip().upper().replace("_", " ").split())
    return cleaned or None


def _text_similarity(left: str, right: str | None) -> float:
    """Return a deterministic similarity score for two normalized text fragments."""

    normalized_right = _clean_text(right)
    if not left or normalized_right is None:
        return 0.0
    if left in normalized_right or normalized_right in left:
        return 1.0
    return SequenceMatcher(a=left, b=normalized_right).ratio()


def _decimal_to_string(value: Decimal) -> str:
    """Serialize one Decimal into a stable plain-string JSON value."""

    return format(value.quantize(Decimal("0.01")), "f")


__all__ = [
    "AUTO_REVIEW_METADATA_KEY",
    "AUTO_TRANSACTION_MATCH_KEY",
    "AutoTransactionMatchResult",
    "AutoTransactionMatchStatus",
    "TransactionMatchingService",
    "evaluate_auto_transaction_match",
    "extract_auto_review_metadata",
    "extract_auto_transaction_match_metadata",
    "update_extraction_auto_review_payload",
    "update_extraction_transaction_match_payload",
]
