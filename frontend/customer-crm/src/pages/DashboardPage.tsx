import { Link } from "react-router-dom";
import { getDashboard } from "../api/dashboard";
import { getMfaStatus } from "../api/mfa";
import { Layout } from "../components/Layout";
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

          <OnboardingChecklist
            sitesCount={dashboard.data.sites_count}
            camerasCount={dashboard.data.cameras_count}
            teamMembersCount={dashboard.data.team_members_count}
            mfaEnrolled={mfa.data?.enrolled ?? false}
          />
        </>
      ) : null}
    </Layout>
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
}: {
  sitesCount: number;
  camerasCount: number;
  teamMembersCount: number;
  mfaEnrolled: boolean;
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
    <div className="card" style={{ padding: "var(--space-4)", marginTop: "var(--space-4)" }}>
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
