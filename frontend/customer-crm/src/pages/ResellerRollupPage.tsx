import { useState } from "react";
import type { CreateChildTenantResult } from "../api/reseller";
import { createChildTenant, getChildTenantRollup } from "../api/reseller";
import { Dialog } from "../components/Dialog";
import {
  ErrorSummary,
  Field,
  FormActions,
  SuccessPanel,
  combine,
  maxLength,
  onSubmitHandler,
  required,
  useForm,
} from "../components/Form";
import { Layout } from "../components/Layout";
import { EmptyPanel, FailureState, LoadingRows } from "../components/States";
import { useOnlineStatus } from "../hooks/useNetwork";
import { useResource } from "../hooks/useResource";

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

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
  const [creating, setCreating] = useState(false);

  return (
    <Layout>
      <div className="page-header">
        <div>
          <h1>Child tenants</h1>
          <p className="muted">Aggregate counts across every active child tenant.</p>
        </div>
        <button type="button" onClick={() => setCreating(true)} disabled={!online}>
          Create child tenant
        </button>
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
            <SummaryTile
              label="Est. monthly list price"
              value={formatCents(rollup.data.total_monthly_list_price_cents)}
            />
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
                  <th scope="col">List price</th>
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
                    {/* "—" (not "$0.00") when list_price_cents is null - an unpriced plan
                        reads as "unknown," not "free". */}
                    <td className="muted">
                      {tenant.list_price_cents == null ? "—" : formatCents(tenant.list_price_cents)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}

      {creating && (
        <CreateChildTenantDialog
          onClose={() => setCreating(false)}
          onCreated={() => rollup.reload()}
        />
      )}
    </Layout>
  );
}

function SummaryTile({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="stat-tile card">
      <span className="stat-tile-value">{value}</span>
      <span className="stat-tile-label">{label}</span>
    </div>
  );
}

/** cents -> "$99.00". List price only - see ChildTenantRollup's own docstring in
 *  api/reseller.ts for why this can never mean "billed amount". */
function formatCents(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}

/** Provisions a new child tenant under this reseller organization - mirrors
 *  CreateChildTenantIn in backend/tenant_api/app/api/reseller.py exactly (same 3-field
 *  owner-provisioning shape as developer-console's CreateOrganizationDialog). The new
 *  owner is invited, never given a password directly; they activate through the
 *  invitation email, or - the one case that needs surfacing here - through the raw link
 *  itself when that email could not be sent (`invitation_link` non-null). Unlike
 *  TeamPage's InviteDialog/CreateOrganizationDialog (which only tell the user a link
 *  exists via a toast, without showing it), this dialog stays open on success and
 *  displays the literal link using Form.tsx's existing SuccessPanel - a reseller handing
 *  off access to a brand-new customer needs the actual URL, not just a reminder that one
 *  exists.
 *
 *  A non-reseller tenant gets a real `403 not_a_reseller` from the backend on submit;
 *  it renders through the same applyServerError -> ErrorSummary path every other form
 *  in this app already uses, not swallowed client-side.
 */
function CreateChildTenantDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const [created, setCreated] = useState<CreateChildTenantResult | null>(null);

  const form = useForm({
    organization_name: {
      initial: "",
      label: "Organization name",
      validate: combine(required("Organization name"), maxLength(200, "Organization name")),
    },
    owner_email: {
      initial: "",
      label: "Owner email",
      validate: (value) => (EMAIL_PATTERN.test(value) ? undefined : "Enter a valid email address."),
    },
    owner_display_name: {
      initial: "",
      label: "Owner name",
      validate: combine(required("Owner name"), maxLength(200, "Owner name")),
    },
  });

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    form.setSubmitting(true);
    form.setFormError(undefined);
    try {
      const result = await createChildTenant({
        organization_name: form.values.organization_name,
        owner_email: form.values.owner_email,
        owner_display_name: form.values.owner_display_name,
      });
      setCreated(result);
      onCreated();
    } catch (err) {
      form.applyServerError(err);
    } finally {
      form.setSubmitting(false);
    }
  });

  if (created) {
    return (
      <Dialog open title="Child tenant created" onClose={onClose}>
        <SuccessPanel
          title={`${created.display_name} created`}
          action={
            <button type="button" onClick={onClose}>
              Done
            </button>
          }
        >
          {created.invitation_link ? (
            <>
              <p>
                The invitation email to <strong>{created.owner_email}</strong> could not be
                sent — share this link with them directly. It is valid for 7 days and can
                only be used once.
              </p>
              <input
                readOnly
                value={created.invitation_link}
                onFocus={(e) => e.currentTarget.select()}
                style={{ width: "100%" }}
                aria-label="Invitation link"
              />
            </>
          ) : (
            <p>
              An invitation email has been sent to <strong>{created.owner_email}</strong>.
            </p>
          )}
        </SuccessPanel>
      </Dialog>
    );
  }

  return (
    <Dialog open title="Create child tenant" onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />

        <Field
          {...form.field("organization_name")}
          label="Organization name"
          required
          placeholder="Northwind Security"
        />
        <Field
          {...form.field("owner_email")}
          label="Owner email"
          required
          type="email"
          placeholder="owner@northwind.example"
        />
        <Field
          {...form.field("owner_display_name")}
          label="Owner name"
          required
          placeholder="Jordan Rivera"
        />

        <FormActions submitting={form.submitting} submitLabel="Create child tenant" onCancel={onClose} />
      </form>
    </Dialog>
  );
}
