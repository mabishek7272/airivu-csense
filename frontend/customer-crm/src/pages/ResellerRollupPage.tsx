import { getChildTenantRollup } from "../api/reseller";
import { Layout } from "../components/Layout";
import { EmptyPanel, FailureState, LoadingRows } from "../components/States";
import { useOnlineStatus } from "../hooks/useNetwork";
import { useResource } from "../hooks/useResource";

/** Reseller aggregate rollup: real counts across every active child tenant, computed by
 *  reseller_child_tenant_rollup() (migration 0056) - see that function's own docstring
 *  for why this can't be an ordinary RLS-scoped query (it would return every child tenant
 *  with a count of zero, silently). A non-reseller tenant calling this gets a 403
 *  `not_a_reseller` from the backend, rendered through the same FailureState/
 *  PermissionDeniedPanel every other page already uses - this page does not hide itself
 *  client-side, matching TeamPage's own documented convention (the backend enforces).
 */
export function ResellerRollupPage() {
  const online = useOnlineStatus();
  const rollup = useResource(getChildTenantRollup, []);

  return (
    <Layout>
      <div className="page-header">
        <div>
          <h1>Child tenants</h1>
          <p className="muted">Aggregate counts across every active child tenant.</p>
        </div>
      </div>

      {rollup.loading ? (
        <LoadingRows rows={4} columns={6} />
      ) : Boolean(rollup.error) && !rollup.data ? (
        <FailureState error={rollup.error} online={online} onRetry={rollup.reload} entity="the reseller rollup" />
      ) : rollup.data && rollup.data.tenants.length === 0 ? (
        <EmptyPanel title="No child tenants yet" icon="◇">
          <p>Child tenants created under this reseller will appear here.</p>
        </EmptyPanel>
      ) : rollup.data ? (
        <>
          <div className="stat-grid">
            <SummaryTile label="Child tenants" value={rollup.data.child_tenant_count} />
            <SummaryTile label="Total sites" value={rollup.data.total_sites} />
            <SummaryTile label="Total cameras" value={rollup.data.total_cameras} />
            <SummaryTile label="Active incidents" value={rollup.data.total_active_incidents} />
          </div>

          <div style={{ overflowX: "auto", marginTop: "var(--space-4)" }}>
            <table className="data-table">
              <caption className="visually-hidden">Child tenants</caption>
              <thead>
                <tr>
                  <th scope="col">Tenant</th>
                  <th scope="col">Status</th>
                  <th scope="col">Sites</th>
                  <th scope="col">Cameras</th>
                  <th scope="col">Active incidents</th>
                  <th scope="col">License</th>
                </tr>
              </thead>
              <tbody>
                {rollup.data.tenants.map((tenant) => (
                  <tr key={tenant.tenant_id}>
                    <td>{tenant.display_name}</td>
                    <td>{tenant.tenant_status}</td>
                    <td>{tenant.site_count}</td>
                    <td>{tenant.camera_count}</td>
                    <td>{tenant.active_incident_count}</td>
                    <td className="muted">{tenant.license_status ?? "none"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
    </Layout>
  );
}

function SummaryTile({ label, value }: { label: string; value: number }) {
  return (
    <div className="stat-tile card">
      <span className="stat-tile-value">{value}</span>
      <span className="stat-tile-label">{label}</span>
    </div>
  );
}
