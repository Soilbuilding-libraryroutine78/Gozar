import { expect, test } from "@playwright/test";

import { installApiMock } from "./fixtures/api-mock";

/**
 * Flow 1 - Operator login (Requirement 17.1, 16.1).
 *
 * Drives the real login form against the mocked `/api/auth/login` endpoint and
 * asserts that a successful sign-in lands the operator on the authenticated
 * dashboard. Also covers the unauthenticated redirect: a protected route bounces
 * to the login screen.
 */
test.describe("operator login", () => {
  test.beforeEach(async ({ page }) => {
    await installApiMock(page);
  });

  test("redirects an unauthenticated operator to the login screen", async ({ page }) => {
    await page.goto("/accounts");
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByRole("heading", { name: "Gozar" })).toBeVisible();
    await expect(page.getByLabel("Username")).toBeVisible();
  });

  test("signs in and reaches the dashboard", async ({ page }) => {
    await page.goto("/login");

    await page.getByLabel("Username").fill("operator");
    await page.getByLabel("Password").fill("correct horse battery staple");
    await page.getByRole("button", { name: "Sign in" }).click();

    // Lands on the dashboard (the "/" route) with the navigation shell.
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
    const nav = page.getByRole("navigation", { name: "Primary" });
    await expect(nav.getByRole("link", { name: "Accounts" })).toBeVisible();
    await expect(nav.getByRole("link", { name: "API keys" })).toBeVisible();
  });
});
