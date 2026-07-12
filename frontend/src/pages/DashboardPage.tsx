import { Link } from "react-router-dom";
import type { ComponentType } from "react";

import {
  AccountsIcon,
  AnalyticsIcon,
  ChainIcon,
  TokenIcon,
  TracesIcon,
} from "../components/icons";
import { ROUTES } from "../routes";
import { ModelCatalogPanel } from "./dashboard/ModelCatalogPanel";
import { SetupWizard } from "./dashboard/SetupWizard";

interface IconProps {
  readonly size?: number;
}

interface Section {
  readonly title: string;
  readonly path: string;
  readonly description: string;
  readonly Icon: ComponentType<IconProps>;
}

interface WorkflowStep {
  readonly title: string;
  readonly description: string;
  readonly Icon: ComponentType<IconProps>;
}

/**
 * Authenticated landing page. Presents the console's areas as navigation cards,
 * each linking to a management view via the {@link ROUTES} single source of truth.
 * The persistent shell (sidebar + top bar) is provided by the surrounding
 * {@link AppLayout}.
 */
export function DashboardPage(): JSX.Element {
  const workflow: ReadonlyArray<WorkflowStep> = [
    {
      title: "Connect accounts",
      description: "Add the provider credentials Gozar can route through.",
      Icon: AccountsIcon,
    },
    {
      title: "Build a chain",
      description: "Order credentials in the fallback path.",
      Icon: ChainIcon,
    },
    {
      title: "Issue a key",
      description: "Give each app its own Gozar API key.",
      Icon: TokenIcon,
    },
    {
      title: "Watch traffic",
      description: "Use traces and analytics to inspect routing and usage.",
      Icon: TracesIcon,
    },
  ];

  const sections: ReadonlyArray<Section> = [
    {
      title: "Accounts",
      path: ROUTES.accounts,
      description: "Connect and manage the upstream credentials Gozar routes through.",
      Icon: AccountsIcon,
    },
    {
      title: "API keys",
      path: ROUTES.tokens,
      description: "Issue and control the Gozar API keys applications use.",
      Icon: TokenIcon,
    },
    {
      title: "Fallback chains",
      path: ROUTES.chains,
      description: "Order routing and failover across your connected accounts.",
      Icon: ChainIcon,
    },
    {
      title: "Traces",
      path: ROUTES.traces,
      description: "Inspect recent proxied requests, outcomes, and durations.",
      Icon: TracesIcon,
    },
    {
      title: "Analytics",
      path: ROUTES.analytics,
      description: "Review usage and consumption per API key, account, or system-wide.",
      Icon: AnalyticsIcon,
    },
  ];

  return (
    <>
      <div className="toolbar">
        <p className="toolbar__lead">
          Welcome back. Manage upstream credentials, app API keys, routing, and
          usage from one place.
        </p>
      </div>

      <SetupWizard />

      <ModelCatalogPanel />

      <section className="workflow-guide" aria-labelledby="dashboard-workflow-title">
        <div className="workflow-guide__head">
          <h2 id="dashboard-workflow-title">Setup flow</h2>
          <p>Connect provider credentials once, then route app traffic through Gozar API keys.</p>
        </div>
        <ol className="workflow-guide__steps">
          {workflow.map((step) => (
            <li key={step.title} className="workflow-guide__step">
              <span className="workflow-guide__icon">
                <step.Icon size={20} />
              </span>
              <span>
                <strong>{step.title}</strong>
                <span>{step.description}</span>
              </span>
            </li>
          ))}
        </ol>
      </section>

      <ul className="card-grid">
        {sections.map((section) => (
          <li key={section.path}>
            <Link className="card card--link" to={section.path}>
              <span className="card__icon">
                <section.Icon size={22} />
              </span>
              <h2 className="card__title">{section.title}</h2>
              <p className="card__body">{section.description}</p>
              <span className="card__cta">Open {section.title.toLowerCase()}</span>
            </Link>
          </li>
        ))}
      </ul>
    </>
  );
}
