import type { CSSProperties } from "react";
import { Link } from "react-router-dom";
import type { AuditEvent } from "../api/audit";
import { listAuditEvents } from "../api/audit";
import { getDashboard } from "../api/dashboard";
import { getMfaStatus } from "../api/mfa";
import { Layout, relativeTime } from "../components/Layout";
import { FailureState, LoadingRows } from "../components/States";
import { useOnlineStatus } from "../hooks/useNetwork";
import { useResource } from "../hooks/useResource";

/** Landing page after login - real counts, not a stub, and a guided-onboarding
 *  checklist for a tenant that hasn't finished setting up yet. The checklist derives
 *  entirely from data the API already reports (dashboard counts + MFA status) - there is
 *  no separate "onboarding progress" record to drift out of sync with what's actually
 *  true.
 */
export function DashboardPage() {
  const online = useOnlineStatus();
  const dashboard = useResource(getDashboard, []);
  const mfa = useResource(getMfaStatus, []);
  const activity = useResource(() => listAuditEvents({ limit: 6 }), []);

  const loading = dashboard.loading || mfa.loading;
  const error = dashboard.error ?? mfa.error;

  return (
    <Layout>
      <div className="page-header">
        <div>
          <h1>Dashboard</h1>
          <p className="muted">An overview of this tenant.</p>
        </div>
      </div>

      {loading ? (
        <LoadingRows rows={4} columns={4} />
      ) : Boolean(error) && !dashboard.data ? (
        <FailureState error={error} online={online} onRetry={dashboard.reload} entity="the dashboard" />
      ) : dashboard.data ? (
        <>
          <div className="stat-grid">
            <StatTile label="Sites" value={dashboard.data.sites_count} to="/sites" />
            <StatTile label="Cameras" value={dashboard.data.cameras_count} to="/cameras" />
            <StatTile label="Open incidents" value={dashboard.data.incidents_open_count} to="/incidents" />
            <StatTile label="Team members" value={dashboard.data.team_members_count} to="/team" />
          </div>

          <div className="dashboard-lower" style={{ display: "flex", gap: "var(--space-4)", marginTop: "var(--space-4)", alignItems: "flex-start" }}>
            <OnboardingChecklist
              sitesCount={dashboard.data.sites_count}
              camerasCount={dashboard.data.cameras_count}
              teamMembersCount={dashboard.data.team_members_count}
              mfaEnrolled={mfa.data?.enrolled ?? false}
              style={{ flex: 1, marginTop: 0 }}
            />
            <RecentActivity events={activity.data?.items ?? null} loading={activity.loading} />
          </div>
        </>
      ) : null}
    </Layout>
  );
}

/** Real recent history, not a fabricated feed - the same `audit_events` row set
 *  AuditPage.tsx reads, just the newest few, with the same friendly `actor_display_name`
 *  join. Shows the raw `action` code (mono, matching AuditPage.tsx's own convention)
 *  rather than a hand-written sentence per action type - a per-action-code English
 *  description would need enumerating every action this product can ever record, which
 *  would drift out of date the moment a new one is added; the audit trail already names
 *  itself clearly enough to read without that translation layer.
 */
function RecentActivity({
  events,
  loading,
}: {
  events: AuditEvent[] | null;
  loading: boolean;
}) {
  return (
    <div className="card" style={{ padding: "var(--space-4)", width: 320, flex: "none" }}>
      <div className="mono" style={{ fontSize: 10, letterSpacing: "0.12em", color: "var(--text-muted)", marginBottom: 12 }}>
        RECENT ACTIVITY
      </div>
      {loading ? (
        <div aria-busy="true" aria-live="polite" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <span className="visually-hidden">Loading…</span>
          {Array.from({ length: 4 }, (_, i) => (
            <div key={i} className="skeleton" style={{ height: 14, width: `${85 - i * 10}%` }} aria-hidden="true" />
          ))}
        </div>
      ) : !events || events.length === 0 ? (
        <p className="muted" style={{ margin: 0, fontSize: 13 }}>
          Nothing recorded yet.
        </p>
      ) : (
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 10 }}>
          {events.map((event) => (
            <li
              key={event.id}
              style={{
                borderLeft: `2px solid ${event.outcome === "failure" ? "var(--critical)" : "var(--border)"}`,
                paddingLeft: 10,
              }}
            >
              <div style={{ fontSize: 12.5 }}>
                {event.actor_display_name ?? event.actor_type}
                <span className="mono muted"> · {event.action}</span>
              </div>
              <div className="mono muted" style={{ fontSize: 9.5, marginTop: 2 }}>
                {relativeTime(event.occurred_at)}
                {event.outcome === "failure" ? " · FAILED" : ""}
              </div>
            </li>
          ))}
        </ul>
      )}
      <Link to="/audit" className="btn-quiet" style={{ marginTop: 12, display: "inline-block" }}>
        View full audit log
      </Link>
    </div>
  );
}

function StatTile({ label, value, to }: { label: string; value: number; to: string }) {
  return (
    <Link to={to} className="stat-tile card">
      <span className="stat-tile-value">{value}</span>
      <span className="stat-tile-label">{label}</span>
    </Link>
  );
}

interface ChecklistStep {
  label: string;
  done: boolean;
  to: string;
  action: string;
}

function OnboardingChecklist({
  sitesCount,
  camerasCount,
  teamMembersCount,
  mfaEnrolled,
  style,
}: {
  sitesCount: number;
  camerasCount: number;
  teamMembersCount: number;
  mfaEnrolled: boolean;
  style?: CSSProperties;
}) {
  const steps: ChecklistStep[] = [
    { label: "Add your first site", done: sitesCount > 0, to: "/sites", action: "Add a site" },
    { label: "Add a camera", done: camerasCount > 0, to: "/cameras", action: "Add a camera" },
    { label: "Invite your team", done: teamMembersCount > 1, to: "/team", action: "Invite someone" },
    { label: "Turn on two-factor authentication", done: mfaEnrolled, to: "/settings", action: "Set up 2FA" },
  ];

  const remaining = steps.filter((s) => !s.done);
  if (remaining.length === 0) return null;

  return (
    <div className="card" style={{ padding: "var(--space-4)", marginTop: "var(--space-4)", ...style }}>
      <h2 style={{ marginTop: 0 }}>Get started</h2>
      <p className="muted">
        {remaining.length} of {steps.length} steps left to finish setting up this tenant.
      </p>
      <ul className="checklist">
        {steps.map((s) => (
          <li key={s.label} className={s.done ? "checklist-done" : undefined}>
            <span aria-hidden="true">{s.done ? "✓" : "○"}</span>
            <span>{s.label}</span>
            {!s.done && (
              <Link to={s.to} className="btn-quiet">
                {s.action}
              </Link>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
