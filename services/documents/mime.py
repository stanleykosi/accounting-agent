"""
Purpose: Sniff uploaded document bytes to validate true content types.
Scope: PDF, Excel Open XML, legacy Excel, and CSV detection for the primary ingestion path.
Dependencies: Python standard-library binary and CSV helpers only.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from enum import StrEnum
from io import StringIO
from pathlib import PurePath
from zipfile import BadZipFile, ZipFile


class SupportedDocumentMime(StrEnum):
    """Enumerate MIME types accepted by the canonical upload path."""

    PDF = "application/pdf"
    EXCEL_OPENXML = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    EXCEL_LEGACY = "application/vnd.ms-excel"
    CSV = "text/csv"


@dataclass(frozen=True, slots=True)
class SniffedMimeResult:
    """Describe the true content type detected for an uploaded file."""

    mime_type: SupportedDocumentMime
    extension: str
    ocr_required: bool


class UnsupportedDocumentMimeError(ValueError):
    """Raised when uploaded bytes do not match a supported accounting document format."""


def sniff_document_mime(*, filename: str, payload: bytes) -> SniffedMimeResult:
    """Return the true supported MIME type for a payload or fail fast with recovery context."""

    if not payload:
        raise UnsupportedDocumentMimeError("Uploaded files must not be empty.")

    extension = _extract_extension(filename)
    if payload.startswith(b"%PDF-"):
        return SniffedMimeResult(
            mime_type=SupportedDocumentMime.PDF,
            extension=".pdf",
            ocr_required=_pdf_appears_to_need_ocr(payload),
        )

    if payload.startswith(b"PK\x03\x04") and _is_openxml_workbook(payload):
        return SniffedMimeResult(
            mime_type=SupportedDocumentMime.EXCEL_OPENXML,
            extension=".xlsx",
            ocr_required=False,
        )

    if payload.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return SniffedMimeResult(
            mime_type=SupportedDocumentMime.EXCEL_LEGACY,
            extension=".xls",
            ocr_required=False,
        )

    if _is_probable_csv(payload):
        return SniffedMimeResult(
            mime_type=SupportedDocumentMime.CSV,
            extension=extension if extension == ".csv" else ".csv",
            ocr_required=False,
        )

    raise UnsupportedDocumentMimeError(
        "Unsupported document content. Upload a readable PDF, Excel workbook, or CSV file."
    )


def _extract_extension(filename: str) -> str:
    """Return a lower-case file extension for diagnostics and normalized storage names."""

    suffix = PurePath(filename.strip()).suffix.lower()
    return suffix or ""


def _is_openxml_workbook(payload: bytes) -> bool:
    """Return whether a ZIP payload has the workbook members required for XLSX files."""

    try:
        with ZipFile(_BytesReader(payload)) as workbook_zip:
            names = set(workbook_zip.namelist())
    except (BadZipFile, ValueError):
        return False

    return "[Content_Types].xml" in names and any(
        name.startswith("xl/worksheets/") for name in names
    )


def _is_probable_csv(payload: bytes) -> bool:
    """Detect plain-text CSV files without trusting the filename extension."""

    sample = payload[:8192]
    if b"\x00" in sample:
        return False

    text = _decode_text_sample(sample)
    if text is None or not text.strip():
        return False

    try:
        dialect = csv.Sniffer().sniff(text, delimiters=",;\t|")
    except csv.Error:
        return False

    rows = list(csv.reader(StringIO(text), dialect=dialect))
    non_empty_rows = [row for row in rows if any(cell.strip() for cell in row)]
    if len(non_empty_rows) < 1:
        return False

    return any(len(row) > 1 for row in non_empty_rows)


def _decode_text_sample(payload: bytes) -> str | None:
    """Decode a short text sample using strict UTF-8, then common spreadsheet encodings."""

    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue

    return None


def _pdf_appears_to_need_ocr(payload: bytes) -> bool:
    """Use a conservative byte-level signal for scanned PDFs until the parser step runs."""

    sample = payload[: min(len(payload), 256_000)].lower()
    has_text_operator = b"/font" in sample or b"tj" in sample
    has_image_signal = b"/image" in sample or b"/xobject" in sample
    return has_image_signal and not has_text_operator


class _BytesReader:
    """Small seekable bytes wrapper accepted by ZipFile without importing BytesIO globally."""

    def __init__(self, payload: bytes) -> None:
        """Store the uploaded payload for ZipFile reads."""

        self._payload = payload
        self._position = 0

    def read(self, size: int = -1) -> bytes:
        """Read bytes from the current position using file-object semantics."""

        if size is None or size < 0:
            size = len(self._payload) - self._position
        chunk = self._payload[self._position : self._position + size]
        self._position += len(chunk)
        return chunk

    def seek(self, offset: int, whence: int = 0) -> int:
        """Move the current position for ZipFile central-directory reads."""

        if whence == 0:
            new_position = offset
        elif whence == 1:
            new_position = self._position + offset
        elif whence == 2:
            new_position = len(self._payload) + offset
        else:
            raise ValueError("Unsupported seek mode.")

        if new_position < 0:
            raise ValueError("Cannot seek before start of payload.")
        self._position = new_position
        return self._position

    def tell(self) -> int:
        """Return the current byte offset."""

        return self._position


__all__ = [
    "SniffedMimeResult",
    "SupportedDocumentMime",
    "UnsupportedDocumentMimeError",
    "sniff_document_mime",
]
