import { api } from "./client";
import { ENDPOINTS } from "./config";
import type {
  AccountAnalyticsResponse,
  SystemAnalyticsResponse,
  TokenAnalyticsResponse,
} from "./types";

/**
 * Analytics_Service admin calls (Requirements 15.1, 15.2, 15.3): per-token,
 * per-account, and system reports over a caller-supplied half-open `[start, end)`
 * UTC time range.
 *
 * Every report is a pure aggregate of usage/trace data and carries no secret
 * material; the responses are typed against the schemas in `gozar/api/schemas.py`
 * with no `any` at the boundary. The backend requires both `start` and `end` query
 * params on every endpoint (`gozar/api/analytics.py`), so callers always pass a
 * complete range.
 */

/** A half-open `[start, end)` range; both bounds are ISO-8601 UTC timestamps. */
export interface AnalyticsRange {
  /** Inclusive range start (ISO-8601, UTC). */
  readonly start: string;
  /** Exclusive range end (ISO-8601, UTC). */
  readonly end: string;
}

/** Serialise a range into the `start`/`end` query the backend expects. */
function rangeQuery(range: AnalyticsRange): Record<string, string> {
  return { start: range.start, end: range.end };
}

/** Aggregate system-wide usage over the range (Requirement 15.3). */
export function systemReport(
  range: AnalyticsRange,
): Promise<SystemAnalyticsResponse> {
  return api.get<SystemAnalyticsResponse>(ENDPOINTS.analytics.system, rangeQuery(range));
}

/** Aggregate a Gozar API key's usage over the range (Requirement 15.1). */
export function tokenReport(
  tokenId: string,
  range: AnalyticsRange,
): Promise<TokenAnalyticsResponse> {
  return api.get<TokenAnalyticsResponse>(
    ENDPOINTS.analytics.token(tokenId),
    rangeQuery(range),
  );
}

/** Aggregate an Upstream_Credential's usage over the range (Requirement 15.2). */
export function accountReport(
  accountId: string,
  range: AnalyticsRange,
): Promise<AccountAnalyticsResponse> {
  return api.get<AccountAnalyticsResponse>(
    ENDPOINTS.analytics.account(accountId),
    rangeQuery(range),
  );
}
