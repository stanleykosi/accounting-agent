/*
Purpose: End-to-end smoke specification for the desktop dashboard and close-run overview information architecture.
Scope: Verify that the protected dashboard and close-run overview surfaces render their core sections once the Playwright harness is configured.
Dependencies: Playwright, authenticated desktop session fixtures, and seeded entity/close-run demo data.
*/

import { expect, test } from "@playwright/test";

test.describe.skip("desktop dashboard information architecture", () => {
  test("renders the global dashboard summary and review queue", async ({ page }) => {
    await page.goto("http://127.0.0.1:3000/");

    await expect(page.getByRole("heading", { name: "Global Dashboard" })).toBeVisible();
    await expect(page.getByText("Review Queue")).toBeVisible();
    await expect(page.getByText("Recent Activity")).toBeVisible();
  });

  test("renders the close-run overview, phase progress, and lifecycle timeline", async ({
    page,
  }) => {
    const entityId = "test-entity-uuid";
    const closeRunId = "test-close-run-uuid";

    await page.goto(`http://127.0.0.1:3000/entities/${entityId}/close-runs/${closeRunId}`);

    await expect(page.getByRole("heading", { name: /Close Run Overview/i })).toBeVisible();
    await expect(page.getByText("Phase Progress")).toBeVisible();
    await expect(page.getByText("Lifecycle Timeline")).toBeVisible();
  });
});
