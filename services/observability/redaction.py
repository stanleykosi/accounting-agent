"""
Purpose: Provide one canonical redaction boundary for structured logs and telemetry attributes.
Scope: Recursive payload sanitization, credential masking, and sensitive financial-value redaction.
Dependencies: Python standard-library regex helpers only so the module stays importable from
low-level logging and observability code without circular imports.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

REDACTED_VALUE = "***REDACTED***"
REDACTED_CREDENTIAL = "***REDACTED_CREDENTIAL***"
REDACTED_FINANCIAL_VALUE = "***REDACTED_FINANCIAL_VALUE***"

_SENSITIVE_FIELD_NAMES = {
    "access_key",
    "access_token",
    "api_key",
    "authorization",
    "bank_account",
    "bvn",
    "client_secret",
    "cookie",
    "credential",
    "credentials",
    "iban",
    "password",
    "private_key",
    "refresh_token",
    "routing_number",
    "secret",
    "session",
    "sort_code",
    "tax_id",
    "token",
}
_FINANCIAL_FIELD_NAMES = {
    "amount",
    "amount_due",
    "balance",
    "credit",
    "credit_amount",
    "debit",
    "debit_amount",
    "exchange_rate",
    "gross_amount",
    "gross_pay",
    "net_amount",
    "net_pay",
    "opening_balance",
    "outstanding_balance",
    "subtotal",
    "tax_amount",
    "total",
    "unit_price",
    "variance_amount",
}
_TOKEN_PATTERN = re.compile(r"(?i)\b(bearer|basic)\s+[a-z0-9._~+/=-]+")
_QUERY_SECRET_PATTERN = re.compile(
    r"(?i)\b("
    r"access_token|api_key|authorization|client_secret|password|refresh_token|token"
    r")\s*[:=]\s*([^\s,;]+)"
)
_COOKIE_PATTERN = re.compile(r"(?i)\b(set-cookie|cookie)\s*[:=]\s*([^\n]+)")
_JWT_PATTERN = re.compile(r"\beyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9._-]+\.[a-zA-Z0-9._-]+\b")


def redact_log_payload(
    value: Any,
    *,
    sensitive_field_names: Iterable[str] = (),
    redact_financial_values: bool = True,
) -> Any:
    """Return a recursively redacted copy of a log or telemetry payload."""

    combined_sensitive_names = {
        *_SENSITIVE_FIELD_NAMES,
        *(_normalize_field_name(field_name) for field_name in sensitive_field_names),
    }
    return _redact_value(
        value,
        field_name=None,
        sensitive_field_names=combined_sensitive_names,
        redact_financial_values=redact_financial_values,
    )


def _redact_value(
    value: Any,
    *,
    field_name: str | None,
    sensitive_field_names: set[str],
    redact_financial_values: bool,
) -> Any:
    """Redact one value while preserving enough structure for operators to debug safely."""

    normalized_field_name = _normalize_field_name(field_name)
    if normalized_field_name in sensitive_field_names:
        return REDACTED_CREDENTIAL

    if redact_financial_values and normalized_field_name in _FINANCIAL_FIELD_NAMES:
        return REDACTED_FINANCIAL_VALUE

    if isinstance(value, Mapping):
        return {
            str(key): _redact_value(
                item_value,
                field_name=str(key),
                sensitive_field_names=sensitive_field_names,
                redact_financial_values=redact_financial_values,
            )
            for key, item_value in value.items()
        }

    if isinstance(value, list):
        return [
            _redact_value(
                item,
                field_name=field_name,
                sensitive_field_names=sensitive_field_names,
                redact_financial_values=redact_financial_values,
            )
            for item in value
        ]

    if isinstance(value, tuple):
        return tuple(
            _redact_value(
                item,
                field_name=field_name,
                sensitive_field_names=sensitive_field_names,
                redact_financial_values=redact_financial_values,
            )
            for item in value
        )

    if isinstance(value, (int, float, Decimal)) and (
        redact_financial_values and normalized_field_name in _FINANCIAL_FIELD_NAMES
    ):
        return REDACTED_FINANCIAL_VALUE

    if isinstance(value, str):
        if redact_financial_values and normalized_field_name in _FINANCIAL_FIELD_NAMES:
            return REDACTED_FINANCIAL_VALUE
        return _redact_string_fragments(value)

    return value


def _redact_string_fragments(value: str) -> str:
    """Mask inline secrets and session-like strings embedded in free-form log messages."""

    redacted_value = _TOKEN_PATTERN.sub(
        lambda match: f"{match.group(1)} {REDACTED_CREDENTIAL}",
        value,
    )
    redacted_value = _QUERY_SECRET_PATTERN.sub(
        lambda match: f"{match.group(1)}={REDACTED_CREDENTIAL}",
        redacted_value,
    )
    redacted_value = _COOKIE_PATTERN.sub(
        lambda match: f"{match.group(1)}={REDACTED_CREDENTIAL}",
        redacted_value,
    )
    return _JWT_PATTERN.sub(REDACTED_CREDENTIAL, redacted_value)


def _normalize_field_name(field_name: str | None) -> str:
    """Normalize a field name into a stable comparison token."""

    if field_name is None:
        return ""

    return re.sub(r"[^a-z0-9]+", "_", field_name.casefold()).strip("_")


__all__ = [
    "REDACTED_CREDENTIAL",
    "REDACTED_FINANCIAL_VALUE",
    "REDACTED_VALUE",
    "redact_log_payload",
]
