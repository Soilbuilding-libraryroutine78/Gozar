// Global test setup for the Web_Console component suite.
//
// Registers the jest-dom matchers (e.g. toBeInTheDocument, toBeDisabled) against
// Vitest's `expect`. Imported by every test through `setupFiles` in vitest.config.ts.
import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

if (!("ResizeObserver" in globalThis)) {
  class TestResizeObserver {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }

  Object.defineProperty(globalThis, "ResizeObserver", {
    configurable: true,
    writable: true,
    value: TestResizeObserver,
  });
}

// Unmount any rendered trees between tests to keep them isolated and deterministic.
afterEach(() => {
  cleanup();
});
