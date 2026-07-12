import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TokensPage } from "./TokensPage";
import { ApiError } from "../api/errors";
import { renderWithProviders, pending } from "../test/render";
import type { ChainResponse, TokenResponse } from "../api/types";

// The Token_Authority API layer is mocked so the view runs without a backend.
vi.mock("../api/tokens", () => ({
  listTokens: vi.fn(),
  listTokenModels: vi.fn(),
  createToken: vi.fn(),
  revealToken: vi.fn(),
  testTokenRoute: vi.fn(),
  setTokenLimit: vi.fn(),
  setTokenChain: vi.fn(),
  setTokenEnabled: vi.fn(),
  revokeToken: vi.fn(),
}));

vi.mock("../api/chains", () => ({
  listChains: vi.fn(),
}));

import {
  createToken,
  listTokenModels,
  listTokens,
  revealToken,
  testTokenRoute,
} from "../api/tokens";
import { listChains } from "../api/chains";

const mockListTokens = vi.mocked(listTokens);
const mockListTokenModels = vi.mocked(listTokenModels);
const mockCreateToken = vi.mocked(createToken);
const mockRevealToken = vi.mocked(revealToken);
const mockTestTokenRoute = vi.mocked(testTokenRoute);
const mockListChains = vi.mocked(listChains);

const sampleToken: TokenResponse = {
  token_id: "tok-1",
  id_prefix: "abc123def4567890",
  label: "CI pipeline",
  status: "active",
  limit: null,
  usage: 0,
  can_reveal: true,
};

const sampleChain: ChainResponse = {
  chain_id: "chain-1",
  name: "Production routing",
  model_selector: null,
  entries: [],
};

describe("TokensPage async states (Requirement 17.2)", () => {
  beforeEach(() => {
    mockListTokens.mockReset();
    mockListChains.mockReset();
    mockListTokenModels.mockReset();
  });

  it("renders the explicit loading state while tokens are pending", () => {
    mockListTokens.mockReturnValue(pending<ReadonlyArray<TokenResponse>>());
    mockListChains.mockResolvedValue([]);
    mockListTokenModels.mockResolvedValue({ object: "list", data: [] });

    renderWithProviders(<TokensPage />);

    expect(screen.getByText("Loading API keys...")).toBeInTheDocument();
  });

  it("renders the empty state when no tokens exist", async () => {
    mockListTokens.mockResolvedValue([]);
    mockListChains.mockResolvedValue([]);
    mockListTokenModels.mockResolvedValue({ object: "list", data: [] });

    renderWithProviders(<TokensPage />);

    expect(await screen.findByText("No API keys yet.")).toBeInTheDocument();
  });

  it("renders the error state with a retry control when the load fails", async () => {
    mockListTokens.mockRejectedValue(
      new ApiError(503, "UNAVAILABLE", "Could not load tokens."),
    );
    mockListChains.mockResolvedValue([]);
    mockListTokenModels.mockResolvedValue({ object: "list", data: [] });

    renderWithProviders(<TokensPage />);

    expect(await screen.findByText("Could not load tokens.")).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: "Retry" });
    expect(retry).toBeInTheDocument();

    mockListTokens.mockResolvedValueOnce([sampleToken]);
    mockListChains.mockResolvedValueOnce([]);
    await userEvent.click(retry);

    expect(await screen.findByText("CI pipeline")).toBeInTheDocument();
  });
});

describe("Token create required fields (Requirement 17.2)", () => {
  beforeEach(() => {
    mockListTokens.mockReset();
    mockListTokens.mockResolvedValue([]);
    mockListChains.mockReset();
    mockListChains.mockResolvedValue([sampleChain]);
    mockListTokenModels.mockReset();
    mockListTokenModels.mockResolvedValue({ object: "list", data: [] });
    mockCreateToken.mockReset();
    mockRevealToken.mockReset();
    mockTestTokenRoute.mockReset();
  });

  it("requires a label before the create action is enabled", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TokensPage />);

    await screen.findByText("No API keys yet.");
    await user.click(screen.getByRole("button", { name: /create api key/i }));

    // Within the dialog the submit stays disabled until a label is entered.
    const dialog = screen.getByRole("dialog");
    const submit = within(dialog).getByRole("button", { name: "Create API key" });
    expect(submit).toBeDisabled();

    await user.type(within(dialog).getByLabelText("Label"), "Mobile app");
    expect(submit).toBeEnabled();

    // Whitespace-only labels do not satisfy the requirement.
    await user.clear(within(dialog).getByLabelText("Label"));
    await user.type(within(dialog).getByLabelText("Label"), "   ");
    expect(submit).toBeDisabled();
  });

  it("submits the selected routing chain when creating a token", async () => {
    const user = userEvent.setup();
    mockCreateToken.mockResolvedValue({
      token_id: "tok-created",
      id_prefix: "prefix",
      label: "Worker",
      status: "active",
      assigned_chain_id: "chain-1",
      secret: "gz-secret",
    });

    renderWithProviders(<TokensPage />);

    await screen.findByText("No API keys yet.");
    await user.click(screen.getByRole("button", { name: /create api key/i }));

    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("Label"), "Worker");
    await user.selectOptions(
      within(dialog).getByLabelText("Routing chain"),
      "chain-1",
    );
    await user.click(within(dialog).getByRole("button", { name: "Create API key" }));

    expect(mockCreateToken).toHaveBeenCalledWith({
      label: "Worker",
      limit: null,
      assigned_chain_id: "chain-1",
    });
  });
});

