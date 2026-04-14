"""
Purpose: Separate application-managed secrets from business-data persistence concerns.
Scope: Read environment-backed provider secrets and vend the credential cipher used for
encrypted integration tokens stored in the database.
Dependencies: Shared application settings and the credential cipher helper.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.common.settings import AppSettings, get_settings
from services.security.crypto import CredentialCipher


class SecretStoreError(ValueError):
    """Represent a missing or invalid application secret with explicit recovery guidance."""


@dataclass(frozen=True, slots=True)
class QuickBooksClientSecrets:
    """Capture the application-level QuickBooks OAuth client configuration."""

    client_id: str
    client_secret: str
    redirect_uri: str
    sandbox_company_id: str | None
    use_sandbox: bool


class SecretStore:
    """Provide the canonical secret-access surface for provider keys and OAuth clients."""

    def __init__(self, *, settings: AppSettings | None = None) -> None:
        """Capture the current application settings for explicit, fail-fast secret reads."""

        self._settings = settings or get_settings()

    def get_model_gateway_api_key(self) -> str:
        """Return the configured model-provider API key from app secrets, never from the DB."""

        self._settings.require_values(
            feature_name="Model gateway",
            variables={"model_gateway_api_key": self._settings.model_gateway.api_key},
        )
        api_key = self._settings.model_gateway.api_key
        if api_key is None:
            raise SecretStoreError(
                "Model gateway is not configured. Set model_gateway_api_key and restart the app."
            )

        return api_key.get_secret_value()

    def get_quickbooks_client_secrets(self) -> QuickBooksClientSecrets:
        """Return the QuickBooks OAuth client secrets from application settings only."""

        self._settings.require_values(
            feature_name="QuickBooks OAuth",
            variables={
                "quickbooks_client_id": self._settings.quickbooks.client_id,
                "quickbooks_client_secret": self._settings.quickbooks.client_secret,
                "quickbooks_redirect_uri": self._settings.quickbooks.redirect_uri,
            },
        )
        client_secret = self._settings.quickbooks.client_secret
        redirect_uri = self._settings.quickbooks.redirect_uri
        if (
            client_secret is None
            or self._settings.quickbooks.client_id is None
            or redirect_uri is None
        ):
            raise SecretStoreError(
                "QuickBooks OAuth is not configured. Set the QuickBooks env vars and restart."
            )

        return QuickBooksClientSecrets(
            client_id=self._settings.quickbooks.client_id,
            client_secret=client_secret.get_secret_value(),
            redirect_uri=redirect_uri,
            sandbox_company_id=self._settings.quickbooks.sandbox_company_id,
            use_sandbox=self._settings.quickbooks.use_sandbox,
        )

    def get_credential_cipher(self) -> CredentialCipher:
        """Return the cipher used to encrypt integration credentials before DB persistence."""

        self._settings.require_values(
            feature_name="Credential encryption",
            variables={
                "security_credential_encryption_key": (
                    self._settings.security.credential_encryption_key
                )
            },
        )
        encryption_key = self._settings.security.credential_encryption_key
        if encryption_key is None:
            raise SecretStoreError(
                "Credential encryption is not configured. Set security_credential_encryption_key."
            )

        return CredentialCipher(base64_key=encryption_key)


__all__ = ["QuickBooksClientSecrets", "SecretStore", "SecretStoreError"]
