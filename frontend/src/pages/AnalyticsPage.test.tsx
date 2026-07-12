import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AnalyticsPage } from "./AnalyticsPage";
import { RangeSelector } from "./analytics/RangeSelector";
import { ApiError } from "../api/errors";
import { renderWithProviders, pending } from "../test/render";
import type {
  AccountResponse,
  SystemAnalyticsResponse,
  TokenResponse,
} from "../api/types";
import type { AnalyticsRange } from "../api/analytics";

// Analytics_Service and the subject lists are mocked so the reports run offline.
vi.mock("../api/analytics", () => ({
  systemReport: vi.fn(),
  tokenReport: vi.fn(),
  accountReport: vi.fn(),
}));

vi.mock("../api/tokens", () => ({
  listTokens: vi.fn(),
}));

vi.mock("../api/accounts", () => ({
  listAccounts: vi.fn(),
}));

import { systemReport } from "../api/analytics";
import { listTokens } from "../api/tokens";
import { listAccounts } from "../api/accounts";

const mockSystemReport = vi.mocked(systemReport);
const mockListTokens = vi.mocked(listTokens);
const mockListAccounts = vi.mocked(listAccounts);

const systemData: SystemAnalyticsResponse = {
  range: { start: "2024-01-01T00:00:00Z", end: "2024-01-08T00:00:00Z" },
  request_count: 0,
  error_count: 0,
  error_rate: 0,
  total_tokens: 0,
};

describe("AnalyticsPage async states (Requirement 17.4)", () => {
  beforeEach(() => {
    mockSystemReport.mockReset();
    mockListTokens.mockReset();
    mockListAccounts.mockReset();
    mockListTokens.mockResolvedValue([] as ReadonlyArray<TokenResponse>);
    mockListAccounts.mockResolvedValue([] as ReadonlyArray<AccountResponse>);
  });

  it("renders the explicit loading state while the report is pending", async () => {
    mockSystemReport.mockReturnValue(pending<SystemAnalyticsResponse>());

    renderWithProviders(<AnalyticsPage />);

    expect(await screen.findByText("Loading report...")).toBeInTheDocument();
  });

  it("renders an empty state when a scope has no subjects to report on", async () => {
    mockSystemReport.mockResolvedValue(systemData);

    renderWithProviders(<AnalyticsPage />);

    await userEvent.click(screen.getByRole("tab", { name: "API key" }));

    expect(
      await screen.findByText("No API keys to report on yet."),
    ).toBeInTheDocument();
  });

  it("renders the error state with a retry control when the report fails", async () => {
    mockSystemReport.mockRejectedValue(
      new ApiError(500, "SERVER_ERROR", "Could not load report."),
    );

    renderWithProviders(<AnalyticsPage />);

    expect(await screen.findByText("Could not load report.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("renders guide copy and derived system KPIs", async () => {
    mockSystemReport.mockResolvedValue({
      ...systemData,
      request_count: 4,
      error_count: 1,
      error_rate: 0.25,
      total_tokens: 100,
    });

    renderWithProviders(<AnalyticsPage />);

    expect(await screen.findByText("Usage review")).toBeInTheDocument();
    expect(screen.getByText("Successful requests")).toBeInTheDocument();
    expect(screen.getByText("Avg tokens / request")).toBeInTheDocument();
    expect(screen.getByText("25")).toBeInTheDocument();
  });
});

describe("Analytics range is required and validated (Requirement 17.4)", () => {
  function noop(): void {
    // intentionally empty
  }

  it("marks both range bounds as required", () => {
    renderWithProviders(
      <RangeSelector
        startInput=""
        endInput=""
        onStartChange={noop}
        onEndChange={noop}
        onApply={vi.fn()}
      />,
    );

    expect(screen.getByLabelText("Start")).toBeRequired();
    expect(screen.getByLabelText("End")).toBeRequired();
  });

  it("rejects a non-increasing range and does not apply it", async () => {
    const onApply = vi.fn<(range: AnalyticsRange) => void>();
    renderWithProviders(
      <RangeSelector
        startInput="2024-06-02T10:00"
        endInput="2024-06-01T10:00"
        onStartChange={noop}
        onEndChange={noop}
        onApply={onApply}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Apply range" }));

    expect(
      await screen.findByText("The start time must be before the end time."),
    ).toBeInTheDocument();
    expect(onApply).not.toHaveBeenCalled();
  });

  it("applies a valid increasing range", async () => {
    const onApply = vi.fn<(range: AnalyticsRange) => void>();
    renderWithProviders(
      <RangeSelector
        startInput="2024-06-01T10:00"
        endInput="2024-06-02T10:00"
        onStartChange={noop}
        onEndChange={noop}
        onApply={onApply}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Apply range" }));

    expect(onApply).toHaveBeenCalledTimes(1);
  });
});
