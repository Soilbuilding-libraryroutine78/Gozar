import { API_BASE_URL } from "./config";
import { ApiError, apiErrorFromResponse } from "./errors";

/**
 * Typed HTTP client for the Gozar admin API.
 *
 * Responsibilities:
 * - prefix requests with the configured base URL,
 * - attach the operator's bearer access token,
 * - transparently refresh the session once on a 401 and retry,
 * - parse JSON responses into the caller-declared type,
 * - turn any non-2xx response into a typed {@link ApiError}.
 *
 * Auth state is injected through {@link configureClient} so this module has no
 * dependency on React; the AuthContext owns the tokens and supplies the hooks.
 */

/** Supplies the current access token, or null when the operator is logged out. */
type AccessTokenProvider = () => string | null;

/** Attempts a session refresh; resolves to the new access token or null on failure. */
type RefreshHandler = () => Promise<string | null>;

/** Invoked when authentication is unrecoverable (refresh failed/absent). */
type AuthFailureHandler = () => void;

interface ClientHooks {
  getAccessToken: AccessTokenProvider;
  refresh: RefreshHandler | null;
  onAuthFailure: AuthFailureHandler | null;
}

const hooks: ClientHooks = {
  getAccessToken: () => null,
  refresh: null,
  onAuthFailure: null,
};

/** Wire the client to the auth layer. Called once during app bootstrap. */
export function configureClient(next: Partial<ClientHooks>): void {
  Object.assign(hooks, next);
}

type Query = Record<string, string | number | boolean | undefined>;

interface RequestOptions {
  readonly method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  readonly body?: unknown;
  readonly query?: Query;
  /** When false, a 401 is surfaced without attempting a refresh (used by login). */
  readonly authRetry?: boolean;
}

function buildUrl(path: string, query?: Query): string {
  const base = `${API_BASE_URL}${path}`;
  if (!query) {
    return base;
  }
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) {
      params.append(key, String(value));
    }
  }
  const qs = params.toString();
  return qs ? `${base}?${qs}` : base;
}

function buildHeaders(hasBody: boolean): Headers {
  const headers = new Headers();
  headers.set("Accept", "application/json");
  if (hasBody) {
    headers.set("Content-Type", "application/json");
  }
  const token = hooks.getAccessToken();
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  return headers;
}

async function parseJson<T>(response: Response): Promise<T> {
  // 204 No Content and empty bodies decode to undefined; the caller's type
  // should be `void` in those cases.
  if (response.status === 204) {
    return undefined as T;
  }
  const text = await response.text();
  if (text.length === 0) {
    return undefined as T;
  }
  return JSON.parse(text) as T;
}

async function execute(path: string, options: RequestOptions): Promise<Response> {
  const hasBody = options.body !== undefined;
  let init: RequestInit;
  try {
    init = {
      method: options.method ?? "GET",
      headers: buildHeaders(hasBody),
      body: hasBody ? JSON.stringify(options.body) : null,
    };
  } catch {
    throw new ApiError(0, "SERIALIZE_ERROR", "Failed to serialize the request body.", []);
  }

  try {
    return await fetch(buildUrl(path, options.query), init);
  } catch {
    // Network/transport failure (server unreachable, DNS, CORS, offline).
    throw new ApiError(0, "NETWORK_ERROR", "Unable to reach the Gozar server.", []);
  }
}

/**
 * Perform a typed request. Resolves with the decoded body (or `undefined` for
 * 204 responses) and throws {@link ApiError} on any failure.
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const allowRefresh = options.authRetry !== false;
  let response = await execute(path, options);

  if (response.status === 401 && allowRefresh && hooks.refresh) {
    const refreshed = await hooks.refresh();
    if (refreshed) {
      response = await execute(path, options);
    } else if (hooks.onAuthFailure) {
      hooks.onAuthFailure();
    }
  }

  if (!response.ok) {
    const error = await apiErrorFromResponse(response);
    if (error.status === 401 && hooks.onAuthFailure && allowRefresh) {
      hooks.onAuthFailure();
    }
    throw error;
  }

  return parseJson<T>(response);
}

/** Convenience verbs over {@link request}. */
export const api = {
  get: <T>(path: string, query?: Query): Promise<T> =>
    request<T>(path, { method: "GET", ...(query ? { query } : {}) }),
  post: <T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> =>
    request<T>(path, { method: "POST", ...(body !== undefined ? { body } : {}), ...options }),
  put: <T>(path: string, body?: unknown): Promise<T> =>
    request<T>(path, { method: "PUT", ...(body !== undefined ? { body } : {}) }),
  patch: <T>(path: string, body?: unknown): Promise<T> =>
    request<T>(path, { method: "PATCH", ...(body !== undefined ? { body } : {}) }),
  delete: <T>(path: string): Promise<T> => request<T>(path, { method: "DELETE" }),
} as const;
