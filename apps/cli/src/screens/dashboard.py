"""
Purpose: Render the Textual entity dashboard for the Accounting AI Agent CLI.
Scope: Load accessible entities, show workspace status and autonomy mode, and
provide keyboard-first refresh/quit behavior for operators.
Dependencies: Textual widgets plus the CLI API client protocol.
"""

from __future__ import annotations

from typing import Any, ClassVar, cast

from apps.cli.src.api_client import CliApiClientError, CliApiClientProtocol
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static


class DashboardScreen(Screen[None]):
    """Show the current operator's entity workspaces in a dense Textual table."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("r", "refresh", "Refresh"),
        ("q", "app.quit", "Quit"),
    ]

    def __init__(self, *, api_client: CliApiClientProtocol) -> None:
        """Capture the API client used to hydrate dashboard data."""

        super().__init__()
        self._api_client = api_client

    def compose(self) -> ComposeResult:
        """Compose the dashboard header, status message, entity table, and footer."""

        yield Header(show_clock=True)
        with Vertical(id="dashboard-root"):
            yield Static("Entity workspaces", id="dashboard-title")
            yield Static("Press r to refresh. Press q to quit.", id="dashboard-help")
            yield Static("", id="dashboard-status")
            yield DataTable(id="entity-table", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        """Load entity rows after the screen is mounted."""

        self._prepare_table()
        self.action_refresh()

    def action_refresh(self) -> None:
        """Refresh entity rows from the API and show any failure inline."""

        status = self.query_one("#dashboard-status", Static)
        table = self.query_one("#entity-table", DataTable)
        table.clear()
        try:
            payload = self._api_client.get("/entities")
        except CliApiClientError as error:
            status.update(f"[bold red]{error.message}[/] ({error.code})")
            return

        status.update("[green]Loaded current workspaces.[/]")
        for entity in _extract_sequence(payload, "entities"):
            table.add_row(
                str(entity.get("name", "Unnamed entity")),
                str(entity.get("base_currency", "—")),
                str(entity.get("country_code", "—")),
                str(entity.get("autonomy_mode", "—")),
                str(entity.get("status", "—")),
                str(entity.get("id", "—")),
            )

    def _prepare_table(self) -> None:
        """Initialize dashboard columns once before rows are loaded."""

        table = self.query_one("#entity-table", DataTable)
        table.add_columns("Entity", "Currency", "Country", "Autonomy", "Status", "Entity ID")


def _extract_sequence(payload: dict[str, Any], key: str) -> tuple[dict[str, Any], ...]:
    """Return a tuple of object rows from an API payload while ignoring malformed entries."""

    value = payload.get(key)
    if not isinstance(value, list):
        return ()

    return tuple(cast(dict[str, Any], item) for item in value if isinstance(item, dict))


__all__ = ["DashboardScreen"]
