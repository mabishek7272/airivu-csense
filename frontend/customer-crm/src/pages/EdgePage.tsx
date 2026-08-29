import { useMemo, useState } from "react";
import type { EdgeDevice, EnrolmentToken } from "../api/cameras";
import {
  createEdgeDevice,
  deleteEdgeDevice,
  issueEnrolmentToken,
  listEdgeDevices,
  provisionVpn,
  updateEdgeDevice,
} from "../api/cameras";
import { ConfirmDialog, Dialog } from "../components/Dialog";
import {
  ErrorSummary,
  Field,
  FormActions,
  combine,
  maxLength,
  onSubmitHandler,
  required,
  useForm,
} from "../components/Form";
import { listSites } from "../api/sites";
import { Layout, relativeTime } from "../components/Layout";
import { useNotifications } from "../components/Notifications";
import {
  EmptyPanel,
  FailureState,
  InlineSpinner,
  LoadingRows,
  NoResultsPanel,
  SlowNetworkNotice,
} from "../components/States";
import { useOnlineStatus, useSlowRequest } from "../hooks/useNetwork";
import { useResource } from "../hooks/useResource";

/** Edge devices: the boxes CSense runs on at customer sites.
 *
 *  The screen leans on one distinction, because it is the one that decides where the
 *  work happens: a `gateway` only makes cameras reachable and the cloud does the
 *  inference, while an `inference` device runs the models itself. Surfacing it in the
 *  list — rather than burying it in a detail page — is what stops someone deploying
 *  twenty Raspberry Pis and wondering why the server is saturated.
 */

const DEVICE_TYPES = [
  { value: "raspberry_pi", label: "Raspberry Pi" },
  { value: "jetson_nano", label: "Jetson Nano" },
  { value: "jetson_orin", label: "Jetson Orin" },
  { value: "dgx_spark", label: "DGX Spark" },
  { value: "pc_linux", label: "PC (Linux)" },
  { value: "pc_windows", label: "PC (Windows)" },
  { value: "other", label: "Other" },
];

const ROLES = [
  { value: "gateway", label: "Gateway — connectivity only" },
  { value: "inference", label: "Inference — runs models locally" },
  { value: "hybrid", label: "Hybrid — both" },
];

