"""
Purpose: Parse digital PDFs into normalized text, page metadata, and derivative bytes.
Scope: PDF encryption checks, page text extraction, simple table-like text heuristics,
page count capture, and normalized PDF serialization.
Dependencies: pypdf and parser-domain models.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import PurePath

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from services.parser.document_splitter import detect_document_splits
from services.parser.models import (
    ParsedPage,
    ParsedTable,
    ParserBlockedError,
    ParserErrorCode,
    ParserPipelineError,
    ParserResult,
)

MIN_DIGITAL_TEXT_CHARACTERS_PER_PAGE = 25


def parse_pdf_document(
    *,
    payload: bytes,
    filename: str,
    ocr_text: str | None = None,
    normalized_payload_override: bytes | None = None,
) -> ParserResult:
    """Parse one PDF payload and return page text plus a normalized PDF derivative."""

    reader = _load_pdf_reader(payload=payload, filename=filename)
    page_count = len(reader.pages)
    parsed_pages: list[ParsedPage] = []
    extracted_text_blocks: list[str] = []

    for page_index, page in enumerate(reader.pages, start=1):
        page_text = page.extract_text() or ""
        parsed_pages.append(
            ParsedPage(
                page_number=page_index,
                text=page_text.strip(),
                extraction_method="pdf_text",
            )
        )
        extracted_text_blocks.append(page_text.strip())

    extracted_text = "\n\n".join(block for block in extracted_text_blocks if block)
    effective_text = ocr_text.strip() if ocr_text and ocr_text.strip() else extracted_text
    pages: tuple[ParsedPage, ...]
    if ocr_text and ocr_text.strip():
        pages = (
            ParsedPage(
                page_number=1,
                text=ocr_text.strip(),
                extraction_method="ocr",
            ),
        )
    else:
        pages = tuple(parsed_pages)

    tables = _extract_table_like_text_blocks(text=effective_text)
    normalized_payload = normalized_payload_override or _normalize_pdf_payload(reader=reader)
    normalized_filename = f"{PurePath(filename).stem or 'document'}-normalized.pdf"
    text_density = len(extracted_text) / max(page_count, 1)

    result = ParserResult(
        parser_name="pdf.pypdf",
        text=effective_text,
        pages=tuple(pages),
        tables=tables,
        page_count=page_count,
        metadata={
            "source_format": "pdf",
            "filename": filename,
            "page_count": page_count,
            "digital_text_characters": len(extracted_text),
            "text_density_per_page": round(text_density, 2),
            "ocr_applied": bool(ocr_text and ocr_text.strip()),
            "requires_ocr": text_density < MIN_DIGITAL_TEXT_CHARACTERS_PER_PAGE,
        },
        normalized_filename=normalized_filename,
        normalized_content_type="application/pdf",
        ocr_text=ocr_text.strip() if ocr_text and ocr_text.strip() else None,
    )
    result.split_candidates = detect_document_splits(
        pages=result.pages,
        tables=result.tables,
        filename=filename,
    )
    result.set_normalized_payload(normalized_payload)
    return result


def _load_pdf_reader(*, payload: bytes, filename: str) -> PdfReader:
    """Create a pypdf reader and fail fast for encrypted or unreadable PDFs."""

    try:
        reader = PdfReader(BytesIO(payload))
    except PdfReadError as error:
        raise ParserPipelineError(
            code=ParserErrorCode.PARSE_FAILED,
            message=f"{filename} could not be read as a valid PDF.",
        ) from error

    if reader.is_encrypted:
        try:
            decrypt_result = reader.decrypt("")
        except Exception as error:
            raise ParserBlockedError(
                message=f"{filename} is password-protected. Upload an unlocked copy to continue.",
            ) from error
        if decrypt_result == 0:
            raise ParserBlockedError(
                message=f"{filename} is password-protected. Upload an unlocked copy to continue.",
            )

    return reader


def _normalize_pdf_payload(*, reader: PdfReader) -> bytes:
    """Serialize PDF pages through pypdf so downstream storage has a normalized derivative."""

    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _extract_table_like_text_blocks(*, text: str) -> tuple[ParsedTable, ...]:
    """Extract simple delimiter-shaped tables from PDF text without model assistance."""

    rows: list[dict[str, str]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        delimiter = "\t" if "\t" in line else "|" if "|" in line else None
        if delimiter is None:
            continue

        cells = tuple(cell.strip() for cell in line.split(delimiter))
        if len(cells) < 2:
            continue

        rows.append(
            {
                "source_line_number": str(line_number),
                **{f"column_{index}": value for index, value in enumerate(cells, start=1)},
            }
        )

    if not rows:
        return ()

    max_width = max(
        int(column_name.replace("column_", ""))
        for row in rows
        for column_name in row
        if column_name.startswith("column_")
    )
    return (
        ParsedTable(
            name="pdf_delimited_text",
            columns=tuple(f"column_{index}" for index in range(1, max_width + 1)),
            rows=tuple(rows),
            source_ref={"kind": "pdf_text"},
        ),
    )


__all__ = ["MIN_DIGITAL_TEXT_CHARACTERS_PER_PAGE", "parse_pdf_document"]
