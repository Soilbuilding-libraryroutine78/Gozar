import type { Page, Route, Request } from "@playwright/test";

import type {
  AccountResponse,
  ApiKeyConnectRequest,
  AuthorizationChallengeResponse,
  ChainResponse,
  CreateChainRequest,
  CreateTokenRequest,
  CredentialSummaryResponse,
  EditChainRequest,
  IssuedTokenResponse,
  ModelCardResponse,
  ModelCatalogResponse,
  ModelListResponse,
  ProviderModelCatalogResponse,
  SubscriptionCompleteRequest,
  TokenResponse,
  TraceDetailResponse,
  TraceSummaryResponse,
  UpdateProviderModelsRequest,
} from "../../src/api/types";
import {
  SESSION,
  accountAnalytics,
  seedAccounts,
  seedChains,
  seedTokens,
  seedTraceDetail,
  seedTraceSummaries,
  systemAnalytics,
  tokenAnalytics,
} from "./data";

/**
 * In-memory, network-layer mock of the Gozar admin + auth HTTP API for the e2e
 * suite (task 16.8). It is installed with `page.route("**\/api/**", ...)` so the
 * console runs against the real production build with zero backend dependency.
 *
 * The mock keeps mutable state (accounts, tokens, chains) so a flow that creates
 * something then re-reads the list observes its own write, exactly as the live
 * backend would. Every response is typed against `src/api/types.ts`.
 */

/** Mutable backend state for a single test. */
export interface MockState {
  accounts: AccountResponse[];
  tokens: TokenResponse[];
  tokenModels: Record<string, ModelListResponse>;
  chains: ChainResponse[];
  providerCatalogs: Record<string, ProviderModelCatalogResponse>;
  traceSummaries: TraceSummaryResponse[];
  traceDetail: TraceDetailResponse;
}

/** Options for seeding the mock for a particular flow. */
export interface MockOptions {
  readonly accounts?: AccountResponse[];
  readonly tokens?: TokenResponse[];
  readonly chains?: ChainResponse[];
}

function jsonHeaders(): Record<string, string> {
  return { "content-type": "application/json" };
}

async function fulfillJson(route: Route, status: number, body: unknown): Promise<void> {
  await route.fulfill({ status, headers: jsonHeaders(), body: JSON.stringify(body) });
}

/** A 204 No Content, matching the void admin endpoints (limit/enabled/delete). */
async function fulfillNoContent(route: Route): Promise<void> {
  await route.fulfill({ status: 204, body: "" });
}

function readBody<T>(request: Request): T {
  const raw = request.postData();
  return (raw ? JSON.parse(raw) : {}) as T;
}

let counter = 0;
function nextId(prefix: string): string {
  counter += 1;
  return `${prefix}-${counter.toString().padStart(4, "0")}`;
}

function configuredModelsForProvider(provider: string): ReadonlyArray<string> {
  const models: Record<string, ReadonlyArray<string>> = {
    openai: ["gpt-4o", "gpt-4o-mini"],
    openrouter: ["openrouter/auto", "anthropic/claude-3.5-sonnet"],
    codex: ["gpt-5.5", "gpt-5.4-mini"],
    anthropic: ["claude-sonnet-5", "claude-haiku-5"],
  };
  return models[provider] ?? [`${provider}-model`];
}

function configuredEmbeddingModelsForProvider(provider: string): ReadonlyArray<string> {
  const models: Record<string, ReadonlyArray<string>> = {
    openai: ["text-embedding-3-small", "text-embedding-3-large"],
    openrouter: [
      "openai/text-embedding-3-small",
      "google/gemini-embedding-001",
    ],
  };
  return models[provider] ?? [];
}

function defaultProviderCatalogs(): Record<string, ProviderModelCatalogResponse> {
  return Object.fromEntries(
    ["anthropic", "codex", "openai", "openrouter"].map((provider) => {
      const models = configuredModelsForProvider(provider);
      return [
        provider,
        {
          provider,
          source: "environment",
          model_count: models.length,
          models,
          updated_at: null,
        },
      ];
    }),
  );
}

function normalizeModelIds(modelIds: ReadonlyArray<string>): ReadonlyArray<string> {
  const seen = new Set<string>();
  const normalized: string[] = [];
  for (const raw of modelIds) {
    const model = raw.trim();
    if (model.length === 0 || seen.has(model)) {
      continue;
    }
    seen.add(model);
    normalized.push(model);
  }
  return normalized;
}

