import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiRequestError } from "../api/client";
import { listDetections } from "../api/resources";
import type { Detection } from "../api/types";
import { CountBadge } from "../components/Badges";
import { EvidenceThumb } from "../components/EvidenceThumb";
import { Layout, SiteTime, relativeTime } from "../components/Layout";
import { EmptyPanel, ErrorPanel, LoadingList } from "../components/States";

const EVENT_TYPES = [
  "person.restricted_zone",
  "fire.smoke",
  "kitchen.ppe",
  "vehicle.plate",
];

function formatAddress(address: Record<string, string> | null): string | null {
  if (!address) return null;
  return [address.line1, address.city, address.country].filter(Boolean).join(", ") || null;
}

function DetectionCard({ detection }: { detection: Detection }) {
  const { camera, location } = detection;
  const address = formatAddress(location.address);

  return (
    <article className="card detection-card">
      <EvidenceThumb
        evidence={detection.evidence}
        alt={
          `Snapshot from ${camera.camera_name} at ${location.site_name}, ` +
          `showing ${detection.objects.length} detected object` +
          `${detection.objects.length === 1 ? "" : "s"} outlined`
        }
      />

      <div className="detection-body">
        <div className="detection-title">
          <h2>{detection.event_type}</h2>
          <CountBadge count={detection.objects.length} noun="object" />
          {detection.incident_number != null && (
            <Link to={`/incidents/${detection.incident_id}`}>
              Incident #{detection.incident_number}
            </Link>
          )}
        </div>

        <dl className="meta-grid">
          <div className="meta-item">
            <dt>Captured</dt>
            <dd>
              <SiteTime iso={detection.captured_at} timezone={location.timezone} />
              <div style={{ color: "var(--text-muted)", fontSize: 12 }}>
                {relativeTime(detection.captured_at)}
              </div>
            </dd>
          </div>

          <div className="meta-item">
            <dt>Camera</dt>
            <dd>
              {camera.camera_name}
              <div className="mono" style={{ color: "var(--text-muted)" }}>
                {camera.camera_code}
              </div>
            </dd>
          </div>

          <div className="meta-item">
            <dt>Location</dt>
            <dd>
              {location.site_name}
              {location.zone_name && ` · ${location.zone_name}`}
              {address && (
                <div style={{ color: "var(--text-muted)", fontSize: 12 }}>{address}</div>
              )}
              {location.latitude != null && location.longitude != null && (
                <div className="mono" style={{ color: "var(--text-muted)" }}>
                  {location.latitude.toFixed(5)}, {location.longitude.toFixed(5)}
                </div>
              )}
            </dd>
          </div>

          <div className="meta-item">
            <dt>Detection ID</dt>
            <dd className="mono">{detection.detection_id}</dd>
          </div>
        </dl>

        {detection.objects.length > 0 && (
          <ul className="boundary-list">
            {detection.objects.map((object, index) => (
              <li key={`${object.track_id ?? index}`}>
                <span className="badge badge-neutral">{object.class_name}</span>
                <span>{(object.confidence * 100).toFixed(0)}%</span>
                <span>
                  x {object.bbox.x1.toFixed(3)}–{object.bbox.x2.toFixed(3)} · y{" "}
                  {object.bbox.y1.toFixed(3)}–{object.bbox.y2.toFixed(3)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </article>
  );
}

export function DetectionsPage() {
  const [detections, setDetections] = useState<Detection[] | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [eventType, setEventType] = useState("");
  const [withEvidenceOnly, setWithEvidenceOnly] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);

  const load = useCallback(async () => {
    setDetections(null);
    setError(null);
    try {
      const page = await listDetections({
        event_type: eventType || undefined,
        with_evidence_only: withEvidenceOnly || undefined,
        limit: 20,
      });
      setDetections(page.items);
      setNextCursor(page.next_cursor);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.body.message : "Unexpected error.");
    }
  }, [eventType, withEvidenceOnly]);

  useEffect(() => {
    void load();
  }, [load]);

  async function loadMore() {
    if (!nextCursor) return;
    setLoadingMore(true);
    try {
      const page = await listDetections({
        event_type: eventType || undefined,
        with_evidence_only: withEvidenceOnly || undefined,
        limit: 20,
        cursor: nextCursor,
      });
      setDetections((current) => [...(current ?? []), ...page.items]);
      setNextCursor(page.next_cursor);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.body.message : "Unexpected error.");
    } finally {
      setLoadingMore(false);
    }
  }

  return (
    <Layout>
      <div className="page-head">
        <div>
          <h1>Detections</h1>
          <p>Every observation the pipeline recorded, newest first.</p>
        </div>
      </div>

      <div className="toolbar">
        <label>
          <span className="visually-hidden">Filter by event type</span>
          <select value={eventType} onChange={(event) => setEventType(event.target.value)}>
            <option value="">All event types</option>
            {EVENT_TYPES.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
        </label>

        <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <input
            type="checkbox"
            checked={withEvidenceOnly}
            onChange={(event) => setWithEvidenceOnly(event.target.checked)}
          />
          With snapshot only
        </label>

        <button type="button" onClick={() => void load()}>
          Refresh
        </button>
      </div>

      {error && <ErrorPanel message={error} onRetry={() => void load()} />}
      {!error && detections === null && <LoadingList />}

      {!error && detections?.length === 0 && (
        <EmptyPanel title="No detections yet">
          <p>
            Detections appear here as cameras report them. If you expected some, check that
            a pipeline is assigned and the camera is online.
          </p>
        </EmptyPanel>
      )}

      {!error && detections && detections.length > 0 && (
        <>
          <div className="card-list">
            {detections.map((detection) => (
              <DetectionCard key={detection.detection_id} detection={detection} />
            ))}
          </div>
          {nextCursor && (
            <div style={{ marginTop: 16, textAlign: "center" }}>
              <button type="button" onClick={() => void loadMore()} disabled={loadingMore}>
                {loadingMore ? "Loading…" : "Load more"}
              </button>
            </div>
          )}
        </>
      )}
    </Layout>
  );
}