export function EdgePage() {
  const online = useOnlineStatus();
  const notify = useNotifications();

  const [search, setSearch] = useState("");
  const [roleFilter, setRoleFilter] = useState("");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<EdgeDevice | null>(null);
  const [deleting, setDeleting] = useState<EdgeDevice | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [token, setToken] = useState<{ device: EdgeDevice; token: EnrolmentToken } | null>(
    null,
  );
  const [issuing, setIssuing] = useState<string | null>(null);
  const [tunneling, setTunneling] = useState<EdgeDevice | null>(null);

  const devices = useResource(
    () => listEdgeDevices({ role: roleFilter || undefined }),
    [roleFilter],
  );
  const slow = useSlowRequest(devices.loading);

  const visible = useMemo(() => {
    if (!devices.data) return [];
    const term = search.trim().toLowerCase();
    if (!term) return devices.data;
    return devices.data.filter(
      (d) =>
        d.name.toLowerCase().includes(term) ||
        (d.serial_number ?? "").toLowerCase().includes(term) ||
        (d.site_name ?? "").toLowerCase().includes(term),
    );
  }, [devices.data, search]);

  const hasFilters = search.trim() !== "" || roleFilter !== "";

  async function handleIssueToken(device: EdgeDevice) {
    setIssuing(device.id);
    try {
      const issued = await issueEnrolmentToken(device.id);
      setToken({ device, token: issued });
    } catch (err) {
      notify.error(
        `Could not issue a token for ${device.name}`,
        err instanceof Error ? err.message : undefined,
      );
    } finally {
      setIssuing(null);
    }
  }

  async function handleDelete() {
    if (!deleting) return;
    setDeleteBusy(true);
    const removed = deleting;
    try {
      await deleteEdgeDevice(removed.id);
      devices.mutate((current) => (current ?? []).filter((d) => d.id !== removed.id));
      notify.success(
        `${removed.name} was retired`,
        "Its credential was destroyed — it can no longer report.",
      );
      setDeleting(null);
    } catch (err) {
      notify.error(
        `Could not retire ${removed.name}`,
        err instanceof Error ? err.message : undefined,
      );
    } finally {
      setDeleteBusy(false);
    }
  }

  return (
    <Layout>
      <div className="page-header">
        <div>
          <h1>Edge</h1>
          <p className="muted">
            {devices.data
              ? `${devices.data.length} device${devices.data.length === 1 ? "" : "s"}`
              : " "}
            {devices.refreshing && (
              <>
                {" "}
                <InlineSpinner label="Refreshing" />
              </>
            )}
          </p>
        </div>
        <button type="button" onClick={() => setCreating(true)} disabled={!online}>
          Add device
        </button>
      </div>

      <div className="filter-bar">
        <label className="visually-hidden" htmlFor="edge-search">
          Search devices
        </label>
        <input
          id="edge-search"
          type="search"
          placeholder="Search by name, serial or site"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <label className="visually-hidden" htmlFor="edge-role">
          Filter by role
        </label>
        <select id="edge-role" value={roleFilter} onChange={(e) => setRoleFilter(e.target.value)}>
          <option value="">Any role</option>
          {ROLES.map((r) => (
            <option key={r.value} value={r.value}>
              {r.label.split(" — ")[0]}
            </option>
          ))}
        </select>
        {hasFilters && (
          <button
            type="button"
            className="btn-quiet"
            onClick={() => {
              setSearch("");
              setRoleFilter("");
            }}
          >
            Clear
          </button>
        )}
      </div>

      {slow && devices.loading && <SlowNetworkNotice />}

      {devices.loading ? (
        <LoadingRows rows={3} columns={5} />
      ) : Boolean(devices.error) && !devices.data ? (
        <FailureState
          error={devices.error}
          online={online}
          onRetry={devices.reload}
          entity="device"
        />
      ) : visible.length === 0 && hasFilters ? (
        <NoResultsPanel
          query={search.trim() || undefined}
          entity="devices"
          onClear={() => {
            setSearch("");
            setRoleFilter("");
          }}
        />
      ) : visible.length === 0 ? (
        <EmptyPanel
          title="No edge devices yet"
          icon="⬡"
          action={
            <button type="button" onClick={() => setCreating(true)}>
              Add your first device
            </button>
          }
        >
          <p>
            An edge device connects cameras that are not reachable from the internet. A
            Raspberry Pi carries connectivity only; a Jetson or DGX Spark can run the models
            on site, which takes the load off the server entirely.
          </p>
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
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {visible.map((device) => (
                <tr key={device.id}>
                  <td>
                    <strong>{device.name}</strong>
                    <div className="muted">
                      {DEVICE_TYPES.find((t) => t.value === device.device_type)?.label ??
                        device.device_type}
                    </div>
                    {device.serial_number && (
                      <div className="muted mono">{device.serial_number}</div>
                    )}
                  </td>
                  <td>
                    <span className="pill">{device.role}</span>
                    <div className="muted">
                      {device.camera_count} camera{device.camera_count === 1 ? "" : "s"}
                    </div>
                  </td>
                  <td>
                    <span className={`pill pill-${device.online ? "online" : device.status}`}>
                      {device.online ? "online" : device.status}
                    </span>
                    {device.last_seen_at && (
                      <div className="muted">seen {relativeTime(device.last_seen_at)}</div>
                    )}
                  </td>
                  <td>
                    {device.connectivity_method ? (
                      <>
                        {device.connectivity_method}
                        {device.vpn_address && (
                          <div className="muted mono">{device.vpn_address}</div>
                        )}
                      </>
                    ) : (
                      <span className="muted">Not yet chosen</span>
                    )}
                  </td>
                  <td className="row-actions">
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => void handleIssueToken(device)}
                      disabled={issuing === device.id || !online}
                    >
                      {issuing === device.id ? "Issuing…" : "Enrol"}
                    </button>
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => setTunneling(device)}
                      disabled={!online || !device.has_wireguard_key}
                      title={
                        device.has_wireguard_key
                          ? undefined
                          : "Enrol the device with a WireGuard key first"
                      }
                    >
                      Tunnel
                    </button>
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => setEditing(device)}
                      disabled={!online}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      className="btn-quiet btn-danger-quiet"
                      onClick={() => setDeleting(device)}
                      disabled={!online}
                    >
                      Retire
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {Boolean(devices.error) && devices.data && (
        <div className="notice notice-warning" role="alert" style={{ marginTop: 16 }}>
          <span aria-hidden="true">⚠</span>
          <div>
            <strong>This list may be out of date.</strong>
            <p>The last refresh failed. What you see was loaded earlier.</p>
          </div>
          <button type="button" className="btn-quiet" onClick={devices.reload}>
            Retry
          </button>
        </div>
      )}

      {(creating || editing) && (
        <DeviceFormDialog
          device={editing}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onSaved={(device, wasNew) => {
            devices.mutate((current) => {
              const list = current ?? [];
              return wasNew
                ? [...list, device]
                : list.map((d) => (d.id === device.id ? device : d));
            });
            notify.success(
              wasNew ? `${device.name} was added` : `${device.name} was updated`,
              wasNew ? "Issue an enrolment token when the device is ready to install." : undefined,
            );
            setCreating(false);
            setEditing(null);
          }}
        />
      )}

      {token && <TokenDialog {...token} onClose={() => setToken(null)} />}

      {tunneling && (
        <VpnProvisionDialog
          device={tunneling}
          onClose={() => setTunneling(null)}
          onProvisioned={(updated) =>
            devices.mutate((current) =>
              (current ?? []).map((d) => (d.id === updated.id ? updated : d)),
            )
          }
        />
      )}

      <ConfirmDialog
        open={deleting !== null}
        title="Retire this device?"
        busy={deleteBusy}
        confirmLabel="Retire device"
        body={
          <>
            <p>
              <strong>{deleting?.name}</strong> will stop being able to report.
            </p>
            <p className="muted">
              Its credential is destroyed immediately, so it cannot send detections or
              heartbeats even if it is still powered on. Any cameras assigned to it are
              detached but kept. History already recorded is preserved.
            </p>
          </>
        }
        onConfirm={() => void handleDelete()}
        onCancel={() => setDeleting(null)}
      />
    </Layout>
  );
}

/* ------------------------------------------------------------------ create / edit */

function DeviceFormDialog({
  device,
  onClose,
  onSaved,
}: {
  device: EdgeDevice | null;
  onClose: () => void;
  onSaved: (device: EdgeDevice, wasNew: boolean) => void;
}) {
  const isNew = device === null;
  const sites = useResource(listSites, []);
  const siteOptions = useMemo(
    () => [
      // A device can legitimately be registered before anyone decides where it goes, so
      // unlike a camera this stays optional.
      { value: "", label: "Not assigned yet" },
      ...(sites.data ?? []).map((s) => ({ value: s.id, label: s.name })),
    ],
    [sites.data],
  );

  const form = useForm({
    name: {
      initial: device?.name ?? "",
      label: "Name",
      validate: combine(required("Name"), maxLength(120, "Name")),
    },
    site_id: { initial: device?.site_id ?? "", label: "Site" },
    device_type: { initial: device?.device_type ?? "raspberry_pi", label: "Hardware" },
    role: { initial: device?.role ?? "gateway", label: "Role" },
    serial_number: { initial: device?.serial_number ?? "", label: "Serial number" },
  });

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    form.setSubmitting(true);
    form.setFormError(undefined);
    try {
      const body = {
        name: form.values.name,
        site_id: form.values.site_id || undefined,
        device_type: form.values.device_type,
        role: form.values.role,
        serial_number: form.values.serial_number || undefined,
      };
      const saved = isNew
        ? await createEdgeDevice(body)
        : await updateEdgeDevice(device.id, body);
      onSaved(saved, isNew);
    } catch (err) {
      form.applyServerError(err);
    } finally {
      form.setSubmitting(false);
    }
  });

  return (
    <Dialog open title={isNew ? "Add edge device" : `Edit ${device.name}`} onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />

        <Field {...form.field("name")} label="Name" required placeholder="Warehouse gateway" />
        <Field
          {...form.field("site_id")}
          label="Site"
          options={siteOptions}
          hint="Optional — a device can be registered before anyone decides where it goes."
        />
        <Field
          {...form.field("device_type")}
          label="Hardware"
          options={DEVICE_TYPES}
          hint="Recorded so capacity can be planned against what is actually deployed."
        />
        <Field
          {...form.field("role")}
          label="Role"
          options={ROLES}
          hint="A gateway costs the server roughly half a core per camera. A device that runs models locally costs almost nothing."
        />
        <Field
          {...form.field("serial_number")}
          label="Serial number"
          hint="Optional. If set, the enrolment token can only be redeemed on this exact device."
        />

        <FormActions
          submitting={form.submitting}
          submitLabel={isNew ? "Add device" : "Save changes"}
          onCancel={onClose}
        />
      </form>
    </Dialog>
  );
}

