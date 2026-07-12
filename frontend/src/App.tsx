import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { AuthProvider } from "./auth/AuthContext";
import { AppLayout } from "./components/AppLayout";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { AccountsPage } from "./pages/AccountsPage";
import { AnalyticsPage } from "./pages/AnalyticsPage";
import { ChainsPage } from "./pages/ChainsPage";
import { DashboardPage } from "./pages/DashboardPage";
import { DocumentationPage } from "./pages/DocumentationPage";
import { LoginPage } from "./pages/LoginPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { TokensPage } from "./pages/TokensPage";
import { TracesPage } from "./pages/TracesPage";
import { ROUTES } from "./routes";

/**
 * Application root: wires the auth provider and the route table. All paths come
 * from the {@link ROUTES} single source of truth. Authenticated areas sit behind
 * {@link ProtectedRoute} and render inside the persistent {@link AppLayout} shell
 * (sidebar + top bar); each console view supplies the content.
 */
export function App(): JSX.Element {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path={ROUTES.login} element={<LoginPage />} />
          <Route element={<ProtectedRoute />}>
            <Route element={<AppLayout />}>
              <Route path={ROUTES.dashboard} element={<DashboardPage />} />
              <Route path={ROUTES.accounts} element={<AccountsPage />} />
              <Route path={ROUTES.tokens} element={<TokensPage />} />
              <Route path={ROUTES.chains} element={<ChainsPage />} />
              <Route path={ROUTES.traces} element={<TracesPage />} />
              <Route path={ROUTES.analytics} element={<AnalyticsPage />} />
              <Route path={ROUTES.docs} element={<DocumentationPage />} />
            </Route>
          </Route>
          <Route path="*" element={<NotFoundPage />} />
          <Route path="" element={<Navigate to={ROUTES.dashboard} replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  );
}
