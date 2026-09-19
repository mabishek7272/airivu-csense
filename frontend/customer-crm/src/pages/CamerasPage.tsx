import { useMemo, useState } from "react";
import type { Camera, CameraInput } from "../api/cameras";
import {
  createCamera,
  deleteCamera,
  listCameras,
  probeCamera,
  setCameraCredentials,
  updateCamera,
} from "../api/cameras";
import { ConfirmDialog, Dialog } from "../components/Dialog";
import { LiveViewDialog } from "../components/LiveViewDialog";
import { NvrDiscoveryDialog } from "../components/NvrDiscoveryDialog";
import { CameraGrid } from "../components/CameraGrid";
import {
  ErrorSummary,
  Field,
  FormActions,
  combine,
  maxLength,
  onSubmitHandler,
  pattern,
  required,
  useForm,
} from "../components/Form";
import { listSites } from "../api/sites";
import { Layout } from "../components/Layout";
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

/** Camera management: the full create/read/update/delete cycle.
 *
 *  Two things this page is careful about.
 *
 *  **Empty and no-results are different screens.** "You have not added a camera yet"
 *  offers a button that adds one. "No cameras match your filter" offers a button that
 *  clears the filter. Showing the first when the second is true hides the fix.
 *
 *  **Credentials are write-only, and the UI says so out loud.** There is no field that
 *  shows a stored password, because the API has no way to return one. The form says
 *  "leave blank to keep the current one" rather than rendering a fake row of dots, which
 *  would imply the value is retrievable.
 */

/** Parses `rtsp://[user[:pass]@]host[:port][/path]` into its parts, or null if `raw`
 *  isn't a recognizable RTSP URL yet (e.g. still mid-paste/mid-typing) — callers treat
 *  null as "don't touch the form", not as an error to surface. The WHATWG URL parser
 *  handles arbitrary schemes' `//user:pass@host:port/path` authority generically, so no
 *  hand-rolled regex is needed for the parsing itself. */
function parseRtspUrl(raw: string): {
  hostname: string;
  port: string;
  path: string;
  username: string;
  password: string;
} | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  let url: URL;
  try {
    url = new URL(trimmed);
  } catch {
    return null;
  }
  if (url.protocol !== "rtsp:" && url.protocol !== "rtsps:") return null;
  if (!url.hostname) return null;
  return {
    hostname: url.hostname,
    port: url.port || "554",
    path: url.pathname && url.pathname !== "/" ? url.pathname : "",
    username: decodeURIComponent(url.username),
    password: decodeURIComponent(url.password),
  };
}