/* --------------------------------------------------------------------- enrolment */

/** Shows the enrolment token, once.
 *
 *  A value that genuinely cannot be retrieved again needs the interface to say so before
 *  the user closes the dialog, not after. The copy button and the explicit warning exist
 *  because the alternative is someone clicking away and needing a second token issued —
 *  which silently invalidates the first, and is confusing if they did not expect it.
 */
function TokenDialog({
  device,
  token,
  onClose,
}: {
  device: EdgeDevice;
  token: EnrolmentToken;
  onClose: () => void;
}) {
  const notify = useNotifications();
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(token.token);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2500);
    } catch {
      // Clipboard access is denied in some contexts. Say so rather than appearing to
      // succeed — the user needs to know to select it by hand.
      notify.notify({
        kind: "warning",
        title: "Could not copy automatically",
        detail: "Select the token and copy it manually.",
      });
    }
  }

  return (
    <Dialog
      open
      title={`Enrolment token for ${device.name}`}
      onClose={onClose}
      footer={<button type="button" onClick={onClose}>Done</button>}
    >
      <div className="notice notice-warning" style={{ marginBottom: 16 }}>
        <span aria-hidden="true">⚠</span>
        <div>
          <strong>This is shown once.</strong>
          <p>
            It is not stored anywhere it can be read back. If you lose it you will have to
            issue a new one, which invalidates this token.
          </p>
        </div>
      </div>

      <div className="token-display">
        <code className="mono">{token.token}</code>
        <button type="button" onClick={() => void copy()}>
          {copied ? "Copied" : "Copy"}
        </button>
      </div>

      <p className="muted" style={{ marginTop: 12 }}>
        Expires {new Date(token.expires_at).toLocaleString()}. It can be redeemed once, and
        after that the device authenticates with its own credential instead.
      </p>
    </Dialog>
  );
}

