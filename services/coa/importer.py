"""
Purpose: Parse and validate chart-of-accounts upload files.
Scope: CSV/XLSX decoding, header normalization, account row validation,
duplicate detection, parent-link validation, and import metadata generation.
Dependencies: Python CSV/io helpers, openpyxl, and shared JSON type aliases.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO, StringIO
from pathlib import Path
from typing import Final

from openpyxl import load_workbook  # type: ignore[import-untyped]
from services.common.types import JsonObject


class CoaImportErrorCode(StrEnum):
    """Enumerate stable validation codes surfaced by COA file imports."""

    INVALID_FILE = "invalid_file"
    UNSUPPORTED_FILE_TYPE = "unsupported_file_type"


class CoaImportError(ValueError):
    """Represent a fail-fast COA import validation failure."""

    def __init__(self, *, code: CoaImportErrorCode, message: str) -> None:
        """Capture a stable validation code and operator-facing message."""

        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ImportedCoaAccountSeed:
    """Describe one validated account row parsed from an upload file."""

    account_code: str
    account_name: str
    account_type: str
    parent_account_code: str | None
    is_postable: bool
    is_active: bool
    external_ref: str | None
    dimension_defaults: JsonObject


@dataclass(frozen=True, slots=True)
class ImportedCoaFile:
    """Describe the fully validated COA upload payload returned to service workflows."""

    accounts: tuple[ImportedCoaAccountSeed, ...]
    import_metadata: JsonObject


_REQUIRED_COLUMNS: Final[frozenset[str]] = frozenset(
    {"account_code", "account_name", "account_type"}
)

_HEADER_ALIASES: Final[dict[str, str]] = {
    "account": "account_name",
    "account_code": "account_code",
    "account_name": "account_name",
    "account_number": "account_code",
    "account_type": "account_type",
    "active": "is_active",
    "category": "account_type",
    "code": "account_code",
    "cost_centre": "cost_centre",
    "cost_center": "cost_centre",
    "default_cost_centre": "cost_centre",
    "default_cost_center": "cost_centre",
    "default_department": "department",
    "default_project": "project",
    "department": "department",
    "external_ref": "external_ref",
    "external_reference": "external_ref",
    "gl_code": "account_code",
    "is_active": "is_active",
    "is_postable": "is_postable",
    "name": "account_name",
    "parent": "parent_account_code",
    "parent_account": "parent_account_code",
    "parent_account_code": "parent_account_code",
    "parent_code": "parent_account_code",
    "postable": "is_postable",
    "project": "project",
    "qbo_id": "external_ref",
    "quickbooks_id": "external_ref",
    "type": "account_type",
}

_TRUE_LITERALS: Final[frozenset[str]] = frozenset({"1", "t", "true", "y", "yes"})
_FALSE_LITERALS: Final[frozenset[str]] = frozenset({"0", "f", "false", "n", "no"})


def import_coa_file(*, filename: str, payload: bytes) -> ImportedCoaFile:
    """Parse a CSV/XLSX COA payload, validate it, and return canonical account seeds."""

    if not payload:
        raise CoaImportError(
            code=CoaImportErrorCode.INVALID_FILE,
            message="Uploaded COA files cannot be empty.",
        )

    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        rows, detected_columns = _read_csv_rows(payload=payload)
        source_format = "csv"
    elif suffix in {".xlsx", ".xlsm"}:
        rows, detected_columns = _read_workbook_rows(payload=payload)
        source_format = "xlsx"
    else:
        raise CoaImportError(
            code=CoaImportErrorCode.UNSUPPORTED_FILE_TYPE,
            message="Upload a CSV or XLSX chart-of-accounts file.",
        )

    if not rows:
        raise CoaImportError(
            code=CoaImportErrorCode.INVALID_FILE,
            message="The COA file does not contain any account rows.",
        )

    accounts = tuple(
        _parse_account_row(row=row, row_number=index + 2) for index, row in enumerate(rows)
    )
    _validate_accounts(accounts)

    metadata: JsonObject = {
        "detected_columns": ", ".join(sorted(detected_columns)),
        "format": source_format,
        "row_count": len(accounts),
        "uploaded_filename": filename,
    }
    return ImportedCoaFile(accounts=accounts, import_metadata=metadata)


def _read_csv_rows(*, payload: bytes) -> tuple[tuple[dict[str, str], ...], frozenset[str]]:
    """Read CSV rows and normalize header names into canonical COA column keys."""

    try:
        decoded_payload = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise CoaImportError(
            code=CoaImportErrorCode.INVALID_FILE,
            message="CSV files must be UTF-8 encoded.",
        ) from error

    reader = csv.DictReader(StringIO(decoded_payload))
    if reader.fieldnames is None:
        raise CoaImportError(
            code=CoaImportErrorCode.INVALID_FILE,
            message="CSV files must include a header row.",
        )

    canonical_header_map = _build_header_map(reader.fieldnames)
    rows: list[dict[str, str]] = []
    for raw_row in reader:
        rows.append(_canonicalize_row(raw_row=raw_row, header_map=canonical_header_map))

    return tuple(rows), frozenset(canonical_header_map.values())


def _read_workbook_rows(*, payload: bytes) -> tuple[tuple[dict[str, str], ...], frozenset[str]]:
    """Read the first worksheet in an XLSX payload using normalized COA headers."""

    try:
        workbook = load_workbook(filename=BytesIO(payload), read_only=True, data_only=True)
    except Exception as error:
        raise CoaImportError(
            code=CoaImportErrorCode.INVALID_FILE,
            message="The workbook could not be opened. Upload a valid XLSX file.",
        ) from error

    worksheet = workbook.active
    row_iter = worksheet.iter_rows(values_only=True)
    try:
        header_row = next(row_iter)
    except StopIteration as error:
        raise CoaImportError(
            code=CoaImportErrorCode.INVALID_FILE,
            message="The workbook does not contain a header row.",
        ) from error

    header_cells = ["" if cell is None else str(cell) for cell in header_row]
    canonical_header_map = _build_header_map(header_cells)

    rows: list[dict[str, str]] = []
    for raw_row in row_iter:
        raw_mapping = {
            str(index): "" if value is None else str(value)
            for index, value in enumerate(raw_row)
            if index < len(header_cells)
        }
        row_by_header = {
            str(header_cells[index]): raw_mapping.get(str(index), "")
            for index in range(len(header_cells))
        }
        rows.append(_canonicalize_row(raw_row=row_by_header, header_map=canonical_header_map))

    return tuple(rows), frozenset(canonical_header_map.values())


def _build_header_map(headers: Sequence[str]) -> dict[str, str]:
    """Map source header names to canonical COA field names and validate required columns."""

    header_map: dict[str, str] = {}
    for header in headers:
        normalized = _normalize_header_name(header)
        canonical_name = _HEADER_ALIASES.get(normalized)
        if canonical_name is None:
            continue
        header_map[header] = canonical_name

    missing = sorted(_REQUIRED_COLUMNS.difference(header_map.values()))
    if missing:
        missing_columns = ", ".join(missing)
        raise CoaImportError(
            code=CoaImportErrorCode.INVALID_FILE,
            message=f"The COA file is missing required columns: {missing_columns}.",
        )

    return header_map


def _canonicalize_row(*, raw_row: dict[str, str], header_map: dict[str, str]) -> dict[str, str]:
    """Project one source row into canonical field names with raw string values."""

    canonical_row: dict[str, str] = {}
    for source_header, value in raw_row.items():
        canonical_name = header_map.get(source_header)
        if canonical_name is None:
            continue
        canonical_row[canonical_name] = value

    return canonical_row


def _parse_account_row(*, row: dict[str, str], row_number: int) -> ImportedCoaAccountSeed:
    """Validate one canonical row dictionary and convert it into an account seed."""

    account_code = _require_text(
        row.get("account_code"), field_name="account_code", row_number=row_number
    )
    account_name = _require_text(
        row.get("account_name"), field_name="account_name", row_number=row_number
    )
    account_type = _normalize_account_type(
        _require_text(row.get("account_type"), field_name="account_type", row_number=row_number)
    )
    parent_account_code = _normalize_optional_text(row.get("parent_account_code"))
    external_ref = _normalize_optional_text(row.get("external_ref"))

    return ImportedCoaAccountSeed(
        account_code=account_code,
        account_name=account_name,
        account_type=account_type,
        parent_account_code=parent_account_code,
        is_postable=_parse_boolean(
            row.get("is_postable"), default=True, field_name="is_postable", row_number=row_number
        ),
        is_active=_parse_boolean(
            row.get("is_active"), default=True, field_name="is_active", row_number=row_number
        ),
        external_ref=external_ref,
        dimension_defaults=_build_dimension_defaults(row),
    )


def _validate_accounts(accounts: tuple[ImportedCoaAccountSeed, ...]) -> None:
    """Run duplicate-code and parent-link checks across the parsed account list."""

    codes = [account.account_code for account in accounts]
    unique_codes = set(codes)
    if len(codes) != len(unique_codes):
        duplicated = sorted({code for code in codes if codes.count(code) > 1})
        duplicate_codes = ", ".join(duplicated)
        raise CoaImportError(
            code=CoaImportErrorCode.INVALID_FILE,
            message=f"Duplicate account_code values were found: {duplicate_codes}.",
        )

    for account in accounts:
        parent_code = account.parent_account_code
        if parent_code is None:
            continue
        if parent_code == account.account_code:
            raise CoaImportError(
                code=CoaImportErrorCode.INVALID_FILE,
                message=(
                    f"Account {account.account_code} cannot reference itself as "
                    "parent_account_code."
                ),
            )
        if parent_code not in unique_codes:
            raise CoaImportError(
                code=CoaImportErrorCode.INVALID_FILE,
                message=(
                    f"Account {account.account_code} references unknown parent_account_code "
                    f"{parent_code}."
                ),
            )


def _build_dimension_defaults(row: dict[str, str]) -> JsonObject:
    """Build optional dimension defaults from known COA upload columns."""

    defaults: JsonObject = {}
    for source_field, target_key in (
        ("cost_centre", "cost_centre"),
        ("department", "department"),
        ("project", "project"),
    ):
        value = _normalize_optional_text(row.get(source_field))
        if value is not None:
            defaults[target_key] = value

    return defaults


def _normalize_header_name(value: str) -> str:
    """Normalize a file header to a lower snake_case comparison key."""

    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _require_text(value: str | None, *, field_name: str, row_number: int) -> str:
    """Return required text field values or raise an import error with row context."""

    normalized = _normalize_optional_text(value)
    if normalized is not None:
        return normalized

    raise CoaImportError(
        code=CoaImportErrorCode.INVALID_FILE,
        message=f"Row {row_number} is missing required field: {field_name}.",
    )


def _normalize_optional_text(value: str | None) -> str | None:
    """Trim optional text values and collapse blanks to null."""

    if value is None:
        return None

    normalized = value.strip()
    return normalized or None


def _normalize_account_type(value: str) -> str:
    """Normalize account-type labels to lower snake_case values."""

    return value.strip().lower().replace(" ", "_")


def _parse_boolean(
    value: str | None,
    *,
    default: bool,
    field_name: str,
    row_number: int,
) -> bool:
    """Parse optional boolean literals with explicit row-scoped validation errors."""

    normalized = _normalize_optional_text(value)
    if normalized is None:
        return default

    lowered = normalized.lower()
    if lowered in _TRUE_LITERALS:
        return True
    if lowered in _FALSE_LITERALS:
        return False

    raise CoaImportError(
        code=CoaImportErrorCode.INVALID_FILE,
        message=(f"Row {row_number} has invalid boolean value for {field_name}: {normalized}."),
    )


__all__ = [
    "CoaImportError",
    "CoaImportErrorCode",
    "ImportedCoaAccountSeed",
    "ImportedCoaFile",
    "import_coa_file",
]
