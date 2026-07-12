import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AccountsPage } from "./AccountsPage";
import { ApiError } from "../api/errors";
import { renderWithProviders, pending } from "../test/render";
import type { AccountResponse } from "../api/types";

// The Account_Manager API layer is a thin typed wrapper; mock it so the view can be
// driven through loading/empty/error/success with no network.
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

import { listAccounts } from "../api/accounts";

const mockListAccounts = vi.mocked(listAccounts);

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

describe("AccountsPage async states (Requirement 17.1)", () => {
  beforeEach(() => {
    mockListAccounts.mockReset();
  });

  it("renders the explicit loading state while accounts are pending", () => {
    mockListAccounts.mockReturnValue(pending<ReadonlyArray<AccountResponse>>());

    renderWithProviders(<AccountsPage />);

    expect(screen.getByText("Loading accounts...")).toBeInTheDocument();
  });

  it("renders the empty state when no accounts are connected", async () => {
    mockListAccounts.mockResolvedValue([]);

    renderWithProviders(<AccountsPage />);

    expect(await screen.findByText("No accounts connected yet.")).toBeInTheDocument();
  });

  it("renders the error state with a retry control when the load fails", async () => {
    mockListAccounts.mockRejectedValue(
      new ApiError(500, "SERVER_ERROR", "Could not load accounts."),
    );

    renderWithProviders(<AccountsPage />);

    expect(await screen.findByText("Could not load accounts.")).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: "Retry" });
    expect(retry).toBeInTheDocument();

    // Retry re-issues the load; a success on the second call clears the error.
    mockListAccounts.mockResolvedValueOnce([sampleAccount]);
    await userEvent.click(retry);

    expect(await screen.findByText("Primary OpenAI")).toBeInTheDocument();
  });
});

describe("Account connect required fields (Requirement 17.1)", () => {
  beforeEach(() => {
    mockListAccounts.mockReset();
    mockListAccounts.mockResolvedValue([]);
  });

  it("requires an API key before the connect action is enabled", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AccountsPage />);

    await screen.findByText("No accounts connected yet.");
    await user.click(screen.getByRole("button", { name: /connect account/i }));

    // The dialog's submit is disabled until the required API key is provided.
    const submit = screen.getByRole("button", { name: "Connect" });
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText("API key"), "sk-secret-value");
    expect(submit).toBeEnabled();
  });
});
