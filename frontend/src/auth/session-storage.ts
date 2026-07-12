import type { SessionTokens } from "../api/types";

/**
 * Persistence for the operator session token bundle.
 *
 * Tokens are kept in `localStorage` so a page reload does not force a re-login.
 * The access token is short-lived and the refresh token rotates on every use
 * (see `gozar/auth/session.py`), which bounds the exposure of persisted tokens.
 */

const STORAGE_KEY = "gozar.session";

function isSessionTokens(value: unknown): value is SessionTokens {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.access_token === "string" &&
    typeof candidate.refresh_token === "string" &&
    typeof candidate.token_type === "string" &&
    typeof candidate.expires_in === "number"
  );
}

/** Load the persisted session, or null when absent/corrupt. */
export function loadSession(): SessionTokens | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return null;
    }
    const parsed: unknown = JSON.parse(raw);
    return isSessionTokens(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/** Persist the session token bundle. */
export function saveSession(tokens: SessionTokens): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(tokens));
  } catch {
    // Storage may be unavailable (private mode/quota); the in-memory session
    // still works for the current page lifetime.
  }
}

/** Remove any persisted session. */
export function clearSession(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Ignore storage errors on clear.
  }
}
