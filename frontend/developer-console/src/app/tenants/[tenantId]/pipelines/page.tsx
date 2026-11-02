"use client";

import { useState } from "react";
import { useAuth } from "@/auth/AuthContext";
import type { AssignableVersion, Assignment, Camera } from "@/api/tenantInfra";
import { createAssignment, listAssignablePipelines, listAssignments, listCameras, revokeAssignment } from "@/api/tenantInfra";
import { Dialog } from "@/components/Dialog";
import { ErrorSummary, Field, FormActions, onSubmitHandler, useForm } from "@/components/Form";
import { useNotifications } from "@/components/Notifications";
import { EmptyPanel, FailureState, LoadingRows } from "@/components/States";
import { useOnlineStatus } from "@/hooks/useNetwork";
import { useResource } from "@/hooks/useResource";

/** Points one of this tenant's cameras at a published pipeline version. Pipelines
 * themselves are authored/published elsewhere (Developer Console → Pipelines, platform-
 * wide) - this page only assigns an already-published one to a camera.
 */
export default function TenantPipelinesPage({ params }: { params: { tenantId: string } }) {
  const { tenantId } = params;
  const { isAuthenticated } = useAuth();
  const online = useOnlineStatus();
  const notify = useNotifications();

  const cameras = useResource(() => listCameras(tenantId), [isAuthenticated, tenantId]);
  const assignable = useResource(() => listAssignablePipelines(tenantId), [isAuthenticated, tenantId]);
  const [assigningFor, setAssigningFor] = useState<Camera | null>(null);
  const [assignments, setAssignments] = useState<Record<string, Assignment[]>>({});
  const [loadingFor, setLoadingFor] = useState<string | null>(null);
  const [revokingId, setRevokingId] = useState<string | null>(null);

  async function loadAssignments(camera: Camera) {
    setLoadingFor(camera.id);
    try {
      const rows = await listAssignments(tenantId, camera.id);
      setAssignments((current) => ({ ...current, [camera.id]: rows }));
    } catch (err) {
      notify.error(`Could not load pipelines for ${camera.name}`, err instanceof Error ? err.message : undefined);
    } finally {
      setLoadingFor(null);
    }
  }

  async function handleRevoke(camera: Camera, assignment: Assignment) {
    setRevokingId(assignment.id);
    try {
      await revokeAssignment(tenantId, assignment.id);
      await loadAssignments(camera);
      notify.success(`Revoked ${assignment.pipeline_code}`);
    } catch (err) {
      notify.error("Could not revoke", err instanceof Error ? err.message : undefined);
    } finally {
      setRevokingId(null);
    }
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h2>Pipelines</h2>
          <p>{cameras.data ? `${cameras.data.length} camera${cameras.data.length === 1 ? "" : "s"}` : " "}</p>
        </div>
      </div>

      {cameras.loading ? (
        <LoadingRows rows={3} columns={2} />
      ) : cameras.error && !cameras.data ? (
        <FailureState error={cameras.error} online={online} onRetry={cameras.reload} entity="cameras" />
      ) : cameras.data && cameras.data.length === 0 ? (
        <EmptyPanel title="No cameras yet" icon="○">
          <p>Add a camera first, then assign a pipeline to it.</p>
        </EmptyPanel>
      ) : (
        <div className="card">
          <table className="data-table">
            <caption className="visually-hidden">Cameras</caption>
            <thead>
              <tr>
                <th scope="col">Camera</th>
                <th scope="col">Pipeline</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {(cameras.data ?? []).map((camera) => {
                const rows = assignments[camera.id];
                const active = rows?.filter((a) => a.status === "active") ?? [];
                return (
                  <tr key={camera.id}>
                    <td>{camera.name}</td>
                    <td>
                      {rows === undefined ? (
                        <button type="button" className="btn-quiet" onClick={() => loadAssignments(camera)} disabled={loadingFor === camera.id}>
                          {loadingFor === camera.id ? "Loading…" : "Show"}
                        </button>
                      ) : active.length === 0 ? (
                        <span className="muted">Not assigned</span>
                      ) : (
                        active.map((a) => (
                          <div key={a.id}>
                            <span className="mono">{a.pipeline_code} v{a.pipeline_version_number}</span>
                            <span className="muted"> · {a.runtime_location} · priority {a.priority}</span>{" "}
                            <button type="button" className="btn-quiet" onClick={() => handleRevoke(camera, a)} disabled={revokingId === a.id}>
                              Revoke
                            </button>
                          </div>
                        ))
                      )}
                    </td>
                    <td className="row-actions">
                      <button type="button" className="btn-quiet" onClick={() => setAssigningFor(camera)} disabled={!online}>
                        Assign pipeline
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {assigningFor && (
        <AssignDialog
          tenantId={tenantId}
          camera={assigningFor}
          assignableVersions={assignable.data ?? []}
          assignableLoading={assignable.loading}
          assignableError={assignable.error}
          onClose={() => setAssigningFor(null)}
          onAssigned={() => {
            loadAssignments(assigningFor);
            notify.success(`Pipeline assigned to ${assigningFor.name}`);
          }}
        />
      )}
    </>
  );
}

function AssignDialog({
  tenantId,
  camera,
  assignableVersions,
  assignableLoading,
  assignableError,
  onClose,
  onAssigned,
}: {
  tenantId: string;
  camera: Camera;
  assignableVersions: AssignableVersion[];
  assignableLoading: boolean;
  assignableError: unknown;
  onClose: () => void;
  onAssigned: (assignment: Assignment) => void;
}) {
  const [versionId, setVersionId] = useState("");
  const [runtimeLocation, setRuntimeLocation] = useState<"cloud" | "edge">("cloud");
  const [priority, setPriority] = useState("100");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | undefined>();

  const selected = assignableVersions.find((v) => v.pipeline_version_id === versionId) ?? null;

  async function handleSubmit(event: React.FormEvent) {
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
    setFormError(undefined);
    setSubmitting(true);
    try {
      const created = await createAssignment(tenantId, camera.id, {
        pipeline_version_id: selected.pipeline_version_id,
        runtime_location: runtimeLocation,
        priority: priorityNum,
      });
      onAssigned(created);
      onClose();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "That could not be saved.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open title={`Assign a pipeline to ${camera.name}`} onClose={onClose}>
      {assignableLoading ? (
        <p>Loading pipelines…</p>
      ) : assignableError ? (
        <p role="alert" className="error-panel">
          Could not load the published pipelines.
        </p>
      ) : assignableVersions.length === 0 ? (
        <div className="notice notice-warning">
          <strong>No pipeline is published yet.</strong>
          <p>Publish one from Pipelines first.</p>
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
            <select value={versionId} onChange={(e) => setVersionId(e.target.value)} required>
              <option value="">Choose a pipeline version…</option>
              {assignableVersions.map((v) => (
                <option key={v.pipeline_version_id} value={v.pipeline_version_id}>
                  {v.pipeline_name} v{v.version_number} — {v.use_case}
                </option>
              ))}
            </select>
          </label>
          {selected?.description && <p className="muted">{selected.description}</p>}
          <label>
            Runtime location
            <select value={runtimeLocation} onChange={(e) => setRuntimeLocation(e.target.value as "cloud" | "edge")}>
              <option value="cloud">Cloud</option>
              <option value="edge">Edge</option>
            </select>
          </label>
          <label>
            Priority
            <input type="number" min={0} max={1000} value={priority} onChange={(e) => setPriority(e.target.value)} />
          </label>
          <p className="muted" style={{ fontSize: 13 }}>
            A camera can run more than one pipeline, but each needs its own priority — two
            pipelines can&apos;t share the same slot. Default is 100; pick something else
            (e.g. 90 or 110) if this camera already has one assigned there.
          </p>
          <FormActions submitting={submitting} submitLabel="Assign" onCancel={onClose} />
        </form>
      )}
    </Dialog>
  );
}
