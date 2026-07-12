import { Navigate, Outlet, useLocation } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { ROUTES } from "../routes";

/**
 * Route guard for authenticated areas. Unauthenticated operators are redirected
 * to the login screen, preserving the attempted location so login can return
 * them there afterwards.
 */
export function ProtectedRoute(): JSX.Element {
  const { isAuthenticated } = useAuth();
  const location = useLocation();

  if (!isAuthenticated) {
    return <Navigate to={ROUTES.login} replace state={{ from: location.pathname }} />;
  }

  return <Outlet />;
}
