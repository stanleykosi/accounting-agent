"""
Purpose: Define typed parser-domain records shared by document parser adapters and workers.
Scope: Parser inputs, normalized parse outputs, table payloads, split candidates,
OCR execution results, and parser-domain exceptions.
Dependencies: Pydantic validation, shared JSON aliases, and canonical document enums.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator
from services.common.enums import DocumentType
from services.common.types import JsonObject

PARSER_VERSION = "2026.04.step20"


class ParserErrorCode(StrEnum):
    """Enumerate stable parser error codes surfaced to worker failure handlers."""

    BLOCKED_INPUT = "blocked_input"
    OCR_DEPENDENCY_UNAVAILABLE = "ocr_dependency_unavailable"
    OCR_FAILED = "ocr_failed"
    PARSE_FAILED = "parse_failed"
    UNSUPPORTED_MIME = "unsupported_mime"


class ParserPipelineError(Exception):
    """Represent an expected parser-domain failure with an operator-facing code."""

    def __init__(self, *, code: ParserErrorCode, message: str) -> None:
        """Capture the stable error code and explicit recovery-oriented message."""

        super().__init__(message)
        self.code = code
        self.message = message


class ParserBlockedError(ParserPipelineError):
    """Represent an input issue that blocks parsing until the source file is replaced."""

    def __init__(self, *, message: str) -> None:
        """Create a blocked-input parser failure."""

        super().__init__(code=ParserErrorCode.BLOCKED_INPUT, message=message)


class ParserSourceDocument(BaseModel):
    """Describe one source document payload entering the deterministic parser pipeline."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    filename: str = Field(min_length=1)
    mime_type: str = Field(min_length=1)
    payload: bytes = Field(min_length=1)
    ocr_required: bool = Field(default=False)

    @field_validator("filename", "mime_type")
    @classmethod
    def normalize_non_empty_string(cls, value: str) -> str:
        """Trim required string fields and reject empty normalized values."""

        normalized = value.strip()
        if not normalized:
            raise ValueError("value cannot be empty.")

        return normalized


class ParsedPage(BaseModel):
    """Describe text extracted from one source page or sheet region."""

    model_config = ConfigDict(extra="forbid")

    page_number: int = Field(ge=1)
    text: str = Field(default="")
    extraction_method: Literal["pdf_text", "ocr", "spreadsheet", "csv"] = Field()


class ParsedTable(BaseModel):
    """Describe a deterministic table extracted from a PDF, Excel sheet, or CSV file."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    columns: tuple[str, ...] = Field(default=())
    rows: tuple[dict[str, str], ...] = Field(default=())
    source_ref: JsonObject = Field(default_factory=dict)


class DocumentSplitCandidate(BaseModel):
    """Describe a suspected child document contained within a larger uploaded file."""

    model_config = ConfigDict(extra="forbid")

    split_id: str = Field(min_length=1)
    document_type_hint: DocumentType = Field(default=DocumentType.UNKNOWN)
    label: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    row_start: int | None = Field(default=None, ge=1)
    row_end: int | None = Field(default=None, ge=1)


class OcrExecutionResult(BaseModel):
    """Describe OCR output generated from a scanned PDF source."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    text: str = Field(default="")
    searchable_pdf_payload: bytes | None = Field(default=None)
    engine_name: str = Field(default="ocrmypdf+tesseract")
    engine_version: str | None = Field(default=None)
    metadata: JsonObject = Field(default_factory=dict)


class ParserResult(BaseModel):
    """Describe one complete deterministic parser result and its optional derivatives."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    parser_name: str = Field(min_length=1)
    parser_version: str = Field(default=PARSER_VERSION, min_length=1)
    text: str = Field(default="")
    pages: tuple[ParsedPage, ...] = Field(default=())
    tables: tuple[ParsedTable, ...] = Field(default=())
    split_candidates: tuple[DocumentSplitCandidate, ...] = Field(default=())
    page_count: int | None = Field(default=None, ge=0)
    metadata: JsonObject = Field(default_factory=dict)
    normalized_filename: str | None = Field(default=None)
    normalized_content_type: str | None = Field(default=None)
    ocr_text: str | None = Field(default=None)
    _normalized_payload: bytes | None = PrivateAttr(default=None)

    def set_normalized_payload(self, payload: bytes | None) -> None:
        """Attach derivative bytes that must not be serialized into raw parser metadata."""

        self._normalized_payload = payload

    def normalized_payload(self) -> bytes | None:
        """Return derivative bytes for storage, if this parser produced a normalized object."""

        return self._normalized_payload

    def raw_parse_payload(self) -> JsonObject:
        """Render the JSON-safe parser payload stored in the `document_versions` table."""

        return self.model_dump(
            mode="json",
            exclude={"normalized_filename", "normalized_content_type", "ocr_text"},
        )


__all__ = [
    "PARSER_VERSION",
    "DocumentSplitCandidate",
    "OcrExecutionResult",
    "ParsedPage",
    "ParsedTable",
    "ParserBlockedError",
    "ParserErrorCode",
    "ParserPipelineError",
    "ParserResult",
    "ParserSourceDocument",
]
