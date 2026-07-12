import { Link } from "react-router-dom";

import { ROUTES } from "../routes";

/** Fallback for unknown routes. */
export function NotFoundPage(): JSX.Element {
  return (
    <main className="centered">
      <div className="centered__content">
        <h1>Page not found</h1>
        <p>The page you requested does not exist.</p>
        <Link className="button button--primary" to={ROUTES.dashboard}>
          Back to dashboard
        </Link>
      </div>
    </main>
  );
}