function modelsForAccount(
  account: AccountResponse,
  providerCatalogs: Record<string, ProviderModelCatalogResponse>,
  route: "chat" | "embeddings" = "chat",
): ReadonlyArray<ModelCardResponse> {
  if (account.status !== "active") {
    return [];
  }
  const created = Math.floor(Date.parse(account.connected_at) / 1000);
  const fallback =
    route === "embeddings"
      ? configuredEmbeddingModelsForProvider(account.provider)
      : providerCatalogs[account.provider]?.models ?? configuredModelsForProvider(account.provider);
  return fallback.map((id) => ({
    id,
    object: "model",
    created: Number.isFinite(created) ? created : undefined,
    owned_by: account.provider,
  }));
}

function mergeModels(
  groups: ReadonlyArray<ReadonlyArray<ModelCardResponse>>,
): ReadonlyArray<ModelCardResponse> {
  const seen = new Set<string>();
  const merged: ModelCardResponse[] = [];
  for (const group of groups) {
    for (const model of group) {
      if (seen.has(model.id)) {
        continue;
      }
      seen.add(model.id);
      merged.push(model);
    }
  }
  return merged;
}

function modelsForChain(
  chain: ChainResponse,
  accounts: ReadonlyArray<AccountResponse>,
  providerCatalogs: Record<string, ProviderModelCatalogResponse>,
  route: "chat" | "embeddings",
): ReadonlyArray<ModelCardResponse> {
  const accountsById = new Map(accounts.map((account) => [account.account_id, account]));
  return mergeModels(
    chain.entries
      .filter((entry) => (entry.route ?? "chat") === route)
      .slice()
      .sort((left, right) => left.position - right.position)
      .map((entry) => {
        const account = accountsById.get(entry.account_id);
        return account === undefined ? [] : modelsForAccount(account, providerCatalogs, route);
      }),
  );
}

function catalogForState(state: MockState, refreshed: boolean): ModelCatalogResponse {
  const accountRows = state.accounts.map((account) => {
    const models = modelsForAccount(account, state.providerCatalogs);
    const embeddingModels = modelsForAccount(account, state.providerCatalogs, "embeddings");
    return {
      account_id: account.account_id,
      label: account.label,
      provider: account.provider,
      kind: account.kind,
      status: account.status,
      model_count: models.length,
      models,
      embedding_model_count: embeddingModels.length,
      embedding_models: embeddingModels,
    };
  });
  const chainRows = state.chains.map((chain) => {
    const models = modelsForChain(chain, state.accounts, state.providerCatalogs, "chat");
    const embeddingModels = modelsForChain(
      chain,
      state.accounts,
      state.providerCatalogs,
      "embeddings",
    );
    return {
      chain_id: chain.chain_id,
      name: chain.name,
      model_selector: chain.model_selector ?? null,
      entry_count: chain.entries.length,
      chat_entry_count: chain.entries.filter((entry) => (entry.route ?? "chat") === "chat")
        .length,
      embedding_entry_count: chain.entries.filter((entry) => entry.route === "embeddings")
        .length,
      model_count: models.length,
      models,
      embedding_model_count: embeddingModels.length,
      embedding_models: embeddingModels,
      health: "healthy",
      issues: [],
    };
  });
  const models = mergeModels(accountRows.map((account) => account.models));
  const embeddingModels = mergeModels(
    accountRows.map((account) => account.embedding_models),
  );
  return {
    generated_at: new Date().toISOString(),
    cache_ttl_seconds: 300,
    refreshed,
    model_count: models.length,
    models,
    embedding_model_count: embeddingModels.length,
    embedding_models: embeddingModels,
    accounts: accountRows,
    chains: chainRows,
    providers: Object.values(state.providerCatalogs).sort((left, right) =>
      left.provider.localeCompare(right.provider),
    ),
    unhealthy_chain_count: 0,
  };
}

/**
 * Install the mocked API on a page. Call before navigating. Returns the mutable
 * state so a test can assert against or extend it.
 */
