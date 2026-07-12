import { api } from "./client";
import { ENDPOINTS } from "./config";
import type {
  AccountResponse,
  ApiKeyConnectRequest,
  AuthorizationChallengeResponse,
  CredentialSummaryResponse,
  DeviceAuthorizationChallengeResponse,
  DeviceAuthorizationCompleteResponse,
  SetEnabledRequest,
  SubscriptionBeginRequest,
  SubscriptionCompleteRequest,
  SubscriptionDeviceBeginRequest,
  SubscriptionDeviceCompleteRequest,
  UsageLimitSpec,
} from "./types";

/**
 * Account_Manager admin calls (Requirement 5.4): list connected accounts, connect
 * subscription (OAuth + PKCE) and metered API-key accounts, configure usage limits,
 * enable/disable, and delete.
 *
 * Every function is typed against the secret-free schemas in `gozar/api/schemas.py`;
 * no `any` crosses the API boundary. The PKCE `code_verifier` never reaches the
 * client: the begin step returns only the authorize URL plus opaque handles, and the
 * complete step echoes back the provider's `code`/`state`.
 */

/** Return non-secret summaries of every connected account, ordered by the backend. */
export function listAccounts(): Promise<ReadonlyArray<AccountResponse>> {
  return api.get<ReadonlyArray<AccountResponse>>(ENDPOINTS.accounts.list);
}

/** Validate and connect a metered API-key account (Requirements 2.1-2.3). */
export function connectApiKey(
  payload: ApiKeyConnectRequest,
): Promise<CredentialSummaryResponse> {
  return api.post<CredentialSummaryResponse>(ENDPOINTS.accounts.connectApiKey, payload);
}

/** Begin a subscription OAuth connect; returns the authorize URL and opaque handles. */
export function beginSubscriptionConnect(
  payload: SubscriptionBeginRequest,
): Promise<AuthorizationChallengeResponse> {
  return api.post<AuthorizationChallengeResponse>(
    ENDPOINTS.accounts.subscriptionBegin,
    payload,
  );
}

/** Begin a device-code subscription connect for providers that support it. */
export function beginSubscriptionDeviceConnect(
  payload: SubscriptionDeviceBeginRequest,
): Promise<DeviceAuthorizationChallengeResponse> {
  return api.post<DeviceAuthorizationChallengeResponse>(
    ENDPOINTS.accounts.subscriptionDeviceBegin,
    payload,
  );
}

/** Poll/complete a device-code subscription connect. */
export function completeSubscriptionDeviceConnect(
  payload: SubscriptionDeviceCompleteRequest,
): Promise<DeviceAuthorizationCompleteResponse> {
  return api.post<DeviceAuthorizationCompleteResponse>(
    ENDPOINTS.accounts.subscriptionDeviceComplete,
    payload,
  );
}

/** Complete a subscription connect with the provider callback's code and state. */
export function completeSubscriptionConnect(
  payload: SubscriptionCompleteRequest,
): Promise<CredentialSummaryResponse> {
  return api.post<CredentialSummaryResponse>(
    ENDPOINTS.accounts.subscriptionComplete,
    payload,
  );
}

/** Persist (create or replace) an account's usage limit (Requirements 4.1, 4.4). */
export function setAccountLimit(
  accountId: string,
  limit: UsageLimitSpec,
): Promise<void> {
  return api.put<void>(ENDPOINTS.accounts.limit(accountId), limit);
}

/** Enable or disable an account (Requirements 5.1, 5.2). */
export function setAccountEnabled(accountId: string, enabled: boolean): Promise<void> {
  const payload: SetEnabledRequest = { enabled };
  return api.patch<void>(ENDPOINTS.accounts.enabled(accountId), payload);
}

/** Delete an account: hard-delete secret material, retain usage history (Req 5.3). */
export function deleteAccount(accountId: string): Promise<void> {
  return api.delete<void>(ENDPOINTS.accounts.detail(accountId));
}
