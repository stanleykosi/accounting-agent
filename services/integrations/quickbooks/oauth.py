"""
Purpose: Handle QuickBooks Online OAuth authorization, token exchange, refresh, revocation, and
signed callback state for entity-scoped integration connections.
Scope: QuickBooks OAuth URLs, HMAC-protected state payloads, token lifetime normalization, and
credential encryption/decryption at the integration persistence boundary.
Dependencies: httpx, shared settings, secret-store accessors, and encrypted integration records.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, cast
from uuid import UUID

import httpx
from pydantic import SecretStr
from services.common.settings import AppSettings, get_settings
from services.common.types import JsonObject, utc_now
from services.db.repositories.integration_repo import IntegrationConnectionRecord
from services.security.secret_store import QuickBooksClientSecrets, SecretStore, SecretStoreError

_AUTHORIZATION_URL: Final[str] = "https://appcenter.intuit.com/connect/oauth2"
_TOKEN_URL: Final[str] = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
_REVOCATION_URL: Final[str] = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
_QUICKBOOKS_SCOPE: Final[str] = "com.intuit.quickbooks.accounting"
_STATE_MAX_AGE_SECONDS: Final[int] = 10 * 60
_TOKEN_EXPIRY_SKEW_SECONDS: Final[int] = 300
_ALLOWED_LOOPBACK_RETURN_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost"})


class QuickBooksOAuthError(Exception):
    """Represent an expected OAuth failure with a recovery-focused diagnostic."""


class QuickBooksReauthorizationRequiredError(QuickBooksOAuthError):
    """Signal that the operator must reconnect QuickBooks before sync can resume."""


@dataclass(frozen=True, slots=True)
class QuickBooksAuthorizationStart:
    """Describe a generated QuickBooks authorization redirect."""

    authorization_url: str
    state: str


@dataclass(frozen=True, slots=True)
class QuickBooksOAuthState:
    """Describe the signed state restored during the OAuth callback."""

    entity_id: UUID
    actor_user_id: UUID
    nonce: str
    return_url: str
    issued_at_epoch: int


@dataclass(frozen=True, slots=True)
class QuickBooksTokenSet:
    """Describe decrypted QuickBooks tokens with UTC expiry timestamps."""

    access_token: str
    refresh_token: str
    token_type: str
    access_token_expires_at: datetime
    refresh_token_expires_at: datetime
    realm_id: str


class QuickBooksOAuth:
    """Manage QuickBooks Online OAuth calls and encrypted token persistence envelopes."""

    def __init__(
        self,
        *,
        settings: AppSettings | None = None,
        secret_store: SecretStore | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        """Capture settings, secret access, and an injectable HTTP client for testable OAuth IO."""

        self._settings = settings or get_settings()
        self._secret_store = secret_store or SecretStore(settings=self._settings)
        self._http_client = http_client or httpx.Client(timeout=30.0)

    def build_authorization_url(
        self,
        *,
        entity_id: UUID,
        actor_user_id: UUID,
        return_url: str,
        now_epoch: int | None = None,
    ) -> QuickBooksAuthorizationStart:
        """Build a signed OAuth authorization URL for one entity workspace connection."""

        client_secrets = self._read_client_secrets()
        state = self.issue_state(
            entity_id=entity_id,
            actor_user_id=actor_user_id,
            return_url=return_url,
            now_epoch=now_epoch,
        )
        params = {
            "client_id": client_secrets.client_id,
            "response_type": "code",
            "scope": _QUICKBOOKS_SCOPE,
            "redirect_uri": client_secrets.redirect_uri,
            "state": state,
        }
        authorization_url = httpx.URL(_AUTHORIZATION_URL, params=params)
        return QuickBooksAuthorizationStart(authorization_url=str(authorization_url), state=state)

    def issue_state(
        self,
        *,
        entity_id: UUID,
        actor_user_id: UUID,
        return_url: str,
        now_epoch: int | None = None,
    ) -> str:
        """Create an HMAC-signed state payload used to validate the OAuth callback."""

        _validate_return_url(return_url=return_url, settings=self._settings)
        payload = {
            "actor_user_id": str(actor_user_id),
            "entity_id": str(entity_id),
            "issued_at_epoch": now_epoch or int(time.time()),
            "nonce": secrets.token_urlsafe(24),
            "return_url": return_url,
        }
        encoded_payload = _base64url_encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        signature = _sign_state(encoded_payload=encoded_payload, secret=self._state_secret())
        return f"{encoded_payload}.{signature}"

    def validate_state(
        self,
        *,
        state: str,
        now_epoch: int | None = None,
    ) -> QuickBooksOAuthState:
        """Validate callback state integrity, age, and required entity/user fields."""

        try:
            encoded_payload, supplied_signature = state.split(".", 1)
        except ValueError as error:
            raise QuickBooksOAuthError("QuickBooks callback state is malformed.") from error

        expected_signature = _sign_state(
            encoded_payload=encoded_payload,
            secret=self._state_secret(),
        )
        if not hmac.compare_digest(expected_signature, supplied_signature):
            raise QuickBooksOAuthError("QuickBooks callback state signature is invalid.")

        try:
            raw_payload = json.loads(_base64url_decode(encoded_payload).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise QuickBooksOAuthError(
                "QuickBooks callback state payload is not valid JSON."
            ) from error

        if not isinstance(raw_payload, dict):
            raise QuickBooksOAuthError("QuickBooks callback state payload must be a JSON object.")

        issued_at_epoch = _require_int(raw_payload, "issued_at_epoch")
        observed_epoch = now_epoch or int(time.time())
        if observed_epoch - issued_at_epoch > _STATE_MAX_AGE_SECONDS:
            raise QuickBooksOAuthError("QuickBooks callback state expired. Start connection again.")

        return_url = _require_str(raw_payload, "return_url")
        _validate_return_url(return_url=return_url, settings=self._settings)
        try:
            return QuickBooksOAuthState(
                actor_user_id=UUID(_require_str(raw_payload, "actor_user_id")),
                entity_id=UUID(_require_str(raw_payload, "entity_id")),
                issued_at_epoch=issued_at_epoch,
                nonce=_require_str(raw_payload, "nonce"),
                return_url=return_url,
            )
        except ValueError as error:
            raise QuickBooksOAuthError(
                "QuickBooks callback state contains an invalid UUID."
            ) from error

    def exchange_code_for_tokens(
        self,
        *,
        code: str,
        realm_id: str,
    ) -> QuickBooksTokenSet:
        """Exchange one authorization code for encrypted-storable QuickBooks tokens."""

        normalized_code = code.strip()
        normalized_realm_id = realm_id.strip()
        if not normalized_code:
            raise QuickBooksOAuthError("QuickBooks authorization code cannot be empty.")
        if not normalized_realm_id:
            raise QuickBooksOAuthError("QuickBooks realmId cannot be empty.")

        client_secrets = self._read_client_secrets()
        response = self._post_token_form(
            data={
                "code": normalized_code,
                "grant_type": "authorization_code",
                "redirect_uri": client_secrets.redirect_uri,
            },
        )
        return _parse_token_response(payload=response, realm_id=normalized_realm_id, now=utc_now())

    def refresh_tokens(
        self,
        *,
        refresh_token: str,
        realm_id: str,
    ) -> QuickBooksTokenSet:
        """Refresh access credentials and return QuickBooks' rotated refresh token values."""

        normalized_refresh_token = refresh_token.strip()
        if not normalized_refresh_token:
            raise QuickBooksReauthorizationRequiredError(
                "QuickBooks refresh token is missing. Reconnect QuickBooks to resume sync."
            )

        try:
            response = self._post_token_form(
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": normalized_refresh_token,
                },
            )
        except QuickBooksReauthorizationRequiredError:
            raise
        except QuickBooksOAuthError as error:
            raise QuickBooksOAuthError(f"QuickBooks token refresh failed: {error}") from error

        return _parse_token_response(payload=response, realm_id=realm_id, now=utc_now())

    def revoke_token(self, *, token: str) -> bool:
        """Revoke one QuickBooks token and return whether Intuit accepted the revocation."""

        normalized_token = token.strip()
        if not normalized_token:
            return False

        client_secrets = self._read_client_secrets()
        response = self._http_client.post(
            _REVOCATION_URL,
            auth=(client_secrets.client_id, client_secrets.client_secret),
            data={"token": normalized_token},
            headers={"Accept": "application/json"},
        )
        return response.status_code == 200

    def encrypt_token_set(self, *, entity_id: UUID, token_set: QuickBooksTokenSet) -> JsonObject:
        """Encrypt one token set for storage in the integration connection table."""

        cipher = self._secret_store.get_credential_cipher()
        return cipher.encrypt_json(
            payload=_serialize_token_set(token_set=token_set),
            context=_credential_context(entity_id=entity_id, realm_id=token_set.realm_id),
        )

    def decrypt_connection_tokens(
        self,
        *,
        connection: IntegrationConnectionRecord,
    ) -> QuickBooksTokenSet:
        """Decrypt persisted QuickBooks credentials for one connection record."""

        if not connection.encrypted_credentials:
            raise QuickBooksReauthorizationRequiredError(
                "QuickBooks credentials are not present. Reconnect QuickBooks to resume sync."
            )
        cipher = self._secret_store.get_credential_cipher()
        payload = cipher.decrypt_json(
            envelope=connection.encrypted_credentials,
            context=_credential_context(
                entity_id=connection.entity_id,
                realm_id=connection.external_realm_id,
            ),
        )
        return _deserialize_token_set(payload=payload)

    def should_refresh(self, *, token_set: QuickBooksTokenSet, now: datetime | None = None) -> bool:
        """Return whether the access token is expired or within the configured refresh skew."""

        observed_at = now or utc_now()
        return token_set.access_token_expires_at <= observed_at + timedelta(
            seconds=_TOKEN_EXPIRY_SKEW_SECONDS
        )

    def _post_token_form(self, *, data: dict[str, str]) -> JsonObject:
        """POST form data to Intuit's token endpoint and normalize expected OAuth failures."""

        client_secrets = self._read_client_secrets()
        response = self._http_client.post(
            _TOKEN_URL,
            auth=(client_secrets.client_id, client_secrets.client_secret),
            data=data,
            headers={"Accept": "application/json"},
        )
        payload = _safe_json_payload(response=response)
        if response.is_success:
            return payload

        error_code = payload.get("error")
        if error_code == "invalid_grant":
            raise QuickBooksReauthorizationRequiredError(
                "QuickBooks rejected the refresh token. Reconnect QuickBooks and retry the sync."
            )
        raise QuickBooksOAuthError(
            f"QuickBooks token endpoint returned HTTP {response.status_code}."
        )

    def _read_client_secrets(self) -> QuickBooksClientSecrets:
        """Return QuickBooks client settings with a stable OAuth-domain error."""

        try:
            return self._secret_store.get_quickbooks_client_secrets()
        except (SecretStoreError, ValueError) as error:
            raise QuickBooksOAuthError(str(error)) from error

    def _state_secret(self) -> str:
        """Return the configured state signing secret or fail fast with recovery guidance."""

        secret: SecretStr | None = (
            self._settings.security.token_signing_secret
            or self._settings.security.session_secret
        )
        if secret is None:
            raise QuickBooksOAuthError(
                "QuickBooks OAuth state signing is not configured. Set token_signing_secret."
            )
        return secret.get_secret_value()


