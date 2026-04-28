"""
Purpose: Render the Textual close-run inspection screen for the CLI.
Scope: Show phase state, document/recommendation/reconciliation/report/export counts,
and refreshable close-run status for one entity-period run.
Dependencies: Textual widgets and the CLI API client protocol.
"""

from __future__ import annotations

from typing import Any, ClassVar

from apps.cli.src.api_client import CliApiClientError, CliApiClientProtocol
from apps.cli.src.command_helpers import extract_rows
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static


class CloseRunScreen(Screen[None]):
    """Show one close run and its primary workflow queues in a Textual view."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("r", "refresh", "Refresh"),
        ("q", "app.quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        api_client: CliApiClientProtocol,
        entity_id: str,
        close_run_id: str,
    ) -> None:
        """Capture the identifiers and API client needed to hydrate this screen."""

        super().__init__()
        self._api_client = api_client
        self._entity_id = entity_id
        self._close_run_id = close_run_id

    def compose(self) -> ComposeResult:
        """Compose the close-run header, phase table, queue summary, and footer."""

        yield Header(show_clock=True)
        with Vertical(id="close-run-root"):
            yield Static("Close run", id="close-run-title")
            yield Static("", id="close-run-status")
            yield DataTable(id="phase-table", zebra_stripes=True)
            yield DataTable(id="queue-table", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        """Initialize screen tables and load close-run data."""

        self._prepare_tables()
        self.action_refresh()

    def action_refresh(self) -> None:
        """Refresh the close-run summary and queue counts from the API."""

        status = self.query_one("#close-run-status", Static)
        phase_table = self.query_one("#phase-table", DataTable)
        queue_table = self.query_one("#queue-table", DataTable)
        phase_table.clear()
        queue_table.clear()

        try:
            close_run = self._api_client.get(self._path(""))
            documents = self._api_client.get(self._path("/documents"))
            recommendations = self._api_client.get(self._path("/recommendations"))
            reconciliations = self._api_client.get(self._path("/reconciliations"))
            reports = self._api_client.get(
                f"/entities/{self._entity_id}/reports/close-runs/{self._close_run_id}/runs"
            )
            exports = self._api_client.get(self._path("/exports"))
        except CliApiClientError as error:
            status.update(f"[bold red]{error.message}[/] ({error.code})")
            return

        status.update(
            "[green]"
            f"{close_run.get('status', 'unknown')} | "
            f"{close_run.get('period_start', '—')} to {close_run.get('period_end', '—')} | "
            f"version {close_run.get('current_version_no', '—')}"
            "[/]"
        )
        for phase in _workflow_phases(close_run):
            phase_table.add_row(
                str(phase.get("phase", "—")),
                str(phase.get("status", "—")),
                str(phase.get("blocking_reason") or "—"),
            )

        queue_table.add_row("Documents", str(_count_items(documents, "documents")))
        queue_table.add_row(
            "Recommendations",
            str(_count_items(recommendations, "recommendations")),
        )
        queue_table.add_row(
            "Reconciliations",
            str(_count_items(reconciliations, "reconciliations")),
        )
        queue_table.add_row("Report runs", str(_count_items(reports, "report_runs")))
        queue_table.add_row("Exports", str(_count_items(exports, "exports")))

    def _prepare_tables(self) -> None:
        """Initialize table columns before adding dynamic rows."""

        phase_table = self.query_one("#phase-table", DataTable)
        phase_table.add_columns("Phase", "Status", "Blocking reason")

        queue_table = self.query_one("#queue-table", DataTable)
        queue_table.add_columns("Queue", "Count")

    def _path(self, suffix: str) -> str:
        """Build a close-run-scoped API path for the current entity and close run."""

        return f"/entities/{self._entity_id}/close-runs/{self._close_run_id}{suffix}"


def _workflow_phases(payload: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Extract workflow phase records from either known close-run response shape."""

    workflow_state = payload.get("workflow_state")
    if not isinstance(workflow_state, dict):
        return ()

    candidate_keys = ("phase_states", "phases")
    for key in candidate_keys:
        value = workflow_state.get(key)
        if isinstance(value, list):
            return extract_rows(workflow_state, key)

    return ()


def _count_items(payload: dict[str, Any], key: str) -> int:
    """Return the number of records stored under a list-valued response key."""

    return len(extract_rows(payload, key))


__all__ = ["CloseRunScreen"]
