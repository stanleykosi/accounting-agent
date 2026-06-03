"""
Purpose: Build canonical source-document classification diagnostics.
Scope: Deterministic and LLM-assisted document type evidence, required-field
completeness, parser warnings, recovery actions, and compact agent summaries.
Dependencies: Canonical document enums, document AI contracts, and shared JSON aliases.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from services.common.enums import DocumentType
from services.common.types import JsonObject
from services.contracts.document_ai_models import DocumentParseAssistOutput

DOCUMENT_INTELLIGENCE_METADATA_KEY: Final = "document_intelligence"
DOCUMENT_INTELLIGENCE_SCHEMA_VERSION: Final = "2026-06-03.document-intelligence.v1"
DOCUMENT_INTELLIGENCE_CLASSIFIER_VERSION: Final = "2026-06-03.source-document-classifier.v1"

_HIGH_CONFIDENCE_THRESHOLD: Final = 0.85
_LOW_CONFIDENCE_THRESHOLD: Final = 0.70

_REQUIRED_FIELD_GROUPS_BY_TYPE: Final[dict[DocumentType, tuple[tuple[str, ...], ...]]] = {
    DocumentType.INVOICE: (
        ("vendor_name", "customer_name"),
        ("invoice_number",),
        ("invoice_date", "due_date"),
        ("total", "subtotal"),
    ),
    DocumentType.BANK_STATEMENT: (
        ("bank_name", "account_name", "account_number"),
        ("statement_start_date", "statement_end_date"),
        ("opening_balance", "closing_balance", "total_credits", "total_debits"),
    ),
    DocumentType.PAYSLIP: (
        ("employee_name", "employee_id"),
        ("employer_name",),
        ("pay_period_start", "pay_period_end", "pay_date"),
        ("gross_pay", "net_pay", "basic_salary"),
    ),
    DocumentType.RECEIPT: (
        ("vendor_name", "customer_name"),
        ("receipt_number",),
        ("receipt_date",),
        ("total", "subtotal"),
    ),
    DocumentType.CONTRACT: (
        ("party_a_name",),
        ("party_b_name",),
        ("contract_date", "effective_date"),
        ("contract_number", "contract_value", "terms"),
    ),
}


@dataclass(frozen=True, slots=True)
class DocumentClassificationSignal:
    """Describe one bounded piece of evidence for a source-document type."""

    source: str
    document_type: DocumentType
    confidence: float | None
    evidence: str | None = None

    def to_metadata(self) -> JsonObject:
        """Serialize one signal into JSON-safe diagnostic metadata."""

        payload: JsonObject = {
            "source": self.source,
            "document_type": self.document_type.value,
            "confidence": _round_confidence(self.confidence),
        }
        if self.evidence is not None and self.evidence.strip():
            payload["evidence"] = self.evidence.strip()
        return payload


@dataclass(frozen=True, slots=True)
class DocumentClassificationDecision:
    """Describe deterministic parser classification before optional LLM assist."""

    document_type: DocumentType
    confidence: float | None
    source: str
    signals: tuple[DocumentClassificationSignal, ...] = ()
    warnings: tuple[str, ...] = ()
    recovery_actions: tuple[str, ...] = ()


def build_document_intelligence_report(
    *,
    filename: str,
    source_format: str,
    ocr_required: bool,
    final_document_type: DocumentType,
    final_confidence: float | None,
    deterministic_decision: DocumentClassificationDecision,
    assist_output: DocumentParseAssistOutput | None,
    ai_assist_applied_classification: bool,
    ai_assist_fields_applied: Sequence[str],
    ai_assist_retried_for_low_confidence: bool,
    field_values: Mapping[str, object | None],
) -> JsonObject:
    """Return canonical diagnostics for one classified source document."""

    missing_required_groups = _missing_required_field_groups(
        document_type=final_document_type,
        field_values=field_values,
    )
    present_required_fields = _present_required_fields(
        document_type=final_document_type,
        field_values=field_values,
    )
    warnings = list(deterministic_decision.warnings)
    recovery_actions = list(deterministic_decision.recovery_actions)
    final_source = (
        "ai_assist"
        if ai_assist_applied_classification
        else deterministic_decision.source
    )

    if final_document_type is DocumentType.UNKNOWN:
        warnings.append("The parser could not classify this document into a supported type.")
        recovery_actions.append(
            "Review the source document and classify it as invoice, bank_statement, payslip, "
            "receipt, contract, or replace the upload with a clearer export."
        )
    if final_confidence is None:
        warnings.append("No classification confidence was available for this document.")
    elif final_confidence < _LOW_CONFIDENCE_THRESHOLD:
        warnings.append(
            f"Classification confidence is low ({final_confidence:.2f}); reviewer "
            "confirmation is required."
        )
    elif final_confidence < _HIGH_CONFIDENCE_THRESHOLD:
        warnings.append(
            "Classification confidence is below the auto-approval threshold "
            f"({final_confidence:.2f})."
        )

    ai_payload = _build_ai_assist_payload(
        assist_output=assist_output,
        ai_assist_applied_classification=ai_assist_applied_classification,
        ai_assist_fields_applied=ai_assist_fields_applied,
        ai_assist_retried_for_low_confidence=ai_assist_retried_for_low_confidence,
    )
    if assist_output is not None:
        if (
            assist_output.predicted_type is not deterministic_decision.document_type
            and not ai_assist_applied_classification
        ):
            warnings.append(
                "LLM assist suggested "
                f"{assist_output.predicted_type.value} at "
                f"{assist_output.classification_confidence:.2f}, "
                f"but deterministic {deterministic_decision.document_type.value} was retained."
            )
            recovery_actions.append(
                "Review the document classification because deterministic and LLM evidence "
                "disagreed."
            )
        elif (
            assist_output.predicted_type is not deterministic_decision.document_type
            and ai_assist_applied_classification
        ):
            warnings.append(
                "LLM assist overrode the deterministic classification from "
                f"{deterministic_decision.document_type.value} to "
                f"{assist_output.predicted_type.value}."
            )

    if missing_required_groups:
        warnings.append(
            "Required classification fields are missing: "
            + ", ".join(missing_required_groups)
            + "."
        )
        recovery_actions.append(
            "Review or correct the extracted fields before using this document for automated "
            "posting, matching, or close evidence."
        )
    if ocr_required:
        warnings.append(
            "This source required OCR; reviewer validation is recommended for extracted fields."
        )

    warnings_tuple = _dedupe_text(warnings)
    recovery_actions_tuple = _dedupe_text(recovery_actions)
    status = _resolve_status(
        final_document_type=final_document_type,
        warnings=warnings_tuple,
        missing_required_groups=missing_required_groups,
    )

    report: JsonObject = {
        "schema_version": DOCUMENT_INTELLIGENCE_SCHEMA_VERSION,
        "classifier_version": DOCUMENT_INTELLIGENCE_CLASSIFIER_VERSION,
        "status": status,
        "filename": filename,
        "source_format": source_format,
        "ocr_required": ocr_required,
        "allowed_document_types": list(DocumentType.values()),
        "final_document_type": final_document_type.value,
        "final_confidence": _round_confidence(final_confidence),
        "classification_source": final_source,
        "deterministic_classification": {
            "document_type": deterministic_decision.document_type.value,
            "confidence": _round_confidence(deterministic_decision.confidence),
            "source": deterministic_decision.source,
        },
        "ai_assist": ai_payload,
        "signals": [signal.to_metadata() for signal in deterministic_decision.signals],
        "field_completeness": {
            "required_groups": [
                list(group) for group in _REQUIRED_FIELD_GROUPS_BY_TYPE.get(final_document_type, ())
            ],
            "present_required_fields": list(present_required_fields),
            "missing_required_groups": list(missing_required_groups),
        },
        "warnings": list(warnings_tuple),
        "recovery_actions": list(recovery_actions_tuple),
        "agent_summary": _build_agent_summary(
            final_document_type=final_document_type,
            final_confidence=final_confidence,
            status=status,
            warnings=warnings_tuple,
        ),
    }
    return report


def read_document_intelligence_report(
    extracted_payload: Mapping[str, object] | None,
) -> JsonObject | None:
    """Return persisted source-document diagnostics from an extraction payload."""

    if not isinstance(extracted_payload, Mapping):
        return None
    report = extracted_payload.get(DOCUMENT_INTELLIGENCE_METADATA_KEY)
    if not isinstance(report, dict):
        return None
    return dict(report)


def _build_ai_assist_payload(
    *,
    assist_output: DocumentParseAssistOutput | None,
    ai_assist_applied_classification: bool,
    ai_assist_fields_applied: Sequence[str],
    ai_assist_retried_for_low_confidence: bool,
) -> JsonObject:
    """Return the deterministic LLM-assist decision fragment."""

    payload: JsonObject = {
        "returned_output": assist_output is not None,
        "classification_applied": ai_assist_applied_classification,
        "field_candidates_applied": list(ai_assist_fields_applied),
        "retried_for_low_confidence": ai_assist_retried_for_low_confidence,
    }
    if assist_output is None:
        return payload
    payload.update(
        {
            "predicted_type": assist_output.predicted_type.value,
            "classification_confidence": _round_confidence(
                assist_output.classification_confidence
            ),
            "classification_reasoning": assist_output.classification_reasoning,
            "field_candidate_count": len(assist_output.field_candidates),
        }
    )
    return payload


def _missing_required_field_groups(
    *,
    document_type: DocumentType,
    field_values: Mapping[str, object | None],
) -> tuple[str, ...]:
    """Return required field groups that have no populated member."""

    missing_groups: list[str] = []
    for group in _REQUIRED_FIELD_GROUPS_BY_TYPE.get(document_type, ()):
        if any(_field_is_present(field_values.get(field_name)) for field_name in group):
            continue
        missing_groups.append(_format_field_group(group))
    return tuple(missing_groups)


def _present_required_fields(
    *,
    document_type: DocumentType,
    field_values: Mapping[str, object | None],
) -> tuple[str, ...]:
    """Return required fields that are populated."""

    present_fields: list[str] = []
    for group in _REQUIRED_FIELD_GROUPS_BY_TYPE.get(document_type, ()):
        for field_name in group:
            if _field_is_present(field_values.get(field_name)):
                present_fields.append(field_name)
    return tuple(present_fields)


def _field_is_present(value: object | None) -> bool:
    """Return whether one extracted value should satisfy a required field group."""

    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _format_field_group(group: Sequence[str]) -> str:
    """Render one alternative required-field group for diagnostics."""

    return "_or_".join(group)


def _resolve_status(
    *,
    final_document_type: DocumentType,
    warnings: Sequence[str],
    missing_required_groups: Sequence[str],
) -> str:
    """Return a stable parser status string for source-document diagnostics."""

    if final_document_type is DocumentType.UNKNOWN:
        return "unclassified"
    if missing_required_groups:
        return "classified_with_missing_fields"
    if warnings:
        return "classified_with_warnings"
    return "classified"


def _build_agent_summary(
    *,
    final_document_type: DocumentType,
    final_confidence: float | None,
    status: str,
    warnings: Sequence[str],
) -> str:
    """Return one compact summary for agent prompts."""

    confidence_text = (
        "unknown confidence" if final_confidence is None else f"{final_confidence:.2f}"
    )
    warning_text = f"; warning: {warnings[0]}" if warnings else ""
    return (
        f"Document classified as {final_document_type.value} with {confidence_text}; "
        f"status={status}{warning_text}"
    )


def _round_confidence(value: float | None) -> float | None:
    """Round confidence values consistently for persisted metadata."""

    if value is None:
        return None
    return round(float(value), 2)


def _dedupe_text(values: Sequence[str]) -> tuple[str, ...]:
    """Return non-empty diagnostic text in first-seen order."""

    deduped: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if not normalized or normalized in deduped:
            continue
        deduped.append(normalized)
    return tuple(deduped)


__all__ = [
    "DOCUMENT_INTELLIGENCE_CLASSIFIER_VERSION",
    "DOCUMENT_INTELLIGENCE_METADATA_KEY",
    "DOCUMENT_INTELLIGENCE_SCHEMA_VERSION",
    "DocumentClassificationDecision",
    "DocumentClassificationSignal",
    "build_document_intelligence_report",
    "read_document_intelligence_report",
]
