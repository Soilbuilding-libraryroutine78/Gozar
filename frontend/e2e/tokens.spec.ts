import { expect, test } from "@playwright/test";

import { installApiMock } from "./fixtures/api-mock";
import { seedSession } from "./fixtures/auth";
import type { ChainResponse } from "../src/api/types";

const routingChain: ChainResponse = {
  chain_id: "chain-prod-0001",
  name: "Production routing",
  model_selector: null,
  entries: [{ account_id: "acc-openai-0001", position: 0 }],
};

/**
 * Flow 3 - Create a Gozar API key and see the issued secret (Requirement 17.2,
 * 8.1, 8.3).
 *
 * Creates a token, asserts the secret is shown in the reveal dialog, dismisses it,
 * and confirms the token appears in the list by its label (and the secret is not
 * present in the list).
 */
test.describe("create token", () => {
  test.beforeEach(async ({ page }) => {
    await seedSession(page);
    await installApiMock(page, { tokens: [], chains: [routingChain] });
  });

  test("creates a token and reveals the issued secret", async ({ page }) => {
    await page.goto("/tokens");

    await expect(page.getByText("No API keys yet.")).toBeVisible();
    const expectedBaseUrl = await page.evaluate(() =>
      new URL("/v1", window.location.origin).toString(),
    );
    await expect(page.getByText(`GOZAR_BASE_URL=${expectedBaseUrl}`)).toBeVisible();
    await expect(page.getByText("GOZAR_MODEL=MODEL_FROM_V1_MODELS")).toBeVisible();
    await expect(page.getByRole("tabpanel")).toContainText(
      `os.environ.setdefault("GOZAR_BASE_URL", "${expectedBaseUrl}")`,
    );

    await page.getByRole("button", { name: "Create API key" }).click();

    // Fill and submit inside the create dialog.
    const createDialog = page.getByRole("dialog");
    await createDialog.getByLabel("Label").fill("CI pipeline token");
    await createDialog.getByLabel("Routing chain").selectOption("chain-prod-0001");
    await createDialog.getByRole("button", { name: "Create API key" }).click();

    // The secret reveal appears with the issued key value.
    await expect(page.getByRole("heading", { name: "API key ready" })).toBeVisible();
    const secret = "gz-e2epub000000001-e2e_REVEALABLE_SECRET_VALUE_abc123";
    await expect(page.getByLabel("Gozar API key", { exact: true })).toHaveValue(secret);

    // Acknowledge and close the reveal.
    await page.getByRole("button", { name: "I have copied the key" }).click();

    // The token now appears in the list by label; the secret is not shown there.
    const row = page.getByRole("row", { name: /CI pipeline token/ });
    await expect(row).toBeVisible();
    await expect(row.getByText("Production routing")).toBeVisible();
    await expect(row.getByText("Active")).toBeVisible();
    await expect(page.getByText("GOZAR_MODEL=gpt-5.5")).toBeVisible();
    await expect(page.getByText(secret)).toHaveCount(0);
  });
});
