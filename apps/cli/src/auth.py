"""
Purpose: Provide the canonical CLI login, logout, and whoami flows for local PAT auth.
Scope: Command parsing, interactive credential capture, token exchange against the
local API, and persistence of one local CLI auth profile.
Dependencies: httpx for API calls plus the local config helpers in apps/cli/src/config.py.
"""

from __future__ import annotations

import argparse
import getpass
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypedDict, cast

import httpx
from apps.cli.src.config import (
    CliAuthConfig,
    delete_cli_auth_config,
    get_cli_config_path,
    load_cli_auth_config,
    save_cli_auth_config,
)
from services.common.settings import AppSettings
from services.common.types import JsonObject


@dataclass(frozen=True, slots=True)
class ApiErrorDetails:
    """Describe one structured API error payload returned by the FastAPI service."""

    code: str
    message: str


class ApiTokenPayload(TypedDict):
    """Describe the token object returned by CLI auth endpoints."""

    expires_at: str | None
    name: str
    token: str
    token_type: str


class AuthUserPayload(TypedDict):
    """Describe the user object returned by CLI auth endpoints."""

    email: str
    full_name: str


class LoginResponsePayload(TypedDict):
    """Describe the successful CLI login response."""

    api_token: ApiTokenPayload
    user: AuthUserPayload


class CurrentTokenResponsePayload(TypedDict):
    """Describe the stored-token inspection response."""

    api_token: ApiTokenPayload
    user: AuthUserPayload


def main(argv: list[str] | None = None) -> int:
    """Parse CLI auth arguments and dispatch to the requested subcommand."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = cast(Callable[[argparse.Namespace], int], args.handler)
    return handler(args)


def login_command(args: argparse.Namespace) -> int:
    """Exchange local credentials for a PAT and persist the resulting CLI auth config."""

    email = args.email or input("Email: ").strip()
    if not email:
        print("Email is required.", file=sys.stderr)
        return 1

    password = args.password or getpass.getpass("Password: ")
    if not password.strip():
        print("Password is required.", file=sys.stderr)
        return 1

    api_base_url = _normalize_api_base_url(args.api_base_url)
    token_name = args.token_name or _default_token_name()
    request_payload = {
        "email": email,
        "password": password,
        "token_name": token_name,
        "scopes": [scope.strip() for scope in args.scope if scope.strip()],
        "expires_in_days": args.expires_in_days,
    }

    try:
        response_payload = cast(
            LoginResponsePayload,
            _request_json(
                method="POST",
                url=f"{api_base_url}/api-tokens/login",
                json_payload=request_payload,
            ),
        )
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1

    issued_token = response_payload["api_token"]
    config_path = save_cli_auth_config(
        CliAuthConfig(
            api_base_url=api_base_url,
            token=issued_token["token"],
            token_type=issued_token["token_type"],
            user_email=response_payload["user"]["email"],
            token_name=issued_token["name"],
            expires_at=issued_token["expires_at"],
        )
    )
    print(
        f"Stored CLI auth for {response_payload['user']['email']} at {config_path}. "
        f"Token '{issued_token['name']}' expires at {issued_token['expires_at']}."
    )
    return 0


def logout_command(args: argparse.Namespace) -> int:
    """Revoke the stored PAT when possible and remove the local CLI auth config file."""

    try:
        config = load_cli_auth_config()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1

    if config is None:
        print("No CLI auth config is currently stored.")
        return 0

    revoke_error: str | None = None
    if not args.local_only:
        try:
            _request_json(
                method="POST",
                url=f"{config.api_base_url}/api-tokens/current/revoke",
                token=config.token,
            )
        except RuntimeError as error:
            revoke_error = str(error)

    removed = delete_cli_auth_config()
    if revoke_error is not None:
        print(revoke_error, file=sys.stderr)

    if removed:
        print(f"Removed CLI auth config at {get_cli_config_path()}.")
    else:
        print("No CLI auth config file was present to remove.")

    return 0 if revoke_error is None else 1


def whoami_command(_: argparse.Namespace) -> int:
    """Validate the stored PAT against the API and print the authenticated CLI identity."""

    try:
        config = load_cli_auth_config()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1

    if config is None:
        print("No CLI auth config is stored. Run the login command first.", file=sys.stderr)
        return 1

    try:
        response_payload = cast(
            CurrentTokenResponsePayload,
            _request_json(
                method="GET",
                url=f"{config.api_base_url}/api-tokens/current",
                token=config.token,
            ),
        )
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1

    print(
        f"Authenticated as {response_payload['user']['full_name']} "
        f"<{response_payload['user']['email']}> using token "
        f"'{response_payload['api_token']['name']}' "
        f"(expires at {response_payload['api_token']['expires_at']})."
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Create the CLI auth parser with one canonical subcommand tree."""

    parser = argparse.ArgumentParser(
        prog="python -m apps.cli.src.auth",
        description="CLI authentication flows for the Accounting AI Agent local API.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    login_parser = subparsers.add_parser(
        "login",
        help="Create and store a CLI personal access token.",
    )
    login_parser.add_argument("--email", help="Email address for the local account.")
    login_parser.add_argument(
        "--password",
        help="Password for the local account. Omit this flag to be prompted securely.",
    )
    login_parser.add_argument(
        "--api-base-url",
        default=AppSettings().api_base_url,
        help="API base URL to target, such as http://127.0.0.1:8000/api.",
    )
    login_parser.add_argument(
        "--token-name",
        help="Optional token label stored on the API. Defaults to a hostname-based label.",
    )
    login_parser.add_argument(
        "--scope",
        action="append",
        default=["cli:access"],
        help="PAT scope to request. Repeat the flag to request multiple scopes.",
    )
    login_parser.add_argument(
        "--expires-in-days",
        type=int,
        default=30,
        help="Token lifetime in days before the API expires it.",
    )
    login_parser.set_defaults(handler=login_command)

    logout_parser = subparsers.add_parser(
        "logout",
        help="Revoke the stored token and clear local auth.",
    )
    logout_parser.add_argument(
        "--local-only",
        action="store_true",
        help="Skip the server-side revoke request and only delete the local config file.",
    )
    logout_parser.set_defaults(handler=logout_command)

    whoami_parser = subparsers.add_parser(
        "whoami",
        help="Validate the stored token and show the active identity.",
    )
    whoami_parser.set_defaults(handler=whoami_command)
    return parser


