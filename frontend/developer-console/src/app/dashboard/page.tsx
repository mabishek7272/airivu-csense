"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/auth/AuthContext";
import { Layout } from "@/components/Layout";
import { EmptyPanel, FailureState, LoadingRows } from "@/components/States";
import { useOnlineStatus } from "@/hooks/useNetwork";
import { useResource } from "@/hooks/useResource";
import { apiFetch } from "@/api/client";

interface Organization {
  id: string;
  display_name: string;
  organization_type: string;
  status: string;
}

export default function DashboardPage() {
  const { isAuthenticated, isLoading } = useAuth();
  const router = useRouter();
  const online = useOnlineStatus();

  useEffect(() => {
    if (!isLoading && !isAuthenticated) router.replace("/login");
  }, [isAuthenticated, isLoading, router]);

  const orgs = useResource(
    () => apiFetch<Organization[]>("/api/v1/admin/organizations"),
    [isAuthenticated],
  );

  if (isLoading || !isAuthenticated) {
    return (
      <div className="auth-shell" aria-busy="true">
        <p>Loading…</p>
      </div>
    );
  }

  return (
    <Layout>
      <div className="page-header">
        <div>
          <h1>Platform dashboard</h1>
          <p>{orgs.data ? `${orgs.data.length} organization${orgs.data.length === 1 ? "" : "s"}` : " "}</p>
        </div>
      </div>

      {orgs.loading ? (
        <LoadingRows rows={3} columns={3} />
      ) : Boolean(orgs.error) && !orgs.data ? (
        <FailureState error={orgs.error} online={online} onRetry={orgs.reload} entity="organizations" />
      ) : orgs.data && orgs.data.length === 0 ? (
        <EmptyPanel title="No organizations yet" icon="○">
          <p>Organizations are created through tenant registration, not from here.</p>
        </EmptyPanel>
      ) : (
        <div className="card">
          <table className="data-table">
            <caption className="visually-hidden">Organizations</caption>
            <thead>
              <tr>
                <th scope="col">Organization</th>
                <th scope="col">Type</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {(orgs.data ?? []).map((org) => (
                <tr key={org.id}>
                  <td>{org.display_name}</td>
                  <td>{org.organization_type}</td>
                  <td>
                    <span className="pill">{org.status}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="notice notice-info" style={{ marginTop: 24 }}>
        <span aria-hidden="true">ⓘ</span>
        <div>
          <strong>Phase 1 foundation shell.</strong>
          <p>
            License/reseller management, a pipeline builder, and audit search land here in
            later phases per CHECKLIST.md. The model registry is live under{" "}
            <strong>Models</strong> above.
          </p>
        </div>
      </div>
    </Layout>
  );
}
