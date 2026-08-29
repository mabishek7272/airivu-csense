"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import type { PipelineVersionOut, PipelineVersionRow } from "@/api/pipelines";
import { VERSION_STATES, VERSION_TRANSITIONS, groupByPipeline, listPipelines } from "@/api/pipelines";
import { useAuth } from "@/auth/AuthContext";
import { Layout } from "@/components/Layout";
import { StateBadge } from "@/components/Badges";
import { CreatePipelineDialog } from "@/components/CreatePipelineDialog";
import { CreateVersionDialog } from "@/components/CreateVersionDialog";
import { PipelineTransitionDialog } from "@/components/PipelineTransitionDialog";
import { useNotifications } from "@/components/Notifications";
import { EmptyPanel, FailureState, LoadingRows, NoResultsPanel } from "@/components/States";
import { useOnlineStatus } from "@/hooks/useNetwork";
import { useResource } from "@/hooks/useResource";

export default function PipelinesPage() {
  const { isAuthenticated, isLoading } = useAuth();
  const router = useRouter();
  const online = useOnlineStatus();
  const notify = useNotifications();

  useEffect(() => {
    if (!isLoading && !isAuthenticated) router.replace("/login");
  }, [isAuthenticated, isLoading, router]);

  const [state, setState] = useState("");
  const [creatingPipeline, setCreatingPipeline] = useState(false);
  const [creatingVersionFor, setCreatingVersionFor] = useState<{ id: string; name: string } | null>(null);
  const [transitioning, setTransitioning] = useState<{
    version: PipelineVersionRow;
    action: "publish" | "deprecate";
  } | null>(null);

  const versions = useResource(() => listPipelines({ state: state || undefined }), [state]);
  const groups = useMemo(() => groupByPipeline(versions.data ?? []), [versions.data]);
  const hasFilters = state !== "";

  function applyTransition(updated: PipelineVersionOut) {
    versions.mutate((current) =>
      (current ?? []).map((v) => (v.version_id === updated.version_id ? updated : v)),
    );
    notify.success(`${updated.code} v${updated.version_number} → ${updated.state}`);
  }

  function applyNewVersion(created: PipelineVersionOut) {
    versions.mutate((current) => [
      // Drop this pipeline's version-less placeholder row (from GET's outer join,
      // still in local state from right after it was created) before adding the real
      // one - otherwise it lingers as a phantom "extra version" the server was never
      // actually asked about.
      ...(current ?? []).filter((v) => !(v.pipeline_id === created.pipeline_id && v.version_id === null)),
      created,
    ]);
    notify.success(`${created.code} v${created.version_number} created as draft`);
  }

  if (isLoading || !isAuthenticated) {
    return (
      <div className="auth-shell" aria-busy="true">
        <p>Loading…</p>
      </div>
    );
  }

  return (
    <Layout>
      <div className="page-header">
        <div>
          <h1>Pipelines</h1>
          <p>
            {versions.data
              ? `${groups.length} pipeline${groups.length === 1 ? "" : "s"}, ${versions.data.length} version${versions.data.length === 1 ? "" : "s"}`
              : " "}
          </p>
        </div>
        <button type="button" onClick={() => setCreatingPipeline(true)}>
          New pipeline
        </button>
      </div>

      <div className="filter-bar">
        <label className="visually-hidden" htmlFor="pipeline-state">
          Filter by state
        </label>
        <select id="pipeline-state" value={state} onChange={(e) => setState(e.target.value)}>
          <option value="">Any state</option>
          {VERSION_STATES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>

        {hasFilters && (
          <button type="button" className="btn-quiet" onClick={() => setState("")}>
            Clear
          </button>
        )}
      </div>

      {versions.loading ? (
        <LoadingRows rows={3} columns={5} />
      ) : Boolean(versions.error) && !versions.data ? (
        <FailureState error={versions.error} online={online} onRetry={versions.reload} entity="pipelines" />
      ) : groups.length === 0 && hasFilters ? (
        <NoResultsPanel entity="pipelines" onClear={() => setState("")} />
      ) : groups.length === 0 ? (
        <EmptyPanel title="No pipelines yet" icon="◇">
          <p>A pipeline defines which model runs against a camera&apos;s frames. Create one to get started.</p>
        </EmptyPanel>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
          {groups.map((group) => (
            <section key={group.pipelineId} className="card" style={{ padding: 16 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 8 }}>
                <div>
                  <h2 style={{ margin: "0 0 4px", fontSize: 15 }}>{group.name}</h2>
                  <p className="muted mono" style={{ margin: 0 }}>
                    {group.code} · {group.useCase}
                  </p>
                </div>
                <button
                  type="button"
                  className="btn-quiet"
                  onClick={() => setCreatingVersionFor({ id: group.pipelineId, name: group.name })}
                >
                  New version
                </button>
              </div>

              {group.versions.length === 0 ? (
                <p className="muted" style={{ marginTop: 12 }}>
                  No versions yet - create one to define what this pipeline actually runs.
                </p>
              ) : (
              <div style={{ overflowX: "auto", marginTop: 12 }}>
                <table className="data-table">
                  <caption className="visually-hidden">Versions of {group.name}</caption>
                  <thead>
                    <tr>
                      <th scope="col">Version</th>
                      <th scope="col">State</th>
                      <th scope="col">Model</th>
                      <th scope="col">Runtime</th>
                      <th scope="col">Definition digest</th>
                      <th scope="col">
                        <span className="visually-hidden">Actions</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {group.versions.map((v) => {
                      const targets = VERSION_TRANSITIONS[v.state] ?? [];
                      return (
                        <tr key={v.version_id}>
                          <td className="mono">v{v.version_number}</td>
                          <td>
                            <StateBadge state={v.state} />
                          </td>
                          <td className="mono">
                            {v.definition_json.stages.map((s) => s.model_name).join(", ")}
                          </td>
                          <td>{v.runtime_target}</td>
                          <td className="mono" title={v.definition_sha256}>
                            {v.definition_sha256.slice(0, 12)}…
                          </td>
                          <td className="row-actions">
                            {targets.map((target) => (
                              <button
                                key={target}
                                type="button"
                                className="btn-quiet"
                                onClick={() =>
                                  setTransitioning({
                                    version: v,
                                    action: target === "published" ? "publish" : "deprecate",
                                  })
                                }
                              >
                                {target === "published" ? "Publish" : "Deprecate"}
                              </button>
                            ))}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              )}
            </section>
          ))}
        </div>
      )}

      {creatingPipeline && (
        <CreatePipelineDialog
          onClose={() => setCreatingPipeline(false)}
          onCreated={() => {
            notify.success("Pipeline created");
            versions.reload();
          }}
        />
      )}

      {creatingVersionFor && (
        <CreateVersionDialog
          pipelineId={creatingVersionFor.id}
          pipelineName={creatingVersionFor.name}
          onClose={() => setCreatingVersionFor(null)}
          onCreated={applyNewVersion}
        />
      )}

      {transitioning && (
        <PipelineTransitionDialog
          version={transitioning.version}
          action={transitioning.action}
          onClose={() => setTransitioning(null)}
          onTransitioned={applyTransition}
        />
      )}
    </Layout>
  );
}
