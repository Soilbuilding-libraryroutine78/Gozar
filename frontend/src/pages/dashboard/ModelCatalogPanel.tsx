import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError } from "../../api/errors";
import { getModelCatalog } from "../../api/models";
import type {
  ModelCardResponse,
  ModelCatalogAccountResponse,
  ModelCatalogChainResponse,
  ModelCatalogResponse,
  ProviderModelCatalogResponse,
} from "../../api/types";
import {
  AccountsIcon,
  AlertIcon,
  ChainIcon,
  CheckIcon,
  ChevronDownIcon,
  RefreshIcon,
  TokenIcon,
} from "../../components/icons";
import { Spinner } from "../../components/Spinner";
import { ROUTES } from "../../routes";
import { providerLabel, providerSupportsEmbeddings } from "../accounts/providers";

function messageFor(cause: unknown): string {
  if (cause instanceof ApiError) {
    return cause.message;
  }
  return "Could not load the model catalog.";
}

function modelIds(models: ReadonlyArray<ModelCardResponse>): ReadonlyArray<string> {
  return models.map((model) => model.id).filter(Boolean);
}

function ModelIdChipList({
  ids,
  limit = 8,
  label = "Available models",
}: {
  readonly ids: ReadonlyArray<string>;
  readonly limit?: number;
  readonly label?: string;
}): JSX.Element {
  if (ids.length === 0) {
    return <span className="model-chip-list__empty">No models advertised</span>;
  }
  const visible = ids.slice(0, limit);
  const overflow = ids.length - visible.length;
  return (
    <ul className="model-chip-list" aria-label={label}>
      {visible.map((id) => (
        <li key={id} className="model-chip">
          {id}
        </li>
      ))}
      {overflow > 0 && <li className="model-chip model-chip--muted">+{overflow} more</li>}
    </ul>
  );
}

function ModelChipList({
  models,
  limit = 8,
  label = "Available models",
}: {
  readonly models: ReadonlyArray<ModelCardResponse>;
  readonly limit?: number;
  readonly label?: string;
}): JSX.Element {
  return <ModelIdChipList ids={modelIds(models)} limit={limit} label={label} />;
}

function RouteModelList({
  label,
  models,
  limit = 5,
}: {
  readonly label: string;
  readonly models: ReadonlyArray<ModelCardResponse>;
  readonly limit?: number;
}): JSX.Element {
  return (
    <div className="model-source-row__catalog">
      <span className="model-source-row__catalog-label">{label}</span>
      <ModelChipList models={models} limit={limit} label={`${label} models`} />
    </div>
  );
}

function accountStatusTone(status: string): "ok" | "warn" | "muted" {
  if (status === "active") {
    return "ok";
  }
  if (status === "requires_reauth") {
    return "warn";
  }
  return "muted";
}

function accountStatusLabel(status: string): string {
  if (status === "requires_reauth") {
    return "Needs reconnect";
  }
  return status.replaceAll("_", " ");
}

function AccountModelRow({
  account,
}: {
  readonly account: ModelCatalogAccountResponse;
}): JSX.Element {
  return (
    <li className="model-source-row">
      <span className="model-source-row__icon">
        <AccountsIcon size={18} />
      </span>
      <span className="model-source-row__main">
        <strong>{account.label}</strong>
        <span className="model-source-row__meta">
          <span>{providerLabel(account.provider)}</span>
          <span>{account.kind.replaceAll("_", " ")}</span>
          <span className={`badge badge--${accountStatusTone(account.status)}`}>
            {accountStatusLabel(account.status)}
          </span>
        </span>
        <div className="model-source-row__catalogs">
          <RouteModelList label="LLM" models={account.models} />
          {providerSupportsEmbeddings(account.provider) && (
            <RouteModelList label="Embeddings" models={account.embedding_models} />
          )}
        </div>
      </span>
      <span className="model-source-row__side model-source-row__counts">
        <span>{account.model_count} LLM</span>
        {providerSupportsEmbeddings(account.provider) && (
          <span>{account.embedding_model_count} Embed</span>
        )}
      </span>
    </li>
  );
}

