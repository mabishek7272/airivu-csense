import { useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router-dom";
import type { Camera } from "../api/cameras";
import { listCameras } from "../api/cameras";
import { ApiRequestError } from "../api/client";
import type { AssignableVersion, Assignment, AssignmentInput } from "../api/pipelines";
import {
  createAssignment,
  listAssignablePipelines,
  listCameraAssignments,
  revokeAssignment,
} from "../api/pipelines";
import { ConfirmDialog, Dialog } from "../components/Dialog";
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

/** Pipeline assignment: pointing a camera at a published detection pipeline.
 *
 *  **This is a thin layer over two resources this tenant doesn't own the other half of.**
 *  Cameras are managed on the Cameras page — this page only reads them (`listCameras`) to
 *  show each one's current assignment. Pipeline *versions* are authored and published by
 *  AIRIVU, not by a tenant — `GET /pipelines/assignable` (added alongside this page; see
 *  its own docstring in `backend/tenant_api/app/api/pipeline_assignments.py`) is
 *  deliberately the only slice of that catalogue a tenant ever sees: published versions
 *  only, and only the fields needed to choose one.
 *
 *  **Empty and no-results stay distinct**, the same discipline `CamerasPage.tsx` follows:
 *  "you have no cameras yet" points at the Cameras page rather than pretending this page
 *  can create one, and "no cameras match your filters" offers to clear them instead.
 */

type AssignmentFilter = "" | "assigned" | "unassigned";

export function PipelineAssignmentsPage() {
  const online = useOnlineStatus();
  const notify = useNotifications();

  const [search, setSearch] = useState("");
  const [assignmentFilter, setAssignmentFilter] = useState<AssignmentFilter>("");
  const [assigningFor, setAssigningFor] = useState<Camera | null>(null);
  const [revoking, setRevoking] = useState<{ assignment: Assignment; camera: Camera } | null>(null);
  const [revokeBusy, setRevokeBusy] = useState(false);

  const cameras = useResource(() => listCameras(), []);
  const assignable = useResource(() => listAssignablePipelines(), []);
  const slow = useSlowRequest(cameras.loading);

  // One active assignment per camera, keyed by camera id. Fetched only once the camera
  // list itself has arrived — there is nothing to look up before then — and re-fetched
  // whenever that list's own reference changes (a reload, or a local mutate after assign/
  // revoke below).
  //
  // `firstRealFetchDone` is deliberately separate from `assignments.loading`/`.data`.
  // The fetcher below has to return *something* before `cameras.data` exists (there is
  // nothing to look up yet), and useResource's own hasData tracking treats that first
  // resolved `{}` placeholder as "this resource has loaded" — so the moment the real
  // camera list arrives and the real per-camera fetch kicks off, useResource reports
  // `refreshing`, not `loading`, and every row below fell back to reading `undefined` out
  // of the still-{} data, rendering "Not assigned" on every camera (including ones with a
  // real active assignment) until that real fetch resolved. Gating the loading-row branch
  // on this ref instead — set only once inside the fetcher, after a real (possibly empty)
  // camera list has actually been looked up — fixes that without changing useResource's
  // own semantics, which other pages also rely on.
  const firstRealFetchDone = useRef(false);
  const assignments = useResource(async () => {
    const list = cameras.data;
    if (!list) return {} as Record<string, Assignment | null>;
    const perCamera = await Promise.all(list.map((c) => listCameraAssignments(c.id)));
    const map: Record<string, Assignment | null> = {};
    list.forEach((camera, i) => {
      map[camera.id] = perCamera[i].find((a) => a.status === "active") ?? null;
    });
    firstRealFetchDone.current = true;
    return map;
  }, [cameras.data]);

  const visible = useMemo(() => {
    if (!cameras.data) return [];
    const term = search.trim().toLowerCase();
    return cameras.data.filter((c) => {
      if (term) {
        const matches =
          c.name.toLowerCase().includes(term) ||
          c.code.toLowerCase().includes(term) ||
          (c.site_name ?? "").toLowerCase().includes(term);
        if (!matches) return false;
      }
      if (assignmentFilter && assignments.data) {
        const hasActive = Boolean(assignments.data[c.id]);
        if (assignmentFilter === "assigned" && !hasActive) return false;
        if (assignmentFilter === "unassigned" && hasActive) return false;
      }
      return true;
    });
  }, [cameras.data, search, assignmentFilter, assignments.data]);

  const hasFilters = search.trim() !== "" || assignmentFilter !== "";

  async function handleRevoke() {
    if (!revoking) return;
    setRevokeBusy(true);
    const { assignment, camera } = revoking;
    try {
      await revokeAssignment(assignment.id);
      assignments.mutate((current) => ({ ...(current ?? {}), [camera.id]: null }));
      notify.success(`${camera.name} is no longer running ${assignment.pipeline_code}`);
      setRevoking(null);
    } catch (err) {
      notify.error(
        "Could not revoke that assignment",
        err instanceof Error ? err.message : undefined,
      );
    } finally {
      setRevokeBusy(false);
    }
  }

  return (
    <Layout>
      <div className="page-header">
        <div>
          <h1>Pipelines</h1>
          <p className="muted">
            {cameras.data
              ? `${cameras.data.length} camera${cameras.data.length === 1 ? "" : "s"}`
              : " "}
            {(cameras.refreshing || assignments.refreshing) && (
              <>
                {" "}
                <InlineSpinner label="Refreshing" />
              </>
            )}
          </p>
        </div>
      </div>

      <div className="filter-bar">
        <label className="visually-hidden" htmlFor="pipeline-camera-search">
          Search cameras
        </label>
        <input
          id="pipeline-camera-search"
          type="search"
          placeholder="Search by camera name, code or site"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <label className="visually-hidden" htmlFor="pipeline-assignment-filter">
          Filter by assignment
        </label>
        <select
          id="pipeline-assignment-filter"
          value={assignmentFilter}
          onChange={(e) => setAssignmentFilter(e.target.value as AssignmentFilter)}
        >
          <option value="">Any assignment</option>
          <option value="assigned">Assigned</option>
          <option value="unassigned">Not assigned</option>
        </select>
        {hasFilters && (
          <button
            type="button"
            className="btn-quiet"
            onClick={() => {
              setSearch("");
              setAssignmentFilter("");
            }}
          >
            Clear
          </button>
        )}
      </div>

      {slow && cameras.loading && <SlowNetworkNotice />}

      {/* Loading is checked before empty, same reasoning as CamerasPage: a slow request
          must never read as "you have no cameras". */}
      {cameras.loading ? (
        <LoadingRows rows={4} columns={3} />
      ) : Boolean(cameras.error) && !cameras.data ? (
        <FailureState error={cameras.error} online={online} onRetry={cameras.reload} entity="cameras" />
      ) : visible.length === 0 && hasFilters ? (
        <NoResultsPanel
          query={search.trim() || undefined}
          entity="cameras"
          onClear={() => {
            setSearch("");
            setAssignmentFilter("");
          }}
        />
      ) : visible.length === 0 ? (
        <EmptyPanel title="No cameras yet" icon="◇">
          <p>
            A pipeline is assigned to a camera, and this account has none yet.{" "}
            <Link to="/cameras">Add a camera</Link> first.
          </p>
        </EmptyPanel>
      ) : (
        <div className="card">
          <table className="data-table">
            <caption className="visually-hidden">Pipeline assignments by camera</caption>
            <thead>
              <tr>
                <th scope="col">Camera</th>
                <th scope="col">Pipeline</th>
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
                  <td>
                    {!firstRealFetchDone.current && !assignments.error ? (
                      <InlineSpinner label="Loading assignment" />
                    ) : assignments.error && !firstRealFetchDone.current ? (
                      <span className="muted">
                        Could not load.{" "}
                        <button type="button" className="btn-quiet" onClick={assignments.reload}>
                          Retry
                        </button>
                      </span>
                    ) : (
                      <AssignmentCell assignment={assignments.data?.[camera.id] ?? null} />
                    )}
                  </td>
                  <td className="row-actions">
                    {assignments.data?.[camera.id] ? (
                      <button
                        type="button"
                        className="btn-quiet btn-danger-quiet"
                        onClick={() =>
                          setRevoking({ assignment: assignments.data![camera.id]!, camera })
                        }
                        disabled={!online}
                      >
                        Revoke
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="btn-quiet"
                        onClick={() => setAssigningFor(camera)}
                        disabled={!online}
                      >
                        Assign pipeline
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

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

      {assigningFor && (
        <AssignDialog
          camera={assigningFor}
          assignableVersions={assignable.data ?? []}
          assignableLoading={assignable.loading}
          assignableError={assignable.error}
          onClose={() => setAssigningFor(null)}
          onAssigned={(assignment) => {
            assignments.mutate((current) => ({
              ...(current ?? {}),
              [assigningFor.id]: assignment,
            }));
            notify.success(
              `${assigningFor.name} is now running ${assignment.pipeline_code} v${assignment.pipeline_version_number}`,
            );
            setAssigningFor(null);
          }}
        />
      )}

      <ConfirmDialog
        open={revoking !== null}
        title="Revoke this pipeline assignment?"
        busy={revokeBusy}
        confirmLabel="Revoke assignment"
        body={
          <>
            <p>
              <strong>{revoking?.camera.name}</strong> will stop running{" "}
              <strong>
                {revoking?.assignment.pipeline_code} v{revoking?.assignment.pipeline_version_number}
              </strong>
              .
            </p>
            <p className="muted">
              This only stops the assignment — nothing pulls this camera's stream yet, so
              there is no live detection to interrupt.
            </p>
          </>
        }
        onConfirm={() => void handleRevoke()}
        onCancel={() => setRevoking(null)}
      />
    </Layout>
  );
}

/** What is actually running on a camera right now, or the plain fact that nothing is. */
function AssignmentCell({ assignment }: { assignment: Assignment | null }) {
  if (!assignment) {
    return <span className="muted">Not assigned</span>;
  }
  return (
    <>
      <span className="pill pill-ready">
        {assignment.pipeline_code} v{assignment.pipeline_version_number}
      </span>
      <div className="muted">
        Priority {assignment.priority} · {assignment.runtime_location}
      </div>
    </>
  );
}

/* ------------------------------------------------------------------------------ assign */

function AssignDialog({
  camera,
  assignableVersions,
  assignableLoading,
  assignableError,
  onClose,
  onAssigned,
}: {
  camera: Camera;
  assignableVersions: AssignableVersion[];
  assignableLoading: boolean;
  assignableError: unknown;
  onClose: () => void;
  onAssigned: (assignment: Assignment) => void;
}) {
  const [versionId, setVersionId] = useState("");
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [priority, setPriority] = useState("100");
  const [runtimeLocation, setRuntimeLocation] = useState<"cloud" | "edge">("cloud");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | undefined>();
  const [overrideErrors, setOverrideErrors] = useState<Record<string, string>>({});

  const selected = assignableVersions.find((v) => v.pipeline_version_id === versionId) ?? null;
  const overrideKeys = useMemo(
    () => (selected ? Object.keys(selected.allowed_overrides_schema).sort() : []),
    [selected],
  );

  function setOverride(key: string, value: string) {
    setOverrides((current) => ({ ...current, [key]: value }));
  }

  // A version change invalidates whatever override values were typed for the previous
  // one — different versions can allow entirely different keys, so carrying values
  // across would silently submit an override the newly-selected version never declared.
  function selectVersion(id: string) {
    setVersionId(id);
    setOverrides({});
    setOverrideErrors({});
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!selected) {
      setFormError("Choose a pipeline version.");
      return;
    }
    const priorityNum = Number(priority);
    if (!Number.isInteger(priorityNum) || priorityNum < 0 || priorityNum > 1000) {
      setFormError("Priority must be a whole number between 0 and 1000.");
      return;
    }

    // The same type check `pipeline_assignments.py`'s own `_validate_overrides` runs
    // server-side — done here too so a typo is caught before the round trip, not instead
    // of the server's own check.
    const tenantOverrides: Record<string, unknown> = {};
    const errors: Record<string, string> = {};
    for (const key of overrideKeys) {
      const raw = overrides[key]?.trim();
      if (!raw) continue; // every override is optional; an unset one just isn't sent
      const type = selected.allowed_overrides_schema[key];
      if (type === "number") {
        const num = Number(raw);
        if (Number.isNaN(num)) {
          errors[key] = "Must be a number.";
          continue;
        }
        tenantOverrides[key] = num;
      } else if (type === "boolean") {
        tenantOverrides[key] = raw === "true";
      } else {
        tenantOverrides[key] = raw;
      }
    }
    if (Object.keys(errors).length > 0) {
      setOverrideErrors(errors);
      return;
    }

    setFormError(undefined);
    setSubmitting(true);
    try {
      const body: AssignmentInput = {
        pipeline_version_id: selected.pipeline_version_id,
        tenant_overrides: tenantOverrides,
        runtime_location: runtimeLocation,
        priority: priorityNum,
      };
      const created = await createAssignment(camera.id, body);
      onAssigned(created);
    } catch (err) {
      setFormError(
        err instanceof ApiRequestError
          ? err.body.message
          : err instanceof Error
            ? err.message
            : "That could not be saved.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open title={`Assign a pipeline to ${camera.name}`} onClose={onClose}>
      {assignableLoading ? (
        <InlineSpinner label="Loading pipelines" />
      ) : assignableError ? (
        <p role="alert" className="error-panel">
          Could not load the pipelines available to assign.
        </p>
      ) : assignableVersions.length === 0 ? (
        <div className="notice notice-warning">
          <span aria-hidden="true">⚠</span>
          <div>
            <strong>No pipeline is published yet.</strong>
            <p>
              Pipelines are authored and published by AIRIVU, not from this account.
              Contact AIRIVU if you expect one to be available here.
            </p>
          </div>
        </div>
      ) : (
        <form onSubmit={handleSubmit} noValidate style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {formError && (
            <p role="alert" className="error-panel">
              {formError}
            </p>
          )}

          <label>
            Pipeline version
            <select value={versionId} onChange={(e) => selectVersion(e.target.value)} required>
              <option value="">Choose a pipeline version…</option>
              {assignableVersions.map((v) => (
                <option key={v.pipeline_version_id} value={v.pipeline_version_id}>
                  {v.pipeline_name} v{v.version_number} — {v.use_case}
                </option>
              ))}
            </select>
          </label>

          {selected?.description && <p className="muted">{selected.description}</p>}

          {overrideKeys.length > 0 && (
            <fieldset style={{ border: "1px solid var(--border)", borderRadius: 8, padding: 12 }}>
              <legend>Overrides for this camera</legend>
              <p className="muted" style={{ marginTop: 0 }}>
                Leave a field blank to use this version's default.
              </p>
              {overrideKeys.map((key) => {
                const type = selected!.allowed_overrides_schema[key];
                return (
                  <label key={key} style={{ display: "block", marginBottom: 8 }}>
                    <span className="mono">{key}</span>{" "}
                    <span className="muted">({type})</span>
                    {type === "boolean" ? (
                      <select
                        value={overrides[key] ?? ""}
                        onChange={(e) => setOverride(key, e.target.value)}
                      >
                        <option value="">Use default</option>
                        <option value="true">True</option>
                        <option value="false">False</option>
                      </select>
                    ) : (
                      <input
                        type={type === "number" ? "number" : "text"}
                        value={overrides[key] ?? ""}
                        onChange={(e) => setOverride(key, e.target.value)}
                      />
                    )}
                    {overrideErrors[key] && (
                      <p className="field-error">
                        <span aria-hidden="true">✕ </span>
                        {overrideErrors[key]}
                      </p>
                    )}
                  </label>
                );
              })}
            </fieldset>
          )}

          <div className="field-row">
            <label style={{ flex: 1 }}>
              Priority
              <input
                type="number"
                min={0}
                max={1000}
                value={priority}
                onChange={(e) => setPriority(e.target.value)}
              />
            </label>
            <label style={{ flex: 1 }}>
              Runtime
              <select
                value={runtimeLocation}
                onChange={(e) => setRuntimeLocation(e.target.value as "cloud" | "edge")}
              >
                <option value="cloud">Cloud</option>
                <option value="edge">Edge</option>
              </select>
            </label>
          </div>

          <div className="form-actions">
            <button type="submit" disabled={submitting || !versionId}>
              {submitting ? "Saving…" : "Assign pipeline"}
            </button>
            <button type="button" className="btn-quiet" onClick={onClose} disabled={submitting}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </Dialog>
  );
}
