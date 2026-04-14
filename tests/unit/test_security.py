"""
Purpose: Verify the canonical encryption and secret-store behavior introduced for Step 14.
Scope: Unit coverage for credential encryption round-trips, fail-fast validation,
and environment-backed secret access.
Dependencies: Security helpers plus the shared application settings model.
"""

from __future__ import annotations

import base64

import pytest
from pydantic import SecretStr
from services.common.settings import AppSettings
from services.common.types import JsonObject
from services.security.crypto import CredentialCipher, CredentialCipherError
from services.security.secret_store import SecretStore

TEST_KEY = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")


def test_credential_cipher_round_trips_json_payload() -> None:
    """Ensure encrypted credential envelopes decrypt back to the original JSON payload."""

    cipher = CredentialCipher(base64_key=TEST_KEY)
    payload: JsonObject = {
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "expires_in": 3600,
    }

    envelope = cipher.encrypt_json(
        payload=payload,
        context="quickbooks_online:entity:1234",
    )
    decrypted = cipher.decrypt_json(
        envelope=envelope,
        context="quickbooks_online:entity:1234",
    )

    assert envelope["algorithm"] == "aes-256-gcm"
    assert decrypted == payload


def test_credential_cipher_rejects_wrong_context() -> None:
    """Ensure decryption fails when callers provide a different encryption context."""

    cipher = CredentialCipher(base64_key=TEST_KEY)
    envelope = cipher.encrypt_json(
        payload={"refresh_token": "refresh-token"},
        context="quickbooks_online:entity:1234",
    )

    with pytest.raises(CredentialCipherError):
        cipher.decrypt_json(
            envelope=envelope,
            context="quickbooks_online:entity:9999",
        )


def test_secret_store_returns_provider_and_encryption_secrets() -> None:
    """Ensure the secret store keeps provider secrets in settings and vends the DB cipher."""

    settings = AppSettings.model_construct(
        model_gateway=AppSettings().model_gateway.model_copy(
            update={"api_key": SecretStr("openrouter-test-key")}
        ),
        quickbooks=AppSettings().quickbooks.model_copy(
            update={
                "client_id": "quickbooks-client-id",
                "client_secret": SecretStr("quickbooks-client-secret"),
            }
        ),
        security=AppSettings().security.model_copy(
            update={"credential_encryption_key": SecretStr(TEST_KEY)}
        ),
    )
    secret_store = SecretStore(settings=settings)

    assert secret_store.get_model_gateway_api_key() == "openrouter-test-key"
    assert secret_store.get_quickbooks_client_secrets().client_id == "quickbooks-client-id"
    assert isinstance(secret_store.get_credential_cipher(), CredentialCipher)


def test_secret_store_fails_fast_when_encryption_key_is_missing() -> None:
    """Ensure missing encryption keys produce a direct recovery message."""

    settings = AppSettings.model_construct(
        security=AppSettings().security.model_copy(update={"credential_encryption_key": None})
    )

    with pytest.raises(ValueError) as error:
        SecretStore(settings=settings).get_credential_cipher()

    assert "security_credential_encryption_key" in str(error.value)


def test_secret_store_fails_fast_when_quickbooks_redirect_uri_is_missing() -> None:
    """Ensure QuickBooks OAuth remains opt-in when hosted env leaves the redirect blank."""

    settings = AppSettings.model_construct(
        quickbooks=AppSettings().quickbooks.model_copy(
            update={
                "client_id": "quickbooks-client-id",
                "client_secret": SecretStr("quickbooks-client-secret"),
                "redirect_uri": None,
            }
        )
    )

    with pytest.raises(ValueError) as error:
        SecretStore(settings=settings).get_quickbooks_client_secrets()

    assert "quickbooks_redirect_uri" in str(error.value)
