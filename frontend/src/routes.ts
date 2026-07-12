/**
 * Single source of truth for client-side route paths (steering section 8).
 *
 * Every navigation target and <Route path> in the app references a member of
 * {@link ROUTES}. No route string literals are scattered through components, so
 * a path only ever changes in one place. Subsequent console views (tasks
 * 16.2-16.5) add their paths here.
 */
export const ROUTES = {
  /** Operator login screen (public). */
  login: "/login",
  /** Authenticated landing page. */
  dashboard: "/",
  /** Upstream credential management (task 16.2). */
  accounts: "/accounts",
  /** Gozar API key management (task 16.3). */
  tokens: "/tokens",
  /** Visual fallback-chain editor (task 16.4). */
  chains: "/chains",
  /** Request trace list and detail (task 16.5). */
  traces: "/traces",
  /** Analytics reports (task 16.5). */
  analytics: "/analytics",
  /** Operator-facing product and integration documentation. */
  docs: "/docs",
} as const;

/** A value that is one of the known route paths. */
export type RoutePath = (typeof ROUTES)[keyof typeof ROUTES];
