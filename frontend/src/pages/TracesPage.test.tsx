import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TracesPage } from "./TracesPage";
import { ApiError } from "../api/errors";
import { renderWithProviders, pending } from "../test/render";
import type { TraceSummaryResponse } from "../api/types";

// Usage_Recorder trace API is mocked so the list runs through its states offline.
vi.mock("../api/traces", () => ({
  listTraces: vi.fn(),
  getTrace: vi.fn(),
}));

import { listTraces } from "../api/traces";

const mockListTraces = vi.mocked(listTraces);

const sampleTrace: TraceSummaryResponse = {
  correlation_id: "corr-1",
  started_at: "2024-01-01T00:00:00Z",
  ended_at: "2024-01-01T00:00:01Z",
  outcome: "success",
  status_code: 200,
  account_id: "acct-1",
  credential: {
    account_id: "acct-1",
    label: "Primary OpenAI",
    provider: "openai",
    kind: "api_key",
    status: "active",
  },
  elapsed_seconds: 1.2,
};

const errorTrace: TraceSummaryResponse = {
  correlation_id: "corr-2",
  started_at: "2024-01-01T00:01:00Z",
  ended_at: "2024-01-01T00:01:01Z",
  outcome: "client_error",
  status_code: 400,
  account_id: "acct-2",
  elapsed_seconds: 0.4,
};

describe("TracesPage async states (Requirement 17.4)", () => {
  beforeEach(() => {
    mockListTraces.mockReset();
  });

  it("renders the explicit loading state while traces are pending", () => {
    mockListTraces.mockReturnValue(pending<ReadonlyArray<TraceSummaryResponse>>());

    renderWithProviders(<TracesPage />);

    expect(screen.getByText("Loading traces...")).toBeInTheDocument();
  });

  it("renders the empty state when there are no traces", async () => {
    mockListTraces.mockResolvedValue([]);

    renderWithProviders(<TracesPage />);

    expect(await screen.findByText("No traces yet.")).toBeInTheDocument();
  });

  it("renders the error state with a retry control when the load fails", async () => {
    mockListTraces.mockRejectedValue(
      new ApiError(500, "SERVER_ERROR", "Could not load traces."),
    );

    renderWithProviders(<TracesPage />);

    expect(await screen.findByText("Could not load traces.")).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: "Retry" });
    expect(retry).toBeInTheDocument();

    mockListTraces.mockResolvedValueOnce([sampleTrace]);
    await userEvent.click(retry);

    // The populated list renders an actionable row for the trace.
    expect(
      await screen.findByRole("button", { name: /view trace corr-1/i }),
    ).toBeInTheDocument();
  });

  it("summarizes loaded traces and filters by outcome", async () => {
    const user = userEvent.setup();
    mockListTraces.mockResolvedValue([sampleTrace, errorTrace]);

    renderWithProviders(<TracesPage />);

    expect(await screen.findByText("Debug routed requests")).toBeInTheDocument();
    expect(screen.getAllByText("Needs attention")).toHaveLength(2);
    expect(screen.getByText("Primary OpenAI (openai)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /view trace corr-1/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /view trace corr-2/i })).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Outcome"), "error");

    expect(screen.queryByRole("button", { name: /view trace corr-1/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /view trace corr-2/i })).toBeInTheDocument();
  });
});
