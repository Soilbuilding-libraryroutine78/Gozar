import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "../test/render";
import { axe } from "../test/axe";

import { LoginPage } from "./LoginPage";
import { AccountsPage } from "./AccountsPage";
import { TokensPage } from "./TokensPage";
import { ChainsPage } from "./ChainsPage";
import { TracesPage } from "./TracesPage";
import { AnalyticsPage } from "./AnalyticsPage";

import { Modal } from "./accounts/Modal";
import { ConnectForm } from "./accounts/ConnectForm";
import { LimitForm } from "./accounts/LimitForm";
import { CreateTokenForm } from "./tokens/CreateTokenForm";
import { SecretReveal } from "./tokens/SecretReveal";
import { ChainEditor } from "./chains/ChainEditor";
import { indexAccounts } from "./chains/format";
import { RangeSelector } from "./analytics/RangeSelector";
import { TraceDetail } from "./traces/TraceDetail";

import type {
  AccountResponse,
  ChainResponse,
  IssuedTokenResponse,
  SystemAnalyticsResponse,
  TokenResponse,
  TraceDetailResponse,
  TraceSummaryResponse,
} from "../api/types";

// Automated accessibility (axe) checks for the interactive console views toward
// WCAG 2.1 AA (Requirement 17.5). Each view is rendered in a populated/idle state
// with the typed API layer mocked (reusing the per-test mocking pattern and the
// renderWithProviders helper from the 16.6 component tests) so the DOM is
// deterministic, then asserted to have no detectable axe violations.
//
// NOTE: axe covers only the machine-detectable subset of WCAG 2.1 AA. Full Level AA
// conformance additionally requires manual assistive-technology review (screen
// readers, keyboard-only navigation, focus order, zoom/reflow) which automation
// alone cannot prove.

// --- Mock the typed API layer so views render without a backend. ---------------
vi.mock("../api/accounts", () => ({
  listAccounts: vi.fn(),
  deleteAccount: vi.fn(),
  setAccountEnabled: vi.fn(),
  setAccountLimit: vi.fn(),
  connectApiKey: vi.fn(),
  beginSubscriptionDeviceConnect: vi.fn(),
  beginSubscriptionConnect: vi.fn(),
  completeSubscriptionDeviceConnect: vi.fn(),
  completeSubscriptionConnect: vi.fn(),
}));

vi.mock("../api/tokens", () => ({
  listTokens: vi.fn(),
  listTokenModels: vi.fn(),
  createToken: vi.fn(),
  revealToken: vi.fn(),
  setTokenLimit: vi.fn(),
  setTokenChain: vi.fn(),
  setTokenEnabled: vi.fn(),
  revokeToken: vi.fn(),
}));

vi.mock("../api/chains", () => ({
  listChains: vi.fn(),
  getChain: vi.fn(),
  createChain: vi.fn(),
  editChain: vi.fn(),
  deleteChain: vi.fn(),
}));

vi.mock("../api/traces", () => ({
  listTraces: vi.fn(),
  getTrace: vi.fn(),
}));

vi.mock("../api/analytics", () => ({
  systemReport: vi.fn(),
  tokenReport: vi.fn(),
  accountReport: vi.fn(),
}));

import { listAccounts } from "../api/accounts";
import { listTokenModels, listTokens } from "../api/tokens";
import { listChains } from "../api/chains";
import { listTraces, getTrace } from "../api/traces";
import { systemReport } from "../api/analytics";

const mockListAccounts = vi.mocked(listAccounts);
const mockListTokens = vi.mocked(listTokens);
const mockListTokenModels = vi.mocked(listTokenModels);
const mockListChains = vi.mocked(listChains);
const mockListTraces = vi.mocked(listTraces);
const mockGetTrace = vi.mocked(getTrace);
const mockSystemReport = vi.mocked(systemReport);

// --- Representative populated data. --------------------------------------------
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

const token: TokenResponse = {
  token_id: "tok-1",
  id_prefix: "abc123def4567890",
  label: "CI pipeline",
  status: "active",
  limit: null,
  usage: 0,
};

const chain: ChainResponse = {
  chain_id: "chain-1",
  name: "Primary failover",
  model_selector: null,
  entries: [
    { account_id: "acct-1", position: 0, model: "gpt-5.4-mini", route: "chat" },
  ],
};

const traceSummary: TraceSummaryResponse = {
  correlation_id: "corr-1",
  started_at: "2024-01-01T00:00:00Z",
  ended_at: "2024-01-01T00:00:01Z",
  outcome: "success",
  status_code: 200,
  account_id: "acct-1",
  elapsed_seconds: 1.2,
};

const traceDetail: TraceDetailResponse = {
  ...traceSummary,
  inbound_meta: { model: "gpt-4o", stream: false },
  outbound_meta: { provider: "openai", status: "ok" },
};

const systemData: SystemAnalyticsResponse = {
  range: { start: "2024-01-01T00:00:00Z", end: "2024-01-08T00:00:00Z" },
  request_count: 128,
  error_count: 3,
  error_rate: 0.0234,
  total_tokens: 45000,
};

