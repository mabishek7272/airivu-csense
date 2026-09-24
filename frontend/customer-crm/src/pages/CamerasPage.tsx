import { useMemo, useState } from "react";
import type { Camera } from "../api/cameras";
import { listCameras } from "../api/cameras";
import { LiveViewDialog } from "../components/LiveViewDialog";
import { Layout } from "../components/Layout";
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

/** View-only now — adding a camera, editing its connection, testing it, storing its
 *  credential, and removing it are all managed on this tenant's behalf from the
 *  Developer Console (see CHECKLIST.md's "move camera/site/zone/edge/rules/pipeline
 *  management to the Console" entry). This page keeps exactly what a client still does
 *  themselves: see what's there, and watch it live.
 */
export function CamerasPage() {
  const online = useOnlineStatus();

  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [watching, setWatching] = useState<Camera | null>(null);

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
        <LoadingRows rows={4} columns={3} />
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
        <EmptyPanel title="No cameras yet" icon="⌸">
          <p>Nothing has been added for this account yet. Contact AIRIVU to have a camera set up.</p>
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
                  </td>
                  <td className="row-actions">
                    <button
                      type="button"
                      className="btn-quiet"
                      onClick={() => setWatching(camera)}
                      disabled={!online || camera.status !== "ready"}
                      title={
                        camera.status !== "ready"
                          ? "This camera isn't ready yet — contact AIRIVU if that seems wrong."
                          : undefined
                      }
                    >
                      Watch live
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

      {watching && <LiveViewDialog camera={watching} onClose={() => setWatching(null)} />}
    </Layout>
  );
}
