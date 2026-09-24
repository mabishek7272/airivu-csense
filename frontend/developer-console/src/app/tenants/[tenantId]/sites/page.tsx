"use client";

import { useState } from "react";
import { useAuth } from "@/auth/AuthContext";
import type { Site } from "@/api/tenantInfra";
import { createSite, deleteSite, listSites } from "@/api/tenantInfra";
import { ConfirmDialog, Dialog } from "@/components/Dialog";
import { ErrorSummary, Field, FormActions, combine, maxLength, onSubmitHandler, pattern, required, useForm } from "@/components/Form";
import { useNotifications } from "@/components/Notifications";
import { EmptyPanel, FailureState, LoadingRows } from "@/components/States";
import { useOnlineStatus } from "@/hooks/useNetwork";
import { useResource } from "@/hooks/useResource";
import { ApiRequestError } from "@/api/client";

export default function TenantSitesPage({ params }: { params: { tenantId: string } }) {
  const { tenantId } = params;
  const { isAuthenticated } = useAuth();
  const online = useOnlineStatus();
  const notify = useNotifications();

  const sites = useResource(() => listSites(tenantId), [isAuthenticated, tenantId]);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<Site | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  async function handleDelete() {
    if (!deleting) return;
    setDeleteBusy(true);
    try {
      await deleteSite(tenantId, deleting.id);
      sites.mutate((current) => (current ?? []).filter((s) => s.id !== deleting.id));
      notify.success(`${deleting.name} removed`);
      setDeleting(null);
    } catch (err) {
      notify.error(`Could not remove ${deleting.name}`, err instanceof Error ? err.message : undefined);
    } finally {
      setDeleteBusy(false);
    }
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h2>Sites</h2>
          <p>{sites.data ? `${sites.data.length} site${sites.data.length === 1 ? "" : "s"}` : " "}</p>
        </div>
        <button type="button" disabled={!online} onClick={() => setCreating(true)}>
          Add site
        </button>
      </div>

      {sites.loading ? (
        <LoadingRows rows={3} columns={4} />
      ) : sites.error && !sites.data ? (
        <FailureState error={sites.error} online={online} onRetry={sites.reload} entity="sites" />
      ) : sites.data && sites.data.length === 0 ? (
        <EmptyPanel title="No sites yet" icon="○">
          <p>Add the first site before wiring up cameras.</p>
        </EmptyPanel>
      ) : (
        <div className="card">
          <table className="data-table">
            <caption className="visually-hidden">Sites</caption>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Timezone</th>
                <th scope="col">Cameras</th>
                <th scope="col">Devices</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {(sites.data ?? []).map((site) => (
                <tr key={site.id}>
                  <td>
                    <strong>{site.name}</strong>
                    <div className="muted mono">{site.code}</div>
                  </td>
                  <td>{site.timezone}</td>
                  <td>{site.camera_count}</td>
                  <td>{site.device_count}</td>
                  <td className="row-actions">
                    <button type="button" className="btn-quiet" onClick={() => setDeleting(site)} disabled={!online}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {creating && (
        <CreateSiteDialog
          tenantId={tenantId}
          onClose={() => setCreating(false)}
          onCreated={(site) => {
            sites.mutate((current) => [...(current ?? []), site]);
            notify.success(`${site.name} added`);
          }}
        />
      )}

      <ConfirmDialog
        open={deleting !== null}
        title={`Remove ${deleting?.name ?? ""}`}
        body={<p>This site&apos;s cameras and edge devices must be moved or removed first.</p>}
        confirmLabel="Remove"
        busy={deleteBusy}
        onConfirm={handleDelete}
        onCancel={() => setDeleting(null)}
      />
    </>
  );
}

function CreateSiteDialog({
  tenantId,
  onClose,
  onCreated,
}: {
  tenantId: string;
  onClose: () => void;
  onCreated: (site: Site) => void;
}) {
  const [submitting, setSubmitting] = useState(false);

  const form = useForm({
    name: { initial: "", label: "Name", validate: combine(required("Name"), maxLength(160, "Name")) },
    code: {
      initial: "",
      label: "Code",
      validate: combine(required("Code"), pattern(/^[a-z0-9][a-z0-9_-]*$/, "Lowercase letters, numbers, - and _ only.")),
    },
    timezone: { initial: "UTC", label: "Timezone", validate: required("Timezone") },
  });

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    setSubmitting(true);
    try {
      const site = await createSite(tenantId, {
        name: form.values.name,
        code: form.values.code,
        timezone: form.values.timezone,
      });
      onCreated(site);
      onClose();
    } catch (err) {
      form.applyServerError(err);
    } finally {
      setSubmitting(false);
    }
  });

  return (
    <Dialog open title="Add a site" onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />
        <Field {...form.field("name")} label="Name" required placeholder="Apti demo site" />
        <Field {...form.field("code")} label="Code" required placeholder="apti-demo-site1" />
        <Field
          {...form.field("timezone")}
          label="Timezone"
          required
          hint="IANA name, e.g. Asia/Kolkata or Europe/London — rule schedules are evaluated in this zone."
        />
        <FormActions submitting={submitting} submitLabel="Add site" onCancel={onClose} />
      </form>
    </Dialog>
  );
}
