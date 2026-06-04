"""
Purpose: Run the canonical production-readiness gates for the Accounting AI Agent.
Scope: Static analysis, backend tests, parser/importer checks, agent-loop checks,
worker resilience, security/observability, frontend checks, and deployed load smoke.
Dependencies: uv, pytest, ruff, mypy, pnpm, and repository-local test fixtures.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


Command = tuple[str, ...]
Check = Callable[[], None]


@dataclass(frozen=True, slots=True)
class GateStep:
    """Describe one command or in-process validation within a production gate."""

    label: str
    command: Command | None = None
    check: Check | None = None


@dataclass(frozen=True, slots=True)
class Gate:
    """Describe one named production-readiness gate."""

    name: str
    description: str
    steps: tuple[GateStep, ...]
    frontend: bool = False
    load: bool = False
    quick: bool = True


def main(argv: list[str] | None = None) -> int:
    """Run selected production-readiness gates."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    gates = _build_gates(require_load=args.require_load)

    if args.list:
        _print_gate_list(gates)
        return 0

    selected_gates = _select_gates(
        gates=gates,
        requested_gate_names=args.gate or (),
        quick=args.quick,
        skip_frontend=args.skip_frontend,
        skip_load=args.skip_load,
    )
    if not selected_gates:
        print("[production-gate] no gates selected")
        return 1

    print(f"[production-gate] running {len(selected_gates)} gate(s)")
    failed_gates: list[str] = []
    started_at = time.monotonic()
    for gate in selected_gates:
        if not _run_gate(
            gate=gate,
            dry_run=args.dry_run,
            step_timeout_seconds=args.step_timeout_seconds,
        ):
            failed_gates.append(gate.name)

    elapsed_seconds = time.monotonic() - started_at
    if failed_gates:
        print(
            "[production-gate] failed "
            f"{len(failed_gates)} gate(s) after {elapsed_seconds:.1f}s: "
            f"{', '.join(failed_gates)}"
        )
        return 1

    print(f"[production-gate] all selected gates passed in {elapsed_seconds:.1f}s")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gate",
        action="append",
        choices=tuple(gate.name for gate in _build_gates(require_load=False)),
        help="Run only this gate. May be supplied more than once.",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Skip longer production-build and deployed-load gates.",
    )
    parser.add_argument(
        "--skip-frontend",
        action="store_true",
        help="Skip frontend gates when node dependencies are not available.",
    )
    parser.add_argument(
        "--skip-load",
        action="store_true",
        help="Skip the deployed API load smoke.",
    )
    parser.add_argument(
        "--require-load",
        action="store_true",
        help="Fail the load smoke when PRODUCTION_GATE_API_BASE_URL is not configured.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the selected gates without executing them.",
    )
    parser.add_argument(
        "--step-timeout-seconds",
        type=int,
        default=int(os.getenv("PRODUCTION_GATE_STEP_TIMEOUT_SECONDS", "240")),
        help="Fail one gate step when it runs longer than this many seconds.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available gates and exit.",
    )
    return parser


