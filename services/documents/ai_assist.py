"""
Purpose: Provide bounded LLM assistance for document classification and field recovery.
Scope: Prompt rendering, invocation decisions, and strict model-gateway calls used by
the parser pipeline to improve PDF and OCR extraction without replacing deterministic parsing.
Dependencies: Model gateway, prompt registry, document AI contracts, and shared settings/logging.
"""

from __future__ import annotations

import json
from typing import Any

from services.common.enums import DocumentType
from services.common.logging import get_logger
from services.common.settings import get_settings
from services.common.types import JsonObject
from services.contracts.document_ai_models import DocumentParseAssistOutput
from services.model_gateway.client import ModelGateway, ModelGatewayError
from services.model_gateway.prompts import DOCUMENT_PARSE_ASSIST_PROMPT

logger = get_logger(__name__)

_FIELD_NAMES_BY_DOCUMENT_TYPE: dict[DocumentType, tuple[str, ...]] = {
    DocumentType.INVOICE: (
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
    ),
    DocumentType.BANK_STATEMENT: (
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
    ),
    DocumentType.PAYSLIP: (
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
    ),
    DocumentType.RECEIPT: (
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
    ),
    DocumentType.CONTRACT: (
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
    ),
}


def should_invoke_document_parse_assist(
    *,
    raw_parse_payload: JsonObject,
    document_type: DocumentType,
    classification_confidence: float | None,
) -> bool:
    """Return whether one parsed document should receive the bounded LLM assist pass."""

    settings = get_settings()
    if settings.model_gateway.api_key is None:
        return False

    text_excerpt = _collect_document_text(raw_parse_payload=raw_parse_payload)
    if not text_excerpt:
        return False

    requires_ocr = _raw_payload_requires_ocr(raw_parse_payload=raw_parse_payload)

    return (
        requires_ocr
        or document_type is DocumentType.UNKNOWN
        or document_type is DocumentType.CONTRACT
        or classification_confidence is None
        or classification_confidence < 0.9
    )


def run_document_parse_assist(
    *,
    filename: str,
    raw_parse_payload: JsonObject,
    deterministic_document_type: DocumentType,
    deterministic_classification_confidence: float | None,
    close_run_period_start: str,
    close_run_period_end: str,
    current_field_hints: dict[str, Any] | None,
) -> DocumentParseAssistOutput | None:
    """Run the bounded LLM assist for one parsed document and return validated output."""

    if not should_invoke_document_parse_assist(
        raw_parse_payload=raw_parse_payload,
        document_type=deterministic_document_type,
        classification_confidence=deterministic_classification_confidence,
    ):
        return None

    system_prompt, user_prompt = DOCUMENT_PARSE_ASSIST_PROMPT.render(
        filename=filename,
        ocr_required=str(_raw_payload_requires_ocr(raw_parse_payload=raw_parse_payload)).lower(),
        deterministic_type=deterministic_document_type.value,
        deterministic_confidence=(
            f"{deterministic_classification_confidence:.2f}"
            if deterministic_classification_confidence is not None
            else "unknown"
        ),
        period_start=close_run_period_start,
        period_end=close_run_period_end,
        current_field_hints=_serialize_prompt_object(current_field_hints or {}),
        available_types=", ".join(DocumentType.values()),
        field_name_reference=_serialize_prompt_object(
            {
                document_type.value: field_names
                for document_type, field_names in _FIELD_NAMES_BY_DOCUMENT_TYPE.items()
            }
        ),
        text_excerpt=_truncate_text(
            _collect_document_text(raw_parse_payload=raw_parse_payload),
            max_chars=12_000,
        ),
        table_preview=_build_table_preview(raw_parse_payload=raw_parse_payload),
    )

    try:
        gateway = ModelGateway()
        return gateway.complete_structured(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_model=DocumentParseAssistOutput,
        )
    except ModelGatewayError as error:
        logger.warning(
            "document_parse_ai_assist_failed",
            filename=filename,
            deterministic_document_type=deterministic_document_type.value,
            error=str(error),
        )
        return None
    except Exception:
        logger.exception(
            "document_parse_ai_assist_failed_unexpectedly",
            filename=filename,
            deterministic_document_type=deterministic_document_type.value,
        )
        return None


def _collect_document_text(*, raw_parse_payload: JsonObject) -> str:
    """Return the combined parser text used to ground one assist prompt."""

    fragments: list[str] = []
    raw_text = raw_parse_payload.get("text")
    if isinstance(raw_text, str) and raw_text.strip():
        fragments.append(raw_text.strip())

    pages = raw_parse_payload.get("pages")
    if isinstance(pages, list):
        for page in pages:
            if not isinstance(page, dict):
                continue
            page_text = page.get("text")
            if isinstance(page_text, str) and page_text.strip():
                fragments.append(page_text.strip())

    return "\n\n".join(fragment for fragment in fragments if fragment)


def _build_table_preview(*, raw_parse_payload: JsonObject) -> str:
    """Serialize a compact table preview suitable for prompt grounding."""

    tables = raw_parse_payload.get("tables")
    if not isinstance(tables, list) or not tables:
        return "[]"

    preview: list[dict[str, Any]] = []
    for table in tables[:3]:
        if not isinstance(table, dict):
            continue
        raw_rows = table.get("rows")
        rows_preview: list[dict[str, str]] = []
        if isinstance(raw_rows, list):
            for raw_row in raw_rows[:5]:
                if not isinstance(raw_row, dict):
                    continue
                rows_preview.append(
                    {
                        str(key): _truncate_text(str(value).strip(), max_chars=120)
                        for key, value in raw_row.items()
                    }
                )
        preview.append(
            {
                "name": str(table.get("name", "")),
                "row_count": len(raw_rows) if isinstance(raw_rows, list) else 0,
                "rows": rows_preview,
            }
        )

    return _serialize_prompt_object(preview)


def _serialize_prompt_object(value: object) -> str:
    """Render one JSON-safe object for prompt injection."""

    return json.dumps(value, default=str, ensure_ascii=True, sort_keys=True)


def _truncate_text(value: str, *, max_chars: int) -> str:
    """Trim oversized prompt fragments while keeping the head of the evidence intact."""

    if len(value) <= max_chars:
        return value
    return value[: max_chars - 15].rstrip() + "\n...[truncated]"


def _raw_payload_requires_ocr(*, raw_parse_payload: JsonObject) -> bool:
    """Read the requires-OCR metadata flag from a JSON-safe parse payload."""

    metadata = raw_parse_payload.get("metadata")
    if not isinstance(metadata, dict):
        return False
    return metadata.get("requires_ocr") is True


__all__ = [
    "run_document_parse_assist",
    "should_invoke_document_parse_assist",
]
