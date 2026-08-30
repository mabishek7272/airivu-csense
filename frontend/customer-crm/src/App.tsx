import type { JSX } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { NotificationProvider } from "./components/Notifications";
import { CamerasPage } from "./pages/CamerasPage";
import { EdgePage } from "./pages/EdgePage";
import { NotificationPoliciesPage } from "./pages/NotificationPoliciesPage";
import { RecipientGroupsPage } from "./pages/RecipientGroupsPage";
import { RulesPage } from "./pages/RulesPage";
import { SitesPage } from "./pages/SitesPage";
import { ZonesPage } from "./pages/ZonesPage";
import { DetectionsPage } from "./pages/DetectionsPage";
import { IncidentDetailPage } from "./pages/IncidentDetailPage";
import { IncidentsPage } from "./pages/IncidentsPage";
import { LoginPage } from "./pages/LoginPage";
import { AcceptInvitationPage } from "./pages/AcceptInvitationPage";
import { TeamPage } from "./pages/TeamPage";

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

function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/accept-invitation" element={<AcceptInvitationPage />} />
      <Route
        path="/team"
        element={
          <RequireAuth>
            <TeamPage />
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
      <Route path="*" element={<Navigate to="/incidents" replace />} />
    </Routes>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <NotificationProvider>
        <AppRoutes />
      </NotificationProvider>
    </AuthProvider>
  );
}