const issuedToken: IssuedTokenResponse = {
  token_id: "tok-1",
  id_prefix: "gzr_ab12",
  label: "CI pipeline",
  status: "active",
  secret: "gzr_ab12_secret-shown-once",
};

beforeEach(() => {
  mockListAccounts.mockReset();
  mockListTokens.mockReset();
  mockListTokenModels.mockReset();
  mockListChains.mockReset();
  mockListTraces.mockReset();
  mockGetTrace.mockReset();
  mockSystemReport.mockReset();
  mockListTokenModels.mockResolvedValue({
    object: "list",
    data: [{ id: "gpt-5.5", object: "model", owned_by: "codex" }],
  });
});

describe("Console views have no detectable axe violations (Requirement 17.5)", () => {
  it("Login view (idle)", async () => {
    const { container } = renderWithProviders(<LoginPage />);

    await screen.findByRole("button", { name: "Sign in" });
    expect(await axe(container)).toHaveNoViolations();
  });

  it("Accounts view (populated)", async () => {
    mockListAccounts.mockResolvedValue([account]);

    const { container } = renderWithProviders(<AccountsPage />);

    await screen.findByText("Primary OpenAI");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("Tokens view (populated)", async () => {
    mockListTokens.mockResolvedValue([token]);
    mockListChains.mockResolvedValue([chain]);

    const { container } = renderWithProviders(<TokensPage />);

    await screen.findByText("CI pipeline");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("Chains view (populated)", async () => {
    mockListChains.mockResolvedValue([chain]);
    mockListAccounts.mockResolvedValue([account]);

    const { container } = renderWithProviders(<ChainsPage />);

    await screen.findByText("Primary failover");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("Traces view (populated)", async () => {
    mockListTraces.mockResolvedValue([traceSummary]);

    const { container } = renderWithProviders(<TracesPage />);

    await screen.findByRole("button", { name: /view trace corr-1/i });
    expect(await axe(container)).toHaveNoViolations();
  });

  it("Analytics view (populated system report)", async () => {
    mockListTokens.mockResolvedValue([token]);
    mockListAccounts.mockResolvedValue([account]);
    mockSystemReport.mockResolvedValue(systemData);

    const { container } = renderWithProviders(<AnalyticsPage />);

    await screen.findByText("Requests");
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("Console modals and forms have no detectable axe violations (Requirement 17.5)", () => {
  it("Connect account dialog", async () => {
    const { container } = renderWithProviders(
      <Modal title="Connect account" onClose={() => {}}>
        <ConnectForm onConnected={() => {}} />
      </Modal>,
    );

    await screen.findByRole("dialog");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("Configure limit form (percentage metric expands capacity field)", async () => {
    const { container } = renderWithProviders(
      <Modal title="Configure limit" onClose={() => {}}>
        <LimitForm
          initial={{
            metric: "percentage",
            limit_value: 80,
            capacity: 1000,
            window: "monthly",
          }}
          submitting={false}
          error={null}
          onSubmit={() => {}}
        />
      </Modal>,
    );

    await screen.findByLabelText("Capacity");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("Create API key form", async () => {
    const { container } = renderWithProviders(
      <Modal title="Create API key" onClose={() => {}}>
        <CreateTokenForm chains={[chain]} onCreated={() => {}} />
      </Modal>,
    );

    await screen.findByLabelText("Label");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("Secret reveal", async () => {
    const { container } = renderWithProviders(
      <Modal title="API key ready" onClose={() => {}}>
        <SecretReveal issued={issuedToken} onDone={() => {}} />
      </Modal>,
    );

    await screen.findByLabelText("Gozar API key");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("Fallback chain editor (populated entries)", async () => {
    const accounts: ReadonlyArray<AccountResponse> = [account];
    const { container } = renderWithProviders(
      <Modal title="Edit chain" onClose={() => {}}>
        <ChainEditor
          initial={chain}
          accounts={accounts}
          accountsById={indexAccounts(accounts)}
          modelsByAccount={
            new Map([
              [
                "acct-1",
                { chat: ["gpt-5.4-mini"], embeddings: ["text-embedding-3-small"] },
              ],
            ])
          }
          submitting={false}
          error={null}
          onSubmit={() => {}}
        />
      </Modal>,
    );

    await screen.findByLabelText("Name");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("Analytics range selector", async () => {
    const { container } = renderWithProviders(
      <RangeSelector
        startInput="2024-06-01T10:00"
        endInput="2024-06-02T10:00"
        onStartChange={() => {}}
        onEndChange={() => {}}
        onApply={() => {}}
      />,
    );

    await screen.findByLabelText("Start");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("Trace detail (populated)", async () => {
    mockGetTrace.mockResolvedValue(traceDetail);

    const { container } = renderWithProviders(
      <Modal title="Trace detail" onClose={() => {}}>
        <TraceDetail correlationId="corr-1" />
      </Modal>,
    );

    await screen.findByText("Correlation ID");
    expect(await axe(container)).toHaveNoViolations();
  });
});