/* ---------------------------------------------------------------- VPN provisioning */

/** Allocates and renders this device's WireGuard tunnel.
 *
 *  The deployment guide's own templates hardcode one address for every client and give
 *  every peer the run of the whole /24 — harmless for one site, and a way for one
 *  tenant's device to route to another's cameras once there is a second. This dialog is
 *  built so nobody ever hand-copies those templates again: opening it allocates a real,
 *  collision-free address and renders the two config blocks directly, each scoped to
 *  exactly this one device.
 */
function VpnProvisionDialog({
  device,
  onClose,
  onProvisioned,
}: {
  device: EdgeDevice;
  onClose: () => void;
  onProvisioned: (device: EdgeDevice) => void;
}) {
  const notify = useNotifications();
  const [lanCidr, setLanCidr] = useState(device.lan_cidr ?? "");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | undefined>();
  const [copied, setCopied] = useState<"server" | "client" | null>(null);

  // Opening the dialog allocates the address (or re-renders the existing one) - that is
  // the whole point of this screen, not a side effect of it, so it happens on mount
  // rather than waiting for the operator to press something.
  const provisioning = useResource(
    () => provisionVpn(device.id, { lan_cidr: device.lan_cidr ?? undefined }),
    [device.id],
  );

  async function copy(kind: "server" | "client", text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(kind);
      window.setTimeout(() => setCopied((c) => (c === kind ? null : c)), 2500);
    } catch {
      notify.notify({
        kind: "warning",
        title: "Could not copy automatically",
        detail: "Select the text and copy it manually.",
      });
    }
  }

  async function handleSave() {
    setSaving(true);
    setSaveError(undefined);
    try {
      const updated = await provisionVpn(device.id, { lan_cidr: lanCidr.trim() || undefined });
      provisioning.mutate(() => updated);
      onProvisioned(updated.device);
      notify.success(`Tunnel updated for ${device.name}`, updated.warnings[0]);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Could not update the tunnel.");
    } finally {
      setSaving(false);
    }
  }

  const result = provisioning.data;

  return (
    <Dialog
      open
      title={`Tunnel for ${device.name}`}
      onClose={onClose}
      footer={<button type="button" onClick={onClose}>Done</button>}
    >
      {provisioning.loading ? (
        <InlineSpinner label="Allocating" />
      ) : provisioning.error && !result ? (
        <div className="notice notice-warning" role="alert">
          <span aria-hidden="true">⚠</span>
          <div>
            <strong>Could not provision a tunnel.</strong>
            <p>
              {provisioning.error instanceof Error
                ? provisioning.error.message
                : "The request failed."}
            </p>
          </div>
        </div>
      ) : (
        result && (
          <>
            {saveError && (
              <div className="notice notice-warning" role="alert" style={{ marginBottom: 16 }}>
                <span aria-hidden="true">⚠</span>
                <div>
                  <strong>Could not update the tunnel.</strong>
                  <p>{saveError}</p>
                </div>
              </div>
            )}

            {result.warnings.map((w) => (
              <div className="notice notice-info" role="status" key={w} style={{ marginBottom: 16 }}>
                <span aria-hidden="true">ⓘ</span>
                <p>{w}</p>
              </div>
            ))}

            <p className="mono">
              Address <strong>{result.device.vpn_address}</strong> — allocated from the
              shared pool, never the guide&apos;s hardcoded one.
            </p>

            <label htmlFor="tunnel-lan-cidr">Site LAN this device tunnels (optional)</label>
            <div className="token-display">
              <input
                id="tunnel-lan-cidr"
                type="text"
                placeholder="192.168.1.0/24"
                value={lanCidr}
                onChange={(e) => setLanCidr(e.target.value)}
              />
              <button type="button" onClick={() => void handleSave()} disabled={saving}>
                {saving ? "Saving…" : "Save"}
              </button>
            </div>
            <p className="muted" style={{ marginTop: 4 }}>
              Leave blank if the only address reachable through this device is its own. A
              range already tunnelled by another device — including another tenant&apos;s
              — is refused, not silently shared.
            </p>

            <h3 style={{ marginTop: 20 }}>Paste onto the device</h3>
            <div className="token-display">
              <code className="mono" style={{ whiteSpace: "pre-wrap" }}>
                {result.client_config}
              </code>
              <button type="button" onClick={() => void copy("client", result.client_config)}>
                {copied === "client" ? "Copied" : "Copy"}
              </button>
            </div>

            <h3 style={{ marginTop: 16 }}>Paste into the platform&apos;s WireGuard server</h3>
            <div className="token-display">
              <code className="mono" style={{ whiteSpace: "pre-wrap" }}>
                {result.server_peer_config}
              </code>
              <button
                type="button"
                onClick={() => void copy("server", result.server_peer_config)}
              >
                {copied === "server" ? "Copied" : "Copy"}
              </button>
            </div>
            <p className="muted" style={{ marginTop: 8 }}>
              <code className="mono">AllowedIPs</code> here is this device&apos;s own
              address only — never a wider range another peer could also claim.
            </p>
          </>
        )
      )}
    </Dialog>
  );
}

