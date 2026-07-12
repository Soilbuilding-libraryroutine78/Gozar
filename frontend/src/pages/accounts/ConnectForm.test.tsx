import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ConnectForm } from "./ConnectForm";
import { renderWithProviders } from "../../test/render";
import type {
  AuthorizationChallengeResponse,
  CredentialSummaryResponse,
  DeviceAuthorizationChallengeResponse,
  DeviceAuthorizationCompleteResponse,
} from "../../api/types";

// The Account_Manager API layer is a thin typed wrapper; mock it so the connect
// form can be driven through the begin -> paste -> complete steps with no network.
vi.mock("../../api/accounts", () => ({
  connectApiKey: vi.fn(),
  beginSubscriptionDeviceConnect: vi.fn(),
  beginSubscriptionConnect: vi.fn(),
  completeSubscriptionDeviceConnect: vi.fn(),
  completeSubscriptionConnect: vi.fn(),
}));

import {
  beginSubscriptionDeviceConnect,
  beginSubscriptionConnect,
  completeSubscriptionDeviceConnect,
  completeSubscriptionConnect,
} from "../../api/accounts";

const mockBegin = vi.mocked(beginSubscriptionConnect);
const mockComplete = vi.mocked(completeSubscriptionConnect);
const mockBeginDevice = vi.mocked(beginSubscriptionDeviceConnect);
const mockCompleteDevice = vi.mocked(completeSubscriptionDeviceConnect);

const challenge: AuthorizationChallengeResponse = {
  pending_id: "pending-123",
  authorize_url: "https://auth.openai.com/oauth/authorize?client_id=abc&state=xyz",
  state: "xyz",
};

const summary: CredentialSummaryResponse = {
  account_id: "acct-1",
  provider: "codex",
  kind: "subscription",
  label: "codex",
  status: "active",
};

const deviceChallenge: DeviceAuthorizationChallengeResponse = {
  pending_id: "device-pending-123",
  verification_url: "https://auth.openai.com/codex/device",
  user_code: "ABCD-EFGH",
  interval_seconds: 30,
};

describe("ConnectForm subscription paste step (domain-independent flow)", () => {
  beforeEach(() => {
    mockBegin.mockReset();
    mockComplete.mockReset();
    mockBeginDevice.mockReset();
    mockCompleteDevice.mockReset();
  });

  it("begins the connect, shows the authorize URL, then completes with the pasted value", async () => {
    const user = userEvent.setup();
    mockBegin.mockResolvedValue(challenge);
    mockComplete.mockResolvedValue(summary);
    const onConnected = vi.fn();

    renderWithProviders(<ConnectForm onConnected={onConnected} />);

    // Switch to the subscription method and start authorization.
    await user.click(screen.getByRole("tab", { name: /subscription/i }));
    await user.click(screen.getByRole("button", { name: /use redirect url/i }));

    // The authorize URL is shown (read-only) for the operator to open/copy.
    const urlField = await screen.findByLabelText("Authorization URL");
    expect(urlField).toHaveValue(challenge.authorize_url);

    // The single paste input drives completion; state is not requested from the user.
    const pasteInput = screen.getByLabelText(/paste the redirect url/i);
    expect(screen.queryByLabelText("State")).not.toBeInTheDocument();

    await user.type(
      pasteInput,
      "http://localhost:1455/auth/callback?code=the-code&state=xyz",
    );
    await user.click(screen.getByRole("button", { name: /finish connecting/i }));

    // Complete is called with the pasted value as `code`; state is omitted (the
    // backend extracts it), keeping the flow independent of the console origin.
    expect(mockComplete).toHaveBeenCalledWith({
      pending_id: "pending-123",
      code: "http://localhost:1455/auth/callback?code=the-code&state=xyz",
      label: null,
    });
    expect(onConnected).toHaveBeenCalledTimes(1);
  });

  it("keeps the finish action disabled until something is pasted", async () => {
    const user = userEvent.setup();
    mockBegin.mockResolvedValue(challenge);

    renderWithProviders(<ConnectForm onConnected={vi.fn()} />);

    await user.click(screen.getByRole("tab", { name: /subscription/i }));
    await user.click(screen.getByRole("button", { name: /use redirect url/i }));

    await screen.findByLabelText("Authorization URL");
    const finish = screen.getByRole("button", { name: /finish connecting/i });
    expect(finish).toBeDisabled();

    await user.type(screen.getByLabelText(/paste the redirect url/i), "bare-code");
    expect(finish).toBeEnabled();
  });

  it("uses Codex device-code sign-in by default and finishes after approval", async () => {
    const user = userEvent.setup();
    mockBeginDevice.mockResolvedValue(deviceChallenge);
    mockCompleteDevice.mockResolvedValue({
      status: "connected",
      account: summary,
    } satisfies DeviceAuthorizationCompleteResponse);
    const onConnected = vi.fn();

    renderWithProviders(<ConnectForm onConnected={onConnected} />);

    await user.click(screen.getByRole("tab", { name: /subscription/i }));
    await user.click(screen.getByRole("button", { name: /start device sign-in/i }));

    expect(await screen.findByLabelText("Verification page")).toHaveValue(
      deviceChallenge.verification_url,
    );
    expect(screen.getByLabelText("One-time code")).toHaveValue(
      deviceChallenge.user_code,
    );

    await user.click(screen.getByRole("button", { name: /check status/i }));

    expect(mockCompleteDevice).toHaveBeenCalledWith({
      pending_id: "device-pending-123",
      label: null,
    });
    expect(onConnected).toHaveBeenCalledTimes(1);
  });
});
