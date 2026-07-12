// Shared axe-core setup for the Web_Console automated accessibility checks.
//
// Registers jest-axe's `toHaveNoViolations` matcher against Vitest's `expect`
// (Vitest uses its own assertion interface, so the matcher is also declared on the
// `vitest` module below for strict-mode type checking), and exports a pre-configured
// `axe` runner scoped to the WCAG 2.0/2.1 Level A and AA success criteria so the
// checks measure against Requirement 17.5's WCAG 2.1 AA target.
//
// NOTE: automated axe checks cover only the machine-detectable subset of WCAG 2.1
// AA. Full Level AA conformance additionally requires manual assistive-technology
// review (screen readers, keyboard-only navigation, zoom/reflow, focus order), which
// cannot be proven by automation alone.
import { expect } from "vitest";
import { configureAxe, toHaveNoViolations } from "jest-axe";

expect.extend(toHaveNoViolations);

// Vitest does not pick up jest-axe's jest-oriented matcher typings, so declare the
// matcher on Vitest's assertion interfaces to keep the suite type-clean under strict
// mode (tsconfig.test.json). The type parameter defaults must match Vitest's own
// `Assertion` declaration exactly for the interface merge to be valid.
interface AxeMatchers<R = unknown> {
  toHaveNoViolations(): R;
}
declare module "vitest" {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  interface Assertion<T = any> extends AxeMatchers<T> {}
  interface AsymmetricMatchersContaining extends AxeMatchers {}
}

/**
 * Run axe against a rendered DOM node, evaluating only the WCAG 2.0/2.1 A and AA
 * rule tags.
 *
 * jsdom has no layout or rendering engine, so layout/paint-dependent rules cannot be
 * evaluated here. `color-contrast` (WCAG 2.1 AA 1.4.3) is therefore disabled: jsdom
 * computes no colors, so it can only ever report "incomplete" and merely emits noise.
 * Contrast must instead be verified by the real-browser end-to-end axe run and by
 * manual review -- part of the manual assistive-technology review WCAG 2.1 AA
 * conformance still requires.
 */
export const axe = configureAxe({
  runOnly: {
    type: "tag",
    values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"],
  },
  rules: {
    "color-contrast": { enabled: false },
  },
});