function ChainModelRow({
  chain,
}: {
  readonly chain: ModelCatalogChainResponse;
}): JSX.Element {
  return (
    <li className="model-source-row">
      <span className="model-source-row__icon">
        <ChainIcon size={18} />
      </span>
      <span className="model-source-row__main">
        <strong>{chain.name}</strong>
        <span>
          {chain.chat_entry_count} LLM / {chain.embedding_entry_count} Embeddings
          {chain.model_selector ? ` - ${chain.model_selector}` : ""}
        </span>
        {chain.health !== "healthy" && chain.issues[0] && (
          <span className="model-source-row__issue">{chain.issues[0].message}</span>
        )}
        <div className="model-source-row__catalogs">
          {chain.chat_entry_count > 0 && <RouteModelList label="LLM" models={chain.models} />}
          {chain.embedding_entry_count > 0 && (
            <RouteModelList label="Embeddings" models={chain.embedding_models} />
          )}
        </div>
      </span>
      <span className="model-source-row__side">
        <span
          className={`badge badge--${
            chain.health === "healthy" ? "ok" : chain.health === "broken" ? "danger" : "warn"
          }`}
        >
          {chain.health === "healthy" ? "Healthy" : "Review"}
        </span>
        <span className="model-source-row__counts">
          <span>{chain.model_count} LLM</span>
          {chain.embedding_entry_count > 0 && (
            <span>{chain.embedding_model_count} Embed</span>
          )}
        </span>
      </span>
    </li>
  );
}

function providerSourceLabel(catalog: ProviderModelCatalogResponse): string {
  return catalog.source === "runtime" ? "Runtime fallback" : "Configured fallback";
}

function ProviderCatalogRow({
  catalog,
}: {
  readonly catalog: ProviderModelCatalogResponse;
}): JSX.Element {
  return (
    <li className="provider-catalog-row">
      <div className="provider-catalog-row__head">
        <div>
          <strong>{providerLabel(catalog.provider)}</strong>
          <span>{providerSourceLabel(catalog)}</span>
        </div>
        <span
          className={
            catalog.source === "runtime"
              ? "provider-catalog-row__badge provider-catalog-row__badge--runtime"
              : "provider-catalog-row__badge"
          }
        >
          {catalog.model_count} model{catalog.model_count === 1 ? "" : "s"}
        </span>
      </div>
      <ModelIdChipList ids={catalog.models} limit={5} />
    </li>
  );
}