def get_quickbooks_oauth(
    *,
    settings: AppSettings | None = None,
    http_client: httpx.Client | None = None,
) -> QuickBooksOAuth:
    """Create a QuickBooks OAuth helper with the canonical settings and optional HTTP client."""

    return QuickBooksOAuth(settings=settings, http_client=http_client)


def _parse_token_response(
    *,
    payload: JsonObject,
    realm_id: str,
    now: datetime,
) -> QuickBooksTokenSet:
    """Validate Intuit token JSON and convert durations into explicit UTC timestamps."""

    access_token = _require_str(payload, "access_token")
    refresh_token = _require_str(payload, "refresh_token")
    token_type = str(payload.get("token_type") or "bearer")
    expires_in = _require_int(payload, "expires_in")
    refresh_expires_in = _require_int(payload, "x_refresh_token_expires_in")
    return QuickBooksTokenSet(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type=token_type,
        access_token_expires_at=now + timedelta(seconds=expires_in),
        refresh_token_expires_at=now + timedelta(seconds=refresh_expires_in),
        realm_id=realm_id,
    )


def _serialize_token_set(*, token_set: QuickBooksTokenSet) -> JsonObject:
    """Serialize a token set into a JSON object safe for encryption."""

    return {
        "access_token": token_set.access_token,
        "access_token_expires_at": token_set.access_token_expires_at.isoformat(),
        "realm_id": token_set.realm_id,
        "refresh_token": token_set.refresh_token,
        "refresh_token_expires_at": token_set.refresh_token_expires_at.isoformat(),
        "token_type": token_set.token_type,
    }


