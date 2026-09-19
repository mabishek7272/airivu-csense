import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { useBrand } from "../branding/BrandProvider";
import { SupportGrantBanner } from "./SupportGrantBanner";
import {
  AuditIcon,
  CameraIcon,
  DashboardIcon,
  DetectionIcon,
  EdgeIcon,
  IncidentIcon,
  NotifyIcon,
  PipelineIcon,
  RecipientIcon,
  ResellerIcon,
  RuleIcon,
  SearchIcon,
  SettingsIcon,
  SiteIcon,
  TeamIcon,
  WebhookIcon,
  ZoneIcon,
} from "./Icons";

/** Grouped sidebar nav (design/csense-ui/Main.dc.html) - Operations / Infrastructure /
 *  Intelligence / Alerting, replacing a flat top nav. Grouping isn't cosmetic: it's the
 *  same order an operator actually works through a site - watch queue, then the
 *  infrastructure feeding it, then the intelligence driving detections, then who gets
 *  told. `Icons.tsx`'s `webhook`/`team`/`reseller` entries aren't in the design system's
 *  own 24-icon sheet (System.dc.html §03 didn't need them - "Webhooks"/"Team"/"Child
 *  tenants" are CRM-specific pages that sheet never enumerated) - drawn in the same
 *  20px/1.4-stroke style to extend the set consistently rather than reach for a
 *  mismatched icon elsewhere. */
const NAV_GROUPS: { label: string; items: { to: string; label: string; Icon: typeof DashboardIcon }[] }[] = [
  {
    label: "Operations",
    items: [
      { to: "/dashboard", label: "Dashboard", Icon: DashboardIcon },
      { to: "/incidents", label: "Incidents", Icon: IncidentIcon },
      { to: "/detections", label: "Detections", Icon: DetectionIcon },
    ],
  },
  {
    label: "Infrastructure",
    items: [
      { to: "/sites", label: "Sites", Icon: SiteIcon },
      { to: "/zones", label: "Zones", Icon: ZoneIcon },
      { to: "/cameras", label: "Cameras", Icon: CameraIcon },
      { to: "/edge", label: "Edge", Icon: EdgeIcon },
    ],
  },
  {
    label: "Intelligence",
    items: [
      { to: "/pipelines", label: "Pipelines", Icon: PipelineIcon },
      { to: "/rules", label: "Rules", Icon: RuleIcon },
    ],
  },
  {
    label: "Alerting",
    items: [
      { to: "/recipient-groups", label: "Recipients", Icon: RecipientIcon },
      { to: "/notification-policies", label: "Notifications", Icon: NotifyIcon },
      { to: "/webhooks", label: "Webhooks", Icon: WebhookIcon },
    ],
  },
  {
    label: "Administration",
    items: [
      { to: "/team", label: "Team", Icon: TeamIcon },
      { to: "/child-tenants/rollup", label: "Child tenants", Icon: ResellerIcon },
      { to: "/audit", label: "Audit", Icon: AuditIcon },
      { to: "/settings", label: "Settings", Icon: SettingsIcon },
    ],
  },
];

export function Layout({ children }: { children: ReactNode }) {
  const { logout, tenantId } = useAuth();
  const brand = useBrand();

  return (
    <div className="app-shell">
      {/* Keyboard users should not have to tab through the whole nav on every page. */}
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <aside className="app-sidebar">
        <div className="app-brand">
          {brand.isDefaultBrand ? (
            <>
              <svg width="26" height="26" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path d="M12 2.5 20.5 7v10L12 21.5 3.5 17V7L12 2.5Z" stroke="#98134E" strokeWidth="1.4" fill="rgba(152,19,78,.18)" />
                <circle cx="12" cy="12" r="3.4" stroke="#FF8ABB" strokeWidth="1.4" />
                <circle cx="12" cy="12" r="1.1" fill="#FF8ABB" />
                <path d="M12 5.6v2.4M12 16v2.4M6.6 9v6M17.4 9v6" stroke="#98134E" strokeWidth="1.2" strokeLinecap="round" />
              </svg>
              <div>
                AIRIVU
                <span>CSENSE</span>
              </div>
            </>
          ) : (
            <>
              {/* A configured brand with no logo uploaded yet is a real, valid state
                  (colors/name set, logo pending) - it must never fall back to showing
                  AIRIVU's own mark, which is exactly the leak white-label branding
                  exists to prevent. Text-only wordmark until a logo exists. */}
              {brand.logoUrl && (
                <img src={brand.logoUrl} alt="" width={26} height={26} style={{ objectFit: "contain" }} />
              )}
              <div>{brand.displayName}</div>
            </>
          )}
        </div>

        {/* Layout only ever renders once RequireAuth has let a page through, so this
            never fetches on the unauthenticated login/accept-invitation screens. */}
        <SupportGrantBanner />

        <nav className="app-nav" aria-label="Main">
          {NAV_GROUPS.map((group) => (
            <div key={group.label}>
              <div className="nav-group">{group.label}</div>
              {group.items.map(({ to, label, Icon }) => (
                <NavLink key={to} className="nav-item" to={to}>
                  <Icon size={16} />
                  {label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>

        <div className="app-sidebar-footer">
          <span className="mono" style={{ fontSize: 10, color: "var(--faint)" }} title="Tenant">
            {tenantId ? `${tenantId.slice(0, 8)}…` : ""}
          </span>
          <button type="button" onClick={logout}>
            Sign out
          </button>
        </div>
      </aside>

      <div className="app-content">
        <header className="app-topbar">
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span className="led" style={{ background: "var(--emerald)", color: "var(--emerald)" }} />
            <span className="mono" style={{ fontSize: 10, letterSpacing: "0.1em", color: "var(--emerald)" }}>
              LIVE
            </span>
          </div>
          <div
            className="card"
            style={{
              marginLeft: "auto",
              width: 280,
              display: "flex",
              alignItems: "center",
              gap: 9,
              padding: "7px 11px",
              background: "#181215",
            }}
          >
            <SearchIcon size={14} style={{ color: "var(--faint)" }} />
            <span style={{ color: "var(--faint)", fontSize: 12 }}>Search incidents, cameras…</span>
          </div>
        </header>

        <main className="app-main" id="main">
          {children}
        </main>
      </div>
    </div>
  );
}

/** Renders a timestamp in the site's own timezone, not the browser's.
 *
 *  An operator in one country reviewing a site in another needs to know when it happened
 *  *there* — "02:14" at the site is the meaningful fact for an overnight intrusion, and
 *  showing it converted to the viewer's local time quietly destroys that. The zone is
 *  always labelled so the reading is unambiguous.
 */
export function SiteTime({ iso, timezone }: { iso: string; timezone?: string }) {
  const date = new Date(iso);
  let formatted: string;
  try {
    formatted = new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "medium",
      timeZone: timezone,
    }).format(date);
  } catch {
    // An unknown IANA zone must not blank the timestamp entirely.
    formatted = date.toISOString();
  }

  return (
    <time dateTime={iso} title={iso}>
      {formatted}
      {timezone ? <span style={{ color: "var(--text-muted)" }}> ({timezone})</span> : null}
    </time>
  );
}

export function relativeTime(iso: string): string {
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ["second", 60],
    ["minute", 60],
    ["hour", 24],
    ["day", 7],
    ["week", 4.35],
    ["month", 12],
    ["year", Infinity],
  ];

  let value = seconds;
  for (const [unit, step] of units) {
    if (Math.abs(value) < step) {
      return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(
        -Math.round(value),
        unit,
      );
    }
    value /= step;
  }
  return iso;
}