export async function installApiMock(
  page: Page,
  options: MockOptions = {},
): Promise<MockState> {
  const state: MockState = {
    accounts: options.accounts ?? seedAccounts(),
    tokens: options.tokens ?? seedTokens(),
    tokenModels: Object.fromEntries(
      (options.tokens ?? seedTokens()).map((token) => [
        token.token_id,
        {
          object: "list",
          data: [{ id: "gpt-5.5", object: "model", owned_by: "codex" }],
        },
      ]),
    ),
    chains: options.chains ?? seedChains(),
    providerCatalogs: defaultProviderCatalogs(),
    traceSummaries: seedTraceSummaries(),
    traceDetail: seedTraceDetail(),
  };

  await page.route("**/v1/models", async (route) => {
    return fulfillJson(route, 200, {
      object: "list",
      data: [{ id: "gpt-test", object: "model" }],
    });
  });

  await page.route("**/v1/chat/completions", async (route) => {
    const body = readBody<{ model?: string }>(route.request());
    return fulfillJson(route, 200, {
      id: nextId("chatcmpl"),
      object: "chat.completion",
      choices: [
        {
          index: 0,
          message: {
            role: "assistant",
            content: `Mock response from ${body.model ?? "selected model"}`,
          },
          finish_reason: "stop",
        },
      ],
    });
  });

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const method = request.method();
    const url = new URL(request.url());
    const path = url.pathname;

    // ---- Auth ------------------------------------------------------------
    if (path === "/api/auth/login" && method === "POST") {
      return fulfillJson(route, 200, SESSION);
    }
    if (path === "/api/auth/refresh" && method === "POST") {
      return fulfillJson(route, 200, SESSION);
    }
    if (path === "/api/auth/bootstrap" && method === "GET") {
      return fulfillJson(route, 200, { bootstrap_required: false });
    }

    // ---- Models ----------------------------------------------------------
    if (path === "/api/models" && method === "GET") {
      return fulfillJson(
        route,
        200,
        catalogForState(state, url.searchParams.get("refresh") === "true"),
      );
    }
    const providerModelsMatch = path.match(/^\/api\/models\/providers\/([^/]+)$/);
    if (providerModelsMatch && method === "PUT") {
      const provider = decodeURIComponent(providerModelsMatch[1]);
      const body = readBody<UpdateProviderModelsRequest>(request);
      const models = normalizeModelIds(body.models);
      const catalog: ProviderModelCatalogResponse = {
        provider,
        source: "runtime",
        model_count: models.length,
        models,
        updated_at: new Date().toISOString(),
      };
      state.providerCatalogs[provider] = catalog;
      return fulfillJson(route, 200, catalog);
    }
    if (providerModelsMatch && method === "DELETE") {
      const provider = decodeURIComponent(providerModelsMatch[1]);
      const models = configuredModelsForProvider(provider);
      const catalog: ProviderModelCatalogResponse = {
        provider,
        source: "environment",
        model_count: models.length,
        models,
        updated_at: null,
      };
      state.providerCatalogs[provider] = catalog;
      return fulfillJson(route, 200, catalog);
    }

    // ---- Accounts --------------------------------------------------------
    if (path === "/api/accounts" && method === "GET") {
      return fulfillJson(route, 200, state.accounts);
    }
    if (path === "/api/accounts/connect/api-key" && method === "POST") {
      const body = readBody<ApiKeyConnectRequest>(request);
      const account: AccountResponse = {
        account_id: nextId("acc"),
        provider: body.provider,
        kind: "api_key",
        label: body.label && body.label.trim() !== "" ? body.label : `${body.provider} key`,
        status: "active",
        connected_at: new Date().toISOString(),
        limit: null,
        consumption: 0,
      };
      state.accounts.push(account);
      const summary: CredentialSummaryResponse = {
        account_id: account.account_id,
        provider: account.provider,
        kind: account.kind,
        label: account.label,
        status: account.status,
      };
      return fulfillJson(route, 201, summary);
    }
    if (path === "/api/accounts/connect/subscription/begin" && method === "POST") {
      const challenge: AuthorizationChallengeResponse = {
        pending_id: nextId("pending"),
        authorize_url: "https://provider.example/oauth/authorize?mock=1",
        state: nextId("state"),
      };
      return fulfillJson(route, 200, challenge);
    }
    if (path === "/api/accounts/connect/subscription/complete" && method === "POST") {
      const body = readBody<SubscriptionCompleteRequest>(request);
      const account: AccountResponse = {
        account_id: nextId("acc"),
        provider: "codex",
        kind: "subscription",
        label: body.label && body.label.trim() !== "" ? body.label : "Subscription account",
        status: "active",
        connected_at: new Date().toISOString(),
        limit: null,
        consumption: 0,
      };
      state.accounts.push(account);
      const summary: CredentialSummaryResponse = {
        account_id: account.account_id,
        provider: account.provider,
        kind: account.kind,
        label: account.label,
        status: account.status,
      };
      return fulfillJson(route, 201, summary);
    }
    {
      const limitMatch = path.match(/^\/api\/accounts\/([^/]+)\/limit$/);
      if (limitMatch && method === "PUT") {
        return fulfillNoContent(route);
      }
      const enabledMatch = path.match(/^\/api\/accounts\/([^/]+)\/enabled$/);
      if (enabledMatch && method === "PATCH") {
        return fulfillNoContent(route);
      }
      const detailMatch = path.match(/^\/api\/accounts\/([^/]+)$/);
      if (detailMatch && method === "DELETE") {
        const id = detailMatch[1];
        state.accounts = state.accounts.filter((a) => a.account_id !== id);
        return fulfillNoContent(route);
      }
    }

    // ---- Tokens ----------------------------------------------------------
    if (path === "/api/tokens" && method === "GET") {
      return fulfillJson(route, 200, state.tokens);
    }
    if (path === "/api/tokens" && method === "POST") {
      const body = readBody<CreateTokenRequest>(request);
      const tokenId = nextId("tok");
      const issued: IssuedTokenResponse = {
        token_id: tokenId,
        id_prefix: "e2epub000000001",
        label: body.label,
        status: "active",
        assigned_chain_id: body.assigned_chain_id ?? null,
        secret: "gz-e2epub000000001-e2e_REVEALABLE_SECRET_VALUE_abc123",
      };
      const assignedChain = state.chains.find(
        (chain) => chain.chain_id === body.assigned_chain_id,
      );
      state.tokens.push({
        token_id: tokenId,
        id_prefix: issued.id_prefix,
        label: body.label,
        status: "active",
        assigned_chain_id: body.assigned_chain_id ?? null,
        assigned_chain_name: assignedChain?.name ?? null,
        limit: body.limit ?? null,
        usage: 0,
        can_reveal: true,
      });
      state.tokenModels[tokenId] = {
        object: "list",
        data: [{ id: "gpt-5.5", object: "model", owned_by: "codex" }],
      };
      return fulfillJson(route, 201, issued);
    }
    {
      const modelsMatch = path.match(/^\/api\/tokens\/([^/]+)\/models$/);
      if (modelsMatch && method === "GET") {
        return fulfillJson(
          route,
          200,
          state.tokenModels[modelsMatch[1]] ?? { object: "list", data: [] },
        );
      }
      const limitMatch = path.match(/^\/api\/tokens\/([^/]+)\/limit$/);
      if (limitMatch && method === "PUT") {
        return fulfillNoContent(route);
      }
      const chainMatch = path.match(/^\/api\/tokens\/([^/]+)\/chain$/);
      if (chainMatch && method === "PATCH") {
        const id = chainMatch[1];
        const body = readBody<{ assigned_chain_id?: string | null }>(request);
        const assignedChain = state.chains.find(
          (chain) => chain.chain_id === body.assigned_chain_id,
        );
        state.tokens = state.tokens.map((token) =>
          token.token_id === id
            ? {
                ...token,
                assigned_chain_id: body.assigned_chain_id ?? null,
                assigned_chain_name: assignedChain?.name ?? null,
              }
            : token,
        );
        return fulfillNoContent(route);
      }
      const revealMatch = path.match(/^\/api\/tokens\/([^/]+)\/reveal$/);
      if (revealMatch && method === "POST") {
        const id = revealMatch[1];
        const body = readBody<{ existing_api_key?: string }>(request);
        const existing = state.tokens.find((token) => token.token_id === id);
        if (existing === undefined) {
          return fulfillJson(route, 404, {
            error: { code: "NOT_FOUND", message: "API key not found.", details: [] },
          });
        }
        if (existing.can_reveal === false && !body.existing_api_key) {
          return fulfillJson(route, 400, {
            error: {
              code: "VALIDATION_ERROR",
              message: "paste the existing API key once to enable future reveal",
              details: [],
            },
          });
        }
        existing.can_reveal = true;
        const issued: IssuedTokenResponse = {
          token_id: existing.token_id,
          id_prefix: existing.id_prefix,
          label: existing.label,
          status: existing.status,
          assigned_chain_id: existing.assigned_chain_id ?? null,
          secret: body.existing_api_key ?? (
            existing.id_prefix === "e2epub000000001"
              ? "gz-e2epub000000001-e2e_REVEALABLE_SECRET_VALUE_abc123"
              : `gz-${existing.id_prefix}-e2e_REVEALED_SECRET_VALUE_abc123`
          ),
        };
        return fulfillJson(route, 200, issued);
      }
      const enabledMatch = path.match(/^\/api\/tokens\/([^/]+)\/enabled$/);
      if (enabledMatch && method === "PATCH") {
        return fulfillNoContent(route);
      }
      const revokeMatch = path.match(/^\/api\/tokens\/([^/]+)\/revoke$/);
      if (revokeMatch && method === "POST") {
        return fulfillNoContent(route);
      }
    }

    // ---- Chains ----------------------------------------------------------
    if (path === "/api/chains" && method === "GET") {
      return fulfillJson(route, 200, state.chains);
    }
    if (path === "/api/chains" && method === "POST") {
      const body = readBody<CreateChainRequest>(request);
      const positions = { chat: 0, embeddings: 0 };
      const chain: ChainResponse = {
        chain_id: nextId("chain"),
        name: body.name,
        model_selector: body.model_selector ?? null,
        entries: body.entries.map((entry) => {
          const routeKind = entry.route ?? "chat";
          const position = positions[routeKind]++;
          return { ...entry, route: routeKind, position };
        }),
      };
      state.chains.push(chain);
      return fulfillJson(route, 201, chain);
    }
    {
      const detailMatch = path.match(/^\/api\/chains\/([^/]+)$/);
      if (detailMatch) {
        const id = detailMatch[1];
        if (method === "GET") {
          const chain = state.chains.find((c) => c.chain_id === id);
          return chain
            ? fulfillJson(route, 200, chain)
            : fulfillJson(route, 404, {
                error: { code: "NOT_FOUND", message: "Chain not found.", details: [] },
              });
        }
        if (method === "PUT") {
          const body = readBody<EditChainRequest>(request);
          const index = state.chains.findIndex((c) => c.chain_id === id);
          if (index >= 0) {
            const existing = state.chains[index];
            const entries = body.entries ?? existing.entries;
            const positions = { chat: 0, embeddings: 0 };
            const updated: ChainResponse = {
              chain_id: existing.chain_id,
              name: body.name ?? existing.name,
              model_selector:
                body.model_selector !== undefined ? body.model_selector : existing.model_selector,
              entries: entries.map((entry) => {
                const routeKind = entry.route ?? "chat";
                const position = positions[routeKind]++;
                return { ...entry, route: routeKind, position };
              }),
            };
            state.chains[index] = updated;
            return fulfillJson(route, 200, updated);
          }
        }
        if (method === "DELETE") {
          state.chains = state.chains.filter((c) => c.chain_id !== id);
          return fulfillNoContent(route);
        }
      }
    }

    // ---- Traces ----------------------------------------------------------
    if (path === "/api/traces" && method === "GET") {
      return fulfillJson(route, 200, state.traceSummaries);
    }
    {
      const detailMatch = path.match(/^\/api\/traces\/([^/]+)$/);
      if (detailMatch && method === "GET") {
        return fulfillJson(route, 200, state.traceDetail);
      }
    }

    // ---- Analytics -------------------------------------------------------
    {
      const start = url.searchParams.get("start") ?? "2024-01-01T00:00:00.000Z";
      const end = url.searchParams.get("end") ?? "2024-01-08T00:00:00.000Z";
      if (path === "/api/analytics/system" && method === "GET") {
        return fulfillJson(route, 200, systemAnalytics(start, end));
      }
      const tokenMatch = path.match(/^\/api\/analytics\/tokens\/([^/]+)$/);
      if (tokenMatch && method === "GET") {
        return fulfillJson(route, 200, tokenAnalytics(tokenMatch[1], start, end));
      }
      const accountMatch = path.match(/^\/api\/analytics\/accounts\/([^/]+)$/);
      if (accountMatch && method === "GET") {
        return fulfillJson(route, 200, accountAnalytics(accountMatch[1], start, end));
      }
    }

    // Anything unmatched is a test bug; fail loudly rather than silently hang.
    return fulfillJson(route, 500, {
      error: {
        code: "MOCK_UNHANDLED",
        message: `Unhandled ${method} ${path}`,
        details: [],
      },
    });
  });

  return state;
}
