import { useCallback, useEffect, useState } from "react";

import {
  deleteAccount,
  listAccounts,
  setAccountEnabled,
  setAccountLimit,
} from "../api/accounts";
import { ApiError } from "../api/errors";
import {
  AlertIcon,
  ChainIcon,
  GaugeIcon,
  InboxIcon,
  KeyIcon,
  PlusIcon,
  PowerIcon,
  RefreshIcon,
  TrashIcon,
} from "../components/icons";
import { PageGuide } from "../components/PageGuide";
import { Spinner } from "../components/Spinner";
import { TableSkeleton } from "../components/Skeleton";
import type { AccountResponse, UsageLimitSpec } from "../api/types";
import { ConnectForm } from "./accounts/ConnectForm";
import {
  describeConsumption,
  describeLimit,
  formatTimestamp,
  statusView,
} from "./accounts/format";
import { LimitForm } from "./accounts/LimitForm";
import { Modal } from "./accounts/Modal";
import { providerLabel } from "./accounts/providers";

/** Translate any thrown error into a secret-free, displayable message. */
function messageFor(cause: unknown): string {
  if (cause instanceof ApiError) {
    return cause.message;
  }
  return "Unexpected error. Please try again.";
}

/**
 * Account management view (Requirements 5.4, 17.1): list connected upstream
 * credentials with provider, status, configured limit, and consumption, and connect,
 * configure limits for, enable/disable, and delete them.
 *
 * Every async surface renders explicit loading, empty, and error states. Icons are
 * outline SVGs; there are no emoji.
 */
