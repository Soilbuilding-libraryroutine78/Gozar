import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { listAccounts } from "../api/accounts";
import { createChain, deleteChain, editChain, listChains } from "../api/chains";
import { getModelCatalog } from "../api/models";
import { ApiError } from "../api/errors";
import {
  AccountsIcon,
  AlertIcon,
  ChainIcon,
  EditIcon,
  InboxIcon,
  PlusIcon,
  RefreshIcon,
  TokenIcon,
  TrashIcon,
} from "../components/icons";
import { PageGuide } from "../components/PageGuide";
import { Spinner } from "../components/Spinner";
import { CardSkeleton } from "../components/Skeleton";
import type {
  AccountResponse,
  ChainResponse,
  ModelCatalogChainResponse,
  ModelCatalogResponse,
} from "../api/types";
import { Modal } from "./accounts/Modal";
import { ChainEditor, type ChainDraft } from "./chains/ChainEditor";
import { entryAvailability, indexAccounts } from "./chains/format";

/** Translate any thrown error into a secret-free, displayable message. */
function messageFor(cause: unknown): string {
  if (cause instanceof ApiError) {
    return cause.message;
  }
  return "Unexpected error. Please try again.";
}

/**
 * Visual fallback-chain editor view (Requirements 10.1, 11.4, 17.3): list the
 * configured Fallback_Chains with their ordered entries, and create, edit, and
 * delete them. Each chain's entries are cross-referenced against the connected
 * accounts so entries referencing a deleted, disabled, or reauth-required credential
 * are visibly marked unavailable (Requirement 11.4).
 *
 * Chains and accounts load together; the accounts list both populates the editor's
 * picker and resolves entry availability. Every async surface renders explicit
 * loading, empty, and error states. Icons are outline SVGs; there are no emoji.
 */
