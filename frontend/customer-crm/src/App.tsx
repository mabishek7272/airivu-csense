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

function AppRoutes({ brandSlug }: { brandSlug: string | null }) {
  return (
    <Routes>
      {/* The bare 3rdi.in root (no brand slug) is the 3RDI parent-company splash, not
          a login form - see Landing3rdiPage's own docs. Under a brand's own basename
          (e.g. 3rdi.in/eaigleye/ with nothing after it), this same "/" instead means
          that brand's own bare root, which keeps its normal behavior (redirect into
          the app via the catch-all below) - only the truly slug-less case shows the
          splash. */}
      <Route path="/" element={brandSlug ? <Navigate to="/dashboard" replace /> : <Landing3rdiPage />} />
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

export default function App({ brandSlug }: { brandSlug: string | null }) {
  return (
    <AuthProvider>
      {/* Inside AuthProvider, not outside: BrandProvider calls useAuth() itself to
          know when to switch from the pre-login (URL-derived) branding to the
          post-login (session-trusted) branding. */}
      <BrandProvider brandSlug={brandSlug}>
        <NotificationProvider>
          <AppRoutes brandSlug={brandSlug} />
        </NotificationProvider>
      </BrandProvider>
    </AuthProvider>
  );
}

