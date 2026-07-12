import type {
  AccountAnalyticsResponse,
  AccountResponse,
  ChainResponse,
  SessionTokens,
  SystemAnalyticsResponse,
  TokenAnalyticsResponse,
  TokenResponse,
  TraceDetailResponse,
  TraceSummaryResponse,
} from "../../src/api/types";

/**
 * Typed fixture responses for the e2e API mock (task 16.8).
 *
 * Every value here is typed against the real client schemas in `src/api/types.ts`,
 * so the mocked backend cannot drift from the contract the console is built against.
 * No secrets appear in any read view; secret-bearing payloads are limited to the
 * create-token response and explicit password-confirmed reveal responses.
 */

/** A session bundle returned by the mocked `POST /api/auth/login` and refresh. */
export const SESSION: SessionTokens = {
  access_token: "e2e-access-token",
  refresh_token: "e2e-refresh-token",
  token_type: "bearer",
  expires_in: 900,
};

/** Two connected accounts used to seed account, chain, and analytics flows. */
export function seedAccounts(): AccountResponse[] {
  return [
    {
      account_id: "acc-openai-0001",
      provider: "openai",
      kind: "api_key",
      label: "Primary OpenAI",
      status: "active",
      connected_at: "2024-01-01T12:00:00.000Z",
      limit: null,
      consumption: 0,
    },
    {
      account_id: "acc-anthropic-0002",
      provider: "anthropic",
      kind: "subscription",
      label: "Claude subscription",
      status: "active",
      connected_at: "2024-01-02T09:30:00.000Z",
      limit: null,
      consumption: 0,
    },
  ];
}

/** API keys used to seed the analytics subject picker (no secret in read views). */
export function seedTokens(): TokenResponse[] {
  return [
    {
      token_id: "tok-existing-0001",
      id_prefix: "e2eexisting0001",
      label: "Existing app key",
      status: "active",
      limit: null,
      usage: 12,
      can_reveal: true,
    },
  ];
}

/** Chains start empty; the build-a-chain flow creates one. */
export function seedChains(): ChainResponse[] {
  return [];
}

/** A single recent trace summary for the trace list. */
export function seedTraceSummaries(): TraceSummaryResponse[] {
  return [
    {
      correlation_id: "trace-abc-123",
      started_at: "2024-03-10T08:15:00.000Z",
      ended_at: "2024-03-10T08:15:01.250Z",
      outcome: "success",
      status_code: 200,
      account_id: "acc-openai-0001",
      elapsed_seconds: 1.25,
    },
  ];
}

/** The full detail for the seeded trace, keyed by correlation id. */
export function seedTraceDetail(): TraceDetailResponse {
  return {
    correlation_id: "trace-abc-123",
    started_at: "2024-03-10T08:15:00.000Z",
    ended_at: "2024-03-10T08:15:01.250Z",
    outcome: "success",
    status_code: 200,
    account_id: "acc-openai-0001",
    elapsed_seconds: 1.25,
    inbound_meta: {
      method: "POST",
      path: "/v1/chat/completions",
      model: "gpt-4o-mini",
      stream: false,
    },
    outbound_meta: {
      provider: "openai",
      finish_reason: "stop",
    },
  };
}

/** System analytics report returned for the default range. */
export function systemAnalytics(start: string, end: string): SystemAnalyticsResponse {
  return {
    range: { start, end },
    request_count: 42,
    error_count: 3,
    error_rate: 0.071,
    total_tokens: 900,
  };
}

/** Per-token analytics report. */
export function tokenAnalytics(
  tokenId: string,
  start: string,
  end: string,
): TokenAnalyticsResponse {
  return {
    token_id: tokenId,
    range: { start, end },
    counts: {
      request_count: 42,
      prompt_tokens: 600,
      completion_tokens: 300,
      total_tokens: 900,
    },
    consumption: {
      spec: null,
      consumed: null,
      percent_of_limit: null,
      reached: null,
    },
  };
}

/** Per-account analytics report. */
export function accountAnalytics(
  accountId: string,
  start: string,
  end: string,
): AccountAnalyticsResponse {
  return {
    account_id: accountId,
    range: { start, end },
    counts: {
      request_count: 30,
      prompt_tokens: 400,
      completion_tokens: 200,
      total_tokens: 600,
    },
    error_count: 1,
    consumption: {
      spec: null,
      consumed: null,
      percent_of_limit: null,
      reached: null,
    },
  };
}
