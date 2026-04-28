"""
Purpose: Bootstrap the Accounting AI Agent keyboard-first CLI.
Scope: Provide Rich command-driven workflows, Textual dashboard screens, auth command
delegation, and API-backed close-run inspection for local/on-premise demo operators.
Dependencies: argparse, Rich, Textual, CLI auth/config modules, command handlers,
and the authenticated API client.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any, Protocol, cast

from apps.cli.src import auth as auth_commands
from apps.cli.src.api_client import CliApiClient, CliApiClientError, CliApiClientProtocol
from apps.cli.src.command_helpers import (
    add_close_run_scope_arguments,
    build_close_run_path,
    extract_rows,
    print_api_error,
)
from apps.cli.src.commands.recommendations import configure_recommendation_subcommands
from apps.cli.src.commands.reports import configure_report_subcommands
from apps.cli.src.screens.close_run import CloseRunScreen
from apps.cli.src.screens.dashboard import DashboardScreen
from apps.cli.src.widgets.status_table import StatusColumn, build_status_table
from rich.console import Console
from services.common.settings import AppSettings
from textual.app import App

CommandHandler = Callable[
    [argparse.Namespace],
    int,
]
ClientFactory = Callable[[], CliApiClientProtocol]


class ClientCommandHandler(Protocol):
    """Describe a command handler that requires an authenticated API client."""

    def __call__(
        self,
        args: argparse.Namespace,
        *,
        client: CliApiClientProtocol,
        console: Console,
    ) -> int:
        """Execute the command using the parsed args, API client, and console."""


class AccountingCliApp(App[None]):
    """Run the Textual dashboard experience for CLI operators."""

    CSS = """
    Screen {
        background: #0B1020;
        color: #F4F7FB;
    }

    #dashboard-root, #close-run-root {
        padding: 1 2;
    }

    #dashboard-title, #close-run-title {
        text-style: bold;
        margin: 0 0 1 0;
    }

    #dashboard-status, #close-run-status {
        margin: 1 0;
    }

    DataTable {
        height: auto;
        margin: 1 0;
    }
    """

    def __init__(self, *, screen: DashboardScreen | CloseRunScreen) -> None:
        """Capture the initial screen that should be mounted when the app starts."""

        super().__init__()
        self._initial_screen = screen

    def on_mount(self) -> None:
        """Push the requested dashboard screen into the Textual app."""

        self.push_screen(self._initial_screen)


def main(
    argv: list[str] | None = None,
    *,
    client_factory: ClientFactory = CliApiClient.from_stored_config,
    console: Console | None = None,
) -> int:
    """Parse command-line arguments and dispatch to the selected CLI workflow."""

    resolved_console = console or Console()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "requires_api_client", True):
        handler = cast(CommandHandler, args.handler)
        return handler(args)

    try:
        client = client_factory()
    except CliApiClientError as error:
        resolved_console.print(f"[bold red]{error.message}[/] [dim]({error.code})[/]")
        return 1

    client_handler = cast(ClientCommandHandler, args.handler)
    return client_handler(args, client=client, console=resolved_console)


def build_parser() -> argparse.ArgumentParser:
    """Build the canonical CLI command tree for dashboard and workflow operations."""

    parser = argparse.ArgumentParser(
        prog="python -m apps.cli.src.main",
        description="Keyboard-first CLI for the Accounting AI Agent local demo.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    _configure_auth_commands(subparsers)
    _configure_dashboard_commands(subparsers)
    _configure_entity_commands(subparsers)
    _configure_close_run_commands(subparsers)
    _configure_queue_commands(subparsers)
    _configure_reconciliation_commands(subparsers)
    configure_recommendation_subcommands(subparsers)
    configure_report_subcommands(subparsers)
    return parser


def run_dashboard_command(
    args: argparse.Namespace,
    client: CliApiClientProtocol,
    console: Console,
) -> int:
    """Launch the interactive Textual dashboard or close-run inspection screen."""

    del console
    screen: DashboardScreen | CloseRunScreen
    if args.dashboard_scope == "close-run":
        screen = CloseRunScreen(
            api_client=client,
            entity_id=args.entity_id,
            close_run_id=args.close_run_id,
        )
    else:
        screen = DashboardScreen(api_client=client)

    AccountingCliApp(screen=screen).run()
    return 0


def list_entities_command(
    _: argparse.Namespace,
    client: CliApiClientProtocol,
    console: Console,
) -> int:
    """List entity workspaces accessible to the current CLI token."""

    try:
        payload = client.get("/entities")
    except CliApiClientError as error:
        return print_api_error(console=console, error=error)

    console.print(
        build_status_table(
            title="Entity workspaces",
            columns=(
                StatusColumn("ID", "id", max_width=12, overflow="ellipsis"),
                StatusColumn("Name", "name"),
                StatusColumn("Currency", "base_currency"),
                StatusColumn("Country", "country_code"),
                StatusColumn("Autonomy", "autonomy_mode", badge=True),
                StatusColumn("Status", "status"),
            ),
            rows=extract_rows(payload, "entities"),
        )
    )
    return 0


def list_close_runs_command(
    args: argparse.Namespace,
    client: CliApiClientProtocol,
    console: Console,
) -> int:
    """List close runs for one entity workspace."""

    try:
        payload = client.get(f"/entities/{args.entity_id}/close-runs")
    except CliApiClientError as error:
        return print_api_error(console=console, error=error)

    console.print(
        build_status_table(
            title="Close runs",
            columns=(
                StatusColumn("ID", "id", max_width=12, overflow="ellipsis"),
                StatusColumn("Period start", "period_start"),
                StatusColumn("Period end", "period_end"),
                StatusColumn("Status", "status", badge=True),
                StatusColumn("Version", "current_version_no"),
                StatusColumn("Currency", "reporting_currency"),
            ),
            rows=extract_rows(payload, "close_runs"),
        )
    )
    return 0


def show_close_run_command(
    args: argparse.Namespace,
    client: CliApiClientProtocol,
    console: Console,
) -> int:
    """Show one close run and its workflow phase states."""

    try:
        close_run = client.get(build_close_run_path(args, ""))
    except CliApiClientError as error:
        return print_api_error(console=console, error=error)

    console.print(
        f"[bold]{close_run.get('period_start', '—')} to {close_run.get('period_end', '—')}[/] "
        f"| status [bold]{close_run.get('status', 'unknown')}[/] "
        f"| version {close_run.get('current_version_no', '—')}"
    )
    console.print(
        build_status_table(
            title="Workflow phases",
            columns=(
                StatusColumn("Phase", "phase", badge=True),
                StatusColumn("Status", "status"),
                StatusColumn("Blocking reason", "blocking_reason", max_width=72),
            ),
            rows=_workflow_phase_rows(close_run),
        )
    )
    return 0


def list_document_queue_command(
    args: argparse.Namespace,
    client: CliApiClientProtocol,
    console: Console,
) -> int:
    """List documents that make up the current close-run review queue."""

    try:
        payload = client.get(build_close_run_path(args, "/documents"))
    except CliApiClientError as error:
        return print_api_error(console=console, error=error)

    console.print(
        build_status_table(
            title="Document review queue",
            columns=(
                StatusColumn("ID", "id", max_width=12, overflow="ellipsis"),
                StatusColumn("Filename", "original_filename", max_width=42),
                StatusColumn("Type", "document_type"),
                StatusColumn("Status", "status"),
                StatusColumn("Confidence", "classification_confidence"),
                StatusColumn("Last touched", "last_touched_by_user_id", max_width=12),
            ),
            rows=extract_rows(payload, "documents"),
        )
    )
    return 0


def reconciliation_status_command(
    args: argparse.Namespace,
    client: CliApiClientProtocol,
    console: Console,
) -> int:
    """Render reconciliation status, anomalies, and trial-balance health."""

    try:
        reconciliations = client.get(build_close_run_path(args, "/reconciliations"))
        anomalies = client.get(build_close_run_path(args, "/anomalies"))
        trial_balance = client.get(build_close_run_path(args, "/trial-balance"))
    except CliApiClientError as error:
        return print_api_error(console=console, error=error)

    console.print(
        build_status_table(
            title="Reconciliations",
            columns=(
                StatusColumn("ID", "id", max_width=12, overflow="ellipsis"),
                StatusColumn("Type", "reconciliation_type"),
                StatusColumn("Status", "status"),
                StatusColumn("Items", "item_count"),
                StatusColumn("Matched", "matched_count"),
                StatusColumn("Exceptions", "exception_count"),
                StatusColumn("Blocker", "blocking_reason", max_width=52),
            ),
            rows=extract_rows(reconciliations, "reconciliations"),
        )
    )
    console.print(
        build_status_table(
            title="Anomalies",
            columns=(
                StatusColumn("ID", "id", max_width=12, overflow="ellipsis"),
                StatusColumn("Type", "anomaly_type"),
                StatusColumn("Severity", "severity"),
                StatusColumn("Resolved", "resolved"),
                StatusColumn("Description", "description", max_width=72),
            ),
            rows=extract_rows(anomalies, "anomalies"),
        )
    )
    snapshot = trial_balance.get("snapshot")
    if isinstance(snapshot, dict):
        console.print(
            "[bold]Trial balance:[/] "
            f"debits {snapshot.get('total_debits', '0.00')} | "
            f"credits {snapshot.get('total_credits', '0.00')} | "
            f"balanced {snapshot.get('is_balanced', True)}"
        )
    return 0


def _configure_auth_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register auth commands backed by the canonical local auth handlers."""

    login = subparsers.add_parser("login", help="Create and store a CLI personal access token.")
    login.add_argument("--email", help="Email address for the local account.")
    login.add_argument("--password", help="Password for the local account.")
    login.add_argument("--api-base-url", default=AppSettings().api_base_url)
    login.add_argument("--token-name", help="Optional API token label.")
    login.add_argument("--scope", action="append", default=["cli:access"])
    login.add_argument("--expires-in-days", type=int, default=30)
    login.set_defaults(handler=auth_commands.login_command, requires_api_client=False)

    logout = subparsers.add_parser("logout", help="Revoke and remove the stored CLI token.")
    logout.add_argument("--local-only", action="store_true")
    logout.set_defaults(handler=auth_commands.logout_command, requires_api_client=False)

    whoami = subparsers.add_parser("whoami", help="Show the active CLI token identity.")
    whoami.set_defaults(handler=auth_commands.whoami_command, requires_api_client=False)


