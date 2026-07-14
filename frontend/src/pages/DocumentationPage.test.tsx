import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DocumentationPage } from "./DocumentationPage";
import { renderWithProviders } from "../test/render";


describe("DocumentationPage", () => {
  it("uses the current browser origin and documents stable chain overrides", async () => {
    const user = userEvent.setup();
    renderWithProviders(<DocumentationPage />);

    expect(
      screen.getAllByText(`${window.location.origin}/v1`, { exact: false }).length,
    ).toBeGreaterThan(0);

    await user.click(screen.getByRole("button", { name: /dynamic chains/i }));

    expect(screen.getByText("Stable, idempotent chain creation")).toBeInTheDocument();
    expect(screen.getByText(/auth_or_retryable/)).toBeInTheDocument();
    expect(screen.getByText(/X-Gozar-Chain-ID/)).toBeInTheDocument();
  });

  it("keeps LangGraph routing below llm.invoke", async () => {
    const user = userEvent.setup();
    renderWithProviders(<DocumentationPage />);

    await user.click(screen.getByRole("button", { name: /^langgraph/i }));

    expect(screen.getByText(/llm\.invoke/)).toBeInTheDocument();
    expect(screen.getAllByText(/use_responses_api/).length).toBeGreaterThan(0);

    await user.click(screen.getByRole("tab", { name: /embeddings python/i }));
    const embeddingsBlock = screen
      .getByText("OpenAI embeddings SDK through Gozar")
      .closest(".docs-code");
    expect(embeddingsBlock).toHaveTextContent("client.embeddings.create");
  });

  it("documents compatibility-safe opt-in routing metadata", async () => {
    const user = userEvent.setup();
    renderWithProviders(<DocumentationPage />);

    await user.click(screen.getByRole("button", { name: /^operations/i }));

    expect(screen.getByText("Compatible routing metadata")).toBeInTheDocument();
    expect(screen.getByText(/x-gozar-trace-id/)).toBeInTheDocument();
    expect(screen.getByText(/include_metadata/)).toBeInTheDocument();
  });
});