def _request_json(
    *,
    method: str,
    url: str,
    json_payload: dict[str, object] | None = None,
    token: str | None = None,
) -> JsonObject:
    """Send one HTTP request to the API and return a JSON object or raise a friendly error."""

    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.request(
                method=method,
                url=url,
                json=json_payload,
                headers=headers,
            )
    except httpx.HTTPError as error:
        raise RuntimeError(f"Unable to reach the API at {url}: {error}") from error

    if response.is_success:
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("The API returned an unexpected non-object JSON payload.")
        return cast(JsonObject, payload)

    error_details = _parse_error_details(response)
    raise RuntimeError(f"{error_details.message} (code: {error_details.code})")


def _parse_error_details(response: httpx.Response) -> ApiErrorDetails:
    """Extract a structured error code/message pair from an API error response."""

    try:
        payload = response.json()
    except ValueError:
        return ApiErrorDetails(
            code=f"http_{response.status_code}",
            message=f"The API returned HTTP {response.status_code} without a JSON error body.",
        )

    if not isinstance(payload, dict):
        return ApiErrorDetails(
            code=f"http_{response.status_code}",
            message=(
                f"The API returned HTTP {response.status_code} with a non-object error payload."
            ),
        )

    detail = payload.get("detail")
    if isinstance(detail, dict):
        code = str(detail.get("code", f"http_{response.status_code}"))
        message = str(detail.get("message", "The API rejected the request."))
        return ApiErrorDetails(code=code, message=message)

    return ApiErrorDetails(
        code=f"http_{response.status_code}",
        message=f"The API returned HTTP {response.status_code} with an unexpected error payload.",
    )


def _normalize_api_base_url(value: str) -> str:
    """Normalize the configured API base URL into a slash-safe form."""

    normalized = value.strip().rstrip("/")
    if not normalized:
        message = "API base URL cannot be blank."
        raise RuntimeError(message)

    return normalized


def _default_token_name() -> str:
    """Build a stable default token name that identifies the current workstation."""

    return f"{socket.gethostname()}-cli"


if __name__ == "__main__":
    raise SystemExit(main())
