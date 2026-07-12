import { expect, test } from "@playwright/test";

import { installApiMock } from "./fixtures/api-mock";
import { seedSession } from "./fixtures/auth";

/**
 * Flow 2 - Connect an account (Requirement 17.1, 2.1).
 *
 * Starts from an empty account list, connects a metered API-key account through
 * the connect dialog, and asserts the new account appears in the list with its
 * provider and active status.
 */
test.describe("connect account", () => {
  test.beforeEach(async ({ page }) => {
    await seedSession(page);
    // Start with no accounts so the created one is unambiguous.
    await installApiMock(page, { accounts: [] });
  });

  test("connects an API-key account and shows it in the list", async ({ page }) => {
    await page.goto("/accounts");

    // Empty state first.
    await expect(page.getByText("No accounts connected yet.")).toBeVisible();

    await page.getByRole("button", { name: "Connect account" }).click();

    // API key is the default method tab.
    await page.getByLabel("API key", { exact: true }).fill("sk-e2e-secret-key");
    await page.getByLabel("Label (optional)").fill("Primary OpenAI");
    await page.getByRole("button", { name: "Connect", exact: true }).click();

    // The dialog closes and the account appears in the table.
    const row = page.getByRole("row", { name: /Primary OpenAI/ });
    await expect(row).toBeVisible();
    await expect(row.getByText("OpenAI", { exact: true })).toBeVisible();
    await expect(row.getByText("Active")).toBeVisible();
  });
});
