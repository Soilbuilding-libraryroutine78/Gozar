import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { LoginPage } from "./LoginPage";
import { renderWithProviders, pending } from "../test/render";
import type { BootstrapStatus, SessionTokens } from "../api/types";

// The auth API layer is mocked so the page can be driven through the first-run
// bootstrap gate (GET /api/auth/bootstrap) without a backend. The AuthProvider
// calls login/bootstrapCreate through this same module.
vi.mock("../api/auth", () => ({
  login: vi.fn(),
  refresh: vi.fn(),
  bootstrapStatus: vi.fn(),
  bootstrapCreate: vi.fn(),
}));

import { bootstrapStatus, bootstrapCreate } from "../api/auth";

const mockBootstrapStatus = vi.mocked(bootstrapStatus);
const mockBootstrapCreate = vi.mocked(bootstrapCreate);

const session: SessionTokens = {
  access_token: "access",
  refresh_token: "refresh",
  token_type: "bearer",
  expires_in: 3600,
};

describe("LoginPage bootstrap gate (first-run setup)", () => {
  beforeEach(() => {
    mockBootstrapStatus.mockReset();
    mockBootstrapCreate.mockReset();
  });

  it("shows the checking state while the bootstrap status is pending", () => {
    mockBootstrapStatus.mockReturnValue(pending<BootstrapStatus>());

    renderWithProviders(<LoginPage />);

    expect(screen.getByText("Checking setup...")).toBeInTheDocument();
  });

  it("shows the login form when bootstrap is not required", async () => {
    mockBootstrapStatus.mockResolvedValue({ bootstrap_required: false });

    renderWithProviders(<LoginPage />);

    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Create administrator" }),
    ).not.toBeInTheDocument();
  });

  it("shows the create-first-administrator form when bootstrap is required", async () => {
    mockBootstrapStatus.mockResolvedValue({ bootstrap_required: true });

    renderWithProviders(<LoginPage />);

    expect(
      await screen.findByRole("button", { name: "Create administrator" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Confirm password")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign in" })).not.toBeInTheDocument();
  });

  it("falls back to the login form (with a notice) when the status check fails", async () => {
    mockBootstrapStatus.mockRejectedValue(new Error("network"));

    renderWithProviders(<LoginPage />);

    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(
      screen.getByText("Could not verify setup status. You can still sign in below."),
    ).toBeInTheDocument();
  });

  it("creates the first administrator and submits the bootstrap credentials", async () => {
    const user = userEvent.setup();
    mockBootstrapStatus.mockResolvedValue({ bootstrap_required: true });
    mockBootstrapCreate.mockResolvedValue(session);

    renderWithProviders(<LoginPage />);

    const submit = await screen.findByRole("button", { name: "Create administrator" });
    // Disabled until a username and matching passwords are present.
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText("Username"), "admin");
    await user.type(screen.getByLabelText("Password"), "Sup3r$ecretPwd!");
    await user.type(screen.getByLabelText("Confirm password"), "Sup3r$ecretPwd!");

    expect(submit).toBeEnabled();
    await user.click(submit);

    expect(mockBootstrapCreate).toHaveBeenCalledWith({
      username: "admin",
      password: "Sup3r$ecretPwd!",
    });
  });

  it("keeps the create action disabled while the passwords do not match", async () => {
    const user = userEvent.setup();
    mockBootstrapStatus.mockResolvedValue({ bootstrap_required: true });

    renderWithProviders(<LoginPage />);

    await screen.findByRole("button", { name: "Create administrator" });
    await user.type(screen.getByLabelText("Username"), "admin");
    await user.type(screen.getByLabelText("Password"), "Sup3r$ecretPwd!");
    await user.type(screen.getByLabelText("Confirm password"), "different");

    expect(screen.getByRole("button", { name: "Create administrator" })).toBeDisabled();
    expect(screen.getByText("The passwords do not match.")).toBeInTheDocument();
  });
});
