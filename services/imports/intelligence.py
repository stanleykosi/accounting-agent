"""
Purpose: Build canonical import diagnostics for accounting baseline uploads.
Scope: Header-detection evidence, parser confidence, warnings, recovery actions,
and compact agent-facing summaries for COA, GL, and trial-balance imports.
Dependencies: Shared JSON type aliases only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, cast

from services.common.types import JsonObject, JsonValue

IMPORT_INTELLIGENCE_METADATA_KEY: Final = "import_intelligence"
IMPORT_INTELLIGENCE_SCHEMA_VERSION: Final = "2026-06-03.import-intelligence.v1"
IMPORT_INTELLIGENCE_PARSER_VERSION: Final = "2026-06-03.flexible-accounting-imports.v1"


@dataclass(frozen=True, slots=True)
class ImportColumnMapping:
    """Describe one source-column to canonical-field mapping."""

    source_column_index: int
    source_header: str
    canonical_field: str
    confidence: float = 1.0
    strategy: str = "header_alias"

    def to_metadata(self) -> JsonObject:
        return {
            "source_column_index": self.source_column_index,
            "source_header": self.source_header,
            "canonical_field": self.canonical_field,
            "confidence": round(self.confidence, 2),
            "strategy": self.strategy,
        }


@dataclass(frozen=True, slots=True)
class ImportDiagnosticIssue:
    """Describe one parser diagnostic visible to operators and the agent."""

    code: str
    severity: str
    message: str
    recovery_action: str | None = None

    def to_metadata(self) -> JsonObject:
        payload: JsonObject = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        if self.recovery_action is not None:
            payload["recovery_action"] = self.recovery_action
        return payload


def build_header_column_mappings(
    *,
    headers: Sequence[str],
    header_map: Mapping[int, str],
    confidence: float = 1.0,
    strategy: str = "header_alias",
) -> tuple[ImportColumnMapping, ...]:
    """Return deterministic source-to-canonical mappings for one detected header row."""

    return tuple(
        ImportColumnMapping(
            source_column_index=index,
            source_header=headers[index] if index < len(headers) else "",
            canonical_field=canonical_field,
            confidence=confidence,
            strategy=strategy,
        )
        for index, canonical_field in sorted(header_map.items())
    )


def build_import_intelligence_report(
    *,
    document_kind: str,
    source_format: str,
    uploaded_filename: str,
    row_count: int,
    accepted_row_count: int,
    detected_columns: Sequence[str],
    header_row_index: int | None,
    column_mappings: Sequence[ImportColumnMapping],
    confidence: float,
    parsing_strategy: str,
    parser_capabilities: Sequence[str],
    warnings: Sequence[ImportDiagnosticIssue] = (),
    blockers: Sequence[ImportDiagnosticIssue] = (),
    recovery_actions: Sequence[str] = (),
    extra: Mapping[str, JsonValue] | None = None,
) -> JsonObject:
    """Build one canonical JSON-safe import-intelligence report."""

    warning_metadata = [cast(JsonValue, warning.to_metadata()) for warning in warnings]
    blocker_metadata = [cast(JsonValue, blocker.to_metadata()) for blocker in blockers]
    report: JsonObject = {
        "schema_version": IMPORT_INTELLIGENCE_SCHEMA_VERSION,
        "parser_version": IMPORT_INTELLIGENCE_PARSER_VERSION,
        "document_kind": document_kind,
        "source_format": source_format,
        "uploaded_filename": uploaded_filename,
        "confidence": round(confidence, 2),
        "status": "blocked" if blockers else "parsed_with_warnings" if warnings else "parsed",
        "header_row_index": header_row_index,
        "header_row_number": header_row_index + 1 if header_row_index is not None else None,
        "detected_columns": list(sorted(set(detected_columns))),
        "column_mappings": [
            cast(JsonValue, mapping.to_metadata()) for mapping in column_mappings
        ],
        "row_count": row_count,
        "accepted_row_count": accepted_row_count,
        "parsing_strategy": parsing_strategy,
        "parser_capabilities": list(parser_capabilities),
        "warnings": warning_metadata,
        "blockers": blocker_metadata,
        "recovery_actions": list(recovery_actions),
    }
    report["agent_summary"] = _build_agent_summary(report=report)
    if extra:
        for key, value in extra.items():
            report[key] = value
    return report


def read_import_intelligence_report(metadata: Mapping[str, object] | None) -> JsonObject | None:
    """Return the canonical import-intelligence report from import metadata if present."""

    if metadata is None:
        return None
    report = metadata.get(IMPORT_INTELLIGENCE_METADATA_KEY)
    if not isinstance(report, dict):
        return None
    return {
        str(key): _json_safe_import_value(value)
        for key, value in report.items()
    }


def _build_agent_summary(*, report: JsonObject) -> str:
    """Render a compact summary for planner context and audit/event payloads."""

    kind = str(report["document_kind"]).replace("_", " ")
    source_format = str(report["source_format"]).upper()
    confidence = report["confidence"]
    header_row_number = report.get("header_row_number")
    accepted_rows = report["accepted_row_count"]
    status = report["status"]
    summary = (
        f"{kind} import parsed as {source_format} with confidence={confidence}; "
        f"header_row={header_row_number}; accepted_rows={accepted_rows}; status={status}."
    )
    warnings = report.get("warnings")
    if isinstance(warnings, list) and warnings:
        summary += f" warnings={len(warnings)}."
    return summary


def _json_safe_import_value(value: object) -> JsonValue:
    """Normalize loosely typed persisted JSONB back into the shared JsonValue shape."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe_import_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_import_value(item) for item in value]
    return str(value)


__all__ = [
    "IMPORT_INTELLIGENCE_METADATA_KEY",
    "IMPORT_INTELLIGENCE_PARSER_VERSION",
    "IMPORT_INTELLIGENCE_SCHEMA_VERSION",
    "ImportColumnMapping",
    "ImportDiagnosticIssue",
    "build_header_column_mappings",
    "build_import_intelligence_report",
    "read_import_intelligence_report",
]
