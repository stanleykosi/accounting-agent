"""
Purpose: Parse and validate imported general-ledger and trial-balance upload files.
Scope: CSV/XLSX decoding, canonical header normalization, amount/date validation,
and conversion into typed import seeds for service-layer persistence.
Dependencies: Python CSV/io helpers, Decimal/date parsing, and openpyxl workbook reads.
"""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from io import BytesIO, StringIO
from pathlib import Path

from openpyxl import load_workbook  # type: ignore[import-untyped]
from services.common.types import JsonObject


class LedgerImportErrorCode(StrEnum):
    """Enumerate stable validation codes surfaced by ledger baseline uploads."""

    INVALID_FILE = "invalid_file"
    UNSUPPORTED_FILE_TYPE = "unsupported_file_type"


class LedgerImportError(ValueError):
    """Represent a fail-fast ledger baseline validation failure."""

    def __init__(self, *, code: LedgerImportErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ImportedGeneralLedgerLineSeed:
    """Describe one validated imported general-ledger line."""

    line_no: int
    posting_date: date
    account_code: str
    account_name: str | None
    reference: str | None
    description: str | None
    debit_amount: Decimal
    credit_amount: Decimal
    dimensions: JsonObject
    external_ref: str | None
    transaction_group_key: str


@dataclass(frozen=True, slots=True)
class ImportedTrialBalanceLineSeed:
    """Describe one validated imported trial-balance row."""

    line_no: int
    account_code: str
    account_name: str | None
    account_type: str | None
    debit_balance: Decimal
    credit_balance: Decimal
    is_active: bool


@dataclass(frozen=True, slots=True)
class ImportedGeneralLedgerFile:
    """Describe one validated general-ledger import payload."""

    lines: tuple[ImportedGeneralLedgerLineSeed, ...]
    import_metadata: JsonObject


@dataclass(frozen=True, slots=True)
class ImportedTrialBalanceFile:
    """Describe one validated trial-balance import payload."""

    lines: tuple[ImportedTrialBalanceLineSeed, ...]
    import_metadata: JsonObject


_GL_REQUIRED_COLUMNS = frozenset({"posting_date", "account_code"})
_TB_REQUIRED_COLUMNS = frozenset({"account_code"})

_GL_HEADER_ALIASES = {
    "account": "account_name",
    "account_code": "account_code",
    "account_name": "account_name",
    "account_number": "account_code",
    "amount": "amount",
    "cost_centre": "cost_centre",
    "cost_center": "cost_centre",
    "credit": "credit_amount",
    "credit_amount": "credit_amount",
    "date": "posting_date",
    "department": "department",
    "description": "description",
    "debit": "debit_amount",
    "debit_amount": "debit_amount",
    "entry_id": "transaction_group_key",
    "entry_key": "transaction_group_key",
    "entry_no": "transaction_group_key",
    "entry_number": "transaction_group_key",
    "external_ref": "external_ref",
    "external_reference": "external_ref",
    "gl_code": "account_code",
    "group_id": "transaction_group_key",
    "group_key": "transaction_group_key",
    "journal_date": "posting_date",
    "journal_id": "transaction_group_key",
    "journal_key": "transaction_group_key",
    "journal_no": "transaction_group_key",
    "journal_number": "transaction_group_key",
    "line_type": "line_type",
    "memo": "description",
    "posting_date": "posting_date",
    "project": "project",
    "ref": "reference",
    "reference": "reference",
    "signed_amount": "signed_amount",
    "transaction_date": "posting_date",
    "transaction_group": "transaction_group_key",
    "transaction_group_id": "transaction_group_key",
    "transaction_group_key": "transaction_group_key",
    "transaction_id": "transaction_group_key",
    "transaction_key": "transaction_group_key",
    "transaction_no": "transaction_group_key",
    "transaction_number": "transaction_group_key",
    "transaction_ref": "reference",
    "type": "line_type",
    "voucher_id": "transaction_group_key",
    "voucher_no": "transaction_group_key",
    "voucher_number": "transaction_group_key",
}

_TB_HEADER_ALIASES = {
    "account": "account_name",
    "account_code": "account_code",
    "account_name": "account_name",
    "account_number": "account_code",
    "account_type": "account_type",
    "active": "is_active",
    "balance": "balance",
    "balance_side": "balance_side",
    "balance_type": "balance_side",
    "code": "account_code",
    "credit": "credit_balance",
    "credit_balance": "credit_balance",
    "debit": "debit_balance",
    "debit_balance": "debit_balance",
    "gl_code": "account_code",
    "is_active": "is_active",
    "name": "account_name",
    "status": "is_active",
    "type": "account_type",
}

_TRUE_LITERALS = frozenset({"1", "active", "t", "true", "y", "yes"})
_FALSE_LITERALS = frozenset({"0", "f", "false", "inactive", "n", "no"})
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d")


def import_general_ledger_file(*, filename: str, payload: bytes) -> ImportedGeneralLedgerFile:
    """Parse a CSV/XLSX general-ledger payload and return validated line seeds."""

    rows, detected_columns, source_format = _read_rows(
        filename=filename,
        payload=payload,
        header_aliases=_GL_HEADER_ALIASES,
        required_columns=_GL_REQUIRED_COLUMNS,
        noun="general ledger",
    )
    lines = tuple(_parse_gl_row(row=row, row_number=index + 2) for index, row in enumerate(rows))
    metadata: JsonObject = {
        "detected_columns": ", ".join(sorted(detected_columns)),
        "format": source_format,
        "row_count": len(lines),
        "transaction_grouping_strategy": (
            "explicit_column"
            if "transaction_group_key" in detected_columns
            else "derived_from_ledger_fields"
        ),
        "uploaded_filename": filename,
    }
    return ImportedGeneralLedgerFile(lines=lines, import_metadata=metadata)


def import_trial_balance_file(*, filename: str, payload: bytes) -> ImportedTrialBalanceFile:
    """Parse a CSV/XLSX trial-balance payload and return validated account seeds."""

    rows, detected_columns, source_format = _read_rows(
        filename=filename,
        payload=payload,
        header_aliases=_TB_HEADER_ALIASES,
        required_columns=_TB_REQUIRED_COLUMNS,
        noun="trial balance",
    )
    lines = tuple(_parse_tb_row(row=row, row_number=index + 2) for index, row in enumerate(rows))
    metadata: JsonObject = {
        "detected_columns": ", ".join(sorted(detected_columns)),
        "format": source_format,
        "row_count": len(lines),
        "uploaded_filename": filename,
    }
    return ImportedTrialBalanceFile(lines=lines, import_metadata=metadata)


def _read_rows(
    *,
    filename: str,
    payload: bytes,
    header_aliases: dict[str, str],
    required_columns: frozenset[str],
    noun: str,
) -> tuple[tuple[dict[str, str], ...], frozenset[str], str]:
    """Read canonicalized rows from a CSV or XLSX payload."""

    if not payload:
        raise LedgerImportError(
            code=LedgerImportErrorCode.INVALID_FILE,
            message=f"Uploaded {noun} files cannot be empty.",
        )

    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        rows, detected_columns = _read_csv_rows(
            payload=payload,
            header_aliases=header_aliases,
            required_columns=required_columns,
            noun=noun,
        )
        return rows, detected_columns, "csv"
    if suffix in {".xlsx", ".xlsm"}:
        rows, detected_columns = _read_workbook_rows(
            payload=payload,
            header_aliases=header_aliases,
            required_columns=required_columns,
            noun=noun,
        )
        return rows, detected_columns, "xlsx"

    raise LedgerImportError(
        code=LedgerImportErrorCode.UNSUPPORTED_FILE_TYPE,
        message=f"Upload a CSV or XLSX {noun} file.",
    )


def _read_csv_rows(
    *,
    payload: bytes,
    header_aliases: dict[str, str],
    required_columns: frozenset[str],
    noun: str,
) -> tuple[tuple[dict[str, str], ...], frozenset[str]]:
    """Read CSV rows and normalize headers into canonical column names."""

    try:
        decoded_payload = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise LedgerImportError(
            code=LedgerImportErrorCode.INVALID_FILE,
            message="CSV files must be UTF-8 encoded.",
        ) from error

    reader = csv.DictReader(StringIO(decoded_payload))
    if reader.fieldnames is None:
        raise LedgerImportError(
            code=LedgerImportErrorCode.INVALID_FILE,
            message=f"The {noun} CSV file must include a header row.",
        )

    header_map = _build_header_map(
        headers=reader.fieldnames,
        header_aliases=header_aliases,
        required_columns=required_columns,
        noun=noun,
    )
    rows = [
        _canonicalize_row(raw_row=raw_row, header_map=header_map)
        for raw_row in reader
    ]
    if not rows:
        raise LedgerImportError(
            code=LedgerImportErrorCode.INVALID_FILE,
            message=f"The {noun} file does not contain any data rows.",
        )
    return tuple(rows), frozenset(header_map.values())


def _read_workbook_rows(
    *,
    payload: bytes,
    header_aliases: dict[str, str],
    required_columns: frozenset[str],
    noun: str,
) -> tuple[tuple[dict[str, str], ...], frozenset[str]]:
    """Read the first worksheet in an XLSX payload and normalize headers."""

    try:
        workbook = load_workbook(filename=BytesIO(payload), read_only=True, data_only=True)
    except Exception as error:
        raise LedgerImportError(
            code=LedgerImportErrorCode.INVALID_FILE,
            message="The workbook could not be opened. Upload a valid XLSX file.",
        ) from error

    worksheet = workbook.active
    row_iter = worksheet.iter_rows(values_only=True)
    try:
        header_row = next(row_iter)
    except StopIteration as error:
        raise LedgerImportError(
            code=LedgerImportErrorCode.INVALID_FILE,
            message=f"The {noun} workbook does not contain a header row.",
        ) from error

    headers = ["" if cell is None else str(cell) for cell in header_row]
    header_map = _build_header_map(
        headers=headers,
        header_aliases=header_aliases,
        required_columns=required_columns,
        noun=noun,
    )

    rows: list[dict[str, str]] = []
    for raw_row in row_iter:
        row_values = {
            str(headers[index]): "" if value is None else str(value)
            for index, value in enumerate(raw_row)
            if index < len(headers)
        }
        rows.append(_canonicalize_row(raw_row=row_values, header_map=header_map))

    if not rows:
        raise LedgerImportError(
            code=LedgerImportErrorCode.INVALID_FILE,
            message=f"The {noun} file does not contain any data rows.",
        )
    return tuple(rows), frozenset(header_map.values())


def _build_header_map(
    *,
    headers: Sequence[str],
    header_aliases: dict[str, str],
    required_columns: frozenset[str],
    noun: str,
) -> dict[str, str]:
    """Map source headers to canonical field names and validate required columns."""

    header_map: dict[str, str] = {}
    for header in headers:
        normalized = _normalize_header_name(header)
        canonical_name = header_aliases.get(normalized)
        if canonical_name is None:
            continue
        header_map[header] = canonical_name

    missing = sorted(required_columns.difference(header_map.values()))
    if missing:
        missing_columns = ", ".join(missing)
        raise LedgerImportError(
            code=LedgerImportErrorCode.INVALID_FILE,
            message=f"The {noun} file is missing required columns: {missing_columns}.",
        )

    return header_map


def _canonicalize_row(*, raw_row: dict[str, str], header_map: dict[str, str]) -> dict[str, str]:
    """Project one source row into canonical field names with trimmed string values."""

    canonical_row: dict[str, str] = {}
    for source_header, raw_value in raw_row.items():
        canonical_name = header_map.get(source_header)
        if canonical_name is None:
            continue
        canonical_row[canonical_name] = raw_value.strip()
    return canonical_row


def _parse_gl_row(*, row: dict[str, str], row_number: int) -> ImportedGeneralLedgerLineSeed:
    """Validate one canonical general-ledger row."""

    posting_date = _parse_required_date(
        row.get("posting_date"),
        field_name="posting_date",
        row_number=row_number,
    )
    account_code = _require_text(
        row.get("account_code"),
        field_name="account_code",
        row_number=row_number,
    )
    explicit_transaction_group_value = _optional_text(row.get("transaction_group_key"))
    debit_amount, credit_amount = _resolve_import_amounts(row=row, row_number=row_number)
    return ImportedGeneralLedgerLineSeed(
        line_no=row_number - 1,
        posting_date=posting_date,
        account_code=account_code,
        account_name=_optional_text(row.get("account_name")),
        reference=_optional_text(row.get("reference")) or explicit_transaction_group_value,
        description=_optional_text(row.get("description")),
        debit_amount=debit_amount,
        credit_amount=credit_amount,
        dimensions=_build_dimensions(row=row),
        external_ref=_optional_text(row.get("external_ref")),
        transaction_group_key=_build_transaction_group_key(
            row=row,
            posting_date=posting_date,
            line_no=row_number - 1,
        ),
    )


def _parse_tb_row(*, row: dict[str, str], row_number: int) -> ImportedTrialBalanceLineSeed:
    """Validate one canonical trial-balance row."""

    account_code = _require_text(
        row.get("account_code"),
        field_name="account_code",
        row_number=row_number,
    )
    debit_balance, credit_balance = _resolve_balance_amounts(row=row, row_number=row_number)
    return ImportedTrialBalanceLineSeed(
        line_no=row_number - 1,
        account_code=account_code,
        account_name=_optional_text(row.get("account_name")),
        account_type=_optional_text(row.get("account_type")),
        debit_balance=debit_balance,
        credit_balance=credit_balance,
        is_active=_parse_optional_bool(row.get("is_active"), default=True),
    )


def _resolve_import_amounts(*, row: dict[str, str], row_number: int) -> tuple[Decimal, Decimal]:
    """Resolve debit/credit values from one ledger row using the supported amount schemes."""

    signed_amount = _optional_decimal(row.get("signed_amount"))
    if signed_amount is not None:
        if signed_amount == Decimal("0"):
            raise LedgerImportError(
                code=LedgerImportErrorCode.INVALID_FILE,
                message=f"Row {row_number} has a zero signed_amount; ledger rows must be non-zero.",
            )
        return (
            signed_amount if signed_amount > 0 else Decimal("0.00"),
            abs(signed_amount) if signed_amount < 0 else Decimal("0.00"),
        )

    debit_amount = _optional_decimal(row.get("debit_amount"))
    credit_amount = _optional_decimal(row.get("credit_amount"))
    if debit_amount is not None or credit_amount is not None:
        resolved_debit = debit_amount or Decimal("0.00")
        resolved_credit = credit_amount or Decimal("0.00")
        _validate_single_sided_amount(
            debit_amount=resolved_debit,
            credit_amount=resolved_credit,
            row_number=row_number,
            noun="ledger",
        )
        return resolved_debit, resolved_credit

    amount = _optional_decimal(row.get("amount"))
    line_type = _optional_text(row.get("line_type"))
    if amount is not None and line_type is not None:
        normalized_line_type = line_type.lower()
        if normalized_line_type not in {"debit", "credit"}:
            raise LedgerImportError(
                code=LedgerImportErrorCode.INVALID_FILE,
                message=(
                    f"Row {row_number} has invalid line_type {line_type!r}; "
                    "use debit or credit."
                ),
            )
        if amount <= 0:
            raise LedgerImportError(
                code=LedgerImportErrorCode.INVALID_FILE,
                message=f"Row {row_number} amount must be greater than zero.",
            )
        return (
            amount if normalized_line_type == "debit" else Decimal("0.00"),
            amount if normalized_line_type == "credit" else Decimal("0.00"),
        )

    raise LedgerImportError(
        code=LedgerImportErrorCode.INVALID_FILE,
        message=(
            f"Row {row_number} must provide either signed_amount, debit/credit amounts, "
            "or amount with line_type."
        ),
    )


def _resolve_balance_amounts(*, row: dict[str, str], row_number: int) -> tuple[Decimal, Decimal]:
    """Resolve debit/credit balances from one trial-balance row."""

    debit_balance = _optional_decimal(row.get("debit_balance"))
    credit_balance = _optional_decimal(row.get("credit_balance"))
    if debit_balance is not None or credit_balance is not None:
        resolved_debit = debit_balance or Decimal("0.00")
        resolved_credit = credit_balance or Decimal("0.00")
        _validate_single_sided_amount(
            debit_amount=resolved_debit,
            credit_amount=resolved_credit,
            row_number=row_number,
            noun="trial balance",
            allow_zero=True,
        )
        return resolved_debit, resolved_credit

    balance = _optional_decimal(row.get("balance"))
    balance_side = _optional_text(row.get("balance_side"))
    if balance is not None and balance_side is not None:
        normalized_side = balance_side.lower()
        if normalized_side not in {"debit", "credit"}:
            raise LedgerImportError(
                code=LedgerImportErrorCode.INVALID_FILE,
                message=(
                    f"Row {row_number} has invalid balance_side {balance_side!r}; "
                    "use debit or credit."
                ),
            )
        if balance < 0:
            raise LedgerImportError(
                code=LedgerImportErrorCode.INVALID_FILE,
                message=f"Row {row_number} balance must be zero or greater.",
            )
        return (
            balance if normalized_side == "debit" else Decimal("0.00"),
            balance if normalized_side == "credit" else Decimal("0.00"),
        )

    raise LedgerImportError(
        code=LedgerImportErrorCode.INVALID_FILE,
        message=(
            f"Row {row_number} must provide debit/credit balances or balance with balance_side."
        ),
    )


def _validate_single_sided_amount(
    *,
    debit_amount: Decimal,
    credit_amount: Decimal,
    row_number: int,
    noun: str,
    allow_zero: bool = False,
) -> None:
    """Ensure one row does not carry both debit and credit amounts."""

    if debit_amount < 0 or credit_amount < 0:
        raise LedgerImportError(
            code=LedgerImportErrorCode.INVALID_FILE,
            message=(
                f"Row {row_number} in the {noun} file cannot contain "
                "negative debit/credit values."
            ),
        )
    if debit_amount > 0 and credit_amount > 0:
        raise LedgerImportError(
            code=LedgerImportErrorCode.INVALID_FILE,
            message=(
                f"Row {row_number} in the {noun} file cannot contain "
                "both debit and credit values."
            ),
        )
    if not allow_zero and debit_amount == 0 and credit_amount == 0:
        raise LedgerImportError(
            code=LedgerImportErrorCode.INVALID_FILE,
            message=f"Row {row_number} in the {noun} file must contain a non-zero amount.",
        )


def _require_text(value: str | None, *, field_name: str, row_number: int) -> str:
    """Return one required non-empty text field or raise a row-specific error."""

    normalized = _optional_text(value)
    if normalized is None:
        raise LedgerImportError(
            code=LedgerImportErrorCode.INVALID_FILE,
            message=f"Row {row_number} is missing required field {field_name}.",
        )
    return normalized


def _optional_text(value: str | None) -> str | None:
    """Normalize optional text values and collapse blanks to null."""

    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _optional_decimal(value: str | None) -> Decimal | None:
    """Parse one optional decimal string, preserving null when blank."""

    normalized = _optional_text(value)
    if normalized is None:
        return None
    sanitized = normalized.replace(",", "")
    try:
        return Decimal(sanitized)
    except InvalidOperation as error:
        raise LedgerImportError(
            code=LedgerImportErrorCode.INVALID_FILE,
            message=f"Value {normalized!r} is not a valid decimal amount.",
        ) from error


def _parse_required_date(value: str | None, *, field_name: str, row_number: int) -> date:
    """Parse one required date field from a supported spreadsheet/string format."""

    normalized = _require_text(value, field_name=field_name, row_number=row_number)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(normalized, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError as error:
        raise LedgerImportError(
            code=LedgerImportErrorCode.INVALID_FILE,
            message=(
                f"Row {row_number} field {field_name} must be a valid date; "
                f"received {normalized!r}."
            ),
        ) from error


def _parse_optional_bool(value: str | None, *, default: bool) -> bool:
    """Parse one optional boolean literal, returning the provided default when blank."""

    normalized = _optional_text(value)
    if normalized is None:
        return default
    lowered = normalized.lower()
    if lowered in _TRUE_LITERALS:
        return True
    if lowered in _FALSE_LITERALS:
        return False
    raise LedgerImportError(
        code=LedgerImportErrorCode.INVALID_FILE,
        message=f"Boolean field value {normalized!r} is not supported.",
    )


def _build_dimensions(*, row: dict[str, str]) -> JsonObject:
    """Extract the supported accounting-dimension fields from one import row."""

    dimensions: JsonObject = {}
    for key in ("cost_centre", "department", "project"):
        value = _optional_text(row.get(key))
        if value is not None:
            dimensions[key] = value
    return dimensions


def _build_transaction_group_key(
    *,
    row: dict[str, str],
    posting_date: date,
    line_no: int,
) -> str:
    """Return one canonical transaction-group key for imported GL rows."""

    explicit_group_value = _normalize_group_key_component(row.get("transaction_group_key"))
    if explicit_group_value is not None:
        return _hash_transaction_group_seed(
            posting_date=posting_date,
            source_name="explicit",
            source_value=explicit_group_value,
        )

    for source_name in ("external_ref", "reference", "description"):
        normalized_value = _normalize_group_key_component(row.get(source_name))
        if normalized_value is not None:
            return _hash_transaction_group_seed(
                posting_date=posting_date,
                source_name=source_name,
                source_value=normalized_value,
            )

    return _hash_transaction_group_seed(
        posting_date=posting_date,
        source_name="line",
        source_value=str(line_no),
    )


def _normalize_group_key_component(value: str | None) -> str | None:
    """Normalize one grouping value into a stable case-insensitive token."""

    normalized = _optional_text(value)
    if normalized is None:
        return None
    return normalized.lower()


def _hash_transaction_group_seed(
    *,
    posting_date: date,
    source_name: str,
    source_value: str,
) -> str:
    """Hash one canonical transaction grouping seed into a compact stable key."""

    seed = f"{posting_date.isoformat()}|{source_name}|{source_value}"
    return f"glgrp_{hashlib.md5(seed.encode('utf-8'), usedforsecurity=False).hexdigest()}"


def _normalize_header_name(value: str) -> str:
    """Normalize header text into a lowercase underscore form used by alias maps."""

    return (
        value.strip()
        .lower()
        .replace("-", "_")
        .replace("/", "_")
        .replace(" ", "_")
    )


__all__ = [
    "ImportedGeneralLedgerFile",
    "ImportedGeneralLedgerLineSeed",
    "ImportedTrialBalanceFile",
    "ImportedTrialBalanceLineSeed",
    "LedgerImportError",
    "LedgerImportErrorCode",
    "import_general_ledger_file",
    "import_trial_balance_file",
]
