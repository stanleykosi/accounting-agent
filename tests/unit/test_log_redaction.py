"""
Purpose: Verify the canonical observability redaction helpers prevent credentials and financial
values from leaking into logs and telemetry attributes.
Scope: Recursive mapping redaction, free-form token masking, and preservation of safe metadata.
Dependencies: services/observability/redaction.py and observability event helpers.
"""

from __future__ import annotations

import subprocess
import sys

from services.observability.events import _build_metric_attributes
from services.observability.redaction import (
    REDACTED_CREDENTIAL,
    REDACTED_FINANCIAL_VALUE,
    redact_log_payload,
)


def test_redact_log_payload_masks_sensitive_fields_recursively() -> None:
    """Sensitive credential-like keys should be redacted even when deeply nested."""

    payload = {
        "authorization": "Bearer super-secret-token",
        "nested": {
            "client_secret": "abc123",
            "session": "session-cookie",
        },
        "safe_field": "visible",
    }

    redacted = redact_log_payload(payload)

    assert redacted["authorization"] == REDACTED_CREDENTIAL
    assert redacted["nested"]["client_secret"] == REDACTED_CREDENTIAL
    assert redacted["nested"]["session"] == REDACTED_CREDENTIAL
    assert redacted["safe_field"] == "visible"


def test_redact_log_payload_masks_financial_values_but_preserves_context() -> None:
    """Financial amount fields should be hidden while adjacent non-sensitive metadata remains."""

    payload = {
        "amount": "125000.45",
        "currency": "NGN",
        "line_items": [
            {
                "description": "Consulting fee",
                "unit_price": 2500,
            }
        ],
    }

    redacted = redact_log_payload(payload)

    assert redacted["amount"] == REDACTED_FINANCIAL_VALUE
    assert redacted["currency"] == "NGN"
    assert redacted["line_items"][0]["description"] == "Consulting fee"
    assert redacted["line_items"][0]["unit_price"] == REDACTED_FINANCIAL_VALUE


def test_redact_log_payload_masks_inline_headers_and_tokens_in_free_text() -> None:
    """Free-form messages should not leak bearer tokens, cookies, or JWT-like strings."""

    message = (
        "Authorization: Bearer abc.def.ghi Cookie=sessionid=xyz "
        "password=hunter2 raw_jwt=eyJhbGciOiJIUzI1NiJ9.payload.signature"
    )

    redacted = redact_log_payload({"message": message})

    assert REDACTED_CREDENTIAL in redacted["message"]
    assert "hunter2" not in redacted["message"]
    assert "sessionid=xyz" not in redacted["message"]
    assert "eyJhbGciOiJIUzI1NiJ9.payload.signature" not in redacted["message"]


def test_redact_log_payload_honors_additional_sensitive_field_names() -> None:
    """Caller-supplied field names should extend the default redaction list."""

    payload = {
        "vendor_reference": "INV-2026-0001",
        "custom_secret_value": "should-not-leak",
    }

    redacted = redact_log_payload(
        payload,
        sensitive_field_names=("custom_secret_value",),
    )

    assert redacted["vendor_reference"] == "INV-2026-0001"
    assert redacted["custom_secret_value"] == REDACTED_CREDENTIAL


def test_logging_module_imports_cleanly_in_fresh_interpreter() -> None:
    """Importing logging directly should not trigger a circular import through observability."""

    completed = subprocess.run(
        [sys.executable, "-c", "import services.common.logging"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_metric_attributes_drop_high_cardinality_fields() -> None:
    """Operational metrics should exclude per-request and per-task identifiers."""

    metric_attributes = _build_metric_attributes(
        {
            "duration_ms": 25.1,
            "error_message": "request failed",
            "error_type": "ValueError",
            "event_name": "api.request",
            "http_method": "GET",
            "http_path": "/api/entities/123/reports",
            "outcome": "failed",
            "request_id": "req-123",
            "route_group": "reports",
            "status_code": 500,
            "task_id": "task-123",
            "task_name": "reporting.generate_close_run_pack",
            "trace_id": "trace-123",
            "workflow_area": "reporting",
        }
    )

    assert metric_attributes == {
        "error_type": "ValueError",
        "event_name": "api.request",
        "http_method": "GET",
        "outcome": "failed",
        "route_group": "reports",
        "status_code": 500,
        "task_name": "reporting.generate_close_run_pack",
        "workflow_area": "reporting",
    }
