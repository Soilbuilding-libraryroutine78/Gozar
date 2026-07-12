import { useCallback, useEffect, useMemo, useState } from "react";

import { listTraces } from "../api/traces";
import { ApiError } from "../api/errors";
import {
  AnalyticsIcon,
  AlertIcon,
  InboxIcon,
  RefreshIcon,
  TracesIcon,
} from "../components/icons";
import { PageGuide } from "../components/PageGuide";
import { TableSkeleton } from "../components/Skeleton";
import type { TraceSummaryResponse } from "../api/types";
import { formatNumber, formatTimestamp } from "./accounts/format";
import { Modal } from "./accounts/Modal";
import { TraceDetail } from "./traces/TraceDetail";
import {
  describeCredential,
  describeElapsed,
  describeStatus,
  outcomeView,
} from "./traces/format";

/** Translate any thrown error into a secret-free, displayable message. */
function messageFor(cause: unknown): string {
  if (cause instanceof ApiError) {
    return cause.message;
  }
  return "Unexpected error. Please try again.";
}

type TraceOutcomeFilter = "all" | "success" | "error" | "in_progress";

/**
 * Request-trace view (Requirements 14.3, 17.4): list recent Trace_Log entries
 * (most recent first) with the started instant, outcome, final status, selected
 * Upstream_Credential, and elapsed duration, and open a per-trace detail showing
 * the full inbound/outbound metadata.
 *
 * Every async surface renders explicit loading, empty, and error states. Trace
 * metadata never carries secrets (Requirement 16.4). Icons are outline SVGs; there
 * are no emoji.
 */
