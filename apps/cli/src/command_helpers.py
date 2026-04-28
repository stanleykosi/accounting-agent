"""
Purpose: Provide shared helpers for API-backed CLI command modules.
Scope: Close-run argument registration, close-run API path composition, row extraction,
and structured API error rendering.
Dependencies: argparse namespaces, Rich console rendering, and the CLI API client errors.
"""

from __future__ import annotations

import argparse
from typing import Any, cast

from apps.cli.src.api_client import CliApiClientError
from rich.console import Console


def add_close_run_scope_arguments(parser: argparse.ArgumentParser) -> None:
    """Add entity and close-run UUID arguments shared by close-run-scoped commands."""

    parser.add_argument("entity_id", help="Entity workspace UUID.")
    parser.add_argument("close_run_id", help="Close-run UUID.")


def build_close_run_path(args: argparse.Namespace, suffix: str) -> str:
    """Build a close-run-scoped API path from parsed command arguments."""

    return f"/entities/{args.entity_id}/close-runs/{args.close_run_id}{suffix}"


def extract_rows(payload: dict[str, Any], key: str) -> tuple[dict[str, Any], ...]:
    """Extract list-valued API rows and ignore malformed row entries."""

    value = payload.get(key)
    if not isinstance(value, list):
        return ()

    return tuple(cast(dict[str, Any], item) for item in value if isinstance(item, dict))


def print_api_error(*, console: Console, error: CliApiClientError) -> int:
    """Render a structured API error and return a shell failure code."""

    console.print(f"[bold red]{error.message}[/] [dim]({error.code})[/]")
    return 1


__all__ = [
    "add_close_run_scope_arguments",
    "build_close_run_path",
    "extract_rows",
    "print_api_error",
]
