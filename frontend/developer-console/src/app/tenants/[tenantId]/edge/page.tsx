"use client";

import { useState } from "react";
import { useAuth } from "@/auth/AuthContext";
import type { EdgeDevice, EnrolmentToken, Site, VpnProvisionResult } from "@/api/tenantInfra";
import { createDevice, issueEnrolmentToken, listDevices, listSites, provisionVpn } from "@/api/tenantInfra";
import { ConfirmDialog, Dialog } from "@/components/Dialog";
import { ErrorSummary, Field, FormActions, combine, maxLength, onSubmitHandler, required, useForm } from "@/components/Form";
import { useNotifications } from "@/components/Notifications";
import { EmptyPanel, FailureState, LoadingRows } from "@/components/States";
import { useOnlineStatus } from "@/hooks/useNetwork";
import { useResource } from "@/hooks/useResource";

const DEVICE_TYPES = ["raspberry_pi", "jetson_nano", "jetson_orin", "dgx_spark", "pc_linux", "pc_windows", "other"];
const DEVICE_ROLES = ["gateway", "inference", "hybrid"];

export default function TenantEdgePage({ params }: { params: { tenantId: string } }) {
  const { tenantId } = params;
  const { isAuthenticated } = useAuth();
  const online = useOnlineStatus();
  const notify = useNotifications();

  const devices = useResource(() => listDevices(tenantId), [isAuthenticated, tenantId]);
  const sites = useResource(() => listSites(tenantId), [isAuthenticated, tenantId]);
  const [creating, setCreating] = useState(false);
  const [issuedToken, setIssuedToken] = useState<EnrolmentToken | null>(null);
  const [tunneling, setTunneling] = useState<VpnProvisionResult | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  async function handleEnrol(device: EdgeDevice) {
    setBusyId(device.id);
    try {
      const token = await issueEnrolmentToken(tenantId, device.id);
      setIssuedToken(token);
    } catch (err) {
      notify.error(`Could not issue a token for ${device.name}`, err instanceof Error ? err.message : undefined);
    } finally {
      setBusyId(null);
    }
  }

  async function handleTunnel(device: EdgeDevice) {
    setBusyId(device.id);
    try {
      const result = await provisionVpn(tenantId, device.id);
      setTunneling(result);
      devices.reload();
    } catch (err) {
      notify.error(`Could not provision a tunnel for ${device.name}`, err instanceof Error ? err.message : undefined);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h2>Edge devices</h2>
          <p>{devices.data ? `${devices.data.length} device${devices.data.length === 1 ? "" : "s"}` : " "}</p>
        </div>
        <button type="button" disabled={!online} onClick={() => setCreating(true)}>
          Add device
        </button>
      </div>

      {devices.loading ? (
        <LoadingRows rows={3} columns={5} />
      ) : devices.error && !devices.data ? (
        <FailureState error={devices.error} online={online} onRetry={devices.reload} entity="edge devices" />
      ) : devices.data && devices.data.length === 0 ? (
        <EmptyPanel title="No edge devices yet" icon="○">
          <p>Register a device, then enrol it and provision its tunnel.</p>
        </EmptyPanel>
      ) : (
        <div className="card">
          <table className="data-table">
            <caption className="visually-hidden">Edge devices</caption>
            <thead>
              <tr>
                <th scope="col">Device</th>
                <th scope="col">Role</th>
                <th scope="col">State</th>
                <th scope="col">Connectivity</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {(devices.data ?? []).map((device) => (
                <tr key={device.id}>
                  <td>
                    <strong>{device.name}</strong>
                    <div className="muted">{device.device_type}</div>
                  </td>
                  <td>
                    <span className="pill">{device.role}</span>
                    <div className="muted">{device.camera_count} cameras</div>
                  </td>
                  <td>
                    <span className={`pill pill-${device.online ? "online" : device.status}`}>{device.online ? "online" : device.status}</span>
                  </td>
                  <td>
                    {device.vpn_address ? (
                      <>
                        {device.connectivity_method ?? "vpn"}
                        <div className="muted mono">{device.vpn_address}</div>
                      </>
                    ) : (
                      <span className="muted">Not yet chosen</span>
                    )}
                  </td>
                  <td className="row-actions">
                    <button type="button" className="btn-quiet" onClick={() => handleEnrol(device)} disabled={!online || busyId === device.id}>
                      Enrol
                    </button>
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => handleTunnel(device)}
                      disabled={!online || busyId === device.id || !device.has_wireguard_key}
                      title={device.has_wireguard_key ? undefined : "Enrol the device with a WireGuard key first"}
                    >
                      Tunnel
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {creating && (
        <CreateDeviceDialog
          tenantId={tenantId}
          sites={sites.data ?? []}
          onClose={() => setCreating(false)}
          onCreated={(device) => {
            devices.mutate((current) => [...(current ?? []), device]);
            notify.success(`${device.name} added`);
          }}
        />
      )}

      {issuedToken && (
        <Dialog open title="Enrolment token" onClose={() => setIssuedToken(null)}>
          <p>{issuedToken.instructions}</p>
          <pre className="code-block" style={{ userSelect: "all" }}>
            {issuedToken.token}
          </pre>
          <p className="muted">Expires {new Date(issuedToken.expires_at).toLocaleString()}.</p>
        </Dialog>
      )}

      {tunneling && (
        <Dialog open title={`Tunnel for ${tunneling.device.name}`} onClose={() => setTunneling(null)}>
          {tunneling.warnings.map((w) => (
            <p key={w} className="notice notice-warning">
              {w}
            </p>
          ))}
          <p>
            Address <strong className="mono">{tunneling.device.vpn_address}</strong>
          </p>
          <label>Paste onto the device</label>
          <pre className="code-block">{tunneling.client_config}</pre>
          <label>Paste into the platform&apos;s WireGuard server</label>
          <pre className="code-block">{tunneling.server_peer_config}</pre>
        </Dialog>
      )}
    </>
  );
}

function CreateDeviceDialog({
  tenantId,
  sites,
  onClose,
  onCreated,
}: {
  tenantId: string;
  sites: Site[];
  onClose: () => void;
  onCreated: (device: EdgeDevice) => void;
}) {
  const [submitting, setSubmitting] = useState(false);

  const form = useForm({
    name: { initial: "", label: "Name", validate: combine(required("Name"), maxLength(120, "Name")) },
    site_id: { initial: sites[0]?.id ?? "", label: "Site" },
    device_type: { initial: "other", label: "Device type" },
    role: { initial: "gateway", label: "Role" },
    serial_number: { initial: "", label: "Serial number" },
  });

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    setSubmitting(true);
    try {
      const device = await createDevice(tenantId, {
        name: form.values.name,
        site_id: form.values.site_id || undefined,
        device_type: form.values.device_type,
        role: form.values.role,
        serial_number: form.values.serial_number || undefined,
      });
      onCreated(device);
      onClose();
    } catch (err) {
      form.applyServerError(err);
    } finally {
      setSubmitting(false);
    }
  });

  return (
    <Dialog open title="Add an edge device" onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />
        <Field {...form.field("name")} label="Name" required placeholder="APTI laptop" />
        <Field {...form.field("site_id")} label="Site" options={[{ value: "", label: "No site yet" }, ...sites.map((s) => ({ value: s.id, label: s.name }))]} />
        <div className="field-row">
          <Field {...form.field("device_type")} label="Device type" options={DEVICE_TYPES.map((t) => ({ value: t, label: t }))} />
          <Field {...form.field("role")} label="Role" options={DEVICE_ROLES.map((r) => ({ value: r, label: r }))} />
        </div>
        <Field {...form.field("serial_number")} label="Serial number" />
        <FormActions submitting={submitting} submitLabel="Add device" onCancel={onClose} />
      </form>
    </Dialog>
  );
}
