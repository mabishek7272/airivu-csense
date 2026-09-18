import { Link } from "react-router-dom";
import type { CameraTile } from "../api/dashboard";
import { relativeTime } from "./Layout";

/** Live camera wall: each camera's most recent evidence snapshot, refreshed on dashboard
 *  load. A camera with no snapshot yet (never triggered a detection) still gets a tile
 *  with a placeholder icon, rather than being silently dropped from the wall — a quiet
 *  camera is still a camera worth seeing at a glance.
 */
export function DashboardCameraWall({
  tiles,
  loading,
}: {
  tiles: CameraTile[] | null;
  loading: boolean;
}) {
  return (
    <div className="card" style={{ padding: "var(--space-4)" }}>
      <div
        className="mono"
        style={{
          fontSize: 10,
          letterSpacing: "0.12em",
          color: "var(--text-muted)",
          marginBottom: 12,
        }}
      >
        CAMERAS
      </div>

      {loading ? (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))",
            gap: "var(--space-3)",
          }}
        >
          {Array.from({ length: 4 }, (_, i) => (
            <div
              key={i}
              className="skeleton"
              style={{ aspectRatio: "4 / 3", borderRadius: "var(--radius-sm)" }}
              aria-hidden="true"
            />
          ))}
        </div>
      ) : !tiles || tiles.length === 0 ? (
        <p className="muted" style={{ margin: 0, fontSize: 13 }}>
          No cameras yet.{" "}
          <Link to="/cameras" className="btn-quiet" style={{ display: "inline" }}>
            Add a camera
          </Link>
        </p>
      ) : (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))",
            gap: "var(--space-3)",
          }}
        >
          {tiles.map((tile) => (
            <CameraTileView key={tile.camera_id} tile={tile} />
          ))}
        </div>
      )}

      <Link to="/cameras" className="btn-quiet" style={{ marginTop: 12, display: "inline-block" }}>
        View all cameras
      </Link>
    </div>
  );
}

function CameraTileView({ tile }: { tile: CameraTile }) {
  return (
    <Link
      to="/cameras"
      style={{
        display: "block",
        textDecoration: "none",
        color: "inherit",
        borderRadius: "var(--radius-sm)",
        overflow: "hidden",
        border: "1px solid var(--hair)",
      }}
    >
      <div
        style={{
          width: "100%",
          aspectRatio: "4 / 3",
          background: "var(--surface-sunken)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          overflow: "hidden",
        }}
      >
        {tile.thumbnail_url ? (
          <img
            src={tile.thumbnail_url}
            alt=""
            style={{ width: "100%", height: "100%", objectFit: "cover" }}
          />
        ) : (
          <span aria-hidden="true" style={{ fontSize: "1.75rem", color: "var(--ink-faint)" }}>
            📷
          </span>
        )}
      </div>
      <div style={{ padding: "var(--space-2)" }}>
        <div style={{ fontSize: 12.5, fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {tile.camera_name}
        </div>
        <div className="mono muted" style={{ fontSize: 9.5, marginTop: 2 }}>
          {tile.captured_at ? relativeTime(tile.captured_at) : "No snapshot yet"}
        </div>
      </div>
    </Link>
  );
}