def _deserialize_token_set(*, payload: JsonObject) -> QuickBooksTokenSet:
    """Deserialize a decrypted token payload and fail fast when fields are malformed."""

    try:
        return QuickBooksTokenSet(
            access_token=_require_str(payload, "access_token"),
            refresh_token=_require_str(payload, "refresh_token"),
            token_type=_require_str(payload, "token_type"),
            access_token_expires_at=_parse_aware_datetime(
                _require_str(payload, "access_token_expires_at")
            ),
            refresh_token_expires_at=_parse_aware_datetime(
                _require_str(payload, "refresh_token_expires_at")
            ),
            realm_id=_require_str(payload, "realm_id"),
        )
    except ValueError as error:
        raise QuickBooksOAuthError("Stored QuickBooks token payload is malformed.") from error


def _credential_context(*, entity_id: UUID, realm_id: str) -> str:
    """Build the authenticated-encryption context for one QuickBooks token envelope."""

    return f"quickbooks_online:{entity_id}:{realm_id}"


def _safe_json_payload(*, response: httpx.Response) -> JsonObject:
    """Parse an HTTP response as a JSON object, returning an empty object for blank bodies."""

    if not response.content:
        return {}
    try:
        payload = response.json()
    except json.JSONDecodeError as error:
        raise QuickBooksOAuthError("QuickBooks returned a non-JSON OAuth response.") from error
    if not isinstance(payload, dict):
        raise QuickBooksOAuthError("QuickBooks OAuth responses must be JSON objects.")
    return cast(JsonObject, payload)


