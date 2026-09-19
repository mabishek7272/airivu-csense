import type { JSX } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { BrandProvider } from "./branding/BrandProvider";
import { NotificationProvider } from "./components/Notifications";
import { CamerasPage } from "./pages/CamerasPage";
import { EdgePage } from "./pages/EdgePage";
import { NotificationPoliciesPage } from "./pages/NotificationPoliciesPage";
import { RecipientGroupsPage } from "./pages/RecipientGroupsPage";
import { PipelineAssignmentsPage } from "./pages/PipelineAssignmentsPage";
import { RulesPage } from "./pages/RulesPage";
import { SitesPage } from "./pages/SitesPage";
import { ZonesPage } from "./pages/ZonesPage";
import { DetectionsPage } from "./pages/DetectionsPage";
import { IncidentDetailPage } from "./pages/IncidentDetailPage";
import { IncidentsPage } from "./pages/IncidentsPage";
import { Landing3rdiPage } from "./pages/Landing3rdiPage";
import { LoginPage } from "./pages/LoginPage";
import { AcceptInvitationPage } from "./pages/AcceptInvitationPage";
import { TeamPage } from "./pages/TeamPage";
import { ResellerRollupPage } from "./pages/ResellerRollupPage";
import { AuditPage } from "./pages/AuditPage";
import { DashboardPage } from "./pages/DashboardPage";
import { SettingsPage } from "./pages/SettingsPage";
import { WebhooksPage } from "./pages/WebhooksPage";

function RequireAuth({ children }: { children: JSX.Element }) {
  const { isAuthenticated, isLoading } = useAuth();
  // While the silent refresh is in flight, render nothing rather than bouncing to the
  // login screen — a redirect here would log out anyone who reloads the page.
  if (isLoading) {
    return (
      <div className="auth-shell" aria-busy="true">
        <p>Loading…</p>
      </div>
    );
  }
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  return children;
}

function AppRoutes({ brandSlug, isApex }: { brandSlug: string | null; isApex: boolean }) {
  return (
    <Routes>
      {/* The bare 3rdi.in root (no brand slug, AND the hostname is the apex, not
          app.3rdi.in) is the 3RDI parent-company splash, not a login form - see
          Landing3rdiPage's and isApexHostname's own docs. Checking isApex here, not
          just "no brand slug", is the real fix for a real bug: app.3rdi.in's own bare
          root also has pathname "/" with no slug, and briefly showed the splash too
          before hostname was taken into account. Under a brand's own basename (e.g.
          3rdi.in/eaigleye/ with nothing after it), this same "/" instead means that
          brand's own bare root, which keeps its normal behavior (redirect into the app
          via the catch-all below). */}
      <Route path="/" element={!brandSlug && isApex ? <Landing3rdiPage /> : <Navigate to="/dashboard" replace />} />
      <Route path="/login" element={<LoginPage />} />
      <Route path="/accept-invitation" element={<AcceptInvitationPage />} />
      <Route
        path="/dashboard"
        element={
          <RequireAuth>
            <DashboardPage />
          </RequireAuth>
        }
      />
      <Route
        path="/settings"
        element={
          <RequireAuth>
            <SettingsPage />
          </RequireAuth>
        }
      />
      <Route
        path="/team"
        element={
          <RequireAuth>
            <TeamPage />
          </RequireAuth>
        }
      />
      <Route
        path="/audit"
        element={
          <RequireAuth>
            <AuditPage />
          </RequireAuth>
        }
      />
      <Route
        path="/child-tenants/rollup"
        element={
          <RequireAuth>
            <ResellerRollupPage />
          </RequireAuth>
        }
      />
      <Route
        path="/incidents"
        element={
          <RequireAuth>
            <IncidentsPage />
          </RequireAuth>
        }
      />
      <Route
        path="/incidents/:incidentId"
        element={
          <RequireAuth>
            <IncidentDetailPage />
          </RequireAuth>
        }
      />
      <Route
        path="/detections"
        element={
          <RequireAuth>
            <DetectionsPage />
          </RequireAuth>
        }
      />
      <Route
        path="/sites"
        element={
          <RequireAuth>
            <SitesPage />
          </RequireAuth>
        }
      />
      <Route
        path="/zones"
        element={
          <RequireAuth>
            <ZonesPage />
          </RequireAuth>
        }
      />
      <Route
        path="/rules"
        element={
          <RequireAuth>
            <RulesPage />
          </RequireAuth>
        }
      />
      <Route
        path="/cameras"
        element={
          <RequireAuth>
            <CamerasPage />
          </RequireAuth>
        }
      />
      <Route
        path="/edge"
        element={
          <RequireAuth>
            <EdgePage />
          </RequireAuth>
        }
      />
      <Route
        path="/pipelines"
        element={
          <RequireAuth>
            <PipelineAssignmentsPage />
          </RequireAuth>
        }
      />
      <Route
        path="/recipient-groups"
        element={
          <RequireAuth>
            <RecipientGroupsPage />
          </RequireAuth>
        }
      />
      <Route
        path="/notification-policies"
        element={
          <RequireAuth>
            <NotificationPoliciesPage />
          </RequireAuth>
        }
      />
      <Route
        path="/webhooks"
        element={
          <RequireAuth>
            <WebhooksPage />
          </RequireAuth>
        }
      />
      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  );
}

export default function App({ brandSlug, isApex }: { brandSlug: string | null; isApex: boolean }) {
  return (
    <AuthProvider>
      {/* Inside AuthProvider, not outside: BrandProvider calls useAuth() itself to
          know when to switch from the pre-login (URL-derived) branding to the
          post-login (session-trusted) branding. */}
      <BrandProvider brandSlug={brandSlug}>
        <NotificationProvider>
          <AppRoutes brandSlug={brandSlug} isApex={isApex} />
        </NotificationProvider>
      </BrandProvider>
    </AuthProvider>
  );
}

