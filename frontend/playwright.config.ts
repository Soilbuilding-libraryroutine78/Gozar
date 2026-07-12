import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright end-to-end configuration for the Gozar Web_Console (task 16.8).
 *
 * This is a SEPARATE tooling layer from the Vitest component suite:
 *  - Playwright specs live in `./e2e` (this `testDir`); Vitest only globs
 *    `src/**\/*.test.{ts,tsx}`, so the two runners never pick up each other's files.
 *  - The tests are hermetic: they never require a running backend. Each spec mocks
 *    the admin/auth HTTP API at the network layer with `page.route` (see
 *    `e2e/fixtures/api-mock.ts`), fulfilling typed fixture responses that match the
 *    backend schemas in `src/api/types.ts`.
 *  - The SPA under test is the real production build, served by `vite preview` via
 *    the `webServer` block below. The build runs first so the e2e suite exercises
 *    exactly what ships.
 *
 * Browsers are downloaded with `npx playwright install chromium`. Where the
 * environment cannot download browsers, the config and specs are still correct and
 * run anywhere Chromium is available.
 */

const PORT = 4173;
const BASE_URL = `http://localhost:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? "list" : [["list"]],
  timeout: 30_000,
  expect: { timeout: 5_000 },

  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  // Build the SPA, then serve the static bundle with `vite preview`. The mocked
  // API means no backend is needed; the preview server only serves the SPA assets.
  webServer: {
    command: "npm run build && npm run preview -- --port 4173 --strictPort",
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
