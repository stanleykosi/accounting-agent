"""
Purpose: Cover QuickBooks OAuth return-url validation edge cases.
Scope: Loopback browser callbacks and hosted-origin allowlist enforcement.
Dependencies: services/integrations/quickbooks/oauth.py and shared settings models.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from services.common.settings import AppSettings
from services.integrations.quickbooks.oauth import QuickBooksOAuthError, _validate_return_url


@pytest.mark.parametrize(
    ("return_url"),
    (
        "http://127.0.0.1:3000/entities/demo/integrations",
        "http://localhost:1420/entities/demo/integrations",
    ),
)
def test_validate_return_url_allows_loopback_origins_on_explicit_ports(return_url: str) -> None:
    """Ensure standard local UI dev ports remain valid QuickBooks callback destinations."""

    _validate_return_url(return_url=return_url, settings=_quickbooks_settings())


def test_validate_return_url_rejects_unconfigured_hosted_origin() -> None:
    """Ensure hosted browser callbacks still require an explicit allowlist entry."""

    with pytest.raises(QuickBooksOAuthError, match="allowed desktop or hosted web application"):
        _validate_return_url(
            return_url="https://app.example.com/entities/demo/integrations",
            settings=_quickbooks_settings(),
        )


def _quickbooks_settings() -> AppSettings:
    """Build deterministic settings for return-url validation tests."""

    return AppSettings(
        quickbooks={
            "client_id": "quickbooks-client-id",
            "client_secret": SecretStr("quickbooks-client-secret"),
            "redirect_uri": "http://127.0.0.1:8000/api/integrations/quickbooks/callback",
            "use_sandbox": True,
        },
        security={
            "credential_encryption_key": SecretStr(
                "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
            ),
            "session_secret": SecretStr("session-secret"),
            "token_signing_secret": SecretStr("token-signing-secret"),
        },
    )
