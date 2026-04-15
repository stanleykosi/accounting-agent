"""
Purpose: Execute the deterministic parser pipeline for uploaded close-run documents.
Scope: Celery task registration, source-object download, PDF/OCR/spreadsheet parser
selection, derivative storage, document-version persistence, status transitions, and
worker audit events.
Dependencies: Celery worker app, document repository, storage repository, parser adapters,
and shared observability context.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from uuid import UUID

from apps.worker.app.celery_app import celery_app
from apps.worker.app.tasks.base import JobRuntimeContext, TrackedJobTask
from apps.worker.app.tasks.document_quality_checks import run_document_quality_checks
from services.common.enums import AutonomyMode, DocumentStatus, DocumentType
from services.common.logging import get_logger
from services.common.types import JsonObject
from services.contracts.storage_models import CloseRunStorageScope, DerivativeKind
from services.db.models.audit import AuditSourceSurface
from services.db.models.documents import Document
from services.db.models.extractions import DocumentExtraction, DocumentLineItem, ExtractedField
from services.db.repositories.document_repo import DocumentRepository, ParseDocumentRecord
from services.db.repositories.entity_repo import EntityRepository
from services.db.session import get_session_factory
from services.documents.transaction_matching import update_extraction_auto_review_payload
from services.extraction.field_extractors import (
    compute_confidence_summary,
    extract_fields_by_document_type,
)
from services.extraction.schemas import DocumentLineItem as SchemaDocumentLineItem
from services.jobs.retry_policy import BlockedJobError
from services.jobs.service import JobService, JobServiceError
from services.jobs.task_names import TaskName, resolve_task_route
from services.observability.context import current_trace_metadata
from services.parser.document_splitter import infer_document_type_from_text
from services.parser.models import ParserPipelineError, ParserResult, ParserSourceDocument
from services.parser.ocr_router import OcrRouter
from services.parser.pdf_parser import parse_pdf_document
from services.parser.spreadsheet_parser import parse_spreadsheet_document
from services.storage.checksums import compute_sha256_text
from services.storage.repository import StorageRepository

logger = get_logger(__name__)

_DATE_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{2}[/-]\d{2}[/-]\d{4}\b")
_CURRENCY_PATTERN = re.compile(r"\b(NGN|USD|GBP|EUR|CAD|AUD|ZAR)\b", re.IGNORECASE)
_TEXT_FIELD_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "invoice_number": (
        re.compile(r"\binvoice\s*(?:number|no\.?|#)\s*[:#-]?\s*([A-Z0-9./-]+)", re.IGNORECASE),
        re.compile(r"\bref(?:erence)?\s*[:#-]?\s*([A-Z0-9./-]+)", re.IGNORECASE),
    ),
    "receipt_number": (
        re.compile(r"\breceipt\s*(?:number|no\.?|#)\s*[:#-]?\s*([A-Z0-9./-]+)", re.IGNORECASE),
        re.compile(r"\bref(?:erence)?\s*[:#-]?\s*([A-Z0-9./-]+)", re.IGNORECASE),
    ),
    "contract_number": (
        re.compile(r"\bcontract\s*(?:number|no\.?|#)\s*[:#-]?\s*([A-Z0-9./-]+)", re.IGNORECASE),
    ),
    "vendor_name": (
        re.compile(r"\b(?:vendor|supplier|payee)\s*[:#-]?\s*(.+)", re.IGNORECASE),
    ),
    "customer_name": (
        re.compile(r"\bcustomer\s*[:#-]?\s*(.+)", re.IGNORECASE),
    ),
    "employee_name": (
        re.compile(r"\bemployee\s*(?:name)?\s*[:#-]?\s*(.+)", re.IGNORECASE),
    ),
    "employee_id": (
        re.compile(r"\bemployee\s*(?:id|number|no\.?)\s*[:#-]?\s*([A-Z0-9./-]+)", re.IGNORECASE),
    ),
    "employer_name": (
        re.compile(r"\bemployer\s*(?:name)?\s*[:#-]?\s*(.+)", re.IGNORECASE),
    ),
    "bank_name": (
        re.compile(r"\bbank\s+name\s*[:#-]?\s*(.+)", re.IGNORECASE),
    ),
    "account_name": (
        re.compile(r"\baccount\s*name\s*[:#-]?\s*(.+)", re.IGNORECASE),
    ),
    "account_number": (
        re.compile(r"\baccount\s*(?:number|no\.?)\s*[:#-]?\s*([A-Z0-9./-]+)", re.IGNORECASE),
    ),
    "party_a_name": (
        re.compile(r"\bparty\s*a\s*[:#-]?\s*(.+)", re.IGNORECASE),
    ),
    "party_b_name": (
        re.compile(r"\bparty\s*b\s*[:#-]?\s*(.+)", re.IGNORECASE),
    ),
}
_COLUMN_ALIASES_BY_FIELD: dict[str, tuple[str, ...]] = {
    "vendor_name": ("vendor", "supplier", "vendor_name", "supplier_name", "payee"),
    "vendor_address": ("vendor_address", "supplier_address", "address"),
    "vendor_tax_id": ("vendor_tax_id", "supplier_tax_id", "tax_id", "vat_number", "tin"),
    "customer_name": ("customer", "customer_name", "client", "buyer"),
    "customer_tax_id": ("customer_tax_id", "customer_vat_number"),
    "invoice_number": ("invoice_number", "invoice_no", "invoice", "reference", "ref"),
    "invoice_date": ("invoice_date", "date"),
    "due_date": ("due_date", "payment_due_date"),
    "currency": ("currency", "ccy"),
    "subtotal": ("subtotal", "sub_total", "net_amount"),
    "tax_amount": ("tax", "tax_amount", "vat", "vat_amount"),
    "tax_rate": ("tax_rate", "vat_rate"),
    "discount_amount": ("discount", "discount_amount"),
    "total": ("total", "amount", "gross_amount"),
    "payment_terms": ("payment_terms", "terms"),
    "notes": ("notes", "memo"),
    "bank_name": ("bank_name", "bank"),
    "account_number": ("account_number", "account_no"),
    "account_name": ("account_name",),
    "statement_start_date": ("statement_start_date", "period_start", "start_date", "from"),
    "statement_end_date": ("statement_end_date", "period_end", "end_date", "to"),
    "opening_balance": ("opening_balance",),
    "closing_balance": ("closing_balance",),
    "total_credits": ("total_credits", "credits_total"),
    "total_debits": ("total_debits", "debits_total"),
    "employee_name": ("employee_name", "employee"),
    "employee_id": ("employee_id", "staff_id"),
    "employer_name": ("employer_name", "company", "company_name"),
    "pay_period_start": ("pay_period_start", "period_start", "start_date"),
    "pay_period_end": ("pay_period_end", "period_end", "end_date"),
    "pay_date": ("pay_date", "payment_date", "date"),
    "basic_salary": ("basic_salary", "basic"),
    "allowances": ("allowances", "allowance"),
    "deductions": ("deductions", "deduction"),
    "gross_pay": ("gross_pay", "gross"),
    "net_pay": ("net_pay", "net"),
    "paye_tax": ("paye_tax", "tax"),
    "pension_contribution": ("pension_contribution", "pension"),
    "receipt_number": ("receipt_number", "receipt_no", "receipt", "reference", "ref"),
    "receipt_date": ("receipt_date", "date"),
    "payment_method": ("payment_method", "method"),
    "contract_number": ("contract_number", "contract_no", "contract"),
    "contract_date": ("contract_date", "date"),
    "effective_date": ("effective_date",),
    "expiration_date": ("expiration_date", "expiry_date", "end_date"),
    "party_a_name": ("party_a_name", "party_a"),
    "party_b_name": ("party_b_name", "party_b"),
    "contract_value": ("contract_value", "value", "amount", "total"),
    "contract_type": ("contract_type", "type"),
    "terms": ("terms",),
    "renewal_terms": ("renewal_terms",),
    "termination_terms": ("termination_terms",),
}
_LINE_ITEM_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "description": ("description", "item", "details", "narration"),
    "quantity": ("quantity", "qty"),
    "unit_price": ("unit_price", "unit price", "price"),
    "amount": ("amount", "line_total", "total"),
    "tax_amount": ("tax_amount", "tax", "vat"),
}
_BANK_STATEMENT_LINE_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "transaction_date", "value_date"),
    "description": ("description", "narration", "details"),
    "reference": ("reference", "ref", "transaction_reference"),
    "debit": ("debit", "withdrawal"),
    "credit": ("credit", "deposit"),
    "amount": ("amount",),
    "balance": ("balance", "running_balance"),
}
_RECONCILIATION_RECOMMENDATION_ELIGIBLE_TYPES = frozenset(
    {
        DocumentType.INVOICE,
        DocumentType.BANK_STATEMENT,
        DocumentType.PAYSLIP,
        DocumentType.RECEIPT,
    }
)


@dataclass(frozen=True, slots=True)
class StoredParseDerivatives:
    """Describe object keys generated while storing parser derivatives."""

    normalized_storage_key: str | None
    ocr_text_storage_key: str | None
    extracted_tables_storage_key: str | None


class StorageRepositoryProtocol(Protocol):
    """Describe the storage methods consumed by the parser pipeline."""

    def download_source_document(self, *, storage_key: str) -> bytes:
        """Download original source bytes from canonical document storage."""

    def store_ocr_text(
        self,
        *,
        scope: CloseRunStorageScope,
        document_id: UUID,
        document_version_no: int,
        source_filename: str,
        text: str,
        content_type: str = "text/plain; charset=utf-8",
        expected_sha256: str | None = None,
    ) -> object:
        """Store OCR text and return derivative metadata."""

    def store_derivative(
        self,
        *,
        scope: CloseRunStorageScope,
        document_id: UUID,
        document_version_no: int,
        derivative_kind: DerivativeKind,
        filename: str,
        payload: bytes,
        content_type: str,
        expected_sha256: str | None = None,
    ) -> object:
        """Store a normalized derivative and return derivative metadata."""


@dataclass(frozen=True, slots=True)
class WorkerTaskReceipt:
    """Describe one Celery task dispatch initiated from within a worker task."""

    task_id: str
    task_name: str
    queue_name: str
    routing_key: str
    trace_id: str | None


class WorkerTaskDispatcher:
    """Dispatch follow-on tasks from worker code while preserving canonical routing."""

    def dispatch_task(
        self,
        *,
        task_name: TaskName | str,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        countdown: int | None = None,
        task_id: str | None = None,
    ) -> WorkerTaskReceipt:
        """Publish a JSON-safe follow-on task into the correct queue."""

        route = resolve_task_route(task_name)
        async_result = celery_app.send_task(
            str(task_name),
            args=tuple(args or ()),
            kwargs=dict(kwargs or {}),
            countdown=countdown,
            queue=route.queue.value,
            routing_key=route.routing_key,
            task_id=task_id,
        )
        return WorkerTaskReceipt(
            task_id=str(async_result.id),
            task_name=str(task_name),
            queue_name=route.queue.value,
            routing_key=route.routing_key,
            trace_id=current_trace_metadata().trace_id,
        )


def _derive_document_classification(
    raw_parse_payload: JsonObject,
) -> tuple[DocumentType, float | None]:
    """Infer the best-effort document type and confidence from parser output."""

    split_candidates = raw_parse_payload.get("split_candidates")
    if isinstance(split_candidates, list) and split_candidates:
        ranked_candidates = sorted(
            (
                candidate
                for candidate in split_candidates
                if isinstance(candidate, dict)
                and isinstance(candidate.get("document_type_hint"), str)
            ),
            key=lambda candidate: float(candidate.get("confidence") or 0.0),
            reverse=True,
        )
        for candidate in ranked_candidates:
            try:
                candidate_type = DocumentType(str(candidate["document_type_hint"]))
            except ValueError:
                continue
            if candidate_type is not DocumentType.UNKNOWN:
                confidence = float(candidate.get("confidence") or 0.0)
                return candidate_type, confidence

    raw_text = raw_parse_payload.get("text")
    if isinstance(raw_text, str) and raw_text.strip():
        inferred_type = infer_document_type_from_text(raw_text)
        if inferred_type is not DocumentType.UNKNOWN:
            return inferred_type, 0.65

    pages = raw_parse_payload.get("pages")
    if isinstance(pages, list):
        page_text = " ".join(
            page.get("text", "")
            for page in pages
            if isinstance(page, dict) and isinstance(page.get("text"), str)
        ).strip()
        if page_text:
            inferred_type = infer_document_type_from_text(page_text)
            if inferred_type is not DocumentType.UNKNOWN:
                return inferred_type, 0.55

    return DocumentType.UNKNOWN, None


def _derive_document_period(
    *,
    document_type: DocumentType,
    fields: list[Any],
) -> tuple[date | None, date | None]:
    """Map extracted field values into one detected document-period window."""

    field_values = {field.field_name: field.field_value for field in fields}

    def _as_date(value: Any) -> date | None:
        return value if isinstance(value, date) else None

    if document_type is DocumentType.BANK_STATEMENT:
        return (
            _as_date(field_values.get("statement_start_date")),
            _as_date(field_values.get("statement_end_date")),
        )
    if document_type is DocumentType.PAYSLIP:
        period_start = _as_date(field_values.get("pay_period_start"))
        period_end = _as_date(field_values.get("pay_period_end"))
        pay_date = _as_date(field_values.get("pay_date"))
        return (period_start or pay_date, period_end or pay_date)
    if document_type is DocumentType.CONTRACT:
        period_start = _as_date(field_values.get("effective_date"))
        period_end = _as_date(field_values.get("expiration_date"))
        contract_date = _as_date(field_values.get("contract_date"))
        return (period_start or contract_date, period_end or contract_date)
    if document_type is DocumentType.INVOICE:
        invoice_date = _as_date(field_values.get("invoice_date"))
        return (invoice_date, invoice_date)
    if document_type is DocumentType.RECEIPT:
        receipt_date = _as_date(field_values.get("receipt_date"))
        return (receipt_date, receipt_date)
    return (None, None)


def _build_extraction_parser_output(
    *,
    raw_parse_payload: JsonObject,
    document_type: DocumentType,
) -> dict[str, Any]:
    """Normalize raw parser output into the field-centric shape extractors expect."""

    pages = raw_parse_payload.get("pages")
    rows = _collect_table_rows(raw_parse_payload=raw_parse_payload)
    raw_fields = _build_raw_fields_for_document_type(
        document_type=document_type,
        rows=rows,
        raw_parse_payload=raw_parse_payload,
    )
    source_type = "parser"
    if _raw_payload_requires_ocr(raw_parse_payload) or (
        isinstance(pages, list)
        and any(
            isinstance(page, dict) and page.get("extraction_method") == "ocr"
            for page in pages
        )
    ):
        source_type = "ocr"
    parser_output: dict[str, Any] = {
        "source_type": source_type,
        "fields": raw_fields,
        "raw_fields": dict(raw_fields),
        "field_locations": {},
    }

    if document_type is DocumentType.BANK_STATEMENT:
        parser_output["statement_lines"] = _build_statement_lines(rows=rows)
    else:
        parser_output["line_items"] = _build_document_line_items(rows=rows)

    return parser_output


def _build_raw_fields_for_document_type(
    *,
    document_type: DocumentType,
    rows: list[dict[str, str]],
    raw_parse_payload: JsonObject,
) -> dict[str, Any]:
    """Project raw parser tables/text into field names consumed by the extractor."""

    text = _collect_parser_text(raw_parse_payload=raw_parse_payload)
    raw_fields: dict[str, Any] = {}

    if document_type is DocumentType.INVOICE:
        field_names = (
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
        )
    elif document_type is DocumentType.BANK_STATEMENT:
        field_names = (
            "bank_name",
            "account_number",
            "account_name",
            "statement_start_date",
            "statement_end_date",
            "opening_balance",
            "closing_balance",
            "total_credits",
            "total_debits",
            "currency",
        )
    elif document_type is DocumentType.PAYSLIP:
        field_names = (
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
            "currency",
            "paye_tax",
            "pension_contribution",
        )
    elif document_type is DocumentType.RECEIPT:
        field_names = (
            "receipt_number",
            "receipt_date",
            "vendor_name",
            "vendor_tax_id",
            "customer_name",
            "currency",
            "subtotal",
            "tax_amount",
            "total",
            "payment_method",
        )
    elif document_type is DocumentType.CONTRACT:
        field_names = (
            "contract_number",
            "contract_date",
            "effective_date",
            "expiration_date",
            "party_a_name",
            "party_b_name",
            "contract_value",
            "currency",
            "contract_type",
            "terms",
            "renewal_terms",
            "termination_terms",
        )
    else:
        return raw_fields

    for field_name in field_names:
        value = _resolve_field_value(
            field_name=field_name,
            rows=rows,
            text=text,
        )
        if value is not None:
            raw_fields[field_name] = value

    if document_type is DocumentType.BANK_STATEMENT:
        statement_lines = _build_statement_lines(rows=rows)
        if statement_lines:
            dates = [
                line["date"]
                for line in statement_lines
                if isinstance(line.get("date"), str) and line["date"]
            ]
            debits = [_coerce_decimal(line.get("debit")) for line in statement_lines]
            credits = [_coerce_decimal(line.get("credit")) for line in statement_lines]
            balances = [_coerce_decimal(line.get("balance")) for line in statement_lines]
            raw_fields.setdefault("statement_start_date", min(dates) if dates else None)
            raw_fields.setdefault("statement_end_date", max(dates) if dates else None)
            debit_total = sum(
                (amount for amount in debits if amount is not None),
                Decimal("0.00"),
            )
            credit_total = sum(
                (amount for amount in credits if amount is not None),
                Decimal("0.00"),
            )
            raw_fields.setdefault(
                "total_debits",
                _decimal_to_string(debit_total),
            )
            raw_fields.setdefault(
                "total_credits",
                _decimal_to_string(credit_total),
            )
            if balances:
                available_balances = [balance for balance in balances if balance is not None]
                if available_balances:
                    raw_fields.setdefault(
                        "opening_balance",
                        _decimal_to_string(available_balances[0]),
                    )
                    raw_fields.setdefault(
                        "closing_balance",
                        _decimal_to_string(available_balances[-1]),
                    )

    return {key: value for key, value in raw_fields.items() if value not in {None, ""}}


def _collect_table_rows(*, raw_parse_payload: JsonObject) -> list[dict[str, str]]:
    """Flatten parsed tables into row dictionaries with normalized header aliases."""

    tables = raw_parse_payload.get("tables")
    if not isinstance(tables, list):
        return []

    rows: list[dict[str, str]] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        raw_rows = table.get("rows")
        if not isinstance(raw_rows, list):
            continue
        for raw_row in raw_rows:
            if not isinstance(raw_row, dict):
                continue
            normalized_row: dict[str, str] = {}
            for key, value in raw_row.items():
                normalized_row[_normalize_column_key(str(key))] = str(value).strip()
            rows.append(normalized_row)
    return rows


def _collect_parser_text(*, raw_parse_payload: JsonObject) -> str:
    """Return flattened parser text for regex-based fallback extraction."""

    text_fragments: list[str] = []
    raw_text = raw_parse_payload.get("text")
    if isinstance(raw_text, str) and raw_text.strip():
        text_fragments.append(raw_text)
    pages = raw_parse_payload.get("pages")
    if isinstance(pages, list):
        for page in pages:
            if (
                isinstance(page, dict)
                and isinstance(page.get("text"), str)
                and page["text"].strip()
            ):
                text_fragments.append(page["text"])
    return "\n".join(fragment for fragment in text_fragments if fragment).strip()


def _resolve_field_value(
    *,
    field_name: str,
    rows: list[dict[str, str]],
    text: str,
) -> str | None:
    """Resolve one field value from table aliases first, then text regex fallbacks."""

    aliases = _COLUMN_ALIASES_BY_FIELD.get(field_name, ())
    for row in rows:
        for alias in aliases:
            value = row.get(_normalize_column_key(alias))
            if value:
                return value

    if field_name in _TEXT_FIELD_PATTERNS:
        for pattern in _TEXT_FIELD_PATTERNS[field_name]:
            match = pattern.search(text)
            if match:
                value = match.group(1).strip()
                if value:
                    return value

    if field_name.endswith("_date"):
        match = _DATE_PATTERN.search(text)
        if match:
            return match.group(0)

    if field_name == "currency":
        match = _CURRENCY_PATTERN.search(text)
        if match:
            return match.group(1).upper()

    if field_name in {"total", "subtotal", "tax_amount", "gross_pay", "net_pay", "contract_value"}:
        amount = _extract_amount_from_text(text)
        if amount is not None:
            return amount

    return None


def _build_document_line_items(*, rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Translate parser table rows into structured line-item payloads when possible."""

    line_items: list[dict[str, Any]] = []
    for row in rows:
        line_item: dict[str, Any] = {
            "line_no": len(line_items) + 1,
            "evidence_ref": {},
            "dimensions": {},
        }
        populated = False
        for target_field, aliases in _LINE_ITEM_COLUMN_ALIASES.items():
            value = _find_first_row_value(row=row, aliases=aliases)
            if value is None:
                continue
            line_item[target_field] = value
            populated = True
        if populated and any(key in line_item for key in ("description", "amount", "quantity")):
            line_items.append(line_item)
    return line_items


