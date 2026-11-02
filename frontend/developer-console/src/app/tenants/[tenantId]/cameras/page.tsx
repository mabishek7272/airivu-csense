"use client";

import { useState } from "react";
import { useAuth } from "@/auth/AuthContext";
import type { Camera, Site } from "@/api/tenantInfra";
import { createCamera, deleteCamera, listCameras, listSites, probeCamera, setCameraCredentials } from "@/api/tenantInfra";
import { ConfirmDialog, Dialog } from "@/components/Dialog";
import { ErrorSummary, Field, FormActions, combine, maxLength, onSubmitHandler, pattern, required, useForm } from "@/components/Form";
import { useNotifications } from "@/components/Notifications";
import { EmptyPanel, FailureState, LoadingRows } from "@/components/States";
import { useOnlineStatus } from "@/hooks/useNetwork";
import { useResource } from "@/hooks/useResource";

/** Camera CRUD is admin-only now, done on a tenant's behalf (see CHECKLIST.md's "move
 * camera/site/zone/edge/rules/pipeline management to the Console" entry) - the tenant's
 * own CRM shows this same camera read-only, Watch Live only.
 */
export default function TenantCamerasPage({ params }: { params: { tenantId: string } }) {
  const { tenantId } = params;
  const { isAuthenticated } = useAuth();
  const online = useOnlineStatus();
  const notify = useNotifications();

  const cameras = useResource(() => listCameras(tenantId), [isAuthenticated, tenantId]);
  const sites = useResource(() => listSites(tenantId), [isAuthenticated, tenantId]);
  const [creating, setCreating] = useState(false);
  const [settingCredsFor, setSettingCredsFor] = useState<Camera | null>(null);
  const [probing, setProbing] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<Camera | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  async function handleProbe(camera: Camera) {
    setProbing(camera.id);
    try {
      const result = await probeCamera(tenantId, camera.id);
      cameras.reload();
      if (result.reachable) {
        notify.success(`${camera.name}: connected`, result.detail);
      } else {
        notify.error(`${camera.name}: not reachable`, result.detail);
      }
    } catch (err) {
      notify.error(`Could not test ${camera.name}`, err instanceof Error ? err.message : undefined);
    } finally {
      setProbing(null);
    }
  }

  async function handleDelete() {
    if (!deleting) return;
    setDeleteBusy(true);
    try {
      await deleteCamera(tenantId, deleting.id);
      cameras.mutate((current) => (current ?? []).filter((c) => c.id !== deleting.id));
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
          <h2>Cameras</h2>
          <p>{cameras.data ? `${cameras.data.length} camera${cameras.data.length === 1 ? "" : "s"}` : " "}</p>
        </div>
        <button type="button" disabled={!online || (sites.data ?? []).length === 0} onClick={() => setCreating(true)}>
          Add camera
        </button>
      </div>

      {cameras.loading ? (
        <LoadingRows rows={3} columns={5} />
      ) : cameras.error && !cameras.data ? (
        <FailureState error={cameras.error} online={online} onRetry={cameras.reload} entity="cameras" />
      ) : cameras.data && cameras.data.length === 0 ? (
        <EmptyPanel title="No cameras yet" icon="○">
          <p>Add a site first if none exists, then add this tenant&apos;s first camera.</p>
        </EmptyPanel>
      ) : (
        <div className="card">
          <table className="data-table">
            <caption className="visually-hidden">Cameras</caption>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Address</th>
                <th scope="col">Status</th>
                <th scope="col">Credential</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {(cameras.data ?? []).map((camera) => (
                <tr key={camera.id}>
                  <td>
                    <strong>{camera.name}</strong>
                    <div className="muted mono">{camera.code}</div>
                  </td>
                  <td className="mono">
                    {camera.hostname ? `${camera.hostname}:${camera.rtsp_port ?? 554}` : "—"}
                    {camera.main_stream_path && <div className="muted">{camera.main_stream_path}</div>}
                  </td>
                  <td>
                    <span className={`pill pill-${camera.status}`}>{camera.status}</span>
                    {camera.last_error && <div className="muted">{camera.last_error}</div>}
                  </td>
                  <td>
                    <span className={camera.has_credentials ? "pill pill-online" : "muted"}>
                      {camera.has_credentials ? "Stored" : "None"}
                    </span>
                  </td>
                  <td className="row-actions">
                    <button type="button" className="btn-quiet" onClick={() => handleProbe(camera)} disabled={!online || probing === camera.id}>
                      {probing === camera.id ? "Testing…" : "Test"}
                    </button>
                    <button type="button" className="btn-quiet" onClick={() => setSettingCredsFor(camera)} disabled={!online}>
                      Credential
                    </button>
                    <button type="button" className="btn-quiet" onClick={() => setDeleting(camera)} disabled={!online}>
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
        <CreateCameraDialog
          tenantId={tenantId}
          sites={sites.data ?? []}
          onClose={() => setCreating(false)}
          onCreated={(camera) => {
            cameras.mutate((current) => [...(current ?? []), camera]);
            notify.success(`${camera.name} added`);
          }}
        />
      )}

      {settingCredsFor && (
        <SetCredentialsDialog
          tenantId={tenantId}
          camera={settingCredsFor}
          onClose={() => setSettingCredsFor(null)}
          onSaved={(camera) => {
            cameras.mutate((current) => (current ?? []).map((c) => (c.id === camera.id ? camera : c)));
            notify.success(`Credential saved for ${camera.name}`);
          }}
        />
      )}

      <ConfirmDialog
        open={deleting !== null}
        title={`Remove ${deleting?.name ?? ""}`}
        body={<p>The stored credential is deleted too. Incidents already raised keep referencing this camera.</p>}
        confirmLabel="Remove"
        busy={deleteBusy}
        onConfirm={handleDelete}
        onCancel={() => setDeleting(null)}
      />
    </>
  );
}

function CreateCameraDialog({
  tenantId,
  sites,
  onClose,
  onCreated,
}: {
  tenantId: string;
  sites: Site[];
  onClose: () => void;
  onCreated: (camera: Camera) => void;
}) {
  const [submitting, setSubmitting] = useState(false);

  const form = useForm({
    site_id: { initial: sites[0]?.id ?? "", label: "Site", validate: required("Site") },
    name: { initial: "", label: "Name", validate: combine(required("Name"), maxLength(120, "Name")) },
    code: {
      initial: "",
      label: "Code",
      validate: combine(required("Code"), pattern(/^[a-z0-9][a-z0-9_-]*$/, "Lowercase letters, numbers, - and _ only.")),
    },
    hostname: { initial: "", label: "Host" },
    rtsp_port: { initial: "554", label: "Port" },
    main_stream_path: { initial: "", label: "Main stream path" },
    sub_stream_path: { initial: "", label: "Substream path" },
    username: { initial: "", label: "Username" },
  });

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    setSubmitting(true);
    try {
      const camera = await createCamera(tenantId, {
        site_id: form.values.site_id,
        name: form.values.name,
        code: form.values.code,
        hostname: form.values.hostname || undefined,
        rtsp_port: form.values.rtsp_port ? Number(form.values.rtsp_port) : undefined,
        main_stream_path: form.values.main_stream_path || undefined,
        sub_stream_path: form.values.sub_stream_path || undefined,
        username: form.values.username || undefined,
      });
      onCreated(camera);
      onClose();
    } catch (err) {
      form.applyServerError(err);
    } finally {
      setSubmitting(false);
    }
  });

  return (
    <Dialog open title="Add a camera" onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />
        <Field {...form.field("site_id")} label="Site" required options={sites.map((s) => ({ value: s.id, label: s.name }))} />
        <div className="field-row">
          <Field {...form.field("name")} label="Name" required placeholder="Loading dock cam" />
          <Field {...form.field("code")} label="Code" required placeholder="loading-dock-1" />
        </div>
        <div className="field-row">
          <Field {...form.field("hostname")} label="Host" placeholder="10.8.0.1 or a hostname" />
          <Field {...form.field("rtsp_port")} label="Port" type="number" />
        </div>
        <div className="field-row">
          <Field {...form.field("main_stream_path")} label="Main stream path" placeholder="/unicast/c1/s0/live" />
          <Field {...form.field("sub_stream_path")} label="Substream path" placeholder="/unicast/c1/s1/live" />
        </div>
        <Field {...form.field("username")} label="Username" hint="Set the password separately, from Credential once the camera exists." />
        <FormActions submitting={submitting} submitLabel="Add camera" onCancel={onClose} />
      </form>
    </Dialog>
  );
}

function SetCredentialsDialog({
  tenantId,
  camera,
  onClose,
  onSaved,
}: {
  tenantId: string;
  camera: Camera;
  onClose: () => void;
  onSaved: (camera: Camera) => void;
}) {
  const [submitting, setSubmitting] = useState(false);
  const form = useForm({
    username: { initial: camera.username ?? "", label: "Username" },
    password: { initial: "", label: "Password", validate: required("Password") },
  });

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    setSubmitting(true);
    try {
      const updated = await setCameraCredentials(tenantId, camera.id, form.values.username || undefined, form.values.password);
      onSaved(updated);
      onClose();
    } catch (err) {
      form.applyServerError(err);
    } finally {
      setSubmitting(false);
    }
  });

  return (
    <Dialog open title={`Credential for ${camera.name}`} onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />
        <Field {...form.field("username")} label="Username" />
        <Field {...form.field("password")} label="Password" type="password" required hint="Write-only — never shown back, only whether one is stored." />
        <FormActions submitting={submitting} submitLabel="Save credential" onCancel={onClose} />
      </form>
    </Dialog>
  );
}
