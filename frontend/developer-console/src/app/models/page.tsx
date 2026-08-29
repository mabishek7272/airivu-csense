"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import type { ModelVersionOut } from "@/api/modelRegistry";
import {
  CLASSIFICATIONS,
  STATES,
  formatBytes,
  groupByModel,
  listModelVersions,
} from "@/api/modelRegistry";
import { useAuth } from "@/auth/AuthContext";
import { Layout } from "@/components/Layout";
import { ClassificationBadge, StateBadge } from "@/components/Badges";
import { PromoteDialog } from "@/components/PromoteDialog";
import { useNotifications } from "@/components/Notifications";
import { EmptyPanel, FailureState, LoadingRows, NoResultsPanel } from "@/components/States";
import { useOnlineStatus } from "@/hooks/useNetwork";
import { useResource } from "@/hooks/useResource";

export default function ModelsPage() {
  const { isAuthenticated, isLoading } = useAuth();
  const router = useRouter();
  const online = useOnlineStatus();
  const notify = useNotifications();

  useEffect(() => {
    if (!isLoading && !isAuthenticated) router.replace("/login");
  }, [isAuthenticated, isLoading, router]);

  const [state, setState] = useState("");
  const [classification, setClassification] = useState("");
  const [promoting, setPromoting] = useState<ModelVersionOut | null>(null);

  const versions = useResource(
    () => listModelVersions({ state: state || undefined, classification: classification || undefined }),
    [state, classification],
  );

  const groups = useMemo(() => groupByModel(versions.data ?? []), [versions.data]);
  const hasFilters = state !== "" || classification !== "";

  function applyPromotion(updated: ModelVersionOut) {
    versions.mutate((current) =>
      (current ?? []).map((v) => (v.id === updated.id ? updated : v)),
    );
    notify.success(
      `${updated.model_name} ${updated.version_label} promoted to ${updated.state}`,
    );
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
          <h1>Models</h1>
          <p>
            {versions.data
              ? `${groups.length} model${groups.length === 1 ? "" : "s"}, ${versions.data.length} version${versions.data.length === 1 ? "" : "s"}`
              : " "}
          </p>
        </div>
      </div>

      <div className="filter-bar">
        <label className="visually-hidden" htmlFor="model-state">
          Filter by state
        </label>
        <select id="model-state" value={state} onChange={(e) => setState(e.target.value)}>
          <option value="">Any state</option>
          {STATES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>

        <label className="visually-hidden" htmlFor="model-classification">
          Filter by classification
        </label>
        <select
          id="model-classification"
          value={classification}
          onChange={(e) => setClassification(e.target.value)}
        >
          <option value="">Any classification</option>
          {CLASSIFICATIONS.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>

        {hasFilters && (
          <button
            type="button"
            className="btn-quiet"
            onClick={() => {
              setState("");
              setClassification("");
            }}
          >
            Clear
          </button>
        )}
      </div>

      {versions.loading ? (
        <LoadingRows rows={4} columns={7} />
      ) : Boolean(versions.error) && !versions.data ? (
        <FailureState
          error={versions.error}
          online={online}
          onRetry={versions.reload}
          entity="models"
        />
      ) : groups.length === 0 && hasFilters ? (
        <NoResultsPanel
          entity="models"
          onClear={() => {
            setState("");
            setClassification("");
          }}
        />
      ) : groups.length === 0 ? (
        <EmptyPanel title="No models are registered yet" icon="◇">
          <p>
            Once a model artifact is registered, its versions appear here for review and
            promotion through their lifecycle.
          </p>
        </EmptyPanel>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
          {groups.map((group) => (
            <section key={group.modelName} className="card" style={{ padding: 16 }}>
              <h2 style={{ margin: "0 0 4px", fontSize: 15 }}>{group.modelName}</h2>
              <p className="muted mono" style={{ margin: "0 0 12px" }}>
                {group.taskCode}
              </p>
              <div style={{ overflowX: "auto" }}>
                <table className="data-table">
                  <caption className="visually-hidden">
                    Versions of {group.modelName}
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">Version</th>
                      <th scope="col">State</th>
                      <th scope="col">Classification</th>
                      <th scope="col">Framework</th>
                      <th scope="col">Runtime</th>
                      <th scope="col">Size</th>
                      <th scope="col">License</th>
                      <th scope="col">Digest</th>
                      <th scope="col">
                        <span className="visually-hidden">Actions</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {group.versions.map((v) => (
                      <tr key={v.id}>
                        <td className="mono">{v.version_label}</td>
                        <td>
                          <StateBadge state={v.state} />
                        </td>
                        <td>
                          <ClassificationBadge classification={v.access_classification} />
                        </td>
                        <td>{v.framework}</td>
                        <td>{v.runtime}</td>
                        <td>{formatBytes(v.size_bytes)}</td>
                        <td>{v.license ?? "—"}</td>
                        <td className="mono" title={v.artifact_sha256}>
                          {v.artifact_sha256.slice(0, 12)}…
                        </td>
                        <td className="row-actions">
                          <button
                            type="button"
                            className="btn-quiet"
                            onClick={() => setPromoting(v)}
                          >
                            Promote
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          ))}
        </div>
      )}

      {promoting && (
        <PromoteDialog
          version={promoting}
          onClose={() => setPromoting(null)}
          onPromoted={applyPromotion}
        />
      )}
    </Layout>
  );
}
