import type { AccountResponse } from "../../api/types";
import type { StatusTone } from "../accounts/format";
import { providerLabel } from "../accounts/providers";

/**
 * Presentation helpers for fallback-chain entries.
 *
 * A chain entry references an Upstream_Credential by `account_id`. The Web_Console
 * must indicate when a referenced entry is unavailable (Requirement 11.4): a deleted
 * credential is simply absent from the accounts list, and a credential that is
 * disabled or requires reauthorization is present but the Flow_Controller will skip
 * it at routing time (Requirements 5.1-5.2, 3.4). These pure helpers cross-reference
 * an entry against the current accounts list so the editor and list views can flag
 * such entries consistently.
 */

/** Whether a referenced credential can currently serve traffic, and why not. */
export interface EntryAvailability {
  /** True only when the credential exists and is active. */
  readonly available: boolean;
  /** A short reason shown when unavailable (e.g. "Deleted", "Disabled"). */
  readonly reason: string | null;
  /** Badge tone for the availability marker. */
  readonly tone: StatusTone;
  /** Operator-facing label for the entry (account label, or a short id when deleted). */
  readonly label: string;
  /** Provider display name when the credential is known, else null. */
  readonly provider: string | null;
}

/** Build a lookup of accounts keyed by their id for fast cross-referencing. */
export function indexAccounts(
  accounts: ReadonlyArray<AccountResponse>,
): ReadonlyMap<string, AccountResponse> {
  const map = new Map<string, AccountResponse>();
  for (const account of accounts) {
    map.set(account.account_id, account);
  }
  return map;
}

/** A short, readable form of a UUID for entries whose account no longer exists. */
export function shortId(accountId: string): string {
  return accountId.length > 8 ? `${accountId.slice(0, 8)}...` : accountId;
}

/**
 * Resolve the availability of a chain entry against the current accounts.
 *
 * An account absent from the list has been deleted (Requirement 11.4); a present
 * account that is disabled or requires reauthorization is flagged unavailable too
 * since the Flow_Controller will skip it (Requirement 11.1-11.3).
 */
export function entryAvailability(
  accountId: string,
  accountsById: ReadonlyMap<string, AccountResponse>,
): EntryAvailability {
  const account = accountsById.get(accountId);
  if (account === undefined) {
    return {
      available: false,
      reason: "Deleted",
      tone: "warn",
      label: shortId(accountId),
      provider: null,
    };
  }

  const provider = providerLabel(account.provider);
  switch (account.status) {
    case "disabled":
      return { available: false, reason: "Disabled", tone: "muted", label: account.label, provider };
    case "requires_reauth":
      return { available: false, reason: "Needs reauth", tone: "warn", label: account.label, provider };
    default:
      return { available: true, reason: null, tone: "ok", label: account.label, provider };
  }
}
