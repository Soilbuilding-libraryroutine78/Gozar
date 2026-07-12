/**
 * API endpoint configuration: a single source of truth for the backend base URL
 * and every admin/auth endpoint path the console calls.
 *
 * The admin control-path is mounted at `/api` on the backend; the auth endpoints
 * live under `/api/auth`. Base URL comes from the build-time `VITE_API_BASE_URL`
 * (wired by compose). When empty, requests are same-origin (a reverse proxy or
 * the Vite dev proxy forwards them), so production URLs are never hardcoded.
 */

/** Backend origin, without a trailing slash. Empty string means same-origin. */
export const API_BASE_URL: string = (import.meta.env.VITE_API_BASE_URL ?? "").replace(
  /\/+$/,
  "",
);

/** Mount prefix of the admin control-path API. */
const API_PREFIX = "/api";

/**
 * Admin + auth endpoint paths.
 *
 * The `auth.*` endpoints below are served by the public auth control-path router
 * (`gozar/api/auth.py`, mounted under `/api/auth`): `POST /login`,
 * `POST /refresh`, `GET /bootstrap` (status), and `POST /bootstrap` (create the
 * first admin). They are public by necessity (login and first-run bootstrap
 * cannot require an existing session); every other admin endpoint below is
 * fail-closed authenticated. Success bodies match the documented `SessionTokens`
 * shape the console is typed against.
 */
export const ENDPOINTS = {
  auth: {
    login: `${API_PREFIX}/auth/login`,
    refresh: `${API_PREFIX}/auth/refresh`,
    bootstrapStatus: `${API_PREFIX}/auth/bootstrap`,
    bootstrap: `${API_PREFIX}/auth/bootstrap`,
  },
  accounts: {
    list: `${API_PREFIX}/accounts`,
    connectApiKey: `${API_PREFIX}/accounts/connect/api-key`,
    subscriptionBegin: `${API_PREFIX}/accounts/connect/subscription/begin`,
    subscriptionComplete: `${API_PREFIX}/accounts/connect/subscription/complete`,
    subscriptionDeviceBegin: `${API_PREFIX}/accounts/connect/subscription/device/begin`,
    subscriptionDeviceComplete: `${API_PREFIX}/accounts/connect/subscription/device/complete`,
    limit: (accountId: string) => `${API_PREFIX}/accounts/${accountId}/limit`,
    enabled: (accountId: string) => `${API_PREFIX}/accounts/${accountId}/enabled`,
    detail: (accountId: string) => `${API_PREFIX}/accounts/${accountId}`,
  },
  tokens: {
    list: `${API_PREFIX}/tokens`,
    create: `${API_PREFIX}/tokens`,
    models: (tokenId: string) => `${API_PREFIX}/tokens/${tokenId}/models`,
    test: (tokenId: string) => `${API_PREFIX}/tokens/${tokenId}/test`,
    limit: (tokenId: string) => `${API_PREFIX}/tokens/${tokenId}/limit`,
    chain: (tokenId: string) => `${API_PREFIX}/tokens/${tokenId}/chain`,
    enabled: (tokenId: string) => `${API_PREFIX}/tokens/${tokenId}/enabled`,
    reveal: (tokenId: string) => `${API_PREFIX}/tokens/${tokenId}/reveal`,
    rotate: (tokenId: string) => `${API_PREFIX}/tokens/${tokenId}/rotate`,
    revoke: (tokenId: string) => `${API_PREFIX}/tokens/${tokenId}/revoke`,
  },
  chains: {
    list: `${API_PREFIX}/chains`,
    create: `${API_PREFIX}/chains`,
    detail: (chainId: string) => `${API_PREFIX}/chains/${chainId}`,
  },
  models: {
    catalog: `${API_PREFIX}/models`,
    provider: (provider: string) =>
      `${API_PREFIX}/models/providers/${encodeURIComponent(provider)}`,
  },
  traces: {
    list: `${API_PREFIX}/traces`,
    detail: (correlationId: string) => `${API_PREFIX}/traces/${correlationId}`,
  },
  analytics: {
    system: `${API_PREFIX}/analytics/system`,
    token: (tokenId: string) => `${API_PREFIX}/analytics/tokens/${tokenId}`,
    account: (accountId: string) => `${API_PREFIX}/analytics/accounts/${accountId}`,
  },
} as const;
