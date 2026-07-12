import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { listAccounts } from "../../api/accounts";
import { listChains } from "../../api/chains";
import { ApiError } from "../../api/errors";
import { getModelCatalog } from "../../api/models";
import { listTokens } from "../../api/tokens";
import { listTraces } from "../../api/traces";
import type {
  AccountResponse,
  ChainResponse,
  ModelCatalogResponse,
  TokenResponse,
  TraceSummaryResponse,
} from "../../api/types";
import {
  AccountsIcon,
  AlertIcon,
  ChainIcon,
  CheckIcon,
  RefreshIcon,
  TokenIcon,
  TracesIcon,
} from "../../components/icons";
import { Spinner } from "../../components/Spinner";
import { ROUTES } from "../../routes";

interface SetupSnapshot {
  readonly accounts: ReadonlyArray<AccountResponse>;
  readonly chains: ReadonlyArray<ChainResponse>;
  readonly tokens: ReadonlyArray<TokenResponse>;
  readonly traces: ReadonlyArray<TraceSummaryResponse>;
  readonly catalog: ModelCatalogResponse;
}

interface WizardStep {
  readonly title: string;
  readonly description: string;
  readonly complete: boolean;
  readonly path?: string;
  readonly action?: string;
  readonly Icon: typeof AccountsIcon;
}

function messageFor(cause: unknown): string {
  if (cause instanceof ApiError) {
    return cause.message;
  }
  return "Could not load setup status.";
}

/** Setup status that guides a fresh install through the real Gozar flow. */
export function SetupWizard(): JSX.Element {
  const [snapshot, setSnapshot] = useState<SetupSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (refreshModels = false): Promise<void> => {
    setError(null);
    if (refreshModels) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }
    try {
      const [accounts, chains, tokens, traces, catalog] = await Promise.all([
        listAccounts(),
        listChains(),
        listTokens(),
        listTraces({ limit: 1 }),
        getModelCatalog(refreshModels),
      ]);
      setSnapshot({ accounts, chains, tokens, traces, catalog });
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

  const steps = useMemo<ReadonlyArray<WizardStep>>(() => {
    const accounts = snapshot?.accounts ?? [];
    const chains = snapshot?.chains ?? [];
    const tokens = snapshot?.tokens ?? [];
    const traces = snapshot?.traces ?? [];
    const modelCount = snapshot?.catalog.model_count ?? 0;

    return [
      {
        title: "Administrator ready",
        description: "You are signed in as an operator.",
        complete: true,
        Icon: CheckIcon,
      },
      {
        title: "Connect a provider account",
        description: accounts.length
          ? `${accounts.length} account${accounts.length === 1 ? "" : "s"} connected.`
          : "Add OpenAI, OpenRouter, Codex, or another configured provider.",
        complete: accounts.length > 0,
        path: ROUTES.accounts,
        action: accounts.length > 0 ? "Manage accounts" : "Connect account",
        Icon: AccountsIcon,
      },
      {
        title: "Sync available models",
        description: modelCount
          ? `${modelCount} model${modelCount === 1 ? "" : "s"} available from active routes.`
          : "Connect an account or refresh the catalog after adding provider access.",
        complete: modelCount > 0,
        Icon: RefreshIcon,
      },
      {
        title: "Build a fallback chain",
        description: chains.length
          ? `${chains.length} chain${chains.length === 1 ? "" : "s"} saved for routing.`
          : "Order accounts so requests can fail over cleanly.",
        complete: chains.length > 0,
        path: ROUTES.chains,
        action: chains.length > 0 ? "Review chains" : "Create chain",
        Icon: ChainIcon,
      },
      {
        title: "Issue a Gozar API key",
        description: tokens.length
          ? `${tokens.length} app key${tokens.length === 1 ? "" : "s"} ready.`
          : "Create the key your app sends as the bearer token.",
        complete: tokens.length > 0,
        path: ROUTES.tokens,
        action: tokens.length > 0 ? "Open keys" : "Create key",
        Icon: TokenIcon,
      },
      {
        title: "Run a test request",
        description: traces.length
          ? "Recent traffic is visible in traces."
          : "Use the test box on API keys, then inspect the trace.",
        complete: traces.length > 0,
        path: traces.length > 0 ? ROUTES.traces : ROUTES.tokens,
        action: traces.length > 0 ? "View traces" : "Test a key",
        Icon: TracesIcon,
      },
    ];
  }, [snapshot]);

  const completed = steps.filter((step) => step.complete).length;
  const progress = Math.round((completed / steps.length) * 100);

  return (
    <section className="setup-wizard" aria-labelledby="setup-wizard-title">
      <div className="setup-wizard__head">
        <div>
          <p className="section-kicker">First-run wizard</p>
          <h2 id="setup-wizard-title">Bring Gozar online</h2>
          <p>
            Follow the live status below from provider connection to a working
            OpenAI-compatible request.
          </p>
        </div>
        <button
          type="button"
          className="button button--ghost"
          onClick={() => void load(true)}
          disabled={loading || refreshing}
        >
          {refreshing ? <Spinner label="Refreshing setup" size={16} /> : <RefreshIcon size={16} />}
          <span>{refreshing ? "Refreshing..." : "Refresh"}</span>
        </button>
      </div>

      <div className="setup-wizard__meter" aria-label={`${completed} of ${steps.length} setup steps complete`}>
        <span style={{ width: `${progress}%` }} />
      </div>

      {error !== null && (
        <p className="alert alert--error" role="alert">
          <AlertIcon size={18} aria-hidden />
          <span>{error}</span>
        </p>
      )}

      {loading && snapshot === null ? (
        <div className="setup-wizard__loading" role="status">
          <Spinner label="Loading setup status" size={20} />
          <span>Loading setup status...</span>
        </div>
      ) : (
        <ol className="setup-wizard__steps">
          {steps.map((step, index) => (
            <li
              key={step.title}
              className={
                step.complete
                  ? "setup-wizard__step setup-wizard__step--complete"
                  : "setup-wizard__step"
              }
            >
              <span className="setup-wizard__step-index">
                {step.complete ? <CheckIcon size={15} /> : index + 1}
              </span>
              <span className="setup-wizard__step-icon">
                <step.Icon size={18} />
              </span>
              <span className="setup-wizard__step-copy">
                <strong>{step.title}</strong>
                <span>{step.description}</span>
              </span>
              {step.path !== undefined && step.action !== undefined && (
                <Link className="button button--ghost setup-wizard__step-action" to={step.path}>
                  {step.action}
                </Link>
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
