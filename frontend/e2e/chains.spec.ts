import { expect, test } from "@playwright/test";

import { installApiMock } from "./fixtures/api-mock";
import { seedSession } from "./fixtures/auth";

/**
 * Flow 4 - Build a fallback chain and order its entries (Requirement 17.3, 10.1).
 *
 * Uses the two seeded accounts. Creates a chain, adds both accounts, reorders them
 * so the second becomes first, saves, and asserts the chain card shows the entries
 * in the chosen order.
 */
test.describe("build fallback chain", () => {
  test.beforeEach(async ({ page }) => {
    await seedSession(page);
    // Default seed provides two active accounts; chains start empty.
    await installApiMock(page);
  });

  test("creates and orders a fallback chain", async ({ page }) => {
    await page.goto("/chains");

    await expect(page.getByText("No fallback chains yet.")).toBeVisible();

    await page.getByRole("button", { name: "Create chain" }).click();

    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Name").fill("Fallback chain E2E");

    // Add both accounts in order: OpenAI first, then Claude.
    await dialog.getByLabel("Add account").selectOption("acc-openai-0001");
    await dialog.getByRole("button", { name: "Add", exact: true }).click();
    await dialog.getByLabel("Add account").selectOption("acc-anthropic-0002");
    await dialog.getByRole("button", { name: "Add", exact: true }).click();

    // Reorder: move "Claude subscription" up so it becomes the first attempt.
    await dialog.getByRole("button", { name: "Move Claude subscription up" }).click();

    await dialog.getByRole("button", { name: "Create chain" }).click();

    // The chain card renders with both entries in the new order.
    await expect(page.getByRole("heading", { name: "Fallback chain E2E" })).toBeVisible();
    const chips = page.locator("ol.chain-chip-list > li");
    await expect(chips).toHaveCount(2);
    await expect(chips.nth(0)).toContainText("Claude subscription");
    await expect(chips.nth(1)).toContainText("Primary OpenAI");
  });
});
