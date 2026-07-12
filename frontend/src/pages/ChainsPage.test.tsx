import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ChainsPage } from "./ChainsPage";
import { ChainEditor } from "./chains/ChainEditor";
import { indexAccounts } from "./chains/format";
import { ApiError } from "../api/errors";
import { renderWithProviders, pending } from "../test/render";
import type {
  AccountResponse,
  ChainResponse,
  ModelCatalogResponse,
} from "../api/types";

// Flow_Controller and Account_Manager API layers are mocked: the chains view loads
// both chains and accounts together so it can cross-reference entry availability.
vi.mock("../api/chains", () => ({
  listChains: vi.fn(),
  getChain: vi.fn(),
  createChain: vi.fn(),
  editChain: vi.fn(),
  deleteChain: vi.fn(),
}));

vi.mock("../api/accounts", () => ({
  listAccounts: vi.fn(),
}));

vi.mock("../api/models", () => ({
  getModelCatalog: vi.fn(),
}));

import { listChains } from "../api/chains";
import { listAccounts } from "../api/accounts";
import { getModelCatalog } from "../api/models";

const mockListChains = vi.mocked(listChains);
const mockListAccounts = vi.mocked(listAccounts);
const mockGetModelCatalog = vi.mocked(getModelCatalog);

const sampleAccount: AccountResponse = {
  account_id: "acct-1",
  provider: "openai",
  kind: "api_key",
  label: "Primary OpenAI",
  status: "active",
  connected_at: "2024-01-01T00:00:00Z",
  limit: null,
  consumption: 0,
};

const sampleChain: ChainResponse = {
  chain_id: "chain-1",
  name: "Primary failover",
  model_selector: null,
  entries: [{ account_id: "acct-1", position: 0, model: "gpt-5.4-mini" }],
};

const sampleCatalog: ModelCatalogResponse = {
  generated_at: "2026-07-11T00:00:00Z",
  cache_ttl_seconds: 300,
  refreshed: false,
  model_count: 1,
  models: [{ id: "gpt-5.4-mini", object: "model", owned_by: "openai" }],
  accounts: [
    {
      account_id: sampleAccount.account_id,
      label: sampleAccount.label,
      provider: sampleAccount.provider,
      kind: sampleAccount.kind,
      status: sampleAccount.status,
      model_count: 1,
      models: [{ id: "gpt-5.4-mini", object: "model", owned_by: "openai" }],
    },
  ],
  chains: [
    {
      chain_id: sampleChain.chain_id,
      name: sampleChain.name,
      model_selector: null,
      entry_count: 1,
      model_count: 1,
      models: [{ id: "gpt-5.4-mini", object: "model", owned_by: "openai" }],
      health: "healthy",
      issues: [],
    },
  ],
  providers: [],
  unhealthy_chain_count: 0,
};

describe("ChainsPage async states (Requirement 17.3)", () => {
  beforeEach(() => {
    mockListChains.mockReset();
    mockListAccounts.mockReset();
    mockGetModelCatalog.mockReset();
    mockGetModelCatalog.mockResolvedValue(sampleCatalog);
  });

  it("renders the explicit loading state while chains are pending", () => {
    mockListChains.mockReturnValue(pending<ReadonlyArray<ChainResponse>>());
    mockListAccounts.mockResolvedValue([]);

    renderWithProviders(<ChainsPage />);

    expect(screen.getByText("Loading chains...")).toBeInTheDocument();
  });

  it("renders the empty state when no chains are configured", async () => {
    mockListChains.mockResolvedValue([]);
    mockListAccounts.mockResolvedValue([]);

    renderWithProviders(<ChainsPage />);

    expect(await screen.findByText("No fallback chains yet.")).toBeInTheDocument();
  });

  it("renders the error state with a retry control when the load fails", async () => {
    mockListChains.mockRejectedValue(
      new ApiError(500, "SERVER_ERROR", "Could not load chains."),
    );
    mockListAccounts.mockResolvedValue([]);

    renderWithProviders(<ChainsPage />);

    expect(await screen.findByText("Could not load chains.")).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: "Retry" });
    expect(retry).toBeInTheDocument();

    mockListChains.mockResolvedValueOnce([sampleChain]);
    mockListAccounts.mockResolvedValueOnce([sampleAccount]);
    await userEvent.click(retry);

    expect(await screen.findByText("Primary failover")).toBeInTheDocument();
  });

  it("renders the chain guide, summary, and route preview", async () => {
    mockListChains.mockResolvedValue([sampleChain]);
    mockListAccounts.mockResolvedValue([sampleAccount]);

    renderWithProviders(<ChainsPage />);

    expect(await screen.findByText("Build a provider-aware route")).toBeInTheDocument();
    expect(screen.getByText("1 step")).toBeInTheDocument();
    expect(screen.getByText("1 available")).toBeInTheDocument();
    expect(screen.getByLabelText("Route preview")).toHaveTextContent("Request");
    expect(screen.getByLabelText("Route preview")).toHaveTextContent("Primary OpenAI");
    expect(screen.getByLabelText("Route preview")).toHaveTextContent("Response");
  });
});

describe("Chain editor required fields (Requirement 17.3)", () => {
  it("requires a name and at least one entry before saving", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    const accounts: ReadonlyArray<AccountResponse> = [sampleAccount];

    renderWithProviders(
      <ChainEditor
        initial={null}
        accounts={accounts}
        accountsById={indexAccounts(accounts)}
        modelsByAccount={new Map([["acct-1", ["gpt-5.4-mini"]]])}
        submitting={false}
        error={null}
        onSubmit={onSubmit}
      />,
    );

    const save = screen.getByRole("button", { name: "Create chain" });

    // No name -> name is required.
    await user.click(save);
    expect(await screen.findByText("Enter a name for the chain.")).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();

    // With a name but no entries -> at least one entry is required.
    await user.type(screen.getByLabelText("Name"), "Primary failover");
    await user.click(save);
    expect(
      await screen.findByText("Add at least one account to the chain."),
    ).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();

    // Add an entry, then the chain can be saved.
    await user.selectOptions(screen.getByLabelText("Add account"), "acct-1");
    await user.click(screen.getByRole("button", { name: "Add node" }));

    expect(screen.getByLabelText("Routing graph preview")).toBeInTheDocument();
    expect(screen.getByLabelText("Fallback waterfall order")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Select Primary OpenAI" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Primary OpenAI" })).toBeInTheDocument();

    await user.click(save);

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith({
      name: "Primary failover",
      entries: [
        {
          account_id: "acct-1",
          model: "gpt-5.4-mini",
          fallback_policy: "any_error",
        },
      ],
      model_selector: null,
    });
  });
});