export function TracesPage(): JSX.Element {
  const [traces, setTraces] = useState<ReadonlyArray<TraceSummaryResponse> | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [detailId, setDetailId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [outcomeFilter, setOutcomeFilter] = useState<TraceOutcomeFilter>("all");

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    setLoadError(null);
    try {
      const result = await listTraces();
      setTraces(result);
    } catch (cause) {
      setLoadError(messageFor(cause));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const filteredTraces = useMemo(
    () => filterTraces(traces, query, outcomeFilter),
    [traces, query, outcomeFilter],
  );
  const hasLoadedTraces = traces !== null && traces.length > 0;

  return (
    <>
      <div className="toolbar">
        <p className="toolbar__lead">
          Inspect recent proxied requests: outcome, selected credential, and duration.
        </p>
        <button type="button" className="button button--ghost" onClick={() => void load()}>
          <RefreshIcon size={18} aria-hidden />
          <span>Refresh</span>
        </button>
      </div>

      <PageGuide
        id="traces-guide-title"
        title="Debug routed requests"
        description="Traces show which route was used, which credential served the request, and why a request failed. Metadata is redacted and survives account cleanup."
        steps={[
          {
            title: "Run traffic",
            description: "Every /v1 request creates a trace tied to a correlation id.",
            Icon: TracesIcon,
          },
          {
            title: "Filter fast",
            description: "Search by id, status, credential label, provider, or outcome.",
            Icon: RefreshIcon,
          },
          {
            title: "Open details",
            description: "Inspect inbound and outbound metadata without exposing secrets.",
            Icon: AnalyticsIcon,
          },
        ]}
      />

      {hasLoadedTraces && (
        <>
          <TraceSummaryStrip traces={traces} />
          <TraceFilters
            query={query}
            outcomeFilter={outcomeFilter}
            onQueryChange={setQuery}
            onOutcomeFilterChange={setOutcomeFilter}
          />
        </>
      )}

      <TracesBody
        loading={loading}
        loadError={loadError}
        traces={filteredTraces}
        sourceCount={traces?.length ?? 0}
        onRetry={() => void load()}
        onView={(trace) => setDetailId(trace.correlation_id)}
      />

      {detailId !== null && (
        <Modal title="Trace detail" onClose={() => setDetailId(null)}>
          <TraceDetail correlationId={detailId} />
        </Modal>
      )}
    </>
  );
}

/** Renders the loading / error / empty / populated states of the trace list. */
function TracesBody({
  loading,
  loadError,
  traces,
  sourceCount,
  onRetry,
  onView,
}: {
  readonly loading: boolean;
  readonly loadError: string | null;
  readonly traces: ReadonlyArray<TraceSummaryResponse> | null;
  readonly sourceCount: number;
  readonly onRetry: () => void;
  readonly onView: (trace: TraceSummaryResponse) => void;
}): JSX.Element {
  if (loading && traces === null) {
    return <TableSkeleton columns={6} label="Loading traces..." />;
  }

  if (loadError !== null && traces === null) {
    return (
      <div className="state state--error" role="alert">
        <AlertIcon size={22} aria-hidden />
        <p>{loadError}</p>
        <button type="button" className="button button--ghost" onClick={onRetry}>
          <RefreshIcon size={18} aria-hidden />
          <span>Retry</span>
        </button>
      </div>
    );
  }

  if (traces !== null && traces.length === 0) {
    if (sourceCount > 0) {
      return (
        <div className="state state--empty">
          <InboxIcon size={28} aria-hidden />
          <p>No traces match the current filters.</p>
          <p className="state__hint">Adjust search or outcome filters to widen the list.</p>
        </div>
      );
    }
    return (
      <div className="state state--empty">
        <InboxIcon size={28} aria-hidden />
        <p>No traces yet.</p>
        <p className="state__hint">Proxied requests will appear here as they are handled.</p>
      </div>
    );
  }

  const rows = traces ?? [];

  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th scope="col">Started</th>
            <th scope="col">Outcome</th>
            <th scope="col">Status</th>
            <th scope="col">Credential</th>
            <th scope="col">Elapsed</th>
            <th scope="col" className="table__actions-col">
              Actions
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((trace) => {
            const outcome = outcomeView(trace.outcome);
            return (
              <tr key={trace.correlation_id}>
                <td>
                  <span className="cell-primary">{formatTimestamp(trace.started_at)}</span>
                  <span className="cell-secondary">{trace.correlation_id}</span>
                </td>
                <td>
                  <span className={`badge badge--${outcome.tone}`}>{outcome.label}</span>
                </td>
                <td>{describeStatus(trace.status_code)}</td>
                <td>
                  <span className="cell-primary">
                    {describeCredential(trace.account_id, trace.credential)}
                  </span>
                  {trace.credential ? (
                    <span className="cell-secondary">
                      {trace.credential.kind.replaceAll("_", " ")} -{" "}
                      {trace.credential.status.replaceAll("_", " ")}
                    </span>
                  ) : null}
                </td>
                <td>{describeElapsed(trace.elapsed_seconds)}</td>
                <td>
                  <div className="row-actions">
                    <button
                      type="button"
                      className="button button--ghost"
                      onClick={() => onView(trace)}
                      aria-label={`View trace ${trace.correlation_id}`}
                    >
                      View
                    </button>
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function TraceSummaryStrip({
  traces,
}: {
  readonly traces: ReadonlyArray<TraceSummaryResponse>;
}): JSX.Element {
  const total = traces.length;
  const successes = traces.filter(isSuccessfulTrace).length;
  const errors = traces.filter(isErrorTrace).length;
  const inProgress = traces.filter(isInProgressTrace).length;
  const elapsed = traces
    .map((trace) => trace.elapsed_seconds)
    .filter((value): value is number => value != null && Number.isFinite(value));
  const averageElapsed =
    elapsed.length === 0
      ? null
      : elapsed.reduce((sum, value) => sum + value, 0) / elapsed.length;

  return (
    <dl className="trace-summary">
      <div>
        <dt>Requests</dt>
        <dd>{formatNumber(total)}</dd>
      </div>
      <div>
        <dt>Success</dt>
        <dd>{formatNumber(successes)}</dd>
      </div>
      <div>
        <dt>Needs attention</dt>
        <dd>{formatNumber(errors)}</dd>
      </div>
      <div>
        <dt>In progress</dt>
        <dd>{formatNumber(inProgress)}</dd>
      </div>
      <div>
        <dt>Avg elapsed</dt>
        <dd>{describeElapsed(averageElapsed)}</dd>
      </div>
    </dl>
  );
}

function TraceFilters({
  query,
  outcomeFilter,
  onQueryChange,
  onOutcomeFilterChange,
}: {
  readonly query: string;
  readonly outcomeFilter: TraceOutcomeFilter;
  readonly onQueryChange: (value: string) => void;
  readonly onOutcomeFilterChange: (value: TraceOutcomeFilter) => void;
}): JSX.Element {
  return (
    <div className="trace-controls">
      <div className="field trace-controls__search">
        <label htmlFor="trace-search">Search traces</label>
        <input
          id="trace-search"
          type="search"
          value={query}
          placeholder="Correlation id, status, outcome, or credential"
          onChange={(event) => onQueryChange(event.target.value)}
        />
      </div>
      <div className="field trace-controls__outcome">
        <label htmlFor="trace-outcome-filter">Outcome</label>
        <select
          id="trace-outcome-filter"
          value={outcomeFilter}
          onChange={(event) =>
            onOutcomeFilterChange(event.target.value as TraceOutcomeFilter)
          }
        >
          <option value="all">All outcomes</option>
          <option value="success">Success</option>
          <option value="error">Needs attention</option>
          <option value="in_progress">In progress</option>
        </select>
      </div>
    </div>
  );
}

function filterTraces(
  traces: ReadonlyArray<TraceSummaryResponse> | null,
  query: string,
  outcomeFilter: TraceOutcomeFilter,
): ReadonlyArray<TraceSummaryResponse> | null {
  if (traces === null) {
    return null;
  }
  const normalizedQuery = query.trim().toLowerCase();
  return traces.filter((trace) => {
    if (!matchesOutcomeFilter(trace, outcomeFilter)) {
      return false;
    }
    if (normalizedQuery === "") {
      return true;
    }
    const outcome = outcomeView(trace.outcome).label;
    const haystack = [
      trace.correlation_id,
      trace.account_id ?? "",
      trace.credential?.label ?? "",
      trace.credential?.provider ?? "",
      trace.credential?.kind ?? "",
      trace.credential?.status ?? "",
      trace.status_code == null ? "" : String(trace.status_code),
      outcome,
      trace.outcome ?? "",
      describeElapsed(trace.elapsed_seconds),
    ]
      .join(" ")
      .toLowerCase();
    return haystack.includes(normalizedQuery);
  });
}

function matchesOutcomeFilter(
  trace: TraceSummaryResponse,
  outcomeFilter: TraceOutcomeFilter,
): boolean {
  switch (outcomeFilter) {
    case "success":
      return isSuccessfulTrace(trace);
    case "error":
      return isErrorTrace(trace);
    case "in_progress":
      return isInProgressTrace(trace);
    case "all":
      return true;
  }
}

function isSuccessfulTrace(trace: TraceSummaryResponse): boolean {
  return trace.outcome === "success" && !isErrorStatus(trace.status_code);
}

function isErrorTrace(trace: TraceSummaryResponse): boolean {
  if (isErrorStatus(trace.status_code)) {
    return true;
  }
  return trace.outcome != null && trace.outcome !== "success";
}

function isInProgressTrace(trace: TraceSummaryResponse): boolean {
  return trace.outcome == null && trace.ended_at == null;
}

function isErrorStatus(status: number | null | undefined): boolean {
  return status != null && status >= 400;
}
