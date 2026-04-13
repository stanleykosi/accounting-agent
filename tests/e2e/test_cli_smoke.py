"""
Purpose: Smoke-test the Accounting AI Agent CLI command router without a live demo stack.
Scope: Exercise parser wiring, Rich command rendering, API path construction, and
state-changing command payloads with a deterministic fake API client.
Dependencies: pytest, Rich Console capture, and apps/cli/src/main.py.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

from apps.cli.src.api_client import JsonObject, QueryParams
from apps.cli.src.main import build_parser, main
from rich.console import Console


def test_cli_entities_list_renders_workspace_rows() -> None:
    """Ensure the entity list command renders data returned by the API client."""

    fake_client = FakeCliApiClient(
        get_routes={
            "/entities": {
                "entities": [
                    {
                        "id": "entity-1",
                        "name": "Kano Retail Ltd",
                        "base_currency": "NGN",
                        "country_code": "NG",
                        "autonomy_mode": "human_review",
                        "status": "active",
                    }
                ]
            }
        }
    )
    output = io.StringIO()
    exit_code = main(
        ["entities", "list"],
        client_factory=lambda: fake_client,
        console=Console(file=output, force_terminal=False, color_system=None),
    )

    assert exit_code == 0
    assert "Kano Retail Ltd" in output.getvalue()
    assert fake_client.get_calls == [("/entities", None)]


def test_cli_recommendation_approve_posts_review_reason() -> None:
    """Ensure recommendation approval posts the expected close-run-scoped payload."""

    fake_client = FakeCliApiClient(
        post_routes={
            "/entities/entity-1/close-runs/close-1/recommendations/rec-1/approve": {
                "recommendation_id": "rec-1",
                "final_status": "approved",
                "journal_draft": {
                    "journal_id": "journal-1",
                    "journal_number": "JE-0001",
                    "total_debits": "100.00",
                    "total_credits": "100.00",
                },
            }
        }
    )
    output = io.StringIO()
    exit_code = main(
        [
            "recommendations",
            "approve",
            "entity-1",
            "close-1",
            "rec-1",
            "--reason",
            "Evidence checked",
        ],
        client_factory=lambda: fake_client,
        console=Console(file=output, force_terminal=False, color_system=None),
    )

    assert exit_code == 0
    assert "Recommendation rec-1 approved" in output.getvalue()
    assert fake_client.post_calls == [
        (
            "/entities/entity-1/close-runs/close-1/recommendations/rec-1/approve",
            {"reason": "Evidence checked"},
            None,
        )
    ]


def test_cli_report_generate_uses_query_flags() -> None:
    """Ensure report generation sends the report flags expected by the API route."""

    fake_client = FakeCliApiClient(
        post_routes={
            "/entities/entity-1/reports/close-runs/close-1/generate": {
                "id": "report-run-1",
                "status": "pending",
            }
        }
    )
    output = io.StringIO()
    exit_code = main(
        [
            "reports",
            "generate",
            "entity-1",
            "close-1",
            "--template-id",
            "template-1",
            "--llm-commentary",
        ],
        client_factory=lambda: fake_client,
        console=Console(file=output, force_terminal=False, color_system=None),
    )

    assert exit_code == 0
    assert "Report generation queued" in output.getvalue()
    assert fake_client.post_calls == [
        (
            "/entities/entity-1/reports/close-runs/close-1/generate",
            None,
            {
                "generate_commentary": True,
                "use_llm_commentary": True,
                "template_id": "template-1",
            },
        )
    ]


def test_cli_parser_includes_textual_dashboard_entrypoints() -> None:
    """Ensure both interactive Textual dashboard entrypoints remain registered."""

    parser = build_parser()

    dashboard_args = parser.parse_args(["dashboard"])
    close_run_args = parser.parse_args(["close-run-dashboard", "entity-1", "close-1"])

    assert dashboard_args.dashboard_scope == "entities"
    assert close_run_args.dashboard_scope == "close-run"
    assert close_run_args.entity_id == "entity-1"
    assert close_run_args.close_run_id == "close-1"


@dataclass
class FakeCliApiClient:
    """Provide a deterministic fake API client for CLI smoke tests."""

    get_routes: dict[str, JsonObject] = field(default_factory=dict)
    post_routes: dict[str, JsonObject] = field(default_factory=dict)
    get_calls: list[tuple[str, QueryParams | None]] = field(default_factory=list)
    post_calls: list[tuple[str, JsonObject | None, QueryParams | None]] = field(
        default_factory=list
    )

    def get(self, path: str, *, params: QueryParams | None = None) -> JsonObject:
        """Return the fake GET payload for the requested path."""

        self.get_calls.append((path, params))
        return self.get_routes[path]

    def post(
        self,
        path: str,
        *,
        json_payload: JsonObject | None = None,
        params: QueryParams | None = None,
    ) -> JsonObject:
        """Return the fake POST payload for the requested path."""

        self.post_calls.append((path, json_payload, params))
        return self.post_routes[path]
