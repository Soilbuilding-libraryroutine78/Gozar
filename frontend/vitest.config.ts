import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Vitest configuration for the Gozar Web_Console component tests.
//
// Kept separate from `vite.config.ts` so the production build (the `build` stage
// in the Dockerfile) never loads the test runner. Tests run in a jsdom DOM with
// React Testing Library; the typed API layer (`src/api/*`) is mocked per-test, so
// no network or backend is required.
export default defineConfig({
  plugins: [react()],
  test: {
    globals: false,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
    restoreMocks: true,
    clearMocks: true,
  },
});
