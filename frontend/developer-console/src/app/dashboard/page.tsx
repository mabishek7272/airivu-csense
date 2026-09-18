"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/auth/AuthContext";
import type { CreatedOrganization } from "@/api/organizations";
import { listOrganizations } from "@/api/organizations";
import type { License, LicensePlan } from "@/api/licensing";
import { listLicensePlans, listLicenses } from "@/api/licensing";
import { ChangeLicensePlanDialog } from "@/components/ChangeLicensePlanDialog";
import { CreateLicensePlanDialog } from "@/components/CreateLicensePlanDialog";
import { CreateOrganizationDialog } from "@/components/CreateOrganizationDialog";
import { IssueLicenseDialog } from "@/components/IssueLicenseDialog";
import { Layout } from "@/components/Layout";
import { useNotifications } from "@/components/Notifications";
import { EmptyPanel, FailureState, LoadingRows } from "@/components/States";
import { useOnlineStatus } from "@/hooks/useNetwork";
import { useResource } from "@/hooks/useResource";

/** Principal Administrator's own org/license screens: provision organizations
 *  (including resellers - see backend/admin_api/app/api/organizations.py), define
 *  license plans, and issue licenses to tenants (step-up-gated - see
 *  components/IssueLicenseDialog.tsx).
 */
export default function DashboardPage() {
  const { isAuthenticated, isLoading } = useAuth();
  const router = useRouter();
  const online = useOnlineStatus();
  const notify = useNotifications();

  useEffect(() => {
    if (!isLoading && !isAuthenticated) router.replace("/login");
  }, [isAuthenticated, isLoading, router]);

  const orgs = useResource(listOrganizations, [isAuthenticated]);
  const plans = useResource(listLicensePlans, [isAuthenticated]);
  const licenses = useResource(() => listLicenses(), [isAuthenticated]);

  const [creatingOrg, setCreatingOrg] = useState(false);
  const [creatingPlan, setCreatingPlan] = useState(false);
  const [issuingLicense, setIssuingLicense] = useState(false);
  const [changingPlan, setChangingPlan] = useState<License | null>(null);

  if (isLoading || !isAuthenticated) {
    return (
      <div className="auth-shell" aria-busy="true">
        <p>Loading…</p>
      </div>
    );
  }

  function orgName(tenantId: string): string {
    return orgs.data?.find((o) => o.tenant_id === tenantId)?.display_name ?? tenantId.slice(0, 8) + "…";
  }

  function handleOrgCreated(created: CreatedOrganization) {
    orgs.mutate((current) => [
      ...(current ?? []),
      { id: created.organization_id, display_name: created.display_name, organization_type: created.organization_type, status: "active", tenant_id: created.tenant_id },
    ]);
    notify.success(
      `${created.display_name} created`,
      created.invitation_link
        ? "The invitation email could not be sent - share this link with the owner directly."
        : "An invitation email has been sent to the owner.",
    );
  }

  function handlePlanCreated(plan: LicensePlan) {
    plans.mutate((current) => [...(current ?? []), plan]);
    notify.success(`Plan "${plan.name}" created`);
  }

  function handleLicenseIssued(license: License) {
    licenses.mutate((current) => [...(current ?? []), license]);
    notify.success("License issued");
  }

  function handlePlanChanged(license: License) {
    licenses.mutate((current) => (current ?? []).map((l) => (l.id === license.id ? license : l)));
    notify.success("License plan changed");
  }

  return (
    <Layout>
      <div className="page-header">
        <div>
          <h1>Platform dashboard</h1>
          <p>{orgs.data ? `${orgs.data.length} organization${orgs.data.length === 1 ? "" : "s"}` : " "}</p>
        </div>
        <button type="button" disabled={!online} onClick={() => setCreatingOrg(true)}>
          Create organization
        </button>
      </div>

      {orgs.loading ? (
        <LoadingRows rows={3} columns={4} />
      ) : orgs.error && !orgs.data ? (
        <FailureState error={orgs.error} online={online} onRetry={orgs.reload} entity="organizations" />
      ) : orgs.data && orgs.data.length === 0 ? (
        <EmptyPanel title="No organizations yet" icon="○">
          <p>Create the first one - a direct customer, or a reseller.</p>
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
                <th scope="col">Tenant</th>
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
                  <td className="mono">{org.tenant_id ? `${org.tenant_id.slice(0, 8)}…` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="page-header" style={{ marginTop: 32 }}>
        <div>
          <h2>License plans</h2>
          <p>{plans.data ? `${plans.data.length} plan${plans.data.length === 1 ? "" : "s"}` : " "}</p>
        </div>
        <button type="button" disabled={!online} onClick={() => setCreatingPlan(true)}>
          Create plan
        </button>
      </div>

      {plans.loading ? (
        <LoadingRows rows={2} columns={4} />
      ) : plans.error && !plans.data ? (
        <FailureState error={plans.error} online={online} onRetry={plans.reload} entity="license plans" />
      ) : plans.data && plans.data.length === 0 ? (
        <EmptyPanel title="No license plans yet" icon="○">
          <p>Create a plan before issuing any licenses.</p>
        </EmptyPanel>
      ) : (
        <div className="card">
          <table className="data-table">
            <caption className="visually-hidden">License plans</caption>
            <thead>
              <tr>
                <th scope="col">Plan</th>
                <th scope="col">Code</th>
                <th scope="col">Billing</th>
                <th scope="col">Entitlements</th>
              </tr>
            </thead>
            <tbody>
              {(plans.data ?? []).map((plan) => (
                <tr key={plan.id}>
                  <td>{plan.name}</td>
                  <td className="mono">{plan.code}</td>
                  <td>{plan.billing_period}</td>
                  <td className="mono">
                    {Object.entries(plan.default_entitlements)
                      .map(([code, spec]) => `${code}=${spec.limit_numeric ?? spec.enabled_boolean ?? "?"}`)
                      .join(", ") || "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="page-header" style={{ marginTop: 32 }}>
        <div>
          <h2>Licenses</h2>
          <p>{licenses.data ? `${licenses.data.length} issued` : " "}</p>
        </div>
        <button type="button" disabled={!online || (plans.data ?? []).length === 0} onClick={() => setIssuingLicense(true)}>
          Issue license
        </button>
      </div>

      {licenses.loading ? (
        <LoadingRows rows={2} columns={5} />
      ) : licenses.error && !licenses.data ? (
        <FailureState error={licenses.error} online={online} onRetry={licenses.reload} entity="licenses" />
      ) : licenses.data && licenses.data.length === 0 ? (
        <EmptyPanel title="No licenses issued yet" icon="○">
          <p>Issue the first one to a tenant once a plan exists.</p>
        </EmptyPanel>
      ) : (
        <div className="card">
          <table className="data-table">
            <caption className="visually-hidden">Licenses</caption>
            <thead>
              <tr>
                <th scope="col">Tenant</th>
                <th scope="col">Plan</th>
                <th scope="col">Status</th>
                <th scope="col">Starts</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {(licenses.data ?? []).map((license) => (
                <tr key={license.id}>
                  <td>{orgName(license.tenant_id)}</td>
                  <td className="mono">{license.plan_code}</td>
                  <td>
                    <span className="pill">{license.status}</span>
                  </td>
                  <td>{new Date(license.starts_at).toLocaleDateString()}</td>
                  <td>
                    <button type="button" disabled={!online} onClick={() => setChangingPlan(license)}>
                      Change plan
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {creatingOrg && <CreateOrganizationDialog onClose={() => setCreatingOrg(false)} onCreated={handleOrgCreated} />}
      {creatingPlan && <CreateLicensePlanDialog onClose={() => setCreatingPlan(false)} onCreated={handlePlanCreated} />}
      {issuingLicense && (
        <IssueLicenseDialog
          organizations={orgs.data ?? []}
          plans={plans.data ?? []}
          onClose={() => setIssuingLicense(false)}
          onIssued={handleLicenseIssued}
        />
      )}
      {changingPlan && (
        <ChangeLicensePlanDialog
          license={changingPlan}
          plans={plans.data ?? []}
          onClose={() => setChangingPlan(null)}
          onChanged={handlePlanChanged}
        />
      )}
    </Layout>
  );
}
