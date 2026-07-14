import { expect, test } from "@playwright/test";

import { installApiMock } from "./fixtures/api-mock";
import { seedSession } from "./fixtures/auth";

/**
 * Flow 4 - Build independent LLM and Embeddings paths in one chain.
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

  test("creates and orders both request paths", async ({ page }) => {
    await page.goto("/chains");

    await expect(page.getByText("No fallback chains yet.")).toBeVisible();

    await page.getByRole("button", { name: "Create chain" }).click();

    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Name").fill("Fallback chain E2E");

    // Add both accounts in order: OpenAI first, then Claude.
    await dialog.getByLabel("Add account").selectOption("acc-openai-0001");
    await dialog.getByRole("button", { name: "Add node" }).click();
    await dialog.getByLabel("Add account").selectOption("acc-anthropic-0002");
    await dialog.getByRole("button", { name: "Add node" }).click();

    // Reorder: move "Claude subscription" up so it becomes the first attempt.
    await dialog.getByRole("button", { name: "Move Claude subscription up" }).click();

    // The same OpenAI account can independently serve the Embeddings path.
    await dialog.getByRole("tab", { name: /Embeddings/ }).click();
    await dialog.getByLabel("Add account").selectOption("acc-openai-0001");
    await dialog.getByRole("button", { name: "Add node" }).click();
    await expect(dialog.getByLabel("Model for this node")).toHaveValue(
      "text-embedding-3-small",
    );
    await expect(dialog.getByText(/2 embedding models discovered/)).toBeVisible();

    await dialog.getByRole("button", { name: "Create chain" }).click();

    // The chain card renders both paths and preserves the LLM order.
    await expect(page.getByRole("heading", { name: "Fallback chain E2E" })).toBeVisible();
    const llmNodes = page
      .getByLabel("LLM route preview")
      .locator(".chain-route-node:not(.chain-route-node--system)");
    await expect(llmNodes).toHaveCount(2);
    await expect(llmNodes.nth(0)).toContainText("Claude subscription");
    await expect(llmNodes.nth(1)).toContainText("Primary OpenAI");
    await expect(page.getByLabel("Embeddings route preview")).toContainText(
      "text-embedding-3-small",
    );
  });

  test("keeps automatic embedding selection usable on mobile", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/chains");
    await page.getByRole("button", { name: "Create chain" }).click();

    const dialog = page.getByRole("dialog");
    await dialog.getByRole("tab", { name: /Embeddings/ }).click();
    await dialog.getByLabel("Add account").selectOption("acc-openai-0001");
    await dialog.getByRole("button", { name: "Add node" }).click();

    const model = dialog.getByLabel("Model for this node");
    await expect(model).toBeVisible();
    await expect(model).toHaveValue("text-embedding-3-small");
    await expect(model.locator("option")).toContainText([
      "Use model sent by client",
      "text-embedding-3-small",
      "text-embedding-3-large",
      "Enter model ID manually...",
    ]);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
      ),
    ).toBe(true);
  });
});
