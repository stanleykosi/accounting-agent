"""
Purpose: Implement report generation and export commands for the CLI.
Scope: Trigger report runs, list report/export records, assemble evidence packs,
preview idempotency keys, and write export manifests to local files for review.
Dependencies: argparse namespaces, pathlib, Rich console rendering, and the CLI API client.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from apps.cli.src.api_client import CliApiClientError, CliApiClientProtocol
from apps.cli.src.command_helpers import (
    add_close_run_scope_arguments,
    extract_rows,
    print_api_error,
)
from apps.cli.src.widgets.status_table import StatusColumn, build_status_table
from rich.console import Console


def configure_report_subcommands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register report and export command trees on the provided parser."""

    reports = subparsers.add_parser("reports", help="Generate and inspect report runs.")
    report_subparsers = reports.add_subparsers(dest="report_command", required=True)

    generate = report_subparsers.add_parser("generate", help="Trigger report generation.")
    add_close_run_scope_arguments(generate)
    generate.add_argument("--template-id", help="Optional report template UUID.")
    generate.add_argument(
        "--no-commentary",
        action="store_true",
        help="Skip commentary generation for this report run.",
    )
    generate.add_argument(
        "--llm-commentary",
        action="store_true",
        help="Allow model-assisted commentary generation.",
    )
    generate.set_defaults(handler=generate_report_command)

    list_runs = report_subparsers.add_parser("list", help="List report runs.")
    add_close_run_scope_arguments(list_runs)
    list_runs.set_defaults(handler=list_reports_command)

    exports = subparsers.add_parser("exports", help="Package and inspect close-run exports.")
    export_subparsers = exports.add_subparsers(dest="export_command", required=True)

    export_list = export_subparsers.add_parser("list", help="List export records.")
    add_close_run_scope_arguments(export_list)
    export_list.set_defaults(handler=list_exports_command)

    create = export_subparsers.add_parser("create", help="Trigger a close-run export.")
    add_close_run_scope_arguments(create)
    create.add_argument("--action-qualifier", default="full_export", help="Idempotency scope.")
    create.add_argument(
        "--no-evidence-pack",
        action="store_true",
        help="Exclude evidence-pack assembly.",
    )
    create.add_argument(
        "--no-audit-trail",
        action="store_true",
        help="Exclude audit-trail output from the export manifest.",
    )
    create.set_defaults(handler=create_export_command)

    evidence_pack = export_subparsers.add_parser(
        "evidence-pack",
        help="Assemble and release an evidence pack.",
    )
    add_close_run_scope_arguments(evidence_pack)
    evidence_pack.set_defaults(handler=assemble_evidence_pack_command)

    key = export_subparsers.add_parser(
        "idempotency-key",
        help="Preview the evidence-pack idempotency key.",
    )
    add_close_run_scope_arguments(key)
    key.add_argument("--version", type=int, default=1, help="Close-run version number.")
    key.set_defaults(handler=preview_idempotency_key_command)

    download = export_subparsers.add_parser(
        "download",
        help="Write an export manifest JSON file for accountant review.",
    )
    add_close_run_scope_arguments(download)
    download.add_argument("export_id", help="Export UUID to fetch.")
    download.add_argument("--output", required=True, help="Destination manifest JSON file.")
    download.set_defaults(handler=download_export_manifest_command)


def generate_report_command(
    args: argparse.Namespace,
    *,
    client: CliApiClientProtocol,
    console: Console,
) -> int:
    """Trigger asynchronous report generation and render the created report-run row."""

    params: dict[str, str | int | bool] = {
        "generate_commentary": not args.no_commentary,
        "use_llm_commentary": args.llm_commentary,
    }
    if args.template_id:
        params["template_id"] = args.template_id

    try:
        payload = client.post(
            f"/entities/{args.entity_id}/reports/close-runs/{args.close_run_id}/generate",
            params=params,
        )
    except CliApiClientError as error:
        return print_api_error(console=console, error=error)

    console.print(
        f"[green]Report generation queued.[/] Run [bold]{payload.get('id', 'unknown')}[/] "
        f"is [bold]{payload.get('status', 'pending')}[/]."
    )
    return 0


