import { api } from "./client";
import { ENDPOINTS } from "./config";
import type {
  ChatCompletionResponse,
  CreateTokenRequest,
  IssuedTokenResponse,
  ModelListResponse,
  RevealTokenRequest,
  RotateTokenRequest,
  SetEnabledRequest,
  SetTokenChainRequest,
  TestTokenRouteRequest,
  TokenResponse,
  UsageLimitSpec,
} from "./types";

/**
 * Token_Authority admin calls (Requirements 8.1, 8.3, 9.x): list issued Gozar
 * API keys, create a key, reveal the same key after password confirmation,
 * configure a usage limit, enable/disable, and revoke.
 *
 * Every function is typed against the secret-free schemas in `gozar/api/schemas.py`;
 * no `any` crosses the API boundary. Secret-carrying responses use
 * {@link IssuedTokenResponse} from {@link createToken} and {@link revealToken};
 * every read view ({@link TokenResponse}) omits the secret entirely (Requirement
 * 8.3).
 */

/** Return secret-free views of every issued Gozar API key, ordered by the backend. */
export function listTokens(): Promise<ReadonlyArray<TokenResponse>> {
  return api.get<ReadonlyArray<TokenResponse>>(ENDPOINTS.tokens.list);
}

/** Issue a new Gozar API key; the response carries the secret for the operator. */
export function createToken(
  payload: CreateTokenRequest,
): Promise<IssuedTokenResponse> {
  return api.post<IssuedTokenResponse>(ENDPOINTS.tokens.create, payload);
}

/** Reveal the same API key after password confirmation; does not rotate/revoke. */
export function revealToken(
  tokenId: string,
  password: string,
  existingApiKey?: string,
): Promise<IssuedTokenResponse> {
  const payload: RevealTokenRequest = {
    password,
    ...(existingApiKey !== undefined && existingApiKey.length > 0
      ? { existing_api_key: existingApiKey }
      : {}),
  };
  return api.post<IssuedTokenResponse>(ENDPOINTS.tokens.reveal(tokenId), payload);
}

/** Issue a replacement API key after password confirmation. */
export function rotateToken(
  tokenId: string,
  password: string,
): Promise<IssuedTokenResponse> {
  const payload: RotateTokenRequest = { password };
  return api.post<IssuedTokenResponse>(ENDPOINTS.tokens.rotate(tokenId), payload);
}

/** Return models reachable through one Gozar API key's selected route. */
export function listTokenModels(tokenId: string): Promise<ModelListResponse> {
  return api.get<ModelListResponse>(ENDPOINTS.tokens.models(tokenId));
}

/** Test an API key route through the authenticated admin control path. */
export function testTokenRoute(
  tokenId: string,
  payload: TestTokenRouteRequest,
): Promise<ChatCompletionResponse> {
  return api.post<ChatCompletionResponse>(ENDPOINTS.tokens.test(tokenId), payload);
}

/** Persist (create or replace) a token's usage limit (Requirement 9.1). */
export function setTokenLimit(
  tokenId: string,
  limit: UsageLimitSpec,
): Promise<void> {
  return api.put<void>(ENDPOINTS.tokens.limit(tokenId), limit);
}

/** Pin a token to a fallback chain, or clear it back to model-selector auto routing. */
export function setTokenChain(
  tokenId: string,
  assignedChainId: string | null,
): Promise<void> {
  const payload: SetTokenChainRequest = { assigned_chain_id: assignedChainId };
  return api.patch<void>(ENDPOINTS.tokens.chain(tokenId), payload);
}

/** Enable or disable a token (Requirements 9.3, 9.4). */
export function setTokenEnabled(tokenId: string, enabled: boolean): Promise<void> {
  const payload: SetEnabledRequest = { enabled };
  return api.patch<void>(ENDPOINTS.tokens.enabled(tokenId), payload);
}

/** Permanently revoke a token; revocation is terminal (Requirement 8.4). */
export function revokeToken(tokenId: string): Promise<void> {
  return api.post<void>(ENDPOINTS.tokens.revoke(tokenId));
}
