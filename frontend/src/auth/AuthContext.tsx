import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { configureClient } from "../api/client";
import * as authApi from "../api/auth";
import type { BootstrapCreateRequest, LoginRequest, SessionTokens } from "../api/types";
import { clearSession, loadSession, saveSession } from "./session-storage";

/**
 * Operator session state shared across the console.
 *
 * Holds the current session tokens, exposes `signIn`/`signOut`, and wires the
 * API client's auth hooks (bearer token, silent refresh, and forced sign-out on
 * unrecoverable 401). Login loading/error states are owned by the LoginPage; the
 * context just performs the network call and stores the result.
 */
export interface AuthContextValue {
  /** True when a session token bundle is present. */
  readonly isAuthenticated: boolean;
  /** Authenticate and store the session. Throws ApiError on failure. */
  readonly signIn: (credentials: LoginRequest) => Promise<void>;
  /**
   * Create the first administrator (first-run bootstrap) and store the returned
   * session, identically to {@link signIn}. Throws ApiError on failure.
   */
  readonly completeBootstrap: (credentials: BootstrapCreateRequest) => Promise<void>;
  /** Clear the session locally. */
  readonly signOut: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }): JSX.Element {
  const [tokens, setTokens] = useState<SessionTokens | null>(() => loadSession());

  // A ref mirror of the tokens so the client hooks always read the latest value.
  const tokensRef = useRef<SessionTokens | null>(tokens);
  tokensRef.current = tokens;

  const applyTokens = useCallback((next: SessionTokens | null): void => {
    tokensRef.current = next;
    setTokens(next);
    if (next) {
      saveSession(next);
    } else {
      clearSession();
    }
  }, []);

  const signOut = useCallback((): void => {
    applyTokens(null);
  }, [applyTokens]);

  const signIn = useCallback(
    async (credentials: LoginRequest): Promise<void> => {
      const session = await authApi.login(credentials);
      applyTokens(session);
    },
    [applyTokens],
  );

  const completeBootstrap = useCallback(
    async (credentials: BootstrapCreateRequest): Promise<void> => {
      const session = await authApi.bootstrapCreate(credentials);
      applyTokens(session);
    },
    [applyTokens],
  );

  // Register synchronously so child route effects do not fire their first API
  // requests before the bearer-token hook is available.
  configureClient({
    getAccessToken: () => tokensRef.current?.access_token ?? null,
    refresh: async (): Promise<string | null> => {
      const current = tokensRef.current;
      if (!current) {
        return null;
      }
      try {
        const session = await authApi.refresh(current.refresh_token);
        applyTokens(session);
        return session.access_token;
      } catch {
        applyTokens(null);
        return null;
      }
    },
    onAuthFailure: () => {
      applyTokens(null);
    },
  });

  const value = useMemo<AuthContextValue>(
    () => ({
      isAuthenticated: tokens !== null,
      signIn,
      completeBootstrap,
      signOut,
    }),
    [tokens, signIn, completeBootstrap, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** Access the auth context; throws if used outside an AuthProvider. */
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (ctx === null) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return ctx;
}
