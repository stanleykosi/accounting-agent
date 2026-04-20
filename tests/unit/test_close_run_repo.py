"""Purpose: Verify deterministic close-run repository helpers without database setup.
Scope: Focused unit coverage over reporting blocker derivation from report artifacts
and active commentary rows.
Dependencies: Close-run repository helpers and reporting commentary enums only.
"""

from __future__ import annotations

from services.db.models.reporting import CommentaryStatus
from services.db.repositories.close_run_repo import _build_missing_required_reports


def test_reporting_gate_requires_only_unapproved_generated_commentary_sections() -> None:
    """Missing commentary blockers should be based on generated active rows only."""

    missing = _build_missing_required_reports(
        artifact_refs=[
            {"type": "report_excel"},
            {"type": "report_pdf"},
        ],
        active_commentary_rows=(
            ("profit_and_loss", CommentaryStatus.APPROVED.value),
            ("balance_sheet", CommentaryStatus.APPROVED.value),
            ("cash_flow", CommentaryStatus.APPROVED.value),
            ("kpi_dashboard", CommentaryStatus.APPROVED.value),
        ),
    )

    assert missing == ()


def test_reporting_gate_blocks_only_the_specific_unapproved_commentary_sections() -> None:
    """Reporting should block on active draft commentary rows, not absent sections."""

    missing = _build_missing_required_reports(
        artifact_refs=[
            {"type": "report_excel"},
            {"type": "report_pdf"},
        ],
        active_commentary_rows=(
            ("profit_and_loss", CommentaryStatus.APPROVED.value),
            ("cash_flow", CommentaryStatus.DRAFT.value),
        ),
    )

    assert missing == ("commentary:cash_flow",)