def _build_gates(*, require_load: bool) -> tuple[Gate, ...]:
    load_command = ["uv", "run", "python", "scripts/load_smoke.py"]
    if require_load:
        load_command.append("--required")

    return (
        Gate(
            name="python-static",
            description="Ruff plus strict mypy over the Python application surface.",
            steps=(
                GateStep(
                    "ruff",
                    (
                        "uv",
                        "run",
                        "ruff",
                        "check",
                        "apps",
                        "services",
                        "tests",
                        "scripts",
                    ),
                ),
                GateStep("mypy", ("uv", "run", "mypy", "apps", "services", "scripts")),
            ),
        ),
        Gate(
            name="python-unit",
            description="Full backend unit suite.",
            steps=(GateStep("pytest-unit", ("uv", "run", "pytest", "tests/unit", "-q")),),
        ),
        Gate(
            name="parser-importers",
            description="Document parser, COA, trial-balance, GL, and classifier/importer gates.",
            steps=(
                GateStep(
                    "pytest-parser-importers",
                    (
                        "uv",
                        "run",
                        "pytest",
                        "tests/integration/test_parser_pipeline.py",
                        "tests/unit/test_parse_document_normalization.py",
                        "tests/unit/test_coa_importer.py",
                        "tests/unit/test_ledger_importer.py",
                        "tests/unit/test_document_ai_assist.py",
                        "tests/unit/test_document_quality_checks.py",
                        "-q",
                    ),
                ),
            ),
        ),
        Gate(
            name="agent-loop",
            description=(
                "Chat/agent loop, deterministic tool routing, transcript evals, and graph checks."
            ),
            steps=(
                GateStep(
                    "pytest-chat-action-execution",
                    (
                        "uv",
                        "run",
                        "pytest",
                        "tests/unit/test_chat_action_execution.py",
                        "-q",
                    ),
                ),
                GateStep(
                    "pytest-chat-routing-api",
                    (
                        "uv",
                        "run",
                        "pytest",
                        "tests/unit/test_chat_action_routing.py",
                        "tests/unit/test_chat_agent_api.py",
                        "-q",
                    ),
                ),
                GateStep(
                    "pytest-agent-kernel",
                    (
                        "uv",
                        "run",
                        "pytest",
                        "tests/unit/test_agent_kernel_native_tools.py",
                        "-q",
                    ),
                ),
                GateStep(
                    "pytest-transcript-evals",
                    (
                        "uv",
                        "run",
                        "pytest",
                        "tests/unit/test_operator_transcript_evals.py",
                        "-q",
                    ),
                ),
                GateStep(
                    "pytest-agent-integrations",
                    (
                        "uv",
                        "run",
                        "pytest",
                        "tests/integration/test_chat_grounding.py",
                        "tests/integration/test_recommendation_graph.py",
                        "-q",
                    ),
                ),
            ),
        ),
        Gate(
            name="worker-resilience",
            description="Worker startup, async jobs, retry/resume, and chat continuation behavior.",
            steps=(
                GateStep(
                    "pytest-worker-resilience",
                    (
                        "uv",
                        "run",
                        "pytest",
                        "tests/integration/test_job_resume_and_retry.py",
                        "tests/unit/test_async_jobs.py",
                        "tests/unit/test_chat_job_continuation.py",
                        "tests/unit/test_runtime_checks.py",
                        "tests/unit/test_startup_imports.py",
                        "-q",
                    ),
                ),
            ),
        ),
        Gate(
            name="security-observability",
            description="Auth/security, log redaction, telemetry, and dashboard config checks.",
            steps=(
                GateStep(
                    "pytest-security-auth",
                    (
                        "uv",
                        "run",
                        "pytest",
                        "tests/unit/test_security.py",
                        "tests/unit/test_api_tokens.py",
                        "tests/unit/test_auth_service.py",
                        "-q",
                    ),
                ),
                GateStep(
                    "pytest-observability",
                    (
                        "uv",
                        "run",
                        "pytest",
                        "tests/unit/test_log_redaction.py",
                        "tests/unit/test_api_observability.py",
                        "tests/unit/test_observability_otel.py",
                        "tests/unit/test_logging_routing.py",
                        "tests/unit/test_database_networking.py",
                        "-q",
                    ),
                ),
                GateStep("observability-assets", check=_validate_observability_assets),
            ),
        ),
        Gate(
            name="contracts-data",
            description=(
                "API contracts, schema baseline, accounting math, close gates, and matchers."
            ),
            steps=(
                GateStep(
                    "pytest-contracts-schema",
                    (
                        "uv",
                        "run",
                        "pytest",
                        "tests/unit/test_api_contracts.py",
                        "tests/unit/test_db_schema_baseline.py",
                        "tests/unit/test_alembic_offline_schema.py",
                        "-q",
                    ),
                ),
                GateStep(
                    "pytest-accounting-integrity",
                    (
                        "uv",
                        "run",
                        "pytest",
                        "tests/unit/test_decimal_math_guards.py",
                        "tests/unit/test_journal_balancing.py",
                        "tests/unit/test_close_run_gates.py",
                        "-q",
                    ),
                ),
                GateStep(
                    "pytest-reconciliation-matchers",
                    (
                        "uv",
                        "run",
                        "pytest",
                        "tests/unit/test_reconciliation_matchers.py",
                        "-q",
                    ),
                ),
            ),
        ),
        Gate(
            name="frontend-static",
            description="Frontend lint and typecheck gates.",
            frontend=True,
            steps=(
                GateStep("pnpm-typecheck", ("pnpm", "typecheck")),
                GateStep("pnpm-lint", ("pnpm", "lint")),
            ),
        ),
        Gate(
            name="frontend-build",
            description="Production frontend/package build.",
            frontend=True,
            quick=False,
            steps=(GateStep("pnpm-build", ("pnpm", "build")),),
        ),
        Gate(
            name="load-smoke",
            description="Optional deployed API health/readiness latency smoke.",
            load=True,
            quick=False,
            steps=(GateStep("deployed-load-smoke", tuple(load_command)),),
        ),
    )


