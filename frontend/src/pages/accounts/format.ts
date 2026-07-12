import type {
  AccountResponse,
  LimitMetric,
  LimitWindow,
  UsageLimitSpec,
} from "../../api/types";

/**
 * Presentation helpers for account rows: status labels, limit descriptions, and
 * consumption rendering. Pure functions, kept separate so the page component stays
 * focused on state and effects.
 */

/** A status badge variant for styling. */
export type StatusTone = "ok" | "muted" | "warn";

interface StatusView {
  readonly label: string;
  readonly tone: StatusTone;
}

/** Map a raw account status to a readable label and a badge tone. */
export function statusView(status: string): StatusView {
  switch (status) {
    case "active":
      return { label: "Active", tone: "ok" };
    case "disabled":
      return { label: "Disabled", tone: "muted" };
    case "requires_reauth":
      return { label: "Needs reauth", tone: "warn" };
    default:
      return { label: status, tone: "muted" };
  }
}

const METRIC_LABELS: Record<LimitMetric, string> = {
  request_count: "requests",
  token_count: "tokens",
  cost_estimate: "cost",
  percentage: "%",
};

const WINDOW_LABELS: Record<LimitWindow, string> = {
  none: "cumulative",
  daily: "per day",
  monthly: "per month",
  rolling_24h: "rolling 24h",
};

/** Human-readable metric name. */
export function metricLabel(metric: LimitMetric): string {
  return METRIC_LABELS[metric];
}

/** Human-readable window name. */
export function windowLabel(window: LimitWindow): string {
  return WINDOW_LABELS[window];
}

/** A compact one-line description of a configured limit, or "No limit" when absent. */
export function describeLimit(limit: UsageLimitSpec | null | undefined): string {
  if (!limit) {
    return "No limit";
  }
  const value = formatNumber(limit.limit_value);
  if (limit.metric === "percentage") {
    const cap = limit.capacity != null ? ` of ${formatNumber(limit.capacity)}` : "";
    return `${value}%${cap} (${windowLabel(limit.window)})`;
  }
  return `${value} ${metricLabel(limit.metric)} (${windowLabel(limit.window)})`;
}

/** Render the recorded consumption for an account row. */
export function describeConsumption(account: AccountResponse): string {
  const consumed = formatNumber(account.consumption);
  const limit = account.limit;
  if (limit && limit.metric !== "percentage" && limit.limit_value > 0) {
    const pct = Math.round((account.consumption / limit.limit_value) * 100);
    return `${consumed} / ${formatNumber(limit.limit_value)} (${pct}%)`;
  }
  if (limit && limit.metric === "percentage" && limit.capacity != null && limit.capacity > 0) {
    const pct = Math.round((account.consumption / limit.capacity) * 100);
    return `${consumed} (${pct}%)`;
  }
  return consumed;
}

/** Format a number with thousands separators, trimming trailing zeros. */
export function formatNumber(value: number): string {
  if (!Number.isFinite(value)) {
    return String(value);
  }
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 4 }).format(value);
}

/** Format an ISO-8601 timestamp for display, falling back to the raw string. */
export function formatTimestamp(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return date.toLocaleString();
}