/** Dashboard panel showing model discovery by account and chain. */
export function ModelCatalogPanel(): JSX.Element {
  const [catalog, setCatalog] = useState<ModelCatalogResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async (refresh = false, quiet = false): Promise<void> => {
    setError(null);
    setNotice(null);
    if (refresh) {
      setRefreshing(true);
    } else if (!quiet) {
      setLoading(true);
    }
    try {
      const nextCatalog = await getModelCatalog(refresh);
      setCatalog(nextCatalog);
      setNotice(refresh ? "Model catalog refreshed from available provider routes." : null);
    } catch (cause) {
      setError(messageFor(cause));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load(false);
  }, [load]);

  useEffect(() => {
    if (catalog === null) {
      return undefined;
    }
    const delay = Math.max(30, catalog.cache_ttl_seconds) * 1000;
    const timer = window.setTimeout(() => void load(false, true), delay);
    return () => window.clearTimeout(timer);
  }, [catalog, load]);

  return (
    <section className="model-catalog" aria-labelledby="model-catalog-title">
      <div className="model-catalog__head">
        <div>
          <p className="section-kicker">Model catalog</p>
          <h2 id="model-catalog-title">LLM and embedding models available now</h2>
          <p>
            Gozar discovers each request type separately from active accounts and refreshes
            the catalog automatically. Inactive or reconnect-needed accounts stay visible
            but are not used for routing.
          </p>
        </div>
        <button
          type="button"
          className="button button--ghost"
          onClick={() => void load(true)}
          disabled={loading || refreshing}
        >
          {refreshing ? <Spinner label="Refreshing models" size={16} /> : <RefreshIcon size={16} />}
          <span>{refreshing ? "Refreshing..." : "Refresh models"}</span>
        </button>
      </div>

      {error !== null && (
        <p className="alert alert--error" role="alert">
          <AlertIcon size={18} aria-hidden />
          <span>{error}</span>
        </p>
      )}
      {notice !== null && (
        <p className="alert alert--success" role="status">
          <CheckIcon size={18} aria-hidden />
          <span>{notice}</span>
        </p>
      )}

      {loading && catalog === null ? (
        <div className="model-catalog__loading" role="status">
          <Spinner label="Loading model catalog" size={20} />
          <span>Loading model catalog...</span>
        </div>
      ) : catalog === null ? (
        <div className="model-catalog__empty">
          <TokenIcon size={22} aria-hidden />
          <p>Model catalog is unavailable.</p>
        </div>
      ) : (
        <>
          {catalog.unhealthy_chain_count > 0 && (
            <div className="model-catalog-alert" role="alert">
              <AlertIcon size={20} aria-hidden />
              <div>
                <strong>
                  {catalog.unhealthy_chain_count} chain
                  {catalog.unhealthy_chain_count === 1 ? "" : "s"} need attention
                </strong>
                <p>
                  A provider account or saved model changed. Review the affected node before it
                  becomes the only available route.
                </p>
              </div>
              <Link to={ROUTES.chains}>Review chains</Link>
            </div>
          )}
          <div className="model-catalog__summary">
            <div>
              <span>{catalog.model_count}</span>
              <small>LLM models</small>
            </div>
            <div>
              <span>{catalog.embedding_model_count}</span>
              <small>embedding models</small>
            </div>
            <div>
              <span>{catalog.accounts.length}</span>
              <small>accounts</small>
            </div>
            <div>
              <span>{catalog.chains.length}</span>
              <small>chains</small>
            </div>
          </div>

          <div className="model-catalog__models">
            <div className="model-catalog__model-lane">
              <span className="model-catalog__label">LLM</span>
              <ModelChipList models={catalog.models} label="Available LLM models" />
            </div>
            <div className="model-catalog__model-lane">
              <span className="model-catalog__label">Embeddings</span>
              <ModelChipList
                models={catalog.embedding_models}
                label="Available embedding models"
              />
            </div>
          </div>

          <details className="model-catalog-details">
            <summary>
              <span>
                <strong>Catalog details</strong>
                <small>Provider sources, account access, and saved chain health</small>
              </span>
              <ChevronDownIcon size={18} aria-hidden />
            </summary>
            <div className="model-catalog-details__body">
              <div className="provider-catalog">
                <div className="model-catalog__column-head">
                  <div>
                    <h3>Provider source status</h3>
                    <p className="provider-catalog__hint">
                      OpenAI/OpenRouter use live discovery when an API-key account is
                      available. Other providers use the configured fallback list.
                    </p>
                  </div>
                </div>
                {catalog.providers.length === 0 ? (
                  <p className="model-catalog__hint">
                    Connect an account to see the model source for each provider.
                  </p>
                ) : (
                  <ul className="provider-catalog-list">
                    {catalog.providers.map((provider) => (
                      <ProviderCatalogRow
                        key={provider.provider}
                        catalog={provider}
                      />
                    ))}
                  </ul>
                )}
              </div>

              <div className="model-catalog__columns">
                <div className="model-catalog__column">
                  <div className="model-catalog__column-head">
                    <h3>By account</h3>
                    <Link to={ROUTES.accounts}>Accounts</Link>
                  </div>
                  {catalog.accounts.length === 0 ? (
                    <p className="model-catalog__hint">
                      Connect an account to discover reachable models.
                    </p>
                  ) : (
                    <ul className="model-source-list">
                      {catalog.accounts.map((account) => (
                        <AccountModelRow key={account.account_id} account={account} />
                      ))}
                    </ul>
                  )}
                </div>

                <div className="model-catalog__column">
                  <div className="model-catalog__column-head">
                    <h3>By chain</h3>
                    <Link to={ROUTES.chains}>Chains</Link>
                  </div>
                  {catalog.chains.length === 0 ? (
                    <p className="model-catalog__hint">
                      Build a fallback chain to preview route-specific models.
                    </p>
                  ) : (
                    <ul className="model-source-list">
                      {catalog.chains.map((chain) => (
                        <ChainModelRow key={chain.chain_id} chain={chain} />
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            </div>
          </details>
        </>
      )}
    </section>
  );
}
