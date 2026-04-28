"""
Purpose: End-to-end smoke test for the reconciliation review workspace.
Scope: Verify that the reconciliation review page loads, displays queue counts,
       filter tabs render, and the disposition panel is reachable.
Dependencies: Playwright, a running demo stack with seeded reconciliation data.

Note: This is a smoke-level E2E test. Full reconciliation flow testing
      (disposition submission, approval, anomaly resolution) requires
      authenticated session fixtures and seeded reconciliation runs,
      which should be added when the full E2E test harness is configured.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.e2e


@pytest.mark.skip(
    reason=(
        "E2E infrastructure (Playwright) not yet configured. "
        "Enable when the desktop UI E2E harness is available."
    )
)
async def test_reconciliation_review_page_loads(page: Any) -> None:
    """Verify the reconciliation review page renders the core workspace elements.

    This test checks that:
    1. The page loads without errors.
    2. The hero section with the reconciliation title is visible.
    3. The close-run context card displays metric chips.
    4. The match review table filter tabs are rendered.
    5. The disposition panel placeholder is present when no item is selected.

    Requires a running demo stack with at least one entity and close run.
    """
    # Navigate to the reconciliation review page for a seeded entity/close-run
    entity_id = "test-entity-uuid"
    close_run_id = "test-close-run-uuid"
    await page.goto(
        f"http://localhost:3000/entities/{entity_id}/close-runs/{close_run_id}/reconciliation"
    )

    # Wait for the page to load (hero section should be visible)
    await page.wait_for_selector("h1", timeout=10_000)

    # Verify hero title
    hero_heading = page.locator("h1")
    await hero_heading.wait_for(state="visible")
    hero_text = await hero_heading.text_content()
    assert "Match results" in hero_text

    # Verify close-run context card is present
    context_card = page.locator("text=Close-run Context")
    await context_card.wait_for(state="visible")

    # Verify metric chips render (even with zero counts)
    metric_chips = page.locator(".document-metric-chip")
    chip_count = await metric_chips.count()
    assert chip_count >= 3  # At least unresolved, matched, exceptions

    # Verify filter tabs in the match review table
    filter_tabs = page.locator(".review-filter-tab")
    tab_count = await filter_tabs.count()
    assert tab_count >= 4  # All, Unresolved, Matched, Exceptions, Unmatched

    # Verify disposition panel placeholder
    disposition_panel = page.locator(".disposition-panel")
    await disposition_panel.wait_for(state="visible")
    panel_text = await disposition_panel.text_content()
    assert "Select a reconciliation item" in panel_text or "Item Detail" in panel_text


@pytest.mark.skip(
    reason=(
        "E2E infrastructure (Playwright) not yet configured. "
        "Enable when the desktop UI E2E harness is available."
    )
)
async def test_reconciliation_review_filter_tabs(page: Any) -> None:
    """Verify that clicking filter tabs updates the active filter styling.

    This test checks that:
    1. The 'All' tab is active by default.
    2. Clicking 'Unresolved' activates that tab.
    3. The active tab has the 'active' CSS class.
    """
    entity_id = "test-entity-uuid"
    close_run_id = "test-close-run-uuid"
    await page.goto(
        f"http://localhost:3000/entities/{entity_id}/close-runs/{close_run_id}/reconciliation"
    )

    await page.wait_for_selector(".review-filter-tab", timeout=10_000)

    # Verify 'All' is active by default
    all_tab = page.locator('.review-filter-tab:text("All")')
    all_active = await all_tab.get_attribute("aria-selected")
    assert all_active == "true"

    # Click 'Unresolved' tab
    unresolved_tab = page.locator('.review-filter-tab:text("Unresolved")')
    await unresolved_tab.click()

    # Verify 'Unresolved' is now active
    unresolved_active = await unresolved_tab.get_attribute("aria-selected")
    assert unresolved_active == "true"

    # Verify 'All' is no longer active
    all_active_after = await all_tab.get_attribute("aria-selected")
    assert all_active_after == "false"


@pytest.mark.skip(
    reason=(
        "E2E infrastructure (Playwright) not yet configured. "
        "Enable when the desktop UI E2E harness is available."
    )
)
async def test_reconciliation_review_empty_queue(page: Any) -> None:
    """Verify the empty state message when no reconciliation items exist.

    This test checks that when there are no items, the review table shows
    the appropriate empty state message.
    """
    entity_id = "test-entity-uuid"
    close_run_id = "test-close-run-uuid-no-items"
    await page.goto(
        f"http://localhost:3000/entities/{entity_id}/close-runs/{close_run_id}/reconciliation"
    )

    await page.wait_for_selector(".review-empty-state", timeout=10_000)

    empty_state = page.locator(".review-empty-state")
    empty_text = await empty_state.text_content()
    assert "No items match" in empty_text