export function AccountsPage(): JSX.Element {
  const [accounts, setAccounts] = useState<ReadonlyArray<AccountResponse> | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [connectOpen, setConnectOpen] = useState(false);

  const [limitTarget, setLimitTarget] = useState<AccountResponse | null>(null);
  const [limitSubmitting, setLimitSubmitting] = useState(false);
  const [limitError, setLimitError] = useState<string | null>(null);

  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    setLoadError(null);
    try {
      const result = await listAccounts();
      setAccounts(result);
    } catch (cause) {
      setLoadError(messageFor(cause));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleConnected = useCallback((): void => {
    setConnectOpen(false);
    void load();
  }, [load]);

  async function handleToggleEnabled(account: AccountResponse): Promise<void> {
    setActionError(null);
    setBusyId(account.account_id);
    const nextEnabled = account.status === "disabled";
    try {
      await setAccountEnabled(account.account_id, nextEnabled);
      await load();
    } catch (cause) {
      setActionError(messageFor(cause));
    } finally {
      setBusyId(null);
    }
  }

  async function handleDelete(account: AccountResponse): Promise<void> {
    const confirmed = window.confirm(
      `Delete account "${account.label}"? Credential secrets are removed; usage history is retained.`,
    );
    if (!confirmed) {
      return;
    }
    setActionError(null);
    setBusyId(account.account_id);
    try {
      await deleteAccount(account.account_id);
      await load();
    } catch (cause) {
      setActionError(messageFor(cause));
    } finally {
      setBusyId(null);
    }
  }

  async function handleSubmitLimit(spec: UsageLimitSpec): Promise<void> {
    if (limitTarget === null) {
      return;
    }
    setLimitError(null);
    setLimitSubmitting(true);
    try {
      await setAccountLimit(limitTarget.account_id, spec);
      setLimitTarget(null);
      await load();
    } catch (cause) {
      setLimitError(messageFor(cause));
    } finally {
      setLimitSubmitting(false);
    }
  }

  return (
    <>
      <div className="toolbar">
        <p className="toolbar__lead">
          Connect and manage the upstream credentials Gozar routes through.
        </p>
        <button
          type="button"
          className="button button--primary"
          onClick={() => setConnectOpen(true)}
        >
          <PlusIcon size={18} aria-hidden />
          <span>Connect account</span>
        </button>
      </div>

      <PageGuide
        id="accounts-guide-title"
        title="Connect providers"
        description="Add each upstream account once. Apps keep using their Gozar API key while you reconnect, disable, or limit provider credentials here."
        steps={[
          {
            title: "Add a provider",
            description: "Use an API key for OpenAI/OpenRouter or sign in to a supported subscription provider.",
            Icon: KeyIcon,
          },
          {
            title: "Manage health",
            description: "Disabled, deleted, over-limit, or reconnect-needed accounts are skipped automatically.",
            Icon: GaugeIcon,
          },
          {
            title: "Build routes",
            description: "Chains use these accounts as ordered fallback steps for each API key.",
            Icon: ChainIcon,
          },
        ]}
      />

      {actionError !== null && (
        <p className="alert alert--error page-alert" role="alert">
          <AlertIcon size={18} aria-hidden />
          <span>{actionError}</span>
        </p>
      )}

      <AccountsBody
        loading={loading}
        loadError={loadError}
        accounts={accounts}
        busyId={busyId}
        onRetry={() => void load()}
        onConfigureLimit={(account) => {
          setLimitError(null);
          setLimitTarget(account);
        }}
        onToggleEnabled={(account) => void handleToggleEnabled(account)}
        onDelete={(account) => void handleDelete(account)}
      />

      {connectOpen && (
        <Modal title="Connect account" onClose={() => setConnectOpen(false)}>
          <ConnectForm onConnected={handleConnected} />
        </Modal>
      )}

      {limitTarget !== null && (
        <Modal
          title={`Configure limit - ${limitTarget.label}`}
          onClose={() => setLimitTarget(null)}
        >
          <LimitForm
            initial={limitTarget.limit}
            submitting={limitSubmitting}
            error={limitError}
            onSubmit={(spec) => void handleSubmitLimit(spec)}
          />
        </Modal>
      )}
    </>
  );
}

/** Renders the loading / error / empty / populated states of the account list. */
function AccountsBody({
  loading,
  loadError,
  accounts,
  busyId,
  onRetry,
  onConfigureLimit,
  onToggleEnabled,
  onDelete,
}: {
  readonly loading: boolean;
  readonly loadError: string | null;
  readonly accounts: ReadonlyArray<AccountResponse> | null;
  readonly busyId: string | null;
  readonly onRetry: () => void;
  readonly onConfigureLimit: (account: AccountResponse) => void;
  readonly onToggleEnabled: (account: AccountResponse) => void;
  readonly onDelete: (account: AccountResponse) => void;
}): JSX.Element {
  if (loading && accounts === null) {
    return <TableSkeleton columns={7} label="Loading accounts..." />;
  }

  if (loadError !== null && accounts === null) {
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

  if (accounts !== null && accounts.length === 0) {
    return (
      <div className="state state--empty">
        <InboxIcon size={28} aria-hidden />
        <p>No accounts connected yet.</p>
        <p className="state__hint">Connect a subscription or API-key account to begin routing.</p>
      </div>
    );
  }

  const rows = accounts ?? [];

  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th scope="col">Account</th>
            <th scope="col">Provider</th>
            <th scope="col">Status</th>
            <th scope="col">Limit</th>
            <th scope="col">Consumption</th>
            <th scope="col">Connected</th>
            <th scope="col" className="table__actions-col">
              Actions
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((account) => {
            const status = statusView(account.status);
            const busy = busyId === account.account_id;
            const isDisabled = account.status === "disabled";
            return (
              <tr key={account.account_id}>
                <td>
                  <span className="cell-primary">{account.label}</span>
                  <span className="cell-secondary">{account.kind}</span>
                </td>
                <td>{providerLabel(account.provider)}</td>
                <td>
                  <span className={`badge badge--${status.tone}`}>{status.label}</span>
                </td>
                <td>{describeLimit(account.limit)}</td>
                <td>{describeConsumption(account)}</td>
                <td>{formatTimestamp(account.connected_at)}</td>
                <td>
                  <div className="row-actions">
                    {busy ? (
                      <Spinner label="Working" size={18} />
                    ) : (
                      <>
                        <button
                          type="button"
                          className="icon-button"
                          onClick={() => onConfigureLimit(account)}
                          aria-label={`Configure limit for ${account.label}`}
                          title="Configure limit"
                        >
                          <GaugeIcon size={18} />
                        </button>
                        <button
                          type="button"
                          className="icon-button"
                          onClick={() => onToggleEnabled(account)}
                          aria-label={`${isDisabled ? "Enable" : "Disable"} ${account.label}`}
                          title={isDisabled ? "Enable" : "Disable"}
                        >
                          <PowerIcon size={18} />
                        </button>
                        <button
                          type="button"
                          className="icon-button icon-button--danger"
                          onClick={() => onDelete(account)}
                          aria-label={`Delete ${account.label}`}
                          title="Delete"
                        >
                          <TrashIcon size={18} />
                        </button>
                      </>
                    )}
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