def _configure_dashboard_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register interactive Textual dashboard entry points."""

    dashboard = subparsers.add_parser("dashboard", help="Open the Textual entity dashboard.")
    dashboard.set_defaults(
        handler=run_dashboard_command,
        dashboard_scope="entities",
        requires_api_client=True,
    )

    close_run = subparsers.add_parser(
        "close-run-dashboard",
        help="Open the Textual close-run dashboard.",
    )
    close_run.add_argument("entity_id", help="Entity workspace UUID.")
    close_run.add_argument("close_run_id", help="Close-run UUID.")
    close_run.set_defaults(
        handler=run_dashboard_command,
        dashboard_scope="close-run",
        requires_api_client=True,
    )


def _configure_entity_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register entity workspace commands."""

    entities = subparsers.add_parser("entities", help="Inspect entity workspaces.")
    entity_subparsers = entities.add_subparsers(dest="entity_command", required=True)
    list_parser = entity_subparsers.add_parser("list", help="List accessible entities.")
    list_parser.set_defaults(handler=list_entities_command)


def _configure_close_run_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register close-run inspection commands."""

    close_runs = subparsers.add_parser("close-runs", help="Inspect close runs.")
    close_run_subparsers = close_runs.add_subparsers(dest="close_run_command", required=True)

    list_parser = close_run_subparsers.add_parser("list", help="List close runs for an entity.")
    list_parser.add_argument("entity_id", help="Entity workspace UUID.")
    list_parser.set_defaults(handler=list_close_runs_command)

    show_parser = close_run_subparsers.add_parser("show", help="Show one close run.")
    add_close_run_scope_arguments(show_parser)
    show_parser.set_defaults(handler=show_close_run_command)


def _configure_queue_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register review-queue commands."""

    queue = subparsers.add_parser("queue", help="Inspect review queues.")
    queue_subparsers = queue.add_subparsers(dest="queue_command", required=True)
    documents = queue_subparsers.add_parser("documents", help="List document queue items.")
    add_close_run_scope_arguments(documents)
    documents.set_defaults(handler=list_document_queue_command)


def _configure_reconciliation_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register reconciliation status commands."""

    reconciliation = subparsers.add_parser("reconciliation", help="Inspect reconciliation state.")
    reconciliation_subparsers = reconciliation.add_subparsers(
        dest="reconciliation_command",
        required=True,
    )
    status = reconciliation_subparsers.add_parser("status", help="Show reconciliation status.")
    add_close_run_scope_arguments(status)
    status.set_defaults(handler=reconciliation_status_command)


def _workflow_phase_rows(payload: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Extract workflow phase rows from the close-run response shape."""

    workflow_state = payload.get("workflow_state")
    if not isinstance(workflow_state, dict):
        return ()

    for key in ("phase_states", "phases"):
        value = workflow_state.get(key)
        if isinstance(value, list):
            return tuple(cast(dict[str, Any], item) for item in value if isinstance(item, dict))

    return ()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AccountingCliApp",
    "build_parser",
    "list_close_runs_command",
    "list_document_queue_command",
    "list_entities_command",
    "main",
    "reconciliation_status_command",
    "run_dashboard_command",
    "show_close_run_command",
]