def _require_str(payload: JsonObject | dict[str, object], key: str) -> str:
    """Read a required non-empty string field from a JSON payload."""

    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise QuickBooksOAuthError(f"QuickBooks OAuth payload is missing {key}.")
    return value.strip()


def _require_int(payload: JsonObject | dict[str, object], key: str) -> int:
    """Read a required integer field from a JSON payload."""

    value = payload.get(key)
    if isinstance(value, bool):
        raise QuickBooksOAuthError(f"QuickBooks OAuth payload field {key} must be an integer.")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise QuickBooksOAuthError(f"QuickBooks OAuth payload is missing integer field {key}.")


def _parse_aware_datetime(value: str) -> datetime:
    """Parse an ISO timestamp and normalize it to UTC."""

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Stored QuickBooks token timestamps must include timezone info.")
    return parsed.astimezone(UTC)


def _validate_return_url(*, return_url: str, settings: AppSettings) -> None:
    """Allow relative URLs plus explicitly configured local or hosted web return URLs."""

    normalized = return_url.strip()
    if not normalized:
        raise QuickBooksOAuthError("QuickBooks return URL cannot be empty.")
    if normalized.startswith("/"):
        return
    parsed = httpx.URL(normalized)
    if _is_allowed_loopback_return_origin(parsed):
        return

    normalized_origin = _normalize_origin(parsed)
    allowed_origins = set(settings.quickbooks.allowed_return_origins)
    if normalized_origin not in allowed_origins:
        raise QuickBooksOAuthError(
            "QuickBooks return URL must point to an allowed desktop or hosted web application."
        )


def _is_allowed_loopback_return_origin(url: httpx.URL) -> bool:
    """Allow canonical local browser callbacks on loopback hosts, regardless of explicit port."""

    return url.scheme == "http" and url.host in _ALLOWED_LOOPBACK_RETURN_HOSTS


def _normalize_origin(url: httpx.URL) -> str:
    """Return the scheme/host[/port] origin string used for QuickBooks return-url allowlists."""

    origin = f"{url.scheme}://{url.host}"
    if url.port is None:
        return origin

    default_port = 80 if url.scheme == "http" else 443 if url.scheme == "https" else None
    if default_port is not None and url.port == default_port:
        return origin

    return f"{origin}:{url.port}"


def _sign_state(*, encoded_payload: str, secret: str) -> str:
    """Sign a base64url state payload with HMAC-SHA256."""

    digest = hmac.new(
        secret.encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return _base64url_encode(digest)


def _base64url_encode(value: bytes) -> str:
    """Encode bytes as unpadded URL-safe base64."""

    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes:
    """Decode unpadded URL-safe base64 text."""

    padding = (-len(value)) % 4
    return base64.urlsafe_b64decode(f"{value}{'=' * padding}".encode("ascii"))


__all__ = [
    "QuickBooksAuthorizationStart",
    "QuickBooksOAuth",
    "QuickBooksOAuthError",
    "QuickBooksOAuthState",
    "QuickBooksReauthorizationRequiredError",
    "QuickBooksTokenSet",
    "get_quickbooks_oauth",
]
