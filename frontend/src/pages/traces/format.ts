import type { StatusTone } from "../accounts/format";
import type { TraceCredentialResponse } from "../../api/types";
import { shortId } from "../chains/format";

/**
 * Presentation helpers for Trace_Log rows (Requirement 14.3): outcome labels and
 * tones, status-code rendering, elapsed-duration formatting, and a readable
 * selected-credential label. Pure functions, kept out of the page component.
 *
 * Outcome values mirror the backend `TRACE_OUTCOMES` tuple in
 * `gozar/usage/service.py` (`success`, `client_error`, `all_fallbacks_failed`,
 * `no_account`); an unfinished trace has no outcome yet.
 */

interface OutcomeView {
  readonly label: string;
  readonly tone: StatusTone;
}

/** Map a raw trace outcome (or null for an in-flight trace) to a label and tone. */
export function outcomeView(outcome: string | null | undefined): OutcomeView {
  switch (outcome) {
    case "success":
      return { label: "Success", tone: "ok" };
    case "client_error":
      return { label: "Client error", tone: "warn" };
    case "all_fallbacks_failed":
      return { label: "All fallbacks failed", tone: "warn" };
    case "no_account":
      return { label: "No account", tone: "muted" };
    case null:
    case undefined:
      return { label: "In progress", tone: "muted" };
    default:
      return { label: outcome, tone: "muted" };
  }
}

/** Render an HTTP status code, or an en dash when the trace has not finalised. */
export function describeStatus(status: number | null | undefined): string {
  return status == null ? "\u2014" : String(status);
}

/** Render the selected Upstream_Credential as a readable label, or "None" when absent. */
export function describeCredential(
  accountId: string | null | undefined,
  credential?: TraceCredentialResponse | null,
): string {
  if (credential !== null && credential !== undefined) {
    return `${credential.label} (${credential.provider})`;
  }
  return accountId == null ? "None" : shortId(accountId);
}

/** Format an elapsed duration in seconds as a compact human-readable string. */
export function describeElapsed(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) {
    return "\u2014";
  }
  if (seconds < 1) {
    return `${Math.round(seconds * 1000)} ms`;
  }
  return `${seconds.toFixed(2)} s`;
}
