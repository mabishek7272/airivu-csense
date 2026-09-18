import type { Camera } from "../api/cameras";

interface CameraGridProps {
  cameras: Camera[];
  onProbe: (camera: Camera) => void;
  onWatch: (camera: Camera) => void;
  onEditCredentials: (camera: Camera) => void;
  onEdit: (camera: Camera) => void;
  onDelete: (camera: Camera) => void;
  probingId: string | null;
  disabled?: boolean;
}

/** Grid view for cameras: 4-column layout showing status, name, and actions. */
export function CameraGrid({
  cameras,
  onProbe,
  onWatch,
  onEditCredentials,
  onEdit,
  onDelete,
  probingId,
  disabled,
}: CameraGridProps) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
        gap: "var(--space-4)",
      }}
    >
      {cameras.map((camera) => (
        <div
          key={camera.id}
          className="card"
          style={{
            display: "flex",
            flexDirection: "column",
            padding: "var(--space-4)",
          }}
        >
          {/* Thumbnail placeholder */}
          <div
            style={{
              width: "100%",
              aspectRatio: "16 / 9",
              backgroundColor: "var(--surface-sunken)",
              borderRadius: "var(--radius-sm)",
              marginBottom: "var(--space-3)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: "3rem",
              color: "var(--ink-faint)",
            }}
          >
            📷
          </div>

          {/* Camera info */}
          <div style={{ flex: 1 }}>
            <strong>{camera.name}</strong>
            <div className="muted mono" style={{ fontSize: "0.85em", marginTop: "var(--space-1)" }}>
              {camera.code}
            </div>
            {camera.site_name && (
              <div className="muted" style={{ fontSize: "0.85em", marginTop: "var(--space-1)" }}>
                {camera.site_name}
              </div>
            )}

            {/* Status badge */}
            <div style={{ marginTop: "var(--space-2)" }}>
              <span className={`pill pill-${camera.status}`}>{camera.status}</span>
            </div>

            {camera.last_error && (
              <div
                className="muted"
                style={{
                  fontSize: "0.75em",
                  marginTop: "var(--space-1)",
                  maxHeight: "2.4em",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
                title={camera.last_error}
              >
                {camera.last_error}
              </div>
            )}
          </div>

          {/* Action buttons */}
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: "var(--space-2)",
              marginTop: "var(--space-3)",
              paddingTop: "var(--space-3)",
              borderTop: "1px solid var(--hair)",
            }}
          >
            <button
              type="button"
              className="primary"
              style={{ width: "100%", padding: "var(--space-2)" }}
              onClick={() => onWatch(camera)}
              disabled={disabled || camera.status !== "ready"}
              title={
                camera.status !== "ready"
                  ? "Probe this camera successfully before it can be watched live."
                  : undefined
              }
            >
              Watch live
            </button>
            <div style={{ display: "flex", gap: "var(--space-1)" }}>
              <button
                type="button"
                className="btn-quiet"
                style={{ flex: 1, padding: "var(--space-1)" }}
                onClick={() => void onProbe(camera)}
                disabled={probingId === camera.id || disabled}
              >
                {probingId === camera.id ? "Testing…" : "Test"}
              </button>
              <button
                type="button"
                className="btn-quiet"
                style={{ flex: 1, padding: "var(--space-1)" }}
                onClick={() => onEditCredentials(camera)}
                disabled={disabled}
              >
                Credential
              </button>
              <button
                type="button"
                className="btn-quiet"
                style={{ flex: 1, padding: "var(--space-1)" }}
                onClick={() => onEdit(camera)}
                disabled={disabled}
              >
                Edit
              </button>
            </div>
            <button
              type="button"
              className="btn-quiet btn-danger-quiet"
              style={{ width: "100%", padding: "var(--space-1)" }}
              onClick={() => onDelete(camera)}
              disabled={disabled}
            >
              Remove
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
