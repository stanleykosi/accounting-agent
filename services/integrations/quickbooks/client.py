"""
Purpose: Provide a DB-backed QuickBooks Online REST API client for account sync operations.
Scope: Authenticated QuickBooks requests, access-token refresh, explicit reauthorization handling,
and chart-of-accounts account queries.
Dependencies: httpx, QuickBooks OAuth helpers, and the integration repository token boundary.
"""

from __future__ import annotations

import json
from typing import cast

import httpx
from services.common.types import JsonObject
from services.db.models.integration import IntegrationConnectionStatus
from services.db.repositories.integration_repo import (
    IntegrationConnectionRecord,
    IntegrationRepository,
)
from services.integrations.quickbooks.oauth import (
    QuickBooksOAuth,
    QuickBooksOAuthError,
    QuickBooksReauthorizationRequiredError,
    QuickBooksTokenSet,
)


class QuickBooksClientError(Exception):
    """Represent a QuickBooks API request failure with explicit recovery guidance."""


class QuickBooksClient:
    """Execute QuickBooks Online API requests for one persisted entity connection."""

    def __init__(
        self,
        *,
        connection: IntegrationConnectionRecord,
        integration_repository: IntegrationRepository,
        oauth: QuickBooksOAuth,
        http_client: httpx.Client | None = None,
        use_sandbox: bool = True,
    ) -> None:
        """Capture connection state, persistence, OAuth helpers, and injectable HTTP transport."""

        self._connection = connection
        self._integration_repository = integration_repository
        self._oauth = oauth
        self._http_client = http_client or httpx.Client(timeout=30.0)
        self._base_url = (
            "https://sandbox-quickbooks.api.intuit.com/v3/company"
            if use_sandbox
            else "https://quickbooks.api.intuit.com/v3/company"
        )

    def query_accounts(self) -> tuple[JsonObject, ...]:
        """Return all QuickBooks Online account records for the connected company realm."""

        response = self._request(
            "GET",
            "query",
            params={"query": "select * from Account"},
        )
        query_response = response.get("QueryResponse")
        if not isinstance(query_response, dict):
            raise QuickBooksClientError("QuickBooks account query did not include QueryResponse.")
        accounts = query_response.get("Account", [])
        if not isinstance(accounts, list):
            raise QuickBooksClientError(
                "QuickBooks account query returned a malformed Account list."
            )
        return tuple(account for account in accounts if isinstance(account, dict))

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> JsonObject:
        """Execute one authenticated request, refreshing once before requiring reauthorization."""

        token_set = self._load_usable_tokens()
        response = self._send_request(method=method, path=path, params=params, token_set=token_set)
        if response.status_code == 401:
            token_set = self._refresh_or_mark_expired(token_set=token_set)
            response = self._send_request(
                method=method,
                path=path,
                params=params,
                token_set=token_set,
            )

        if response.status_code == 401:
            self._integration_repository.update_status(
                connection_id=self._connection.id,
                status=IntegrationConnectionStatus.EXPIRED,
            )
            raise QuickBooksReauthorizationRequiredError(
                "QuickBooks rejected the refreshed token. Reconnect QuickBooks and retry sync."
            )

        if not response.is_success:
            raise QuickBooksClientError(
                f"QuickBooks API returned HTTP {response.status_code} for {path}."
            )
        return _parse_json_response(response=response)

    def _load_usable_tokens(self) -> QuickBooksTokenSet:
        """Decrypt tokens and refresh proactively when the access token is near expiry."""

        if self._connection.status is not IntegrationConnectionStatus.CONNECTED:
            raise QuickBooksReauthorizationRequiredError(
                "QuickBooks is not connected. Reconnect QuickBooks and retry sync."
            )
        token_set = self._oauth.decrypt_connection_tokens(connection=self._connection)
        if self._oauth.should_refresh(token_set=token_set):
            return self._refresh_or_mark_expired(token_set=token_set)
        return token_set

    def _refresh_or_mark_expired(self, *, token_set: QuickBooksTokenSet) -> QuickBooksTokenSet:
        """Refresh tokens, persist the rotated token set, or mark the connection expired."""

        try:
            refreshed = self._oauth.refresh_tokens(
                refresh_token=token_set.refresh_token,
                realm_id=token_set.realm_id,
            )
        except QuickBooksReauthorizationRequiredError:
            self._integration_repository.update_status(
                connection_id=self._connection.id,
                status=IntegrationConnectionStatus.EXPIRED,
            )
            raise
        except QuickBooksOAuthError as error:
            self._integration_repository.update_status(
                connection_id=self._connection.id,
                status=IntegrationConnectionStatus.ERROR,
            )
            raise QuickBooksClientError(str(error)) from error

        encrypted_credentials = self._oauth.encrypt_token_set(
            entity_id=self._connection.entity_id,
            token_set=refreshed,
        )
        self._connection = self._integration_repository.replace_encrypted_credentials(
            connection_id=self._connection.id,
            encrypted_credentials=encrypted_credentials,
            external_realm_id=refreshed.realm_id,
        )
        self._connection = self._integration_repository.update_status(
            connection_id=self._connection.id,
            status=IntegrationConnectionStatus.CONNECTED,
        )
        return refreshed

    def _send_request(
        self,
        *,
        method: str,
        path: str,
        params: dict[str, str] | None,
        token_set: QuickBooksTokenSet,
    ) -> httpx.Response:
        """Send one raw HTTP request to the connected QuickBooks company realm."""

        url = f"{self._base_url}/{self._connection.external_realm_id}/{path.lstrip('/')}"
        return self._http_client.request(
            method,
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token_set.access_token}",
            },
            params=params,
        )


def _parse_json_response(*, response: httpx.Response) -> JsonObject:
    """Parse QuickBooks API JSON and reject non-object root payloads."""

    try:
        payload = response.json()
    except json.JSONDecodeError as error:
        raise QuickBooksClientError("QuickBooks returned a non-JSON API response.") from error
    if not isinstance(payload, dict):
        raise QuickBooksClientError("QuickBooks API responses must be JSON objects.")
    return cast(JsonObject, payload)


__all__ = ["QuickBooksClient", "QuickBooksClientError"]
