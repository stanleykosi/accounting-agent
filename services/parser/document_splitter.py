"""
Purpose: Detect multi-document uploads and group boundaries from deterministic parser output.
Scope: Page-level PDF split candidates and row-level table group candidates used by
the parser worker metadata layer.
Dependencies: Parser-domain models and canonical document type enums.
"""

from __future__ import annotations

import re

from services.common.enums import DocumentType
from services.parser.models import DocumentSplitCandidate, ParsedPage, ParsedTable

BOUNDARY_PATTERNS: tuple[tuple[DocumentType, re.Pattern[str]], ...] = (
    (DocumentType.INVOICE, re.compile(r"\b(invoice|tax invoice)\b", re.IGNORECASE)),
    (DocumentType.BANK_STATEMENT, re.compile(r"\b(bank|account)\s+statement\b", re.IGNORECASE)),
    (DocumentType.PAYSLIP, re.compile(r"\b(pay\s*slip|payslip|payroll)\b", re.IGNORECASE)),
    (DocumentType.RECEIPT, re.compile(r"\b(receipt|payment received)\b", re.IGNORECASE)),
    (DocumentType.CONTRACT, re.compile(r"\b(contract|agreement)\b", re.IGNORECASE)),
)


def detect_document_splits(
    *,
    pages: tuple[ParsedPage, ...],
    tables: tuple[ParsedTable, ...],
    filename: str,
) -> tuple[DocumentSplitCandidate, ...]:
    """Return suspected split candidates for parsed pages or spreadsheet table rows."""

    page_splits = _detect_page_splits(pages=pages, filename=filename)
    if page_splits:
        return page_splits

    return _detect_table_group_splits(tables=tables, filename=filename)


def infer_document_type_from_text(text: str) -> DocumentType:
    """Infer a coarse document type from deterministic keyword evidence."""

    for document_type, pattern in BOUNDARY_PATTERNS:
        if pattern.search(text):
            return document_type

    return DocumentType.UNKNOWN


def _detect_page_splits(
    *,
    pages: tuple[ParsedPage, ...],
    filename: str,
) -> tuple[DocumentSplitCandidate, ...]:
    """Detect page boundaries where a new accounting document likely begins."""

    if len(pages) < 2:
        return ()

    boundary_pages: list[tuple[int, DocumentType, str]] = []
    for page in pages:
        text_window = page.text[:1_500]
        document_type = infer_document_type_from_text(text_window)
        if document_type is not DocumentType.UNKNOWN:
            boundary_pages.append((page.page_number, document_type, document_type.label))

    # A single keyword page in a multi-page PDF is not enough evidence for splitting.
    if len(boundary_pages) < 2:
        return ()

    candidates: list[DocumentSplitCandidate] = []
    for index, boundary in enumerate(boundary_pages):
        page_start, document_type, label = boundary
        next_start = (
            boundary_pages[index + 1][0]
            if index + 1 < len(boundary_pages)
            else pages[-1].page_number + 1
        )
        page_end = max(page_start, next_start - 1)
        candidates.append(
            DocumentSplitCandidate(
                split_id=f"{_normalize_split_id(filename)}-pages-{page_start}-{page_end}",
                document_type_hint=document_type,
                label=f"{label} pages {page_start}-{page_end}",
                reason="Repeated document header keywords were detected on separate PDF pages.",
                confidence=0.82,
                page_start=page_start,
                page_end=page_end,
            )
        )

    return tuple(candidates)


def _detect_table_group_splits(
    *,
    tables: tuple[ParsedTable, ...],
    filename: str,
) -> tuple[DocumentSplitCandidate, ...]:
    """Detect spreadsheet row groups using repeated document-type marker columns."""

    candidates: list[DocumentSplitCandidate] = []
    for table in tables:
        marker_column = _find_marker_column(table.columns)
        if marker_column is None:
            continue

        current_value: str | None = None
        group_start_row: int | None = None
        for row_index, row in enumerate(table.rows, start=1):
            marker_value = row.get(marker_column, "").strip()
            if not marker_value:
                continue

            if current_value is not None and marker_value != current_value and group_start_row:
                candidates.append(
                    _build_table_split_candidate(
                        filename=filename,
                        table_name=table.name,
                        marker_value=current_value,
                        row_start=group_start_row,
                        row_end=row_index - 1,
                    )
                )
                group_start_row = row_index

            if current_value is None:
                group_start_row = row_index
            current_value = marker_value

        if current_value is not None and group_start_row is not None:
            candidates.append(
                _build_table_split_candidate(
                    filename=filename,
                    table_name=table.name,
                    marker_value=current_value,
                    row_start=group_start_row,
                    row_end=len(table.rows),
                )
            )

    if len(candidates) < 2:
        return ()

    return tuple(candidates)


def _build_table_split_candidate(
    *,
    filename: str,
    table_name: str,
    marker_value: str,
    row_start: int,
    row_end: int,
) -> DocumentSplitCandidate:
    """Build one row-group split candidate from a spreadsheet marker value."""

    document_type = infer_document_type_from_text(marker_value)
    return DocumentSplitCandidate(
        split_id=f"{_normalize_split_id(filename)}-{_normalize_split_id(table_name)}-rows-"
        f"{row_start}-{row_end}",
        document_type_hint=document_type,
        label=f"{marker_value} rows {row_start}-{row_end}",
        reason="Repeated spreadsheet document-type markers indicate grouped source documents.",
        confidence=0.76,
        row_start=row_start,
        row_end=row_end,
    )


def _find_marker_column(columns: tuple[str, ...]) -> str | None:
    """Find a likely document-group marker column in a normalized table header."""

    for column in columns:
        normalized = column.strip().lower().replace("_", " ")
        if normalized in {"document type", "doctype", "doc type", "source type"}:
            return column

    return None


def _normalize_split_id(value: str) -> str:
    """Normalize free-form filenames and table names for stable split identifiers."""

    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "document"


__all__ = ["detect_document_splits", "infer_document_type_from_text"]
