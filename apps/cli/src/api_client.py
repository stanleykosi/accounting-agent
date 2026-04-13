"""
Purpose: Provide the authenticated HTTP boundary used by the Accounting AI Agent CLI.
Scope: Load the stored personal access token, send bearer-authenticated JSON requests,
and normalize API failures into operator-facing exceptions for Rich/Textual surfaces.
Dependencies: httpx, the CLI auth config helpers, and the API's structured error payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx
from apps.cli.src.config import CliAuthConfig, load_cli_auth_config

type JsonObject = dict[str, Any]
type QueryParams = dict[str, str | int | bool]


class CliApiClientProtocol(Protocol):
    """Describe the API-client surface consumed by command handlers and Textual screens."""

    def get(self, path: str, *, params: QueryParams | None = None) -> JsonObject:
        """Send an authenticated GET request and return a JSON object."""

    def post(
        self,
        path: str,
        *,
        json_payload: JsonObject | None = None,
        params: QueryParams | None = None,
    ) -> JsonObject:
        """Send an authenticated POST request and return a JSON object."""


@dataclass(frozen=True, slots=True)
class ApiErrorDetails:
    """Describe one structured API error returned by the local FastAPI service."""

    code: str
    message: str


class CliApiClientError(RuntimeError):
    """Represent a CLI-facing API failure with a stable code and recovery-oriented message."""

    def __init__(self, *, code: str, message: str) -> None:
        """Capture the API error code and message for consistent rendering."""

        super().__init__(message)
        self.code = code
        self.message = message


class CliApiClient:
    """Send bearer-authenticated requests to the configured local Accounting AI Agent API."""

    def __init__(self, *, config: CliAuthConfig, timeout_seconds: float = 30.0) -> None:
        """Capture the persisted CLI profile and request timeout used for all API calls."""

        self._config = config
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_stored_config(cls) -> CliApiClient:
        """Load the canonical stored CLI auth config or fail with a login recovery step."""

        config = load_cli_auth_config()
        if config is None:
            raise CliApiClientError(
                code="cli_auth_required",
                message="No CLI auth profile is stored. Run `python -m apps.cli.src.main login`.",
            )

        return cls(config=config)

    @property
    def api_base_url(self) -> str:
        """Return the slash-safe API base URL configured during CLI login."""

        return self._config.api_base_url.rstrip("/")

    def get(self, path: str, *, params: QueryParams | None = None) -> JsonObject:
        """Send an authenticated GET request and return a JSON object."""

        return self._request_json("GET", path, params=params)

    def post(
        self,
        path: str,
        *,
        json_payload: JsonObject | None = None,
        params: QueryParams | None = None,
    ) -> JsonObject:
        """Send an authenticated POST request and return a JSON object."""

        return self._request_json("POST", path, json_payload=json_payload, params=params)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_payload: JsonObject | None = None,
        params: QueryParams | None = None,
    ) -> JsonObject:
        """Execute one JSON API request and normalize HTTP, transport, and shape failures."""

        url = f"{self.api_base_url}{_normalize_path(path)}"
        headers = {"Authorization": f"{self._config.token_type} {self._config.token}"}

        try:
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.request(
                    method=method,
                    url=url,
                    json=json_payload,
                    params=params,
                    headers=headers,
                )
        except httpx.HTTPError as error:
            raise CliApiClientError(
                code="api_unreachable",
                message=f"Unable to reach the local API at {url}: {error}",
            ) from error

        if response.is_success:
            payload = response.json()
            if not isinstance(payload, dict):
                raise CliApiClientError(
                    code="unexpected_api_payload",
                    message="The API returned a non-object JSON payload.",
                )
            return cast(JsonObject, payload)

        error_details = _parse_error_details(response)
        raise CliApiClientError(code=error_details.code, message=error_details.message)


def _normalize_path(path: str) -> str:
    """Normalize API paths so callers can pass either `/x` or `x` safely."""

    stripped = path.strip()
    if not stripped:
        raise CliApiClientError(code="invalid_api_path", message="API path cannot be blank.")

    return stripped if stripped.startswith("/") else f"/{stripped}"


def _parse_error_details(response: httpx.Response) -> ApiErrorDetails:
    """Extract the API's structured `detail` code/message pair from an error response."""

    try:
        payload = response.json()
    except ValueError:
        return ApiErrorDetails(
            code=f"http_{response.status_code}",
            message=f"The API returned HTTP {response.status_code} without JSON details.",
        )

    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict):
            return ApiErrorDetails(
                code=str(detail.get("code", f"http_{response.status_code}")),
                message=str(detail.get("message", "The API rejected the request.")),
            )

    return ApiErrorDetails(
        code=f"http_{response.status_code}",
        message=f"The API returned HTTP {response.status_code} with an unexpected error shape.",
    )


__all__ = [
    "CliApiClient",
    "CliApiClientError",
    "CliApiClientProtocol",
    "JsonObject",
    "QueryParams",
]
