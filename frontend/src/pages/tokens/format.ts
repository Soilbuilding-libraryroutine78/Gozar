import type { TokenResponse } from "../../api/types";
import { formatNumber } from "../accounts/format";
import type { StatusTone } from "../accounts/format";

/**
 * Presentation helpers for client-token rows: status labels and a usage
 * description. The limit and number formatting helpers are shared with the
 * accounts views (`../accounts/format`) so a token's configured limit reads the
 * same way as a credential's. Pure functions, kept out of the page component.
 */

interface TokenStatusView {
  readonly label: string;
  readonly tone: StatusTone;
}

/**
 * Map a raw token status to a readable label and a badge tone. Token lifecycle
 * statuses (`gozar/tokens/models.py` TokenStatus) are `active`, `disabled`, and
 * the terminal `revoked`.
 */
export function tokenStatusView(status: string): TokenStatusView {
  switch (status) {
    case "active":
      return { label: "Active", tone: "ok" };
    case "disabled":
      return { label: "Disabled", tone: "muted" };
    case "revoked":
      return { label: "Revoked", tone: "warn" };
    default:
      return { label: status, tone: "muted" };
  }
}

/** True once a token is permanently revoked; lifecycle actions no longer apply. */
export function isRevoked(status: string): boolean {
  return status === "revoked";
}

/** Render a token's recorded usage, against its configured limit when present. */
export function describeUsage(token: TokenResponse): string {
  const used = formatNumber(token.usage);
  const limit = token.limit;
  if (limit && limit.metric !== "percentage" && limit.limit_value > 0) {
    const pct = Math.round((token.usage / limit.limit_value) * 100);
    return `${used} / ${formatNumber(limit.limit_value)} (${pct}%)`;
  }
  if (limit && limit.metric === "percentage" && limit.capacity != null && limit.capacity > 0) {
    const pct = Math.round((token.usage / limit.capacity) * 100);
    return `${used} (${pct}%)`;
  }
  return used;
}