def _select_gates(
    *,
    gates: Sequence[Gate],
    requested_gate_names: Sequence[str],
    quick: bool,
    skip_frontend: bool,
    skip_load: bool,
) -> tuple[Gate, ...]:
    requested = set(requested_gate_names)
    selected: list[Gate] = []
    for gate in gates:
        if requested and gate.name not in requested:
            continue
        if quick and not gate.quick:
            continue
        if skip_frontend and gate.frontend:
            continue
        if skip_load and gate.load:
            continue
        selected.append(gate)
    return tuple(selected)


def _run_gate(*, gate: Gate, dry_run: bool, step_timeout_seconds: int) -> bool:
    print(f"\n[production-gate] {gate.name}: {gate.description}")
    gate_started_at = time.monotonic()
    for step in gate.steps:
        step_started_at = time.monotonic()
        if step.command is not None:
            print(f"[production-gate] + {shlex.join(step.command)}")
            if dry_run:
                continue
            try:
                result = subprocess.run(
                    step.command,
                    cwd=REPO_ROOT,
                    check=False,
                    timeout=step_timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                elapsed = time.monotonic() - step_started_at
                print(
                    f"[production-gate] step timed out after {elapsed:.1f}s: "
                    f"{gate.name}/{step.label}"
                )
                return False
            if result.returncode != 0:
                elapsed = time.monotonic() - step_started_at
                print(
                    f"[production-gate] step failed after {elapsed:.1f}s: "
                    f"{gate.name}/{step.label}"
                )
                return False
            continue

        if step.check is None:
            raise RuntimeError(f"Gate step {gate.name}/{step.label} has no command or check.")
        print(f"[production-gate] * {step.label}")
        if dry_run:
            continue
        try:
            step.check()
        except Exception as error:
            elapsed = time.monotonic() - step_started_at
            print(
                f"[production-gate] check failed after {elapsed:.1f}s: "
                f"{gate.name}/{step.label}: {error}"
            )
            return False

    elapsed = time.monotonic() - gate_started_at
    print(f"[production-gate] passed {gate.name} in {elapsed:.1f}s")
    return True


def _validate_observability_assets() -> None:
    dashboard_path = REPO_ROOT / "infra/otel/dashboards/demo-ops.json"
    collector_path = REPO_ROOT / "infra/otel/otel-collector.yaml"
    if not dashboard_path.is_file():
        raise FileNotFoundError(f"Missing Grafana dashboard: {dashboard_path}")
    if not collector_path.is_file():
        raise FileNotFoundError(f"Missing OTel collector config: {collector_path}")

    dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
    if not isinstance(dashboard, dict):
        raise ValueError("Grafana dashboard must be a JSON object.")
    panels = dashboard.get("panels")
    if not isinstance(panels, list) or not panels:
        raise ValueError("Grafana dashboard must define at least one panel.")
    panel_titles = {panel.get("title") for panel in panels if isinstance(panel, dict)}
    required_titles = {"Operational Events", "Errors", "API Request Latency"}
    missing_titles = required_titles.difference(panel_titles)
    if missing_titles:
        raise ValueError(f"Grafana dashboard is missing panels: {sorted(missing_titles)}")

    collector_config = collector_path.read_text(encoding="utf-8")
    for required_section in ("receivers:", "exporters:", "service:"):
        if required_section not in collector_config:
            raise ValueError(f"OTel collector config is missing {required_section}")


def _print_gate_list(gates: Sequence[Gate]) -> None:
    for gate in gates:
        flags: list[str] = []
        if gate.frontend:
            flags.append("frontend")
        if gate.load:
            flags.append("load")
        if not gate.quick:
            flags.append("long")
        suffix = f" ({', '.join(flags)})" if flags else ""
        print(f"{gate.name}{suffix}: {gate.description}")


if __name__ == "__main__":
    sys.exit(main())
