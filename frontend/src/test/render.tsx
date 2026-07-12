import type { ReactElement } from "react";
import { render, type RenderResult } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { AuthProvider } from "../auth/AuthContext";

/**
 * Render a console view inside the providers it depends on at runtime: a router
 * (the pages use <Link> and route constants) and the auth context (the page
 * headers call `useAuth().signOut`). The API layer is mocked separately per test,
 * so nothing here touches the network.
 */
export function renderWithProviders(ui: ReactElement): RenderResult {
  return render(
    <MemoryRouter>
      <AuthProvider>{ui}</AuthProvider>
    </MemoryRouter>,
  );
}

/**
 * A promise that never settles, used to hold a mocked API call in its pending
 * state so the explicit loading view stays mounted for assertions.
 */
export function pending<T>(): Promise<T> {
  return new Promise<T>(() => {});
}
