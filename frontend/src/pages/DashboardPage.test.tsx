import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";

import { DashboardPage } from "./DashboardPage";
import { renderWithProviders } from "../test/render";
import type {
  AccountResponse,
  ChainResponse,
  ModelCatalogResponse,
  TokenResponse,
  TraceSummaryResponse,
} from "../api/types";

vi.mock("../api/accounts", () => ({
  listAccounts: vi.fn(),
}));

vi.mock("../api/chains", () => ({
  listChains: vi.fn(),
}));

vi.mock("../api/tokens", () => ({
  listTokens: vi.fn(),
}));

vi.mock("../api/traces", () => ({
  listTraces: vi.fn(),
}));

vi.mock("../api/models", () => ({
  getModelCatalog: vi.fn(),
  resetProviderModels: vi.fn(),
  updateProviderModels: vi.fn(),
}));

import { listAccounts } from "../api/accounts";
import { listChains } from "../api/chains";
import { getModelCatalog } from "../api/models";
import { listTokens } from "../api/tokens";
import { listTraces } from "../api/traces";

const account: AccountResponse = {
  account_id: "acct-1",
  provider: "openai",
  kind: "api_key",
  label: "Primary OpenAI",
  status: "active",
  connected_at: "2024-01-01T00:00:00Z",
  limit: null,
  consumption: 0,
};

const chain: ChainResponse = {
  chain_id: "chain-1",
  name: "Production route",
  model_selector: null,
  entries: [{ account_id: "acct-1", position: 0, model: "gpt-5.5", route: "chat" }],
};

const token: TokenResponse = {
  token_id: "tok-1",
  id_prefix: "abc123def4567890",
  label: "App key",
  status: "active",
  assigned_chain_id: "chain-1",
  assigned_chain_name: "Production route",
  limit: null,
  usage: 0,
};

const trace: TraceSummaryResponse = {
  correlation_id: "trace-1",
  started_at: "2024-01-01T00:00:00Z",
  ended_at: "2024-01-01T00:00:01Z",
  outcome: "success",
  status_code: 200,
  account_id: "acct-1",
  elapsed_seconds: 1,
};

const catalog: ModelCatalogResponse = {
  generated_at: "2024-01-01T00:00:00Z",
  cache_ttl_seconds: 300,
  refreshed: false,
  model_count: 1,
  models: [{ id: "gpt-5.5", object: "model", owned_by: "codex" }],
  embedding_model_count: 1,
  embedding_models: [
    { id: "text-embedding-3-small", object: "model", owned_by: "openai" },
  ],
  accounts: [
    {
      account_id: account.account_id,
      label: account.label,
      provider: account.provider,
      kind: account.kind,
      status: account.status,
      model_count: 1,
      models: [{ id: "gpt-5.5", object: "model", owned_by: "codex" }],
      embedding_model_count: 1,
      embedding_models: [
        { id: "text-embedding-3-small", object: "model", owned_by: "openai" },
      ],
    },
  ],
  chains: [
    {
      chain_id: chain.chain_id,
      name: chain.name,
      model_selector: null,
      entry_count: 1,
      chat_entry_count: 1,
      embedding_entry_count: 0,
      model_count: 1,
      models: [{ id: "gpt-5.5", object: "model", owned_by: "codex" }],
      embedding_model_count: 0,
      embedding_models: [],
      health: "healthy",
      issues: [],
    },
  ],
  providers: [
    {
      provider: "codex",
      source: "environment",
      model_count: 1,
      models: ["gpt-5.5"],
      updated_at: null,
    },
  ],
  unhealthy_chain_count: 0,
};

const mockListAccounts = vi.mocked(listAccounts);
const mockListChains = vi.mocked(listChains);
const mockListTokens = vi.mocked(listTokens);
const mockListTraces = vi.mocked(listTraces);
const mockGetModelCatalog = vi.mocked(getModelCatalog);

describe("DashboardPage setup wizard and model catalog", () => {
  beforeEach(() => {
    mockListAccounts.mockReset();
    mockListChains.mockReset();
    mockListTokens.mockReset();
    mockListTraces.mockReset();
    mockGetModelCatalog.mockReset();
    mockListAccounts.mockResolvedValue([account]);
    mockListChains.mockResolvedValue([chain]);
    mockListTokens.mockResolvedValue([token]);
    mockListTraces.mockResolvedValue([trace]);
    mockGetModelCatalog.mockResolvedValue(catalog);
  });

  it("renders setup progress and route-aware model catalog", async () => {
    renderWithProviders(<DashboardPage />);

    expect(await screen.findByText("Bring Gozar online")).toBeInTheDocument();
    expect(screen.getByText("LLM and embedding models available now")).toBeInTheDocument();
    expect(
      screen.getByText("1 LLM and 1 embedding model available from active routes."),
    ).toBeInTheDocument();
    expect(await screen.findAllByText("gpt-5.5")).not.toHaveLength(0);
    expect(await screen.findAllByText("text-embedding-3-small")).not.toHaveLength(0);
    expect(screen.getByText("Primary OpenAI")).toBeInTheDocument();
    expect(screen.getByText("Production route")).toBeInTheDocument();
  });
});
