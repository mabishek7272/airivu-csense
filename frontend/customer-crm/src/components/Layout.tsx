import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { SupportGrantBanner } from "./SupportGrantBanner";

export function Layout({ children }: { children: ReactNode }) {
  const { logout, tenantId } = useAuth();

  return (
    <div className="app-shell">
      {/* Keyboard users should not have to tab through the whole nav on every page. */}
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      {/* Layout only ever renders once RequireAuth has let a page through, so this never
          fetches on the unauthenticated login/accept-invitation screens. */}
      <SupportGrantBanner />

      <header className="app-header">
        <div className="app-brand">
          AIRIVU <span>CSense</span>
        </div>
        <nav className="app-nav" aria-label="Main">
          <NavLink to="/dashboard">Dashboard</NavLink>
          <NavLink to="/incidents">Incidents</NavLink>
          <NavLink to="/detections">Detections</NavLink>
          <NavLink to="/sites">Sites</NavLink>
          <NavLink to="/zones">Zones</NavLink>
          <NavLink to="/cameras">Cameras</NavLink>
          <NavLink to="/pipelines">Pipelines</NavLink>
          <NavLink to="/rules">Rules</NavLink>
          <NavLink to="/edge">Edge</NavLink>
          <NavLink to="/recipient-groups">Recipients</NavLink>
          <NavLink to="/notification-policies">Notifications</NavLink>
          <NavLink to="/webhooks">Webhooks</NavLink>
          <NavLink to="/team">Team</NavLink>
          <NavLink to="/audit">Audit</NavLink>
          <NavLink to="/settings">Settings</NavLink>
        </nav>
        <span className="mono" style={{ color: "var(--text-muted)" }} title="Tenant">
          {tenantId ? `${tenantId.slice(0, 8)}…` : ""}
        </span>
        <button type="button" onClick={logout}>
          Sign out
        </button>
      </header>

      <main className="app-main" id="main">
        {children}
      </main>
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

