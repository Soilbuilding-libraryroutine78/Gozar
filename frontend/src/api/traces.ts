import { api } from "./client";
import { ENDPOINTS } from "./config";
import type { TraceDetailResponse, TraceSummaryResponse } from "./types";

/**
 * Usage_Recorder trace admin calls (Requirement 14.3): list recent Trace_Log
 * entries and read a single trace's full detail.
 *
 * Every function is typed against the secret-free trace schemas in
 * `gozar/api/schemas.py`; trace metadata never carries credential material
 * (Requirement 16.4), so no `any` and no secret crosses the API boundary. The
 * list returns lightweight {@link TraceSummaryResponse} rows (most recent first,
 * per `gozar/api/traces.py`); the detail call adds the inbound/outbound metadata.
 */

/** Paging controls for the trace list, mirroring the backend `limit`/`offset` query. */
export interface TraceListParams {
  /** Maximum rows to return (backend clamps to 1..500; default 100). */
  readonly limit?: number;
  /** Number of rows to skip from the most recent (default 0). */
  readonly offset?: number;
}

/** Return recent Trace_Log summaries, most recent first (Requirement 14.3). */
export function listTraces(
  params: TraceListParams = {},
): Promise<ReadonlyArray<TraceSummaryResponse>> {
  const query: Record<string, number> = {};
  if (params.limit !== undefined) {
    query.limit = params.limit;
  }
  if (params.offset !== undefined) {
    query.offset = params.offset;
  }
  return api.get<ReadonlyArray<TraceSummaryResponse>>(ENDPOINTS.traces.list, query);
}

/** Read a single trace by correlation id, or reject with a 404 ApiError when absent. */
export function getTrace(correlationId: string): Promise<TraceDetailResponse> {
  return api.get<TraceDetailResponse>(ENDPOINTS.traces.detail(correlationId));
}
