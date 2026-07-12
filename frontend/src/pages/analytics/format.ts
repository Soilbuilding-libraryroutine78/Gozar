import type { AnalyticsRange } from "../../api/analytics";
import type { LimitConsumptionResponse } from "../../api/types";
import { describeLimit, formatNumber } from "../accounts/format";

/**
 * Presentation and range helpers for the analytics views (Requirement 15.x).
 *
 * The Analytics_Service reports over a half-open `[start, end)` UTC range. The UI
 * collects the range with `datetime-local` inputs (local wall-clock), so these
 * helpers convert between a `Date`, the input's local string form, and the ISO-8601
 * UTC string the backend expects. Pure functions, kept out of the page component.
 */

/** Two-digit zero-pad for the local datetime-local input format. */
function pad(value: number): string {
  return String(value).padStart(2, "0");
}

/**
 * Render a `Date` as the value a `<input type="datetime-local">` expects:
 * `YYYY-MM-DDTHH:mm` in the browser's local time.
 */
export function toLocalInputValue(date: Date): string {
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

/** Parse a datetime-local input value into a `Date`, or null when unparseable. */
export function parseLocalInputValue(value: string): Date | null {
  if (value.length === 0) {
    return null;
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** The default range shown on first load: the last 7 days ending now. */
export function defaultRangeInputs(): { start: string; end: string } {
  const end = new Date();
  const start = new Date(end.getTime() - 7 * 24 * 60 * 60 * 1000);
  return { start: toLocalInputValue(start), end: toLocalInputValue(end) };
}

/**
 * Validate a pair of datetime-local input values and convert them to an
 * ISO-8601 UTC {@link AnalyticsRange}. Returns a typed error message instead when
 * a bound is missing/unparseable or the range is not strictly increasing.
 */
export function buildRange(
  startInput: string,
  endInput: string,
): { range: AnalyticsRange } | { error: string } {
  const start = parseLocalInputValue(startInput);
  const end = parseLocalInputValue(endInput);
  if (start === null || end === null) {
    return { error: "Enter both a start and an end time." };
  }
  if (start.getTime() >= end.getTime()) {
    return { error: "The start time must be before the end time." };
  }
  return { range: { start: start.toISOString(), end: end.toISOString() } };
}

/** Format a fraction (e.g. an error rate of 0.25) as a percentage string. */
export function formatRate(rate: number): string {
  if (!Number.isFinite(rate)) {
    return "\u2014";
  }
  return `${(rate * 100).toFixed(1)}%`;
}

/** A readable label for an ISO-8601 range, falling back to the raw strings. */
export function describeRange(range: AnalyticsRange): string {
  const start = new Date(range.start);
  const end = new Date(range.end);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) {
    return `${range.start} to ${range.end}`;
  }
  return `${start.toLocaleString()} to ${end.toLocaleString()}`;
}

/** Describe consumption against a configured limit, or note when none is set. */
export function describeConsumptionReport(
  consumption: LimitConsumptionResponse,
): string {
  if (!consumption.spec) {
    return "No limit configured";
  }
  const limit = describeLimit(consumption.spec);
  if (consumption.consumed == null) {
    return limit;
  }
  const consumed = formatNumber(consumption.consumed);
  const pct =
    consumption.percent_of_limit != null
      ? ` (${Math.round(consumption.percent_of_limit)}%)`
      : "";
  const reached = consumption.reached ? " - limit reached" : "";
  return `${consumed}${pct} of ${limit}${reached}`;
}