export function ChainsPage(): JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const [chains, setChains] = useState<ReadonlyArray<ChainResponse> | null>(null);
  const [accounts, setAccounts] = useState<ReadonlyArray<AccountResponse>>([]);
  const [catalog, setCatalog] = useState<ModelCatalogResponse | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<ChainResponse | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const accountsById = useMemo(() => indexAccounts(accounts), [accounts]);
  const modelsByAccount = useMemo(() => {
    const result = new Map<string, ReadonlyArray<string>>();
    for (const account of catalog?.accounts ?? []) {
      result.set(
        account.account_id,
        account.models.map((model) => model.id),
      );
    }
    return result;
  }, [catalog]);
  const chainHealthById = useMemo(
    () => new Map((catalog?.chains ?? []).map((chain) => [chain.chain_id, chain])),
    [catalog],
  );

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    setLoadError(null);
    setCatalogError(null);
    try {
      const [chainResult, accountResult] = await Promise.all([
        listChains(),
        listAccounts(),
      ]);
      setChains(chainResult);
      setAccounts([...accountResult]);
      try {
        setCatalog(await getModelCatalog());
      } catch (cause) {
        setCatalogError(messageFor(cause));
      }
    } catch (cause) {
      setLoadError(messageFor(cause));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (catalog === null) {
      return undefined;
    }
    const delay = Math.max(30, catalog.cache_ttl_seconds) * 1000;
    const timer = window.setTimeout(() => {
      getModelCatalog()
        .then((nextCatalog) => {
          setCatalog(nextCatalog);
          setCatalogError(null);
        })
        .catch((cause) => setCatalogError(messageFor(cause)));
    }, delay);
    return () => window.clearTimeout(timer);
  }, [catalog]);

  useEffect(() => {
    const requestedId = searchParams.get("edit");
    if (!requestedId || chains === null || editorOpen) {
      return;
    }
    const requested = chains.find((chain) => chain.chain_id === requestedId);
    if (requested) {
      setSaveError(null);
      setEditing(requested);
      setEditorOpen(true);
    }
    setSearchParams({}, { replace: true });
  }, [chains, editorOpen, searchParams, setSearchParams]);

  function openCreate(): void {
    setSaveError(null);
    setEditing(null);
    setEditorOpen(true);
  }

  function openEdit(chain: ChainResponse): void {
    setSaveError(null);
    setEditing(chain);
    setEditorOpen(true);
  }

  function closeEditor(): void {
    setEditorOpen(false);
    setEditing(null);
  }

  async function handleSubmit(draft: ChainDraft): Promise<void> {
    setSaveError(null);
    setSaving(true);
    try {
      if (editing) {
        await editChain(editing.chain_id, {
          name: draft.name,
          entries: draft.entries,
          model_selector: draft.model_selector,
        });
      } else {
        await createChain({
          name: draft.name,
          entries: draft.entries,
          model_selector: draft.model_selector,
        });
      }
      closeEditor();
      await load();
    } catch (cause) {
      setSaveError(messageFor(cause));
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(chain: ChainResponse): Promise<void> {
    const confirmed = window.confirm(
      `Delete chain "${chain.name}"? This removes the chain and its routing order.`,
    );
    if (!confirmed) {
      return;
    }
    setActionError(null);
    setBusyId(chain.chain_id);
    try {
      await deleteChain(chain.chain_id);
      await load();
    } catch (cause) {
      setActionError(messageFor(cause));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <>
      <div className="toolbar">
        <p className="toolbar__lead">
          Map each provider account to its own model, then order the fallback path.
        </p>
        <button type="button" className="button button--primary" onClick={openCreate}>
          <PlusIcon size={18} aria-hidden />
          <span>Create chain</span>
        </button>
      </div>

      <PageGuide
        id="chains-guide-title"
        title="Build a provider-aware route"
        description="Every node owns an account and model. Gozar rewrites the model at each fallback, so OpenAI can fail over to an OpenRouter or subscription model with a different ID."
        steps={[
          {
            title: "Choose accounts",
            description: "Add the primary account, then choose one of that account's live models.",
            Icon: AccountsIcon,
          },
          {
            title: "Order the fallback",
            description: "Add fallback accounts with their own models; the first successful node wins.",
            Icon: ChainIcon,
          },
          {
            title: "Attach API keys",
            description: "Pin this as a key default or override it for one LLM call by chain ID.",
            Icon: TokenIcon,
          },
        ]}
      />

      {actionError !== null && (
        <p className="alert alert--error page-alert" role="alert">
          <AlertIcon size={18} aria-hidden />
          <span>{actionError}</span>
        </p>
      )}
      {catalogError !== null && (
        <p className="alert alert--warn page-alert" role="status">
          <AlertIcon size={18} aria-hidden />
          <span>Model discovery is temporarily unavailable. Manual model IDs still work.</span>
        </p>
      )}

      <ChainsBody
        loading={loading}
        loadError={loadError}
        chains={chains}
        accountsById={accountsById}
        healthById={chainHealthById}
        busyId={busyId}
        onRetry={() => void load()}
        onEdit={openEdit}
        onDelete={(chain) => void handleDelete(chain)}
      />

      {editorOpen && (
        <Modal
          title={editing ? `Edit chain - ${editing.name}` : "Create chain"}
          size="wide"
          onClose={closeEditor}
        >
          <ChainEditor
            initial={editing}
            accounts={accounts}
            accountsById={accountsById}
            modelsByAccount={modelsByAccount}
            submitting={saving}
            error={saveError}
            onSubmit={(draft) => void handleSubmit(draft)}
          />
        </Modal>
      )}
    </>
  );
}

/** Renders the loading / error / empty / populated states of the chain list. */
function ChainsBody({
  loading,
  loadError,
  chains,
  accountsById,
  healthById,
  busyId,
  onRetry,
  onEdit,
  onDelete,
}: {
  readonly loading: boolean;
  readonly loadError: string | null;
  readonly chains: ReadonlyArray<ChainResponse> | null;
  readonly accountsById: ReadonlyMap<string, AccountResponse>;
  readonly healthById: ReadonlyMap<string, ModelCatalogChainResponse>;
  readonly busyId: string | null;
  readonly onRetry: () => void;
  readonly onEdit: (chain: ChainResponse) => void;
  readonly onDelete: (chain: ChainResponse) => void;
}): JSX.Element {
  if (loading && chains === null) {
    return <CardSkeleton count={3} label="Loading chains..." />;
  }

  if (loadError !== null && chains === null) {
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

  if (chains !== null && chains.length === 0) {
    return (
      <div className="state state--empty">
        <InboxIcon size={28} aria-hidden />
        <p>No fallback chains yet.</p>
        <p className="state__hint">
          Create a chain to order routing and failover across your accounts.
        </p>
      </div>
    );
  }

  const rows = chains ?? [];

  return (
    <ul className="chain-card-list">
      {rows.map((chain) => {
        const busy = busyId === chain.chain_id;
        const ordered = [...chain.entries].sort((a, b) => a.position - b.position);
        const health = healthById.get(chain.chain_id);
        return (
          <li key={chain.chain_id} className="chain-card">
            <div className="chain-card__head">
              <div>
                <h2 className="chain-card__title">{chain.name}</h2>
                <p className="chain-card__meta">
                  {chain.client_key ? `Stable key: ${chain.client_key}` : "Console-managed chain"}
                </p>
              </div>
              <div className="chain-card__head-actions">
                {health && (
                  <span
                    className={`badge badge--${
                      health.health === "healthy"
                        ? "ok"
                        : health.health === "broken"
                          ? "danger"
                          : "warn"
                    }`}
                  >
                    {health.health === "healthy" ? "Healthy" : "Needs attention"}
                  </span>
                )}
                <div className="row-actions">
                {busy ? (
                  <Spinner label="Working" size={18} />
                ) : (
                  <>
                    <button
                      type="button"
                      className="icon-button"
                      onClick={() => onEdit(chain)}
                      aria-label={`Edit ${chain.name}`}
                      title="Edit"
                    >
                      <EditIcon size={18} />
                    </button>
                    <button
                      type="button"
                      className="icon-button icon-button--danger"
                      onClick={() => onDelete(chain)}
                      aria-label={`Delete ${chain.name}`}
                      title="Delete"
                    >
                      <TrashIcon size={18} />
                    </button>
                  </>
                )}
                </div>
              </div>
            </div>

            <ChainCardStats chain={chain} accountsById={accountsById} ordered={ordered} />

            {health && health.issues.length > 0 && (
              <div className="chain-health-note" role="status">
                <AlertIcon size={18} aria-hidden />
                <div>
                  <strong>Route needs an update</strong>
                  <ul>
                    {health.issues.slice(0, 3).map((issue) => (
                      <li key={`${issue.code}-${issue.position ?? "chain"}`}>{issue.message}</li>
                    ))}
                  </ul>
                </div>
              </div>
            )}

            {ordered.length === 0 ? (
              <p className="chain-card__empty">This chain has no entries.</p>
            ) : (
              <>
                <ChainRoutePreview ordered={ordered} accountsById={accountsById} />
                <ol className="chain-chip-list">
                  {ordered.map((entry) => {
                    const availability = entryAvailability(entry.account_id, accountsById);
                    return (
                      <li
                        key={`${entry.account_id}-${entry.position}`}
                        className={
                          availability.available
                            ? "chain-chip"
                            : "chain-chip chain-chip--unavailable"
                        }
                      >
                        <span className="chain-chip__order" aria-hidden>
                          {entry.position + 1}
                        </span>
                        <span className="chain-chip__label">{availability.label}</span>
                        <span className="chain-chip__model">
                          {entry.model ?? "Request model"}
                        </span>
                        {availability.reason !== null && (
                          <span className={`badge badge--${availability.tone}`}>
                            {availability.reason}
                          </span>
                        )}
                      </li>
                    );
                  })}
                </ol>
              </>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function ChainCardStats({
  chain,
  accountsById,
  ordered,
}: {
  readonly chain: ChainResponse;
  readonly accountsById: ReadonlyMap<string, AccountResponse>;
  readonly ordered: ReadonlyArray<ChainResponse["entries"][number]>;
}): JSX.Element {
  const total = ordered.length;
  const available = ordered.filter((entry) =>
    entryAvailability(entry.account_id, accountsById).available,
  ).length;
  const stepLabel = total === 1 ? "step" : "steps";

  return (
    <div className="chain-card__stats" aria-label={`${chain.name} route summary`}>
      <span>{total} {stepLabel}</span>
      <span>{available} available</span>
      <span>{ordered.filter((entry) => Boolean(entry.model)).length} mapped models</span>
    </div>
  );
}

function ChainRoutePreview({
  ordered,
  accountsById,
}: {
  readonly ordered: ReadonlyArray<ChainResponse["entries"][number]>;
  readonly accountsById: ReadonlyMap<string, AccountResponse>;
}): JSX.Element {
  return (
    <div className="chain-route-preview" aria-label="Route preview">
      <span className="chain-route-node chain-route-node--system">Request</span>
      {ordered.map((entry) => {
        const availability = entryAvailability(entry.account_id, accountsById);
        return (
          <span key={`${entry.account_id}-${entry.position}`} className="chain-route-step">
            <span className="chain-route-connector" aria-hidden />
            <span
              className={
                availability.available
                  ? "chain-route-node"
                  : "chain-route-node chain-route-node--unavailable"
              }
            >
              {availability.label}
              <small>{entry.model ?? "Request model"}</small>
            </span>
          </span>
        );
      })}
      <span className="chain-route-step">
        <span className="chain-route-connector" aria-hidden />
        <span className="chain-route-node chain-route-node--system">Response</span>
      </span>
    </div>
  );
}
