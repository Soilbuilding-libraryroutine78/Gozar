import { api } from "./client";
import { ENDPOINTS } from "./config";
import type {
  BootstrapCreateRequest,
  BootstrapStatus,
  LoginRequest,
  RefreshRequest,
  SessionTokens,
} from "./types";

/**
 * Operator authentication calls against the expected `/api/auth` surface.
 *
 * See the backend-dependency note in `config.ts`: these endpoints are the
 * documented contract for the existing Auth_Service logic and must be mounted
 * by a backend auth router for live login to work. The client is fully typed
 * against the `SessionTokens` shape the service already returns.
 */

/** Exchange operator credentials for a session token bundle. */
export function login(credentials: LoginRequest): Promise<SessionTokens> {
  // authRetry:false -> a 401 here means "bad credentials", not "expired session",
  // so the client must not attempt a refresh.
  return api.post<SessionTokens>(ENDPOINTS.auth.login, credentials, { authRetry: false });
}

/** Exchange a refresh token for a fresh session token bundle. */
export function refresh(refreshToken: string): Promise<SessionTokens> {
  const payload: RefreshRequest = { refresh_token: refreshToken };
  return api.post<SessionTokens>(ENDPOINTS.auth.refresh, payload, { authRetry: false });
}

/** Query whether first-run admin bootstrap is still required. */
export function bootstrapStatus(): Promise<BootstrapStatus> {
  return api.get<BootstrapStatus>(ENDPOINTS.auth.bootstrapStatus);
}

/**
 * Create the first administrator during first-run bootstrap and receive a session
 * token bundle. Like {@link login}, a 4xx here is a validation/policy failure, not
 * an expired session, so the client must not attempt a refresh (`authRetry:false`).
 */
export function bootstrapCreate(
  credentials: BootstrapCreateRequest,
): Promise<SessionTokens> {
  return api.post<SessionTokens>(ENDPOINTS.auth.bootstrap, credentials, {
    authRetry: false,
  });
}