export function CamerasPage() {
  const online = useOnlineStatus();
  const notify = useNotifications();

  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [editing, setEditing] = useState<Camera | null>(null);
  const [creating, setCreating] = useState(false);
  const [credentialsFor, setCredentialsFor] = useState<Camera | null>(null);
  const [deleting, setDeleting] = useState<Camera | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [probing, setProbing] = useState<string | null>(null);
  const [watching, setWatching] = useState<Camera | null>(null);
  const [discoveringNvr, setDiscoveringNvr] = useState(false);
  const [viewMode, setViewMode] = useState<"table" | "grid">("table");

  const cameras = useResource(() => listCameras({ status: statusFilter || undefined }), [
    statusFilter,
  ]);
  const slow = useSlowRequest(cameras.loading);

  // Filtering client-side because the list is small and the feedback is instant. If it
  // grows past a page this moves to the server, and the no-results state is already the
  // right shape for that.
  const visible = useMemo(() => {
    if (!cameras.data) return [];
    const term = search.trim().toLowerCase();
    if (!term) return cameras.data;
    return cameras.data.filter(
      (c) =>
        c.name.toLowerCase().includes(term) ||
        c.code.toLowerCase().includes(term) ||
        (c.hostname ?? "").toLowerCase().includes(term) ||
        (c.site_name ?? "").toLowerCase().includes(term),
    );
  }, [cameras.data, search]);

  const hasFilters = search.trim() !== "" || statusFilter !== "";

  async function handleProbe(camera: Camera) {
    setProbing(camera.id);
    try {
      const result = await probeCamera(camera.id);
      if (result.reachable) {
        notify.success(
          `${camera.name} is reachable`,
          [result.codec, result.framerate ? `${result.framerate} fps` : null]
            .filter(Boolean)
            .join(" · ") || result.detail,
        );
      } else {
        // A failed probe is a finding, not an error — the request worked, the camera did
        // not answer. A warning says that; an error would imply CSense broke.
        notify.notify({
          kind: "warning",
          title: `${camera.name} did not answer`,
          detail: result.detail,
        });
      }
      cameras.reload();
    } catch (err) {
      notify.error(
        "Could not probe that camera",
        err instanceof Error ? err.message : undefined,
      );
    } finally {
      setProbing(null);
    }
  }

  async function handleDelete() {
    if (!deleting) return;
    setDeleteBusy(true);
    const removed = deleting;
    try {
      await deleteCamera(removed.id);
      // Remove locally rather than re-fetching, so the row disappears the instant the
      // server confirms rather than after a second round trip.
      cameras.mutate((current) => (current ?? []).filter((c) => c.id !== removed.id));
      notify.success(`${removed.name} was removed`, "Its stored credential was destroyed.");
      setDeleting(null);
    } catch (err) {
      notify.error(
        `Could not remove ${removed.name}`,
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
          <h1>Cameras</h1>
          <p className="muted">
            {cameras.data
              ? `${cameras.data.length} camera${cameras.data.length === 1 ? "" : "s"}`
              : " "}
            {cameras.refreshing && (
              <>
                {" "}
                <InlineSpinner label="Refreshing" />
              </>
            )}
          </p>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {cameras.data && cameras.data.length > 0 && (
            <>
              <button
                type="button"
                className={viewMode === "table" ? "btn-quiet" : "btn-quiet"}
                onClick={() => setViewMode("table")}
                title="Table view"
              >
                ≡ Table
              </button>
              <button
                type="button"
                className={viewMode === "grid" ? "btn-quiet" : "btn-quiet"}
                onClick={() => setViewMode("grid")}
                title="Grid view"
              >
                ⊞ Grid
              </button>
            </>
          )}
          <button type="button" className="btn-quiet" onClick={() => setDiscoveringNvr(true)} disabled={!online}>
            Discover from NVR
          </button>
          <button type="button" onClick={() => setCreating(true)} disabled={!online}>
            Add camera
          </button>
        </div>
      </div>

      <div className="filter-bar">
        <label className="visually-hidden" htmlFor="camera-search">
          Search cameras
        </label>
        <input
          id="camera-search"
          type="search"
          placeholder="Search by name, code, host or site"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <label className="visually-hidden" htmlFor="camera-status">
          Filter by status
        </label>
        <select
          id="camera-status"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
        >
          <option value="">Any status</option>
          <option value="provisioning">Provisioning</option>
          <option value="ready">Ready</option>
          <option value="disabled">Disabled</option>
        </select>
        {hasFilters && (
          <button
            type="button"
            className="btn-quiet"
            onClick={() => {
              setSearch("");
              setStatusFilter("");
            }}
          >
            Clear
          </button>
        )}
      </div>

      {slow && cameras.loading && <SlowNetworkNotice />}

      {/* Order matters: loading is checked before empty, so a slow request never reads as
          "you have no cameras". */}
      {cameras.loading ? (
        <LoadingRows rows={4} columns={5} />
      ) : Boolean(cameras.error) && !cameras.data ? (
        <FailureState
          error={cameras.error}
          online={online}
          onRetry={cameras.reload}
          entity="camera"
        />
      ) : visible.length === 0 && hasFilters ? (
        <NoResultsPanel
          query={search.trim() || undefined}
          entity="cameras"
          onClear={() => {
            setSearch("");
            setStatusFilter("");
          }}
        />
      ) : visible.length === 0 ? (
        <EmptyPanel
          title="No cameras yet"
          icon="⌸"
          action={
            <button type="button" onClick={() => setCreating(true)}>
              Add your first camera
            </button>
          }
        >
          <p>
            Add a camera to start detecting. You will need its address and stream path —
            for a camera on a private network, add an edge device first.
          </p>
        </EmptyPanel>
      ) : viewMode === "grid" ? (
        <CameraGrid
          cameras={visible}
          onProbe={handleProbe}
          onWatch={(camera) => setWatching(camera)}
          onEditCredentials={(camera) => setCredentialsFor(camera)}
          onEdit={(camera) => setEditing(camera)}
          onDelete={(camera) => setDeleting(camera)}
          probingId={probing}
          disabled={!online}
        />
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
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {visible.map((camera) => (
                <tr key={camera.id}>
                  <td>
                    <strong>{camera.name}</strong>
                    <div className="muted mono">{camera.code}</div>
                    {camera.site_name && <div className="muted">{camera.site_name}</div>}
                  </td>
                  <td className="mono">
                    {camera.hostname ? (
                      <>
                        {camera.hostname}:{camera.rtsp_port ?? 554}
                        <div className="muted">{camera.main_stream_path ?? "—"}</div>
                      </>
                    ) : (
                      <span className="muted">Not configured</span>
                    )}
                  </td>
                  <td>
                    <span className={`pill pill-${camera.status}`}>{camera.status}</span>
                    {camera.last_error && (
                      <div className="muted" title={camera.last_error}>
                        {camera.last_error.slice(0, 40)}…
                      </div>
                    )}
                  </td>
                  <td>
                    {/* States whether one exists, never what it is. */}
                    {camera.has_credentials ? (
                      <span className="pill pill-ready">Stored</span>
                    ) : (
                      <span className="muted">None</span>
                    )}
                  </td>
                  <td className="row-actions">
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => setWatching(camera)}
                      disabled={!online || camera.status !== "ready"}
                      title={
                        camera.status !== "ready"
                          ? "Probe this camera successfully before it can be watched live."
                          : undefined
                      }
                    >
                      Watch live
                    </button>
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => void handleProbe(camera)}
                      disabled={probing === camera.id || !online}
                    >
                      {probing === camera.id ? "Testing…" : "Test"}
                    </button>
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => setCredentialsFor(camera)}
                      disabled={!online}
                    >
                      Credential
                    </button>
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => setEditing(camera)}
                      disabled={!online}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      className="btn-quiet btn-danger-quiet"
                      onClick={() => setDeleting(camera)}
                      disabled={!online}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* A refresh that failed while data is still on screen: show both, so the list stays
          usable and the staleness is admitted rather than hidden. */}
      {Boolean(cameras.error) && cameras.data && (
        <div className="notice notice-warning" role="alert" style={{ marginTop: 16 }}>
          <span aria-hidden="true">⚠</span>
          <div>
            <strong>This list may be out of date.</strong>
            <p>The last refresh failed. What you see was loaded earlier.</p>
          </div>
          <button type="button" className="btn-quiet" onClick={cameras.reload}>
            Retry
          </button>
        </div>
      )}

      {(creating || editing) && (
        <CameraFormDialog
          camera={editing}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onSaved={(camera, wasNew) => {
            cameras.mutate((current) => {
              const list = current ?? [];
              return wasNew
                ? [...list, camera]
                : list.map((c) => (c.id === camera.id ? camera : c));
            });
            notify.success(
              wasNew ? `${camera.name} was added` : `${camera.name} was updated`,
              wasNew && !camera.has_credentials
                ? "Add its credential next so it can connect."
                : undefined,
            );
            setCreating(false);
            setEditing(null);
          }}
        />
      )}

      {watching && <LiveViewDialog camera={watching} onClose={() => setWatching(null)} />}

      {discoveringNvr && (
        <NvrDiscoveryDialog
          onClose={() => setDiscoveringNvr(false)}
          onCreated={() => {
            setDiscoveringNvr(false);
            cameras.reload();
          }}
        />
      )}

      {credentialsFor && (
        <CredentialDialog
          camera={credentialsFor}
          onClose={() => setCredentialsFor(null)}
          onSaved={(camera) => {
            cameras.mutate((current) =>
              (current ?? []).map((c) => (c.id === camera.id ? camera : c)),
            );
            notify.success(`Credential saved for ${camera.name}`);
            setCredentialsFor(null);
          }}
        />
      )}

      <ConfirmDialog
        open={deleting !== null}
        title="Remove this camera?"
        busy={deleteBusy}
        confirmLabel="Remove camera"
        body={
          <>
            {/* Names the thing. "Are you sure?" is not a question anyone can answer. */}
            <p>
              <strong>{deleting?.name}</strong> ({deleting?.code}) will be removed from this
              site.
            </p>
            <p className="muted">
              Its stored credential is destroyed immediately. Incidents and evidence already
              recorded are kept and will still reference it.
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

function CameraFormDialog({
  camera,
  onClose,
  onSaved,
}: {
  camera: Camera | null;
  onClose: () => void;
  onSaved: (camera: Camera, wasNew: boolean) => void;
}) {
  const isNew = camera === null;
  const sites = useResource(listSites, []);
  const siteOptions = useMemo(
    () => [
      { value: "", label: "Choose a site…" },
      ...(sites.data ?? []).map((s) => ({
        // The timezone is in the label because it is the field's real consequence, and
        // picking the wrong site is far easier to notice here than at 3am.
        value: s.id,
        label: `${s.name} (${s.timezone})`,
      })),
    ],
    [sites.data],
  );

  const form = useForm({
    site_id: {
      initial: camera?.site_id ?? "",
      label: "Site",
      validate: required("Site"),
    },
    name: {
      initial: camera?.name ?? "",
      label: "Name",
      validate: combine(required("Name"), maxLength(120, "Name")),
    },
    code: {
      initial: camera?.code ?? "",
      label: "Code",
      validate: combine(
        required("Code"),
        pattern(
          /^[a-z0-9][a-z0-9_-]*$/,
          "Use lowercase letters, numbers, hyphens and underscores, starting with a letter or number.",
        ),
      ),
    },
    hostname: { initial: camera?.hostname ?? "", label: "Host" },
    rtsp_port: {
      initial: String(camera?.rtsp_port ?? 554),
      label: "Port",
      validate: (value) => {
        const port = Number(value);
        return Number.isInteger(port) && port >= 1 && port <= 65535
          ? undefined
          : "Port must be a whole number between 1 and 65535.";
      },
    },
    main_stream_path: {
      initial: camera?.main_stream_path ?? "",
      label: "Main stream path",
      // Caught here as well as by the server, because the reason is worth explaining at
      // the moment of typing rather than after a failed save.
      validate: pattern(
        /^\/[^\s@]*$/,
        "Give the path only, starting with / — not a full rtsp:// URL, which would carry the password.",
      ),
    },
    sub_stream_path: {
      initial: camera?.sub_stream_path ?? "",
      label: "Sub stream path",
      validate: pattern(/^\/[^\s@]*$/, "Give the path only, starting with /."),
    },
    username: { initial: camera?.username ?? "", label: "Username" },
  });

  // A pasted rtsp:// URL is a convenience that fills the fields below, not a field of
  // its own — its password half never enters `form.values` (which only ever holds
  // plain strings destined for the request body/error mapping); it is held here and
  // sent straight to setCameraCredentials after save, the same one-way path the
  // Credential dialog itself uses. Cleared whenever the pasted text stops parsing, so a
  // stale password from an earlier paste can never be applied to a later edit.
  const [rtspInput, setRtspInput] = useState("");
  const [rtspPassword, setRtspPassword] = useState<string | null>(null);
  const [rtspParsed, setRtspParsed] = useState(false);

  function handleRtspInput(value: string) {
    setRtspInput(value);
    const parsed = parseRtspUrl(value);
    if (!parsed) {
      setRtspParsed(false);
      setRtspPassword(null);
      return;
    }
    form.setValue("hostname", parsed.hostname);
    form.setValue("rtsp_port", parsed.port);
    if (parsed.path) form.setValue("main_stream_path", parsed.path);
    if (parsed.username) form.setValue("username", parsed.username);
    setRtspPassword(parsed.password || null);
    setRtspParsed(true);
  }

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    form.setSubmitting(true);
    form.setFormError(undefined);
    try {
      const body: CameraInput = {
        site_id: form.values.site_id,
        name: form.values.name,
        code: form.values.code,
        hostname: form.values.hostname || undefined,
        rtsp_port: Number(form.values.rtsp_port),
        main_stream_path: form.values.main_stream_path || undefined,
        sub_stream_path: form.values.sub_stream_path || undefined,
        username: form.values.username || undefined,
      };
      const saved = isNew
        ? await createCamera(body)
        : await updateCamera(camera.id, body);
      // A password came along with the pasted URL - store it exactly like the
      // Credential dialog would, right after the camera itself exists to attach it to.
      // Best-effort: the camera is already saved either way, so a credential failure
      // here is surfaced but does not roll back the save (matches this page's existing
      // "add credential next" nudge for a camera saved with none).
      const finalCamera = rtspPassword
        ? await setCameraCredentials(saved.id, {
            username: form.values.username || undefined,
            password: rtspPassword,
          })
        : saved;
      onSaved(finalCamera, isNew);
    } catch (err) {
      form.applyServerError(err);
    } finally {
      form.setSubmitting(false);
    }
  });

  return (
    <Dialog open title={isNew ? "Add camera" : `Edit ${camera.name}`} onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />

        <Field {...form.field("name")} label="Name" required placeholder="Loading Bay 2" />
        <Field
          {...form.field("code")}
          label="Code"
          required
          hint="A short identifier, unique within your account."
          placeholder="loading-bay-2"
        />
        {/* A picker, not a typed id. Asking someone to paste a UUID is not a form, and
            a site with no cameras is invisible until one is attached — so when there are
            none, the field says how to fix that rather than presenting an empty dropdown
            with no explanation. */}
        {sites.data && sites.data.length === 0 ? (
          <div className="notice notice-warning" style={{ marginBottom: 16 }}>
            <span aria-hidden="true">⚠</span>
            <div>
              <strong>You have no sites yet.</strong>
              <p>
                A camera belongs to a site, and the site's timezone decides when time-based
                rules apply. Add one under Sites first.
              </p>
            </div>
          </div>
        ) : (
          <Field
            {...form.field("site_id")}
            label="Site"
            required
            options={siteOptions}
            hint={
              sites.loading
                ? "Loading sites…"
                : "Its timezone is what time-based rules are evaluated in."
            }
          />
        )}
        <Field
          name="rtsp_url_paste"
          label="Paste RTSP URL"
          value={rtspInput}
          onChange={handleRtspInput}
          onBlur={() => {}}
          placeholder="rtsp://username:password@10.0.0.2:554/stream1"
          hint={
            rtspParsed
              ? `Parsed — filled in the fields below${rtspPassword ? ", including the password (stored as a credential on save, never shown here)" : ""}.`
              : "Optional. Paste a full rtsp:// URL and the fields below fill themselves in — nothing here is saved as typed."
          }
        />
        <div className="field-row">
          <Field {...form.field("hostname")} label="Host" placeholder="nvr.example.com" />
          <Field {...form.field("rtsp_port")} label="Port" type="number" />
        </div>
        <Field
          {...form.field("main_stream_path")}
          label="Main stream path"
          hint="Path only — /Streaming/Channels/101"
        />
        <Field
          {...form.field("sub_stream_path")}
          label="Sub stream path"
          hint="Lower resolution. Used for live view, which is far cheaper than the main stream."
        />
        <Field {...form.field("username")} label="Username" />

        <FormActions
          submitting={form.submitting}
          submitLabel={isNew ? "Add camera" : "Save changes"}
          onCancel={onClose}
        />
      </form>
    </Dialog>
  );
}

/* --------------------------------------------------------------------- credential */

function CredentialDialog({
  camera,
  onClose,
  onSaved,
}: {
  camera: Camera;
  onClose: () => void;
  onSaved: (camera: Camera) => void;
}) {
  const form = useForm({
    username: { initial: camera.username ?? "", label: "Username" },
    password: {
      initial: "",
      label: "Password",
      validate: required("Password"),
    },
  });

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    form.setSubmitting(true);
    try {
      const saved = await setCameraCredentials(camera.id, {
        username: form.values.username || undefined,
        password: form.values.password,
      });
      onSaved(saved);
    } catch (err) {
      form.applyServerError(err);
    } finally {
      form.setSubmitting(false);
    }
  });

  return (
    <Dialog open title={`Credential for ${camera.name}`} onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />

        {/* Said plainly rather than implied by a masked field of fake dots, which would
            suggest the stored value can be read back. It cannot. */}
        <div className="notice notice-info" style={{ marginBottom: 16 }}>
          <span aria-hidden="true">⚿</span>
          <div>
            <strong>
              {camera.has_credentials
                ? "A credential is already stored."
                : "No credential is stored yet."}
            </strong>
            <p>
              Stored credentials cannot be read back — not here, and not through the API.
              Entering one below replaces whatever is saved.
            </p>
          </div>
        </div>

        <Field {...form.field("username")} label="Username" />
        <Field
          {...form.field("password")}
          label="Password"
          type="password"
          required
          hint="Encrypted before it is stored, and never returned by any request."
        />

        <FormActions
          submitting={form.submitting}
          submitLabel="Save credential"
          onCancel={onClose}
        />
      </form>
    </Dialog>
  );
}