describe("Token integration guide", () => {
  beforeEach(() => {
    mockListTokens.mockReset();
    mockListTokens.mockResolvedValue([sampleToken]);
    mockListChains.mockReset();
    mockListChains.mockResolvedValue([]);
    mockListTokenModels.mockReset();
    mockListTokenModels.mockResolvedValue({
      object: "list",
      data: [{ id: "gpt-5.5", object: "model", owned_by: "codex" }],
    });
    mockRevealToken.mockReset();
    mockTestTokenRoute.mockReset();
    vi.unstubAllGlobals();
  });

  it("shows one compact snippet and switches between integration examples", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TokensPage />);

    await screen.findByText("CI pipeline");
    const expectedBaseUrl = new URL("/v1", window.location.origin).toString();

    expect(screen.getAllByRole("tab")).toHaveLength(3);
    expect(
      screen.getByText(`GOZAR_BASE_URL=${expectedBaseUrl}`),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Example API key")).toHaveValue("tok-1");
    await waitFor(() => expect(screen.getByLabelText("Model")).toHaveValue("gpt-5.5"));
    expect(screen.getByText("CI pipeline uses automatic routing")).toBeInTheDocument();
    expect(screen.getByRole("tabpanel")).toHaveTextContent(
      `os.environ.setdefault("GOZAR_BASE_URL", "${expectedBaseUrl}")`,
    );
    expect(screen.getByRole("tabpanel")).toHaveTextContent(
      'os.environ.setdefault("GOZAR_MODEL", "gpt-5.5")',
    );

    await user.click(screen.getByRole("tab", { name: /langgraph node/i }));

    expect(screen.getByRole("tabpanel")).toHaveTextContent(
      "from langchain_openai import ChatOpenAI",
    );
    expect(
      screen.getByRole("button", { name: /copy langgraph node snippet/i }),
    ).toBeInTheDocument();
  });

  it("sends a browser test request with the discovered route model", async () => {
    const user = userEvent.setup();
    mockTestTokenRoute.mockResolvedValue({
      id: "chatcmpl-test",
      object: "chat.completion",
      created: 1,
      model: "gpt-5.5",
      choices: [
        {
          index: 0,
          message: { role: "assistant", content: "Hello through Gozar" },
          finish_reason: "stop",
        },
      ],
    });

    renderWithProviders(<TokensPage />);

    await screen.findByText("CI pipeline");
    const send = screen.getByRole("button", { name: "Send test request" });
    await waitFor(() => expect(send).toBeEnabled());
    await user.clear(screen.getByLabelText("Prompt"));
    await user.type(screen.getByLabelText("Prompt"), "Ping");
    await user.click(send);

    await waitFor(() => {
      expect(mockTestTokenRoute).toHaveBeenCalledWith("tok-1", {
        model: "gpt-5.5",
        prompt: "Ping",
      });
    });
    expect(await screen.findByText("Request succeeded")).toBeInTheDocument();
    expect(screen.getByText("Hello through Gozar")).toBeInTheDocument();
  });

  it("reveals the same API key after password confirmation without rotating it", async () => {
    const user = userEvent.setup();
    mockRevealToken.mockResolvedValue({
      token_id: "tok-1",
      id_prefix: "abc123def4567890",
      label: "CI pipeline",
      status: "active",
      assigned_chain_id: null,
      secret: "gz-abc123def4567890-existing-secret",
    });

    renderWithProviders(<TokensPage />);

    await screen.findByText("CI pipeline");

    await user.click(
      screen.getByRole("button", { name: /reveal api key for ci pipeline/i }),
    );

    const revealDialog = screen.getByRole("dialog");
    const submit = within(revealDialog).getByRole("button", {
      name: "Reveal key",
    });
    expect(submit).toBeDisabled();

    await user.type(within(revealDialog).getByLabelText("Your password"), "password");
    await user.click(submit);

    await waitFor(() => {
      expect(mockRevealToken).toHaveBeenCalledWith("tok-1", "password", undefined);
    });
    expect(await screen.findByText("API key ready")).toBeInTheDocument();
    const readyDialog = screen.getByRole("dialog");
    expect(within(readyDialog).getByLabelText("Gozar API key")).toHaveValue(
      "gz-abc123def4567890-existing-secret",
    );
  });

  it("stores an existing legacy API key for future reveal without replacing it", async () => {
    const user = userEvent.setup();
    mockListTokens.mockResolvedValueOnce([
      { ...sampleToken, can_reveal: false },
    ]);
    mockRevealToken.mockResolvedValue({
      token_id: "tok-1",
      id_prefix: "abc123def4567890",
      label: "CI pipeline",
      status: "active",
      assigned_chain_id: null,
      secret: "gz-abc123def4567890-existing-secret",
    });

    renderWithProviders(<TokensPage />);

    await screen.findByText("CI pipeline");
    await user.click(
      screen.getByRole("button", { name: /reveal api key for ci pipeline/i }),
    );

    const revealDialog = screen.getByRole("dialog");
    expect(
      within(revealDialog).getByText(/created before encrypted reveal storage/i),
    ).toBeInTheDocument();
    const submit = within(revealDialog).getByRole("button", {
      name: "Save and reveal key",
    });
    expect(submit).toBeDisabled();

    await user.type(
      within(revealDialog).getByLabelText("Existing API key"),
      "gz-abc123def4567890-existing-secret",
    );
    await user.type(within(revealDialog).getByLabelText("Your password"), "password");
    await user.click(submit);

    await waitFor(() => {
      expect(mockRevealToken).toHaveBeenCalledWith(
        "tok-1",
        "password",
        "gz-abc123def4567890-existing-secret",
      );
    });
    expect(await screen.findByText("API key ready")).toBeInTheDocument();
  });
});
