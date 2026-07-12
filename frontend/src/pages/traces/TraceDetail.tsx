import { useCallback, useEffect, useState } from "react";

import { getTrace } from "../../api/traces";
import { ApiError } from "../../api/errors";
import { AlertIcon, RefreshIcon } from "../../components/icons";
import { Spinner } from "../../components/Spinner";
import type { TraceDetailResponse } from "../../api/types";
import { formatTimestamp } from "../accounts/format";
import {
  describeCredential,
  describeElapsed,
  describeStatus,
  outcomeView,
} from "./format";

/** Translate any thrown error into a secret-free, displayable message. */
function messageFor(cause: unknown): string {
  if (cause instanceof ApiError) {
    return cause.message;
  }
  return "Unexpected error. Please try again.";
}

/** Render an arbitrary metadata value compactly; objects/arrays as pretty JSON. */
function renderMetaValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "\u2014";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** A definition list of the entries in a metadata object, or an empty note. */
function MetaList({ meta }: { readonly meta: Record<string, unknown> }): JSX.Element {
  const entries = Object.entries(meta);
  if (entries.length === 0) {
    return <p className="form__hint">No metadata recorded.</p>;
  }
  return (
    <dl className="detail-list">
      {entries.map(([key, value]) => (
        <div className="detail-list__row" key={key}>
          <dt>{key}</dt>
          <dd>
            <pre className="trace-detail__meta-value">{renderMetaValue(value)}</pre>
          </dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * Loads and renders a single Trace_Log entry's full detail (Requirement 14.3):
 * the inbound request metadata, the selected Upstream_Credential, the outcome and
 * final status, and the elapsed duration. Rendered inside a {@link Modal} by the
 * TracesPage. Has its own explicit loading, error, and populated states; trace
 * metadata never carries secrets (Requirement 16.4).
 */
export function TraceDetail({
  correlationId,
}: {
  readonly correlationId: string;
}): JSX.Element {
  const [trace, setTrace] = useState<TraceDetailResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const result = await getTrace(correlationId);
      setTrace(result);
    } catch (cause) {
      setError(messageFor(cause));
    } finally {
      setLoading(false);
    }
  }, [correlationId]);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading && trace === null) {
    return (
      <div className="state state--loading" role="status">
        <Spinner label="Loading trace" size={22} />
        <span>Loading trace...</span>
      </div>
    );
  }

  if (error !== null && trace === null) {
    return (
      <div className="state state--error" role="alert">
        <AlertIcon size={22} aria-hidden />
        <p>{error}</p>
        <button type="button" className="button button--ghost" onClick={() => void load()}>
          <RefreshIcon size={18} aria-hidden />
          <span>Retry</span>
        </button>
      </div>
    );
  }

  if (trace === null) {
    return <p className="form__hint">No trace to display.</p>;
  }

  const outcome = outcomeView(trace.outcome);

  return (
    <div className="trace-detail">
      <dl className="detail-list">
        <div className="detail-list__row">
          <dt>Correlation ID</dt>
          <dd>
            <code className="trace-detail__id">{trace.correlation_id}</code>
          </dd>
        </div>
        <div className="detail-list__row">
          <dt>Started</dt>
          <dd>{formatTimestamp(trace.started_at)}</dd>
        </div>
        <div className="detail-list__row">
          <dt>Ended</dt>
          <dd>{trace.ended_at ? formatTimestamp(trace.ended_at) : "\u2014"}</dd>
        </div>
        <div className="detail-list__row">
          <dt>Outcome</dt>
          <dd>
            <span className={`badge badge--${outcome.tone}`}>{outcome.label}</span>
          </dd>
        </div>
        <div className="detail-list__row">
          <dt>Status</dt>
          <dd>{describeStatus(trace.status_code)}</dd>
        </div>
        <div className="detail-list__row">
          <dt>Selected credential</dt>
          <dd>
            <span className="cell-primary">
              {describeCredential(trace.account_id, trace.credential)}
            </span>
            {trace.credential ? (
              <span className="cell-secondary">
                {trace.credential.kind.replaceAll("_", " ")} -{" "}
                {trace.credential.status.replaceAll("_", " ")}
              </span>
            ) : null}
          </dd>
        </div>
        <div className="detail-list__row">
          <dt>Elapsed</dt>
          <dd>{describeElapsed(trace.elapsed_seconds)}</dd>
        </div>
      </dl>

      <section className="trace-detail__section">
        <h3 className="trace-detail__heading">Inbound metadata</h3>
        <MetaList meta={trace.inbound_meta} />
      </section>

      <section className="trace-detail__section">
        <h3 className="trace-detail__heading">Outbound metadata</h3>
        {trace.outbound_meta ? (
          <MetaList meta={trace.outbound_meta} />
        ) : (
          <p className="form__hint">No outbound metadata (request did not finalise).</p>
        )}
      </section>
    </div>
  );
}
