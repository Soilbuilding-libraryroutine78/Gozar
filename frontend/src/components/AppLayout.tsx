import { useEffect, useState, type ComponentType } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { ROUTES } from "../routes";
import {
  AccountsIcon,
  AnalyticsIcon,
  ChainIcon,
  DashboardIcon,
  DocsIcon,
  MenuIcon,
  ShieldIcon,
  SignOutIcon,
  TokenIcon,
  TracesIcon,
} from "./icons";

interface IconProps {
  readonly size?: number;
}

interface NavItem {
  readonly to: string;
  readonly label: string;
  readonly Icon: ComponentType<IconProps>;
  /** Match the path exactly (used for the index "/" route). */
  readonly end?: boolean;
}

/**
 * Primary navigation, ordered as it appears in the sidebar. Every target comes
 * from the {@link ROUTES} single source of truth so paths only change in one place.
 */
const NAV_ITEMS: ReadonlyArray<NavItem> = [
  { to: ROUTES.dashboard, label: "Dashboard", Icon: DashboardIcon, end: true },
  { to: ROUTES.accounts, label: "Accounts", Icon: AccountsIcon },
  { to: ROUTES.tokens, label: "API keys", Icon: TokenIcon },
  { to: ROUTES.chains, label: "Chains", Icon: ChainIcon },
  { to: ROUTES.traces, label: "Traces", Icon: TracesIcon },
  { to: ROUTES.analytics, label: "Analytics", Icon: AnalyticsIcon },
  { to: ROUTES.docs, label: "Docs", Icon: DocsIcon },
];

/** Resolve the title shown in the top bar for the active route. */
function titleFor(pathname: string): string {
  // Longest non-index match wins so "/accounts" beats the "/" dashboard entry.
  const match = NAV_ITEMS.filter((item) => !item.end).find((item) =>
    pathname.startsWith(item.to),
  );
  if (match) {
    return match.label;
  }
  return "Dashboard";
}

/**
 * Authenticated application shell: a persistent left sidebar (brand, primary
 * navigation, and sign out) and a slim top bar showing the current page title.
 * The page content renders into the right-hand area through the router
 * {@link Outlet}.
 *
 * Responsive: on narrow viewports the sidebar collapses into an accessible drawer
 * toggled by the top-bar menu button, with a backdrop that closes it. Route
 * changes gently fade the content in (disabled under prefers-reduced-motion).
 */
export function AppLayout(): JSX.Element {
  const { signOut } = useAuth();
  const location = useLocation();
  const [drawerOpen, setDrawerOpen] = useState(false);

  // Close the mobile drawer whenever the route changes.
  useEffect(() => {
    setDrawerOpen(false);
  }, [location.pathname]);

  // Close the drawer on Escape for keyboard users.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent): void {
      if (event.key === "Escape") {
        setDrawerOpen(false);
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  const title = titleFor(location.pathname);

  return (
    <div className={`shell${drawerOpen ? " shell--drawer-open" : ""}`}>
      {drawerOpen && (
        <button
          type="button"
          className="shell__scrim"
          aria-label="Close navigation"
          onClick={() => setDrawerOpen(false)}
        />
      )}

      <aside className="sidebar" id="primary-navigation">
        <div className="sidebar__brand">
          <ShieldIcon size={26} aria-hidden />
          <span className="sidebar__wordmark">Gozar</span>
        </div>

        <nav className="sidebar__nav" aria-label="Primary">
          {NAV_ITEMS.map(({ to, label, Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end ?? false}
              className={({ isActive }) =>
                isActive ? "navlink navlink--active" : "navlink"
              }
            >
              <Icon size={20} />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>

        <div className="sidebar__footer">
          <button type="button" className="navlink navlink--action" onClick={signOut}>
            <SignOutIcon size={20} />
            <span>Sign out</span>
          </button>
        </div>
      </aside>

      <div className="shell__main">
        <header className="topbar">
          <button
            type="button"
            className="topbar__menu icon-button"
            aria-label="Open navigation"
            aria-controls="primary-navigation"
            aria-expanded={drawerOpen}
            onClick={() => setDrawerOpen((open) => !open)}
          >
            <MenuIcon size={20} />
          </button>
          <h1 className="topbar__title">{title}</h1>
        </header>

        <main className="content">
          <div className="content__inner" key={location.pathname}>
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