def list_reports_command(
    args: argparse.Namespace,
    *,
    client: CliApiClientProtocol,
    console: Console,
) -> int:
    """List report-generation runs for one close run."""

    try:
        payload = client.get(
            f"/entities/{args.entity_id}/reports/close-runs/{args.close_run_id}/runs"
        )
    except CliApiClientError as error:
        return print_api_error(console=console, error=error)

    console.print(
        build_status_table(
            title="Report runs",
            columns=(
                StatusColumn("ID", "id", max_width=12, overflow="ellipsis"),
                StatusColumn("Version", "version_no"),
                StatusColumn("Status", "status"),
                StatusColumn("Failure", "failure_reason", max_width=52),
                StatusColumn("Created", "created_at", max_width=28),
            ),
            rows=extract_rows(payload, "report_runs"),
        )
    )
    return 0


def list_exports_command(
    args: argparse.Namespace,
    *,
    client: CliApiClientProtocol,
    console: Console,
) -> int:
    """List export records for one close run."""

    try:
        payload = client.get(_export_path(args, ""))
    except CliApiClientError as error:
        return print_api_error(console=console, error=error)

    console.print(
        build_status_table(
            title="Exports",
            columns=(
                StatusColumn("ID", "id", max_width=12, overflow="ellipsis"),
                StatusColumn("Version", "version_no"),
                StatusColumn("Status", "status"),
                StatusColumn("Artifacts", "artifact_count"),
                StatusColumn("Idempotency", "idempotency_key", max_width=42),
            ),
            rows=extract_rows(payload, "exports"),
        )
    )
    return 0


def create_export_command(
    args: argparse.Namespace,
    *,
    client: CliApiClientProtocol,
    console: Console,
) -> int:
    """Trigger an idempotent export package for the selected close run."""

    try:
        payload = client.post(
            _export_path(args, ""),
            json_payload={
                "include_evidence_pack": not args.no_evidence_pack,
                "include_audit_trail": not args.no_audit_trail,
                "action_qualifier": args.action_qualifier,
            },
        )
    except CliApiClientError as error:
        return print_api_error(console=console, error=error)

    console.print(
        f"[green]Export created.[/] "
        f"Status: [bold]{payload.get('status', 'pending')}[/], "
        f"artifacts: [bold]{payload.get('artifact_count', 0)}[/]."
    )
    return 0


def assemble_evidence_pack_command(
    args: argparse.Namespace,
    *,
    client: CliApiClientProtocol,
    console: Console,
) -> int:
    """Assemble an evidence-pack bundle and render its storage metadata."""

    try:
        payload = client.post(_export_path(args, "/evidence-pack"))
    except CliApiClientError as error:
        return print_api_error(console=console, error=error)

    console.print(
        "[green]Evidence pack ready.[/] "
        f"Storage key: [bold]{payload.get('storage_key', 'not released')}[/] "
        f"({payload.get('size_bytes', 0)} bytes)."
    )
    return 0


def preview_idempotency_key_command(
    args: argparse.Namespace,
    *,
    client: CliApiClientProtocol,
    console: Console,
) -> int:
    """Preview the idempotency key that protects evidence-pack assembly."""

    try:
        payload = client.get(
            _export_path(args, "/evidence-pack/idempotency-key"),
            params={"version": args.version},
        )
    except CliApiClientError as error:
        return print_api_error(console=console, error=error)

    console.print(
        f"[green]{payload.get('artifact_type', 'artifact')} idempotency key:[/] "
        f"{payload.get('idempotency_key', 'unavailable')}"
    )
    return 0


def download_export_manifest_command(
    args: argparse.Namespace,
    *,
    client: CliApiClientProtocol,
    console: Console,
) -> int:
    """Fetch export detail and write its manifest JSON to a local review file."""

    try:
        payload = client.get(_export_path(args, f"/{args.export_id}"))
    except CliApiClientError as error:
        return print_api_error(console=console, error=error)

    manifest = payload.get("manifest")
    if not isinstance(manifest, dict):
        console.print("[bold red]The export has no downloadable manifest yet.[/]")
        return 1

    output_path = Path(args.output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    console.print(f"[green]Wrote export manifest to {output_path}.[/]")
    return 0


def _export_path(args: argparse.Namespace, suffix: str) -> str:
    """Build a close-run export path from parsed command arguments."""

    return f"/entities/{args.entity_id}/close-runs/{args.close_run_id}/exports{suffix}"


__all__ = [
    "assemble_evidence_pack_command",
    "configure_report_subcommands",
    "create_export_command",
    "download_export_manifest_command",
    "generate_report_command",
    "list_exports_command",
    "list_reports_command",
    "preview_idempotency_key_command",
]
