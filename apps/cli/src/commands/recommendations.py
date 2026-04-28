"""
Purpose: Implement recommendation and journal review commands for the CLI.
Scope: List recommendations, approve/reject recommendations, list journals, and
approve/apply/reject journals through the authenticated local API.
Dependencies: argparse namespaces, Rich console rendering, the CLI API client protocol,
and the reusable status-table builder.
"""

from __future__ import annotations

import argparse
from typing import cast

from apps.cli.src.api_client import CliApiClientError, CliApiClientProtocol
from apps.cli.src.command_helpers import (
    add_close_run_scope_arguments,
    build_close_run_path,
    extract_rows,
    print_api_error,
)
from apps.cli.src.widgets.status_table import StatusColumn, build_status_table
from rich.console import Console


def configure_recommendation_subcommands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register recommendation and journal review subcommands on the provided parser."""

    recommendations = subparsers.add_parser(
        "recommendations",
        help="Review and approve accounting recommendations.",
    )
    recommendation_subparsers = recommendations.add_subparsers(
        dest="recommendation_command",
        required=True,
    )

    list_parser = recommendation_subparsers.add_parser(
        "list",
        help="List recommendations for one close run.",
    )
    add_close_run_scope_arguments(list_parser)
    list_parser.set_defaults(handler=list_recommendations_command)

    approve_parser = recommendation_subparsers.add_parser(
        "approve",
        help="Approve one recommendation and generate its journal draft.",
    )
    add_close_run_scope_arguments(approve_parser)
    approve_parser.add_argument("recommendation_id", help="Recommendation UUID to approve.")
    approve_parser.add_argument("--reason", help="Optional reviewer reason.")
    approve_parser.set_defaults(handler=approve_recommendation_command)

    reject_parser = recommendation_subparsers.add_parser(
        "reject",
        help="Reject one recommendation.",
    )
    add_close_run_scope_arguments(reject_parser)
    reject_parser.add_argument("recommendation_id", help="Recommendation UUID to reject.")
    reject_parser.add_argument("--reason", required=True, help="Required rejection reason.")
    reject_parser.set_defaults(handler=reject_recommendation_command)

    journals = recommendation_subparsers.add_parser(
        "journals",
        help="Review journal drafts for one close run.",
    )
    journal_subparsers = journals.add_subparsers(dest="journal_command", required=True)

    journal_list = journal_subparsers.add_parser("list", help="List journals.")
    add_close_run_scope_arguments(journal_list)
    journal_list.set_defaults(handler=list_journals_command)

    for action in ("approve", "apply", "reject"):
        action_parser = journal_subparsers.add_parser(action, help=f"{action.title()} a journal.")
        add_close_run_scope_arguments(action_parser)
        action_parser.add_argument("journal_id", help="Journal UUID to mutate.")
        action_parser.add_argument(
            "--reason",
            required=action == "reject",
            help="Reviewer reason. Required when rejecting.",
        )
        action_parser.set_defaults(handler=journal_action_command, journal_action=action)


def list_recommendations_command(
    args: argparse.Namespace,
    *,
    client: CliApiClientProtocol,
    console: Console,
) -> int:
    """Fetch and render recommendations for the selected close run."""

    try:
        payload = client.get(build_close_run_path(args, "/recommendations"))
    except CliApiClientError as error:
        return print_api_error(console=console, error=error)

    rows = extract_rows(payload, "recommendations")
    console.print(
        build_status_table(
            title="Recommendations",
            columns=(
                StatusColumn("ID", "id", max_width=12, overflow="ellipsis"),
                StatusColumn("Type", "recommendation_type"),
                StatusColumn("Status", "status", badge=True),
                StatusColumn("Confidence", "confidence"),
                StatusColumn("Summary", "reasoning_summary", max_width=72),
            ),
            rows=rows,
        )
    )
    return 0


def approve_recommendation_command(
    args: argparse.Namespace,
    *,
    client: CliApiClientProtocol,
    console: Console,
) -> int:
    """Approve one recommendation and render the resulting status."""

    return _recommendation_action(
        args,
        client=client,
        console=console,
        action="approve",
        payload={"reason": args.reason},
    )


def reject_recommendation_command(
    args: argparse.Namespace,
    *,
    client: CliApiClientProtocol,
    console: Console,
) -> int:
    """Reject one recommendation and render the resulting status."""

    return _recommendation_action(
        args,
        client=client,
        console=console,
        action="reject",
        payload={"reason": args.reason},
    )


def list_journals_command(
    args: argparse.Namespace,
    *,
    client: CliApiClientProtocol,
    console: Console,
) -> int:
    """Fetch and render journal drafts for the selected close run."""

    try:
        payload = client.get(build_close_run_path(args, "/journals"))
    except CliApiClientError as error:
        return print_api_error(console=console, error=error)

    rows = extract_rows(payload, "journals")
    console.print(
        build_status_table(
            title="Journals",
            columns=(
                StatusColumn("ID", "id", max_width=12, overflow="ellipsis"),
                StatusColumn("Journal", "journal_number"),
                StatusColumn("Status", "status", badge=True),
                StatusColumn("Debits", "total_debits"),
                StatusColumn("Credits", "total_credits"),
                StatusColumn("Description", "description", max_width=64),
            ),
            rows=rows,
        )
    )
    return 0


def journal_action_command(
    args: argparse.Namespace,
    *,
    client: CliApiClientProtocol,
    console: Console,
) -> int:
    """Approve, apply, or reject one journal through the local API."""

    action = cast(str, args.journal_action)
    try:
        payload = client.post(
            build_close_run_path(args, f"/journals/{args.journal_id}/{action}"),
            json_payload={"reason": args.reason},
        )
    except CliApiClientError as error:
        return print_api_error(console=console, error=error)

    journal = payload.get("journal")
    if isinstance(journal, dict):
        console.print(
            f"[green]Journal {journal.get('journal_number', args.journal_id)} "
            f"{action}d.[/] Status: [bold]{journal.get('status', 'unknown')}[/]"
        )
    else:
        console.print(f"[green]Journal {args.journal_id} {action}d.[/]")
    return 0


def _recommendation_action(
    args: argparse.Namespace,
    *,
    client: CliApiClientProtocol,
    console: Console,
    action: str,
    payload: dict[str, object],
) -> int:
    """Execute one recommendation mutation and render the API response summary."""

    try:
        response_payload = client.post(
            build_close_run_path(args, f"/recommendations/{args.recommendation_id}/{action}"),
            json_payload=payload,
        )
    except CliApiClientError as error:
        return print_api_error(console=console, error=error)

    final_status = response_payload.get("final_status") or response_payload.get("status")
    console.print(
        f"[green]Recommendation {args.recommendation_id} {action}d.[/] "
        f"Status: [bold]{final_status or 'updated'}[/]"
    )
    journal_draft = response_payload.get("journal_draft")
    if isinstance(journal_draft, dict):
        console.print(
            "Journal draft: "
            f"{journal_draft.get('journal_number', journal_draft.get('journal_id', 'created'))} "
            f"({journal_draft.get('total_debits')} / {journal_draft.get('total_credits')})."
        )
    return 0


__all__ = [
    "approve_recommendation_command",
    "configure_recommendation_subcommands",
    "journal_action_command",
    "list_journals_command",
    "list_recommendations_command",
    "reject_recommendation_command",
]
