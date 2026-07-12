import { useState, type FormEvent } from "react";

import { AlertIcon } from "../../components/icons";
import type { AnalyticsRange } from "../../api/analytics";
import { buildRange } from "./format";

/**
 * A start/end time-range selector for the analytics views (Requirement 15.x: all
 * reports are computed over a selected time range). Collects two `datetime-local`
 * values, validates that the range is complete and strictly increasing, and emits
 * an ISO-8601 UTC {@link AnalyticsRange} on apply. Validation errors are shown
 * inline; the parent owns the report fetch.
 */
export function RangeSelector({
  startInput,
  endInput,
  onStartChange,
  onEndChange,
  onApply,
}: {
  readonly startInput: string;
  readonly endInput: string;
  readonly onStartChange: (value: string) => void;
  readonly onEndChange: (value: string) => void;
  readonly onApply: (range: AnalyticsRange) => void;
}): JSX.Element {
  const [error, setError] = useState<string | null>(null);

  function handleSubmit(event: FormEvent): void {
    event.preventDefault();
    const result = buildRange(startInput, endInput);
    if ("error" in result) {
      setError(result.error);
      return;
    }
    setError(null);
    onApply(result.range);
  }

  return (
    <form className="range-selector" onSubmit={handleSubmit}>
      <div className="range-selector__fields">
        <div className="field">
          <label htmlFor="analytics-start">Start</label>
          <input
            id="analytics-start"
            type="datetime-local"
            value={startInput}
            onChange={(event) => onStartChange(event.target.value)}
            required
          />
        </div>
        <div className="field">
          <label htmlFor="analytics-end">End</label>
          <input
            id="analytics-end"
            type="datetime-local"
            value={endInput}
            onChange={(event) => onEndChange(event.target.value)}
            required
          />
        </div>
        <button type="submit" className="button button--primary">
          Apply range
        </button>
      </div>
      {error !== null && (
        <p className="alert alert--error" role="alert">
          <AlertIcon size={18} aria-hidden />
          <span>{error}</span>
        </p>
      )}
    </form>
  );
}
