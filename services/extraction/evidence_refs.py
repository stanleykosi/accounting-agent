"""
Purpose: Build evidence references that ground extracted values to their source documents.
Scope: Helper functions that create evidence references from parser outputs,
coordinate normalization, and snippet extraction for downstream evidence-first
review and audit.
Dependencies: Extraction schemas, parser models (from Step 20), and
structured logging.
"""

from __future__ import annotations

from typing import Any

from services.extraction.schemas import EvidenceRef


def build_evidence_ref(
    page: int | None = None,
    row: int | None = None,
    cell: str | None = None,
    x_coordinate: float | None = None,
    y_coordinate: float | None = None,
    snippet: str | None = None,
) -> EvidenceRef:
    """Construct an evidence reference from discrete location components.

    Args:
        page: One-indexed page number in the source document.
        row: One-indexed row number in table or structured content.
        cell: Cell coordinate for spreadsheet evidence.
        x_coordinate: X coordinate in PDF points.
        y_coordinate: Y coordinate in PDF points.
        snippet: Text snippet surrounding the extracted value.

    Returns:
        Validated EvidenceRef instance.
    """

    return EvidenceRef(
        page=page,
        row=row,
        cell=cell,
        x_coordinate=x_coordinate,
        y_coordinate=y_coordinate,
        snippet=snippet,
    )


def build_pdf_evidence_ref(
    page: int,
    x_coordinate: float,
    y_coordinate: float,
    snippet: str | None = None,
) -> EvidenceRef:
    """Build an evidence reference for PDF-extracted content.

    Args:
        page: One-indexed page number.
        x_coordinate: X coordinate in PDF points.
        y_coordinate: Y coordinate in PDF points.
        snippet: Optional surrounding text for verification.

    Returns:
        EvidenceRef tied to PDF coordinates.
    """

    return EvidenceRef(
        page=page,
        x_coordinate=x_coordinate,
        y_coordinate=y_coordinate,
        snippet=snippet,
    )


def build_table_evidence_ref(
    page: int | None,
    row: int,
    column: int | None = None,
    snippet: str | None = None,
) -> EvidenceRef:
    """Build an evidence reference for table-extracted content.

    Args:
        page: One-indexed page number (optional for non-PDF tables).
        row: One-indexed row number.
        column: One-indexed column number.
        snippet: Optional table cell text for verification.

    Returns:
        EvidenceRef tied to table coordinates.
    """

    cell = None
    if column is not None:
        cell = f"{chr(64 + column)}{row}"

    return EvidenceRef(
        page=page,
        row=row,
        cell=cell,
        snippet=snippet,
    )


def build_spreadsheet_evidence_ref(
    cell: str,
    snippet: str | None = None,
) -> EvidenceRef:
    """Build an evidence reference for spreadsheet-extracted content.

    Args:
        cell: Cell coordinate (e.g., 'A1', 'B3').
        snippet: Optional cell content for verification.

    Returns:
        EvidenceRef tied to spreadsheet cell.
    """

    return EvidenceRef(
        cell=cell,
        snippet=snippet,
    )


def normalize_parser_output_to_evidence_ref(
    parser_output: dict[str, Any],
) -> EvidenceRef:
    """Convert a parser's raw output to a standardized evidence reference.

    This function normalizes heterogeneous parser outputs (PDF, OCR, Excel,
    CSV) into a consistent EvidenceRef structure for downstream storage.

    Args:
        parser_output: Raw parser output dictionary containing location
            metadata. Expected keys vary by parser type:
            - PDF: 'page', 'x', 'y', 'text'
            - Table: 'page', 'row', 'col', 'cell_text'
            - Excel: 'cell', 'value'

    Returns:
        Normalized EvidenceRef instance. Fields not present in the parser
        output are set to None.
    """

    source_type = parser_output.get("source_type", "").lower()

    if source_type == "pdf":
        return build_pdf_evidence_ref(
            page=parser_output.get("page", 1),
            x_coordinate=parser_output.get("x", 0.0),
            y_coordinate=parser_output.get("y", 0.0),
            snippet=parser_output.get("text"),
        )

    if source_type == "table":
        return build_table_evidence_ref(
            page=parser_output.get("page"),
            row=parser_output.get("row", 1),
            column=parser_output.get("col"),
            snippet=parser_output.get("cell_text"),
        )

    if source_type in ("excel", "csv"):
        cell = parser_output.get("cell", "A1")
        if cell:
            return build_spreadsheet_evidence_ref(
                cell=cell,
                snippet=parser_output.get("value"),
            )

    return build_evidence_ref(
        snippet=parser_output.get("text") or parser_output.get("value"),
    )


def merge_snippet_context(
    base_snippet: str,
    target_value: str,
    context_length: int = 50,
) -> str:
    """Create a focused snippet that highlights the target value.

    Args:
        base_snippet: The full text block containing the target value.
        target_value: The extracted value to highlight.
        context_length: Maximum characters of context on each side.

    Returns:
        A snippet string with the target value preserved and surrounding
        context, trimmed to context_length on each side.
    """

    if not base_snippet or not target_value:
        return base_snippet or ""

    value_index = base_snippet.find(target_value)
    if value_index == -1:
        return (
            base_snippet[: context_length * 2]
            if len(base_snippet) > context_length * 2
            else base_snippet
        )

    start = max(0, value_index - context_length)
    end = min(len(base_snippet), value_index + len(target_value) + context_length)

    snippet = base_snippet[start:end]

    if start > 0:
        snippet = "..." + snippet
    if end < len(base_snippet):
        snippet = snippet + "..."

    return snippet


__all__ = [
    "build_evidence_ref",
    "build_pdf_evidence_ref",
    "build_spreadsheet_evidence_ref",
    "build_table_evidence_ref",
    "merge_snippet_context",
    "normalize_parser_output_to_evidence_ref",
]
