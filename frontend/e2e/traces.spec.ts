import { expect, test } from "@playwright/test";

import { installApiMock } from "./fixtures/api-mock";
import { seedSession } from "./fixtures/auth";

/**
 * Flow 5 - View a trace: list and detail (Requirement 17.4, 14.3).
 *
 * Lists recent traces, opens the detail for one, and asserts the detail surfaces
 * the correlation id, the outcome, and the inbound request metadata.
 */
test.describe("view trace", () => {
  test.beforeEach(async ({ page }) => {
    await seedSession(page);
    await installApiMock(page);
  });

  test("opens a trace detail from the list", async ({ page }) => {
    await page.goto("/traces");

    // The seeded trace summary appears in the list.
    const row = page.getByRole("row", { name: /Success/ });
    await expect(row).toBeVisible();

    await page.getByRole("button", { name: /View trace/ }).click();

    // The detail dialog shows the trace fields and inbound metadata.
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByRole("heading", { name: "Trace detail" })).toBeVisible();
    await expect(dialog.getByText("Correlation ID", { exact: true })).toBeVisible();
    await expect(dialog.getByText("trace-abc-123", { exact: true })).toBeVisible();
    await expect(dialog.getByRole("heading", { name: "Inbound metadata" })).toBeVisible();
    await expect(dialog.getByText("gpt-4o-mini", { exact: true })).toBeVisible();
  });
});
