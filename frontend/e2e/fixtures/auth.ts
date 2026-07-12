import type { Page } from "@playwright/test";

import { SESSION } from "./data";

/**
 * Seed an authenticated operator session before the app loads.
 *
 * The console persists its session bundle in `localStorage` under
 * `gozar.session` (see `src/auth/session-storage.ts`). Seeding it via an init
 * script means the AuthProvider hydrates as already-authenticated, so flows that
 * are not specifically testing login can start on a protected view directly.
 *
 * The login flow itself is exercised through the real UI in `login.spec.ts`.
 */
export async function seedSession(page: Page): Promise<void> {
  await page.addInitScript((session) => {
    window.localStorage.setItem("gozar.session", JSON.stringify(session));
  }, SESSION);
}
