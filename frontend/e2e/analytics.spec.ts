import { expect, test } from "@playwright/test";

import { installApiMock } from "./fixtures/api-mock";
import { seedSession } from "./fixtures/auth";

/**
 * Flow 6 - Read analytics over a range (Requirement 17.4, 15.1, 15.3).
 *
 * The system report loads for the default range on first view. The test asserts
 * the system metrics, then switches scope to a specific API key, selects it, and
 * asserts the per-key metrics.
 */
test.describe("read analytics", () => {
  test.beforeEach(async ({ page }) => {
    await seedSession(page);
    await installApiMock(page);
  });

  test("shows system metrics and a per-token report", async ({ page }) => {
    await page.goto("/analytics");

    // System scope is the default; its report loads for the default range.
    await expect(page.getByText("Requests", { exact: true })).toBeVisible();
    await expect(page.getByText("42", { exact: true })).toBeVisible();
    await expect(page.getByText("Total tokens", { exact: true })).toBeVisible();
    await expect(page.getByText("900", { exact: true })).toBeVisible();
    await expect(page.getByText("Error rate", { exact: true })).toBeVisible();
    await expect(page.getByText("7.1%")).toBeVisible();

    // Switch to the per-key report and pick the seeded key.
    await page.getByRole("tab", { name: "API key" }).click();
    await page.getByLabel("API key").selectOption("tok-existing-0001");

    // Per-key metrics render.
    await expect(page.getByText("Prompt tokens")).toBeVisible();
    await expect(page.getByText("600", { exact: true })).toBeVisible();
    await expect(page.getByText("Completion tokens")).toBeVisible();
    await expect(page.getByText("300", { exact: true })).toBeVisible();
  });
});
