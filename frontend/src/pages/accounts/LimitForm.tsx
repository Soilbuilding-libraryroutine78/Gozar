import { useState, type FormEvent } from "react";

import { AlertIcon } from "../../components/icons";
import { Spinner } from "../../components/Spinner";
import type { LimitMetric, LimitWindow, UsageLimitSpec } from "../../api/types";

const METRICS: ReadonlyArray<{ value: LimitMetric; label: string }> = [
  { value: "request_count", label: "Request count" },
  { value: "token_count", label: "Token count" },
  { value: "cost_estimate", label: "Cost estimate" },
  { value: "percentage", label: "Percentage of capacity" },
];

const WINDOWS: ReadonlyArray<{ value: LimitWindow; label: string }> = [
  { value: "none", label: "Cumulative (never resets)" },
  { value: "daily", label: "Daily" },
  { value: "monthly", label: "Monthly" },
  { value: "rolling_24h", label: "Rolling 24h" },
];

/**
 * Configure (or replace) an account's usage limit. Validates client-side for UX,
 * builds a {@link UsageLimitSpec}, and hands it to {@link onSubmit}; the parent owns
 * the async call and surfaces the loading and error states passed back in.
 */
export function LimitForm({
  initial,
  submitting,
  error,
  onSubmit,
}: {
  readonly initial: UsageLimitSpec | null | undefined;
  readonly submitting: boolean;
  readonly error: string | null;
  readonly onSubmit: (spec: UsageLimitSpec) => void;
}): JSX.Element {
  const [metric, setMetric] = useState<LimitMetric>(initial?.metric ?? "request_count");
  const [limitValue, setLimitValue] = useState<string>(
    initial ? String(initial.limit_value) : "",
  );
  const [capacity, setCapacity] = useState<string>(
    initial?.capacity != null ? String(initial.capacity) : "",
  );
  const [window, setWindow] = useState<LimitWindow>(initial?.window ?? "none");
  const [localError, setLocalError] = useState<string | null>(null);

  const isPercentage = metric === "percentage";

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    setLocalError(null);

    const value = Number(limitValue);
    if (limitValue.trim() === "" || Number.isNaN(value) || value < 0) {
      setLocalError("Enter a non-negative limit value.");
      return;
    }

    let capacityValue: number | null = null;
    if (isPercentage) {
      const parsed = Number(capacity);
      if (capacity.trim() === "" || Number.isNaN(parsed) || parsed <= 0) {
        setLocalError("Percentage limits require a capacity greater than zero.");
        return;
      }
      capacityValue = parsed;
    }

    onSubmit({
      metric,
      limit_value: value,
      capacity: capacityValue,
      window,
    });
  }

  const message = localError ?? error;

  return (
    <form className="form" onSubmit={handleSubmit} noValidate>
      <div className="field">
        <label htmlFor="limit-metric">Metric</label>
        <select
          id="limit-metric"
          value={metric}
          onChange={(e) => setMetric(e.target.value as LimitMetric)}
          disabled={submitting}
        >
          {METRICS.map((m) => (
            <option key={m.value} value={m.value}>
              {m.label}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label htmlFor="limit-value">{isPercentage ? "Threshold (%)" : "Limit value"}</label>
        <input
          id="limit-value"
          type="number"
          min={0}
          step="any"
          inputMode="decimal"
          value={limitValue}
          onChange={(e) => setLimitValue(e.target.value)}
          disabled={submitting}
          required
        />
      </div>

      {isPercentage && (
        <div className="field">
          <label htmlFor="limit-capacity">Capacity</label>
          <input
            id="limit-capacity"
            type="number"
            min={0}
            step="any"
            inputMode="decimal"
            value={capacity}
            onChange={(e) => setCapacity(e.target.value)}
            disabled={submitting}
            required
          />
        </div>
      )}

      <div className="field">
        <label htmlFor="limit-window">Measurement window</label>
        <select
          id="limit-window"
          value={window}
          onChange={(e) => setWindow(e.target.value as LimitWindow)}
          disabled={submitting}
        >
          {WINDOWS.map((w) => (
            <option key={w.value} value={w.value}>
              {w.label}
            </option>
          ))}
        </select>
      </div>

      {message !== null && (
        <p className="alert alert--error" role="alert">
          <AlertIcon size={18} aria-hidden />
          <span>{message}</span>
        </p>
      )}

      <div className="form__actions">
        <button type="submit" className="button button--primary" disabled={submitting}>
          {submitting ? (
            <>
              <Spinner label="Saving" size={18} />
              <span>Saving...</span>
            </>
          ) : (
            <span>Save limit</span>
          )}
        </button>
      </div>
    </form>
  );
}