def _build_statement_lines(*, rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Translate parser table rows into bank-statement line payloads."""

    statement_lines: list[dict[str, Any]] = []
    for row in rows:
        line_payload: dict[str, Any] = {
            "line_no": len(statement_lines) + 1,
            "evidence_ref": {},
        }
        for target_field, aliases in _BANK_STATEMENT_LINE_ALIASES.items():
            value = _find_first_row_value(row=row, aliases=aliases)
            if value is None:
                continue
            line_payload[target_field] = value

        if not any(field in line_payload for field in ("debit", "credit", "amount", "balance")):
            continue
        if "date" not in line_payload and "description" not in line_payload:
            continue
        statement_lines.append(line_payload)
    return statement_lines


def _find_first_row_value(*, row: dict[str, str], aliases: tuple[str, ...]) -> str | None:
    """Return the first populated value for the supplied aliases from one normalized row."""

    for alias in aliases:
        value = row.get(_normalize_column_key(alias))
        if value:
            return value
    return None


def _normalize_column_key(value: str) -> str:
    """Normalize free-form table headers into stable lookup keys."""

    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return normalized


def _extract_amount_from_text(text: str) -> str | None:
    """Return the first decimal-like amount found in free-form parser text."""

    for token in re.findall(r"-?\d[\d,]*\.?\d*", text):
        normalized = token.replace(",", "").strip()
        if normalized in {"", "-", "."}:
            continue
        try:
            return _decimal_to_string(Decimal(normalized))
        except InvalidOperation:
            continue
    return None


def _coerce_decimal(value: Any) -> Decimal | None:
    """Convert JSON-like numeric values into Decimal safely."""

    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _decimal_to_string(value: Decimal) -> str:
    """Render one Decimal with a stable accounting-style scale."""

    return f"{value.quantize(Decimal('0.01')):.2f}"


def _queue_recommendation_job(
    *,
    parse_record: ParseDocumentRecord,
    actor_user_id: UUID,
    trace_id: str | None,
) -> str | None:
    """Queue recommendation generation after a document auto-approves, if possible."""

    with get_session_factory()() as db_session:
        try:
            job = JobService(db_session=db_session).dispatch_job(
                dispatcher=WorkerTaskDispatcher(),
                task_name=TaskName.ACCOUNTING_RECOMMEND_CLOSE_RUN,
                payload={
                    "entity_id": str(parse_record.entity.id),
                    "close_run_id": str(parse_record.close_run.id),
                    "document_id": str(parse_record.document.id),
                    "actor_user_id": str(actor_user_id),
                },
                entity_id=parse_record.entity.id,
                close_run_id=parse_record.close_run.id,
                document_id=parse_record.document.id,
                actor_user_id=actor_user_id,
                trace_id=trace_id,
            )
        except JobServiceError as error:
            logger.warning(
                "recommendation_dispatch_failed",
                document_id=str(parse_record.document.id),
                close_run_id=str(parse_record.close_run.id),
                code=str(error.code),
                message=error.message,
            )
            return None
        except Exception:
            logger.exception(
                "recommendation_dispatch_failed_unexpectedly",
                document_id=str(parse_record.document.id),
                close_run_id=str(parse_record.close_run.id),
            )
            return None
    return str(job.id)


def _post_process_parsed_document(
    *,
    parse_record: ParseDocumentRecord,
    raw_parse_payload: JsonObject,
    actor_user_id: UUID,
    trace_id: str | None,
) -> dict[str, object]:
    """Complete classification, extraction, and collection-phase quality checks."""

    document_type, classification_confidence = _derive_document_classification(raw_parse_payload)
    extraction_created = False
    needs_review = False
    quality_issue_count = 0
    recommendation_job_id: str | None = None
    auto_approved = False
    extraction_confidence_summary: dict[str, Any] | None = None
    latest_extraction: DocumentExtraction | None = None
    with get_session_factory()() as db_session:
        repository = DocumentRepository(db_session=db_session)
        entity_repository = EntityRepository(db_session=db_session)
        storage_repository = StorageRepository()
        try:
            repository.update_document_classification(
                document_id=parse_record.document.id,
                document_type=document_type,
                classification_confidence=classification_confidence,
            )

            document = (
                db_session.query(Document)
                .filter(Document.id == parse_record.document.id)
                .first()
            )
            if document is None:
                raise LookupError(
                    f"Document {parse_record.document.id} disappeared during post-processing."
                )

            extracted_period_start: date | None = None
            extracted_period_end: date | None = None

            if document_type is not DocumentType.UNKNOWN:
                existing_extraction = (
                    db_session.query(DocumentExtraction)
                    .filter(DocumentExtraction.document_id == parse_record.document.id)
                    .order_by(
                        DocumentExtraction.version_no.desc(),
                        DocumentExtraction.created_at.desc(),
                    )
                    .first()
                )
                if existing_extraction is None:
                    parser_output = _build_extraction_parser_output(
                        raw_parse_payload=raw_parse_payload,
                        document_type=document_type,
                    )
                    fields = extract_fields_by_document_type(document_type, parser_output)
                    confidence_summary = compute_confidence_summary(fields)
                    needs_review = confidence_summary.low_confidence_fields > 0
                    extraction = DocumentExtraction(
                        id=parse_record.document.id,
                        document_id=parse_record.document.id,
                        version_no=1,
                        schema_name=document_type.value,
                        schema_version="1.0.0",
                        extracted_payload={
                            "fields": [field.model_dump(mode="json") for field in fields],
                            "parser_output": parser_output,
                            "raw_parse_payload": raw_parse_payload,
                        },
                        confidence_summary=confidence_summary.model_dump(mode="json"),
                        needs_review=needs_review,
                    )
                    db_session.add(extraction)
                    latest_extraction = extraction

                    for field in fields:
                        db_session.add(
                            ExtractedField(
                                document_extraction_id=extraction.id,
                                field_name=field.field_name,
                                field_value=field.model_dump(mode="json")["field_value"],
                                field_type=field.field_type,
                                confidence=field.confidence,
                                evidence_ref=field.evidence_ref.model_dump(mode="json"),
                                is_human_corrected=field.is_human_corrected,
                            )
                        )

                    raw_line_items = parser_output.get("line_items", [])
                    if isinstance(raw_line_items, list):
                        for line_data in raw_line_items:
                            if not isinstance(line_data, dict):
                                continue
                            line_item = SchemaDocumentLineItem.model_validate(line_data)
                            db_session.add(
                                DocumentLineItem(
                                    document_extraction_id=extraction.id,
                                    line_no=line_item.line_no,
                                    description=line_item.description,
                                    quantity=(
                                        float(line_item.quantity)
                                        if line_item.quantity is not None
                                        else None
                                    ),
                                    unit_price=(
                                        float(line_item.unit_price)
                                        if line_item.unit_price is not None
                                        else None
                                    ),
                                    amount=(
                                        float(line_item.amount)
                                        if line_item.amount is not None
                                        else None
                                    ),
                                    tax_amount=(
                                        float(line_item.tax_amount)
                                        if line_item.tax_amount is not None
                                        else None
                                    ),
                                    dimensions=line_item.dimensions,
                                    evidence_ref=line_item.evidence_ref.model_dump(mode="json"),
                                )
                            )

                    extracted_period_start, extracted_period_end = _derive_document_period(
                        document_type=document_type,
                        fields=fields,
                    )
                    repository.update_document_period(
                        document_id=parse_record.document.id,
                        period_start=extracted_period_start,
                        period_end=extracted_period_end,
                    )

                    extraction_created = True
                    extraction_confidence_summary = confidence_summary.model_dump(mode="json")
                    document.status = DocumentStatus.NEEDS_REVIEW.value
                else:
                    latest_extraction = existing_extraction
                    extraction_confidence_summary = dict(existing_extraction.confidence_summary)

            if document_type is DocumentType.UNKNOWN:
                document.status = DocumentStatus.NEEDS_REVIEW.value

            quality_results = run_document_quality_checks(
                entity_id=parse_record.entity.id,
                close_run_id=parse_record.close_run.id,
                document_id=parse_record.document.id,
                document_hash=parse_record.document.sha256_hash,
                document_file_size=parse_record.document.file_size_bytes,
                document_period_start=document.period_start,
                document_period_end=document.period_end,
                close_run_period_start=parse_record.close_run.period_start,
                close_run_period_end=parse_record.close_run.period_end,
                actor_user_id=actor_user_id,
                document_repo=repository,
                entity_repo=entity_repository,
                storage_repo=storage_repository,
                db_session=db_session,
            )
            quality_issue_count = len(quality_results["issues_created"])
            has_blocking_quality_issue = not bool(quality_results["passed_all_checks"])

            if any(
                issue.get("issue_type") == "duplicate_document"
                for issue in quality_results["issues_created"]
            ):
                document.status = DocumentStatus.DUPLICATE.value
                needs_review = True
            elif (
                has_blocking_quality_issue
                and document.status != DocumentStatus.NEEDS_REVIEW.value
            ):
                document.status = DocumentStatus.NEEDS_REVIEW.value
                needs_review = True

            auto_transaction_match = quality_results.get("transaction_match")
            auto_approval_reasons: tuple[str, ...] = ()
            auto_approved = (
                parse_record.entity.autonomy_mode is AutonomyMode.REDUCED_INTERRUPTION
                and latest_extraction is not None
                and document.document_type != DocumentType.UNKNOWN.value
                and document.document_type != DocumentType.CONTRACT.value
                and not has_blocking_quality_issue
                and (
                    extraction_confidence_summary is None
                    or int(extraction_confidence_summary.get("low_confidence_fields", 0)) == 0
                )
                and classification_confidence is not None
                and classification_confidence >= 0.85
                and isinstance(auto_transaction_match, dict)
                and str(auto_transaction_match.get("status", "")).strip().lower()
                in {"matched", "not_applicable"}
            )
            if auto_approved and latest_extraction is not None:
                document.status = DocumentStatus.APPROVED.value
                latest_extraction.approved_version = True
                latest_extraction.needs_review = False
                needs_review = False
                auto_approval_reasons = (
                    "Reduced interruption mode auto-approved this document because classification, "
                    "extraction confidence, period checks, and transaction-linking all passed.",
                )
            if latest_extraction is not None:
                latest_extraction.extracted_payload = update_extraction_auto_review_payload(
                    extracted_payload=latest_extraction.extracted_payload,
                    auto_approved=auto_approved,
                    autonomy_mode=parse_record.entity.autonomy_mode.value,
                    reasons=auto_approval_reasons,
                )

            repository.create_activity_event(
                entity_id=parse_record.entity.id,
                close_run_id=parse_record.close_run.id,
                actor_user_id=actor_user_id,
                event_type="document.processing.completed",
                source_surface=AuditSourceSurface.WORKER,
                payload={
                    "summary": (
                        f"Post-processing completed for {parse_record.document.original_filename}."
                    ),
                    "document_id": str(parse_record.document.id),
                    "document_type": document.document_type,
                    "classification_confidence": classification_confidence,
                    "auto_approved": auto_approved,
                    "extraction_created": extraction_created,
                    "issue_count": quality_issue_count,
                    "needs_review": needs_review,
                },
                trace_id=trace_id,
            )

            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    if (
        document_type in _RECONCILIATION_RECOMMENDATION_ELIGIBLE_TYPES
        and auto_approved
        and latest_extraction is not None
    ):
        recommendation_job_id = _queue_recommendation_job(
            parse_record=parse_record,
            actor_user_id=actor_user_id,
            trace_id=trace_id,
        )

    return {
        "document_type": document_type.value,
        "classification_confidence": classification_confidence,
        "extraction_created": extraction_created,
        "needs_review": needs_review,
        "quality_issue_count": quality_issue_count,
        "recommendation_job_id": recommendation_job_id,
    }


def _run_parse_document_task(
    *,
    entity_id: str,
    close_run_id: str,
    document_id: str,
    actor_user_id: str,
    job_context: JobRuntimeContext,
) -> dict[str, object]:
    """Run parser work from a Celery invocation using JSON-serializable identifiers."""

    parsed_entity_id = UUID(entity_id)
    parsed_close_run_id = UUID(close_run_id)
    parsed_document_id = UUID(document_id)
    parsed_actor_user_id = UUID(actor_user_id)
    trace_id = current_trace_metadata().trace_id

    with get_session_factory()() as db_session:
        repository = DocumentRepository(db_session=db_session)
        try:
            parse_record = repository.get_document_for_parse(
                entity_id=parsed_entity_id,
                close_run_id=parsed_close_run_id,
                document_id=parsed_document_id,
            )
            if parse_record is None:
                raise LookupError(
                    "Document parse task cannot continue because the document was not found "
                    "for the supplied entity and close run."
                )

            repository.update_document_status(
                document_id=parse_record.document.id,
                status=DocumentStatus.PROCESSING,
            )
            repository.commit()
            job_context.checkpoint(
                step="load_document_context",
                state={
                    "document_id": str(parse_record.document.id),
                    "original_filename": parse_record.document.original_filename,
                },
            )
        except Exception:
            repository.rollback()
            raise

    storage_repository = StorageRepository()
    try:
        job_context.ensure_not_canceled()
        if job_context.step_completed("parse_and_store_document"):
            result = _restore_parse_pipeline_receipt(job_context=job_context)
        else:
            result = parse_and_store_document(
                parse_record=parse_record,
                storage_repository=storage_repository,
            )
            job_context.checkpoint(
                step="parse_and_store_document",
                state=_serialize_parse_pipeline_receipt(result),
            )
    except ParserPipelineError as error:
        failure_status = (
            DocumentStatus.BLOCKED
            if error.code.value == "blocked_input"
            else DocumentStatus.FAILED
        )
        _record_parse_failure(
            parse_record=parse_record,
            actor_user_id=parsed_actor_user_id,
            status=failure_status,
            error_payload={"code": error.code.value, "message": error.message},
            trace_id=trace_id,
        )
        if failure_status is DocumentStatus.BLOCKED:
            raise BlockedJobError(
                error.message,
                details={"document_id": str(parse_record.document.id), "code": error.code.value},
            ) from error
        raise
    except Exception as error:
        _record_parse_failure(
            parse_record=parse_record,
            actor_user_id=parsed_actor_user_id,
            status=DocumentStatus.FAILED,
            error_payload={"code": "unexpected_parse_failure", "message": str(error)},
            trace_id=trace_id,
        )
        raise

    with get_session_factory()() as db_session:
        repository = DocumentRepository(db_session=db_session)
        try:
            job_context.ensure_not_canceled()
            repository.update_document_status(
                document_id=parse_record.document.id,
                status=DocumentStatus.PARSED,
                ocr_required=_raw_payload_requires_ocr(result.raw_parse_payload),
            )
            repository.create_activity_event(
                entity_id=parse_record.entity.id,
                close_run_id=parse_record.close_run.id,
                actor_user_id=parsed_actor_user_id,
                event_type="document.parsed",
                source_surface=AuditSourceSurface.WORKER,
                payload={
                    "summary": f"Parsed {parse_record.document.original_filename}.",
                    "document_id": str(parse_record.document.id),
                    "document_version_no": result.document_version_no,
                    "parser_name": result.parser_name,
                    "parser_version": result.parser_version,
                    "page_count": result.page_count,
                    "table_count": result.table_count,
                    "split_candidate_count": result.split_candidate_count,
                },
                trace_id=trace_id,
            )
            repository.commit()
            if not job_context.step_completed("persist_parse_results"):
                job_context.checkpoint(
                    step="persist_parse_results",
                    state={
                        "document_version_no": result.document_version_no,
                        "parser_name": result.parser_name,
                        "page_count": result.page_count,
                    },
                )
        except Exception:
            repository.rollback()
            raise

    post_processing_result = _post_process_parsed_document(
        parse_record=parse_record,
        raw_parse_payload=result.raw_parse_payload,
        actor_user_id=parsed_actor_user_id,
        trace_id=trace_id,
    )

    return {
        "document_id": str(parse_record.document.id),
        "document_version_no": result.document_version_no,
        "parser_name": result.parser_name,
        "parser_version": result.parser_version,
        "page_count": result.page_count,
        "table_count": result.table_count,
        "split_candidate_count": result.split_candidate_count,
        "document_type": post_processing_result["document_type"],
        "classification_confidence": post_processing_result["classification_confidence"],
        "extraction_created": post_processing_result["extraction_created"],
        "needs_review": post_processing_result["needs_review"],
        "quality_issue_count": post_processing_result["quality_issue_count"],
        "recommendation_job_id": post_processing_result["recommendation_job_id"],
    }


@dataclass(frozen=True, slots=True)
class ParsePipelineReceipt:
    """Describe the persisted parser output returned by parse-and-store execution."""

    document_version_no: int
    parser_name: str
    parser_version: str
    page_count: int | None
    table_count: int
    split_candidate_count: int
    checksum: str
    raw_parse_payload: JsonObject
    derivatives: StoredParseDerivatives


def parse_and_store_document(
    *,
    parse_record: ParseDocumentRecord,
    storage_repository: StorageRepository | StorageRepositoryProtocol,
) -> ParsePipelineReceipt:
    """Parse one source document, store derivatives, and persist its version metadata."""

    with get_session_factory()() as db_session:
        repository = DocumentRepository(db_session=db_session)
        document_version_no = repository.next_document_version_no(
            document_id=parse_record.document.id,
        )

    source_payload = storage_repository.download_source_document(
        storage_key=parse_record.document.storage_key
    )
    parser_result = parse_source_document(
        ParserSourceDocument(
            filename=parse_record.document.original_filename,
            mime_type=parse_record.document.mime_type,
            payload=source_payload,
            ocr_required=parse_record.document.ocr_required,
        )
    )
    scope = CloseRunStorageScope(
        entity_id=parse_record.close_run.entity_id,
        close_run_id=parse_record.close_run.id,
        period_start=parse_record.close_run.period_start,
        period_end=parse_record.close_run.period_end,
        close_run_version_no=parse_record.close_run.current_version_no,
    )
    derivatives = store_parse_derivatives(
        storage_repository=storage_repository,
        scope=scope,
        document_id=parse_record.document.id,
        document_version_no=document_version_no,
        source_filename=parse_record.document.original_filename,
        parser_result=parser_result,
    )
    raw_parse_payload = _build_raw_parse_payload(
        parser_result=parser_result,
        derivatives=derivatives,
    )
    checksum = compute_sha256_text(
        json.dumps(raw_parse_payload, ensure_ascii=True, sort_keys=True)
    )

    with get_session_factory()() as db_session:
        repository = DocumentRepository(db_session=db_session)
        try:
            repository.create_document_version(
                document_id=parse_record.document.id,
                version_no=document_version_no,
                normalized_storage_key=derivatives.normalized_storage_key,
                ocr_text_storage_key=derivatives.ocr_text_storage_key,
                parser_name=parser_result.parser_name,
                parser_version=parser_result.parser_version,
                raw_parse_payload=raw_parse_payload,
                page_count=parser_result.page_count,
                checksum=checksum,
            )
            repository.commit()
        except Exception:
            repository.rollback()
            raise

    return ParsePipelineReceipt(
        document_version_no=document_version_no,
        parser_name=parser_result.parser_name,
        parser_version=parser_result.parser_version,
        page_count=parser_result.page_count,
        table_count=len(parser_result.tables),
        split_candidate_count=len(parser_result.split_candidates),
        checksum=checksum,
        raw_parse_payload=raw_parse_payload,
        derivatives=derivatives,
    )


def parse_source_document(source_document: ParserSourceDocument) -> ParserResult:
    """Select and run the deterministic parser adapter for one uploaded source document."""

    mime_type = source_document.mime_type.lower()
    filename = source_document.filename.lower()
    if mime_type == "application/pdf" or filename.endswith(".pdf"):
        initial_parse = parse_pdf_document(
            payload=source_document.payload,
            filename=source_document.filename,
        )
        routing_decision, ocr_result = OcrRouter().run_if_required(
            payload=source_document.payload,
            filename=source_document.filename,
            initial_parse_result=initial_parse,
            intake_ocr_required=source_document.ocr_required,
        )
        if ocr_result is None:
            initial_parse.metadata["ocr_routing"] = routing_decision.model_dump(mode="json")
            return initial_parse

        parsed = parse_pdf_document(
            payload=ocr_result.searchable_pdf_payload or source_document.payload,
            filename=source_document.filename,
            ocr_text=ocr_result.text,
            normalized_payload_override=ocr_result.searchable_pdf_payload,
        )
        parsed.metadata["ocr_routing"] = routing_decision.model_dump(mode="json")
        parsed.metadata["ocr"] = ocr_result.metadata
        return parsed

    return parse_spreadsheet_document(
        payload=source_document.payload,
        filename=source_document.filename,
        mime_type=source_document.mime_type,
    )


def store_parse_derivatives(
    *,
    storage_repository: StorageRepository | StorageRepositoryProtocol,
    scope: CloseRunStorageScope,
    document_id: UUID,
    document_version_no: int,
    source_filename: str,
    parser_result: ParserResult,
) -> StoredParseDerivatives:
    """Store normalized documents, OCR text, and extracted-table payloads."""

    normalized_key: str | None = None
    normalized_payload = parser_result.normalized_payload()
    if (
        normalized_payload is not None
        and parser_result.normalized_filename is not None
        and parser_result.normalized_content_type is not None
    ):
        metadata = storage_repository.store_derivative(
            scope=scope,
            document_id=document_id,
            document_version_no=document_version_no,
            derivative_kind=DerivativeKind.NORMALIZED_DOCUMENT,
            filename=parser_result.normalized_filename,
            payload=normalized_payload,
            content_type=parser_result.normalized_content_type,
        )
        normalized_key = _extract_object_key(metadata)

    ocr_text_key: str | None = None
    if parser_result.ocr_text:
        metadata = storage_repository.store_ocr_text(
            scope=scope,
            document_id=document_id,
            document_version_no=document_version_no,
            source_filename=source_filename,
            text=parser_result.ocr_text,
        )
        ocr_text_key = _extract_object_key(metadata)

    extracted_tables_key: str | None = None
    if parser_result.tables:
        payload = json.dumps(
            {"tables": [table.model_dump(mode="json") for table in parser_result.tables]},
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        metadata = storage_repository.store_derivative(
            scope=scope,
            document_id=document_id,
            document_version_no=document_version_no,
            derivative_kind=DerivativeKind.EXTRACTED_TABLES,
            filename=f"{source_filename}-tables.json",
            payload=payload,
            content_type="application/json",
        )
        extracted_tables_key = _extract_object_key(metadata)

    return StoredParseDerivatives(
        normalized_storage_key=normalized_key,
        ocr_text_storage_key=ocr_text_key,
        extracted_tables_storage_key=extracted_tables_key,
    )


def _build_raw_parse_payload(
    *,
    parser_result: ParserResult,
    derivatives: StoredParseDerivatives,
) -> JsonObject:
    """Merge parser metadata with derivative storage keys for DB persistence."""

    payload = parser_result.raw_parse_payload()
    payload["derivatives"] = {
        "normalized_storage_key": derivatives.normalized_storage_key,
        "ocr_text_storage_key": derivatives.ocr_text_storage_key,
        "extracted_tables_storage_key": derivatives.extracted_tables_storage_key,
    }
    return payload


def _record_parse_failure(
    *,
    parse_record: ParseDocumentRecord,
    actor_user_id: UUID,
    status: DocumentStatus,
    error_payload: JsonObject,
    trace_id: str | None,
) -> None:
    """Persist a parser failure status and emit a worker audit event."""

    with get_session_factory()() as db_session:
        repository = DocumentRepository(db_session=db_session)
        try:
            repository.update_document_status(
                document_id=parse_record.document.id,
                status=status,
            )
            repository.create_activity_event(
                entity_id=parse_record.entity.id,
                close_run_id=parse_record.close_run.id,
                actor_user_id=actor_user_id,
                event_type="document.parse_failed",
                source_surface=AuditSourceSurface.WORKER,
                payload={
                    "summary": f"Parsing failed for {parse_record.document.original_filename}.",
                    "document_id": str(parse_record.document.id),
                    "error": error_payload,
                    "status": status.value,
                },
                trace_id=trace_id,
            )
            repository.commit()
        except Exception:
            repository.rollback()
            raise


def _extract_object_key(metadata: object) -> str:
    """Extract the object key from derivative metadata returned by storage repositories."""

    reference = getattr(metadata, "reference", None)
    object_key = getattr(reference, "object_key", None)
    if not isinstance(object_key, str) or not object_key:
        raise ValueError("Storage derivative metadata did not include a valid object key.")

    return object_key


def _raw_payload_requires_ocr(raw_parse_payload: JsonObject) -> bool:
    """Read the requires-OCR parser metadata flag from a JSON-safe payload."""

    metadata = raw_parse_payload.get("metadata")
    if not isinstance(metadata, dict):
        return False

    return metadata.get("requires_ocr") is True


def _serialize_parse_pipeline_receipt(receipt: ParsePipelineReceipt) -> JsonObject:
    """Convert a persisted parse receipt into checkpoint-safe JSON state."""

    return {
        "document_version_no": receipt.document_version_no,
        "parser_name": receipt.parser_name,
        "parser_version": receipt.parser_version,
        "page_count": receipt.page_count,
        "table_count": receipt.table_count,
        "split_candidate_count": receipt.split_candidate_count,
        "checksum": receipt.checksum,
        "raw_parse_payload": receipt.raw_parse_payload,
        "derivatives": {
            "normalized_storage_key": receipt.derivatives.normalized_storage_key,
            "ocr_text_storage_key": receipt.derivatives.ocr_text_storage_key,
            "extracted_tables_storage_key": receipt.derivatives.extracted_tables_storage_key,
        },
    }


def _restore_parse_pipeline_receipt(*, job_context: JobRuntimeContext) -> ParsePipelineReceipt:
    """Rebuild the prior parse receipt from checkpoint state during resume execution."""

    checkpoint_state = job_context.step_state("parse_and_store_document")
    raw_parse_payload = checkpoint_state.get("raw_parse_payload")
    raw_derivatives = checkpoint_state.get("derivatives")
    if not isinstance(raw_parse_payload, dict) or not isinstance(raw_derivatives, dict):
        raise RuntimeError(
            "Parse job resume requires a completed parse_and_store_document checkpoint payload."
        )

    return ParsePipelineReceipt(
        document_version_no=int(checkpoint_state["document_version_no"]),
        parser_name=str(checkpoint_state["parser_name"]),
        parser_version=str(checkpoint_state["parser_version"]),
        page_count=(
            int(checkpoint_state["page_count"])
            if checkpoint_state.get("page_count") is not None
            else None
        ),
        table_count=int(checkpoint_state["table_count"]),
        split_candidate_count=int(checkpoint_state["split_candidate_count"]),
        checksum=str(checkpoint_state["checksum"]),
        raw_parse_payload=dict(raw_parse_payload),
        derivatives=StoredParseDerivatives(
            normalized_storage_key=_optional_string(raw_derivatives.get("normalized_storage_key")),
            ocr_text_storage_key=_optional_string(raw_derivatives.get("ocr_text_storage_key")),
            extracted_tables_storage_key=_optional_string(
                raw_derivatives.get("extracted_tables_storage_key")
            ),
        ),
    )


def _optional_string(value: object) -> str | None:
    """Normalize an optional checkpoint field into a string or None."""

    if value is None:
        return None

    return str(value)


@celery_app.task(
    bind=True,
    base=TrackedJobTask,
    name=TaskName.DOCUMENT_PARSE_AND_EXTRACT.value,
    autoretry_for=(),
    retry_backoff=False,
    retry_jitter=False,
    max_retries=resolve_task_route(TaskName.DOCUMENT_PARSE_AND_EXTRACT).max_retries,
)
def parse_document(
    self: TrackedJobTask,
    *,
    entity_id: str,
    close_run_id: str,
    document_id: str,
    actor_user_id: str,
) -> dict[str, object]:
    """Execute the parse pipeline under the canonical checkpointed job wrapper."""

    return self.run_tracked_job(
        runner=lambda job_context: _run_parse_document_task(
            entity_id=entity_id,
            close_run_id=close_run_id,
            document_id=document_id,
            actor_user_id=actor_user_id,
            job_context=job_context,
        )
    )


__all__ = [
    "ParsePipelineReceipt",
    "StoredParseDerivatives",
    "parse_and_store_document",
    "parse_document",
    "parse_source_document",
    "store_parse_derivatives",
]
