import type { SVGProps } from "react";

/**
 * Minimal outline SVG icon set (steering section 21: outline icons only, no
 * emoji). Each icon inherits color via `currentColor` and is sized by the
 * `size` prop (default 20). Icons are marked `aria-hidden` by default since they
 * accompany text labels; pass an `aria-label` and `role="img"` to make one
 * meaningful on its own.
 */

interface IconProps extends Omit<SVGProps<SVGSVGElement>, "width" | "height"> {
  readonly size?: number;
}

function baseProps({ size = 20, ...rest }: IconProps): SVGProps<SVGSVGElement> {
  return {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.6,
    strokeLinecap: "round",
    strokeLinejoin: "round",
    "aria-hidden": rest["aria-label"] === undefined ? true : undefined,
    focusable: false,
    ...rest,
  };
}

/** A shield/lock mark for the brand and login screen. */
export function ShieldIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6l7-3z" />
      <path d="M9.5 12l1.8 1.8 3.2-3.6" />
    </svg>
  );
}

/** A spinning loader ring (animation via CSS class). */
export function SpinnerIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M12 3a9 9 0 1 0 9 9" />
    </svg>
  );
}

/** A small alert/exclamation circle for error states. */
export function AlertIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 8v4.5" />
      <path d="M12 16h.01" />
    </svg>
  );
}

/** An arrow leaving a doorway for sign-out. */
export function SignOutIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M15 4h3a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-3" />
      <path d="M10 17l-5-5 5-5" />
      <path d="M5 12h11" />
    </svg>
  );
}

/** A plus sign for create/connect actions. */
export function PlusIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M12 5v14" />
      <path d="M5 12h14" />
    </svg>
  );
}

/** A key for API-key credentials. */
export function KeyIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <circle cx="8" cy="15" r="4" />
      <path d="M10.8 12.2 19 4" />
      <path d="M16 7l3 3" />
      <path d="M14 9l2 2" />
    </svg>
  );
}

/** A link/chain mark for subscription (OAuth) connections. */
export function LinkIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M9 12a3 3 0 0 1 3-3h3a3 3 0 0 1 0 6h-1.5" />
      <path d="M15 12a3 3 0 0 1-3 3H9a3 3 0 0 1 0-6h1.5" />
    </svg>
  );
}

/** A trash can for delete actions. */
export function TrashIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M4 7h16" />
      <path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
      <path d="M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13" />
      <path d="M10 11v6" />
      <path d="M14 11v6" />
    </svg>
  );
}

/** A power symbol for enable/disable toggles. */
export function PowerIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M12 4v8" />
      <path d="M7.5 7a7 7 0 1 0 9 0" />
    </svg>
  );
}

/** A gauge/meter for configuring usage limits. */
export function GaugeIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M4 16a8 8 0 1 1 16 0" />
      <path d="M12 16l4-4" />
      <path d="M4 16h16" />
    </svg>
  );
}

/** A left chevron/arrow for back navigation. */
export function ArrowLeftIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M15 6l-6 6 6 6" />
    </svg>
  );
}

/** A box with an out-arrow for opening an external authorize URL. */
export function ExternalLinkIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M14 4h6v6" />
      <path d="M20 4l-8 8" />
      <path d="M18 13v5a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" />
    </svg>
  );
}

/** A circular arrow for retry/refresh. */
export function RefreshIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M20 11a8 8 0 0 0-14-4.5L4 8" />
      <path d="M4 4v4h4" />
      <path d="M4 13a8 8 0 0 0 14 4.5L20 16" />
      <path d="M20 20v-4h-4" />
    </svg>
  );
}

/** An X mark for closing panels. */
export function CloseIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M6 6l12 12" />
      <path d="M18 6 6 18" />
    </svg>
  );
}

/** A simple inbox/tray outline for empty states. */
export function InboxIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M4 13l2.5-7a1 1 0 0 1 1-.7h9a1 1 0 0 1 1 .7L20 13v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1z" />
      <path d="M4 13h4l1.5 2.5h5L16 13h4" />
    </svg>
  );
}

/** Overlapping sheets for copy-to-clipboard actions. */
export function CopyIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1" />
    </svg>
  );
}

/** A check mark to confirm an action (e.g. copied to clipboard). */
export function CheckIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M5 12.5l4.5 4.5L19 7" />
    </svg>
  );
}

/** A revoke/block mark: a circle with a diagonal bar. */
export function BanIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <circle cx="12" cy="12" r="9" />
      <path d="M5.6 5.6l12.8 12.8" />
    </svg>
  );
}

/** An up chevron for moving a list entry earlier in order. */
export function ChevronUpIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M6 15l6-6 6 6" />
    </svg>
  );
}

/** A down chevron for moving a list entry later in order. */
export function ChevronDownIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M6 9l6 6 6-6" />
    </svg>
  );
}

/** A pencil/edit mark for editing an existing record. */
export function EditIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M4 20h4l10-10a2 2 0 0 0-2.8-2.8L5 17z" />
      <path d="M13.5 6.5l4 4" />
    </svg>
  );
}

/** Three stacked links for the fallback-chain feature. */
export function ChainIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M9.5 12a3 3 0 0 1 3-3h2.5a3 3 0 0 1 0 6H13" />
      <path d="M14.5 12a3 3 0 0 1-3 3H9a3 3 0 0 1 0-6h1.5" />
    </svg>
  );
}

/** A 2x2 grid for the dashboard / overview navigation. */
export function DashboardIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <rect x="4" y="4" width="7" height="7" rx="1.5" />
      <rect x="13" y="4" width="7" height="7" rx="1.5" />
      <rect x="4" y="13" width="7" height="7" rx="1.5" />
      <rect x="13" y="13" width="7" height="7" rx="1.5" />
    </svg>
  );
}

/** Stacked server tiers for the connected accounts navigation. */
export function AccountsIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <rect x="4" y="4" width="16" height="6" rx="1.5" />
      <rect x="4" y="14" width="16" height="6" rx="1.5" />
      <path d="M7.5 7h.01" />
      <path d="M7.5 17h.01" />
    </svg>
  );
}

/** A ticket/credential mark for the API keys navigation. */
export function TokenIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M4 8a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2 2 2 0 0 0 0 4 2 2 0 0 1-2 2H6a2 2 0 0 1-2-2 2 2 0 0 0 0-4z" />
      <path d="M14 6.5v11" />
    </svg>
  );
}

/** A pulse/activity line for the request traces navigation. */
export function TracesIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M3 12h4l2-6 4 13 2-7h6" />
    </svg>
  );
}

/** A bar chart for the analytics navigation. */
export function AnalyticsIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M4 20V4" />
      <path d="M4 20h16" />
      <rect x="7.5" y="11" width="3" height="6" rx="0.5" />
      <rect x="13.5" y="7" width="3" height="10" rx="0.5" />
    </svg>
  );
}

/** An open book for product and API documentation. */
export function DocsIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H11a2 2 0 0 1 2 2v15a2 2 0 0 0-2-2H6.5A2.5 2.5 0 0 0 4 20.5z" />
      <path d="M20 5.5A2.5 2.5 0 0 0 17.5 3H13v17a2 2 0 0 1 2-2h2.5a2.5 2.5 0 0 1 2.5 2.5z" />
    </svg>
  );
}

/** A hamburger menu glyph for the responsive drawer toggle. */
export function MenuIcon(props: IconProps): JSX.Element {
  return (
    <svg {...baseProps(props)}>
      <path d="M4 7h16" />
      <path d="M4 12h16" />
      <path d="M4 17h16" />
    </svg>
  );
}
