"""
Purpose: Verify repository production-gate helper behavior.
Scope: Gate selection, URL joining, and latency percentile calculations.
Dependencies: scripts.production_gate and scripts.load_smoke.
"""

from __future__ import annotations

from scripts import load_smoke, production_gate


def test_load_smoke_joins_urls_without_double_slashes() -> None:
    """Base URLs and paths should join deterministically for Railway URLs and local URLs."""

    assert load_smoke._join_url("https://api.example.com/", "/api/health") == (
        "https://api.example.com/api/health"
    )
    assert load_smoke._join_url("http://localhost:8000", "api/ready") == (
        "http://localhost:8000/api/ready"
    )


def test_load_smoke_percentile_uses_sorted_latency_values() -> None:
    """Latency thresholds should be independent of completion order."""

    assert load_smoke._percentile([120, 10, 40, 80], 50) == 40
    assert load_smoke._percentile([120, 10, 40, 80], 95) == 120
    assert load_smoke._percentile([], 95) == 0.0


def test_production_gate_selection_honors_quick_frontend_and_load_filters() -> None:
    """Quick local runs should keep backend gates while dropping long/frontend/load gates."""

    gates = production_gate._build_gates(require_load=False)

    selected = production_gate._select_gates(
        gates=gates,
        requested_gate_names=(),
        quick=True,
        skip_frontend=True,
        skip_load=True,
    )

    selected_names = {gate.name for gate in selected}
    assert "python-static" in selected_names
    assert "python-unit" in selected_names
    assert "frontend-static" not in selected_names
    assert "frontend-build" not in selected_names
    assert "load-smoke" not in selected_names
