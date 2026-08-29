import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ApiRequestError } from "../api/client";
import { listIncidents } from "../api/resources";
import type { IncidentSummary } from "../api/types";
import { CountBadge, SeverityBadge, StatusBadge } from "../components/Badges";
import { Layout, relativeTime } from "../components/Layout";
import { EmptyPanel, ErrorPanel, LoadingList } from "../components/States";
import { useNotifications } from "../components/Notifications";
import { useIncidentSocket } from "../hooks/useIncidentSocket";

// "Active" is the default: everything not yet closed. Defaulting to `open` alone means
// an incident disappears from the queue the moment someone acknowledges it — exactly when
// it becomes their responsibility — which is how work gets silently dropped.
const STATUS_FILTERS = [
  { value: "active", label: "Active (needs attention)" },
  { value: "", label: "All statuses" },
  { value: "open", label: "Open" },
  { value: "acknowledged", label: "Acknowledged" },
  { value: "investigating", label: "Investigating" },
  { value: "escalated", label: "Escalated" },
  { value: "resolved", label: "Resolved" },
  { value: "dismissed", label: "Dismissed" },
];

const SEVERITIES = ["critical", "high", "medium", "low", "info"];

function IncidentRow({ incident }: { incident: IncidentSummary }) {
  return (
    <article className="card" style={{ padding: "var(--space-4)" }}>
      <div className="detection-title">
        <h2 style={{ flex: 1 }}>
          <Link to={`/incidents/${incident.id}`}>
            #{incident.incident_number} · {incident.title}
          </Link>
        </h2>
        <SeverityBadge severity={incident.severity} />
        <StatusBadge status={incident.status} />
      </div>

      {incident.summary && (
        <p style={{ margin: "0 0 var(--space-2)", color: "var(--text-muted)" }}>
          {incident.summary}
        </p>
      )}

      <div
        style={{
          display: "flex",
          gap: "var(--space-4)",
          flexWrap: "wrap",
          alignItems: "center",
          color: "var(--text-muted)",
          fontSize: 13,
        }}
      >
        <CountBadge count={incident.detection_count} noun="detection" />
        <span>
          First seen <strong>{relativeTime(incident.first_detected_at)}</strong>
        </span>
        <span>
          Last seen <strong>{relativeTime(incident.last_detected_at)}</strong>
        </span>
        {incident.acknowledged_at && (
          <span>Acknowledged {relativeTime(incident.acknowledged_at)}</span>
        )}
      </div>
    </article>
  );
}

export function IncidentsPage() {
  const [incidents, setIncidents] = useState<IncidentSummary[] | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState("active");
  const [severity, setSeverity] = useState("");

  // `clearFirst`: the manual "Refresh" button and a filter change should show the
  // familiar loading skeleton - but a live update arriving from the socket should not
  // flash the whole list to a skeleton just because one row changed underneath it.
  const load = useCallback(async (clearFirst = true) => {
    if (clearFirst) setIncidents(null);
    setError(null);
    try {
      const page = await listIncidents({
        status: status || undefined,
        severity: severity || undefined,
        limit: 25,
      });
      setIncidents(page.items);
      setNextCursor(page.next_cursor);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.body.message : "Unexpected error.");
    }
  }, [status, severity]);

  useEffect(() => {
    void load();
  }, [load]);

  const notifications = useNotifications();
  const reloadTimer = useRef<number | null>(null);

  const socketStatus = useIncidentSocket(
    useCallback(
      (event) => {
        if (event.type === "incident.created.v1") {
          notifications.notify({
            kind: "info",
            title: "New incident",
            detail: "The list below has been updated.",
            durationMs: 6000,
          });
        }
        // A burst of events (several transitions landing in the same second) collapses
        // into one reload rather than one fetch per event.
        if (reloadTimer.current !== null) window.clearTimeout(reloadTimer.current);
        reloadTimer.current = window.setTimeout(() => void load(false), 300);
      },
      [load, notifications],
    ),
  );

  async function loadMore() {
    if (!nextCursor) return;
    try {
      const page = await listIncidents({
        status: status || undefined,
        severity: severity || undefined,
        limit: 25,
        cursor: nextCursor,
      });
      setIncidents((current) => [...(current ?? []), ...page.items]);
      setNextCursor(page.next_cursor);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.body.message : "Unexpected error.");
    }
  }

  return (
    <Layout>
      <div className="page-head">
        <div>
          <h1>Incidents</h1>
          <p>Detections the rules escalated into something worth a human decision.</p>
        </div>
        <span
          className={`live-indicator live-indicator-${socketStatus}`}
          role="status"
          aria-label={
            socketStatus === "open"
              ? "Live updates connected"
              : socketStatus === "connecting"
                ? "Live updates connecting"
                : "Live updates disconnected, retrying"
          }
        >
          <span aria-hidden="true" className="live-indicator-dot" />
          {socketStatus === "open" ? "Live" : socketStatus === "connecting" ? "Connecting…" : "Reconnecting…"}
        </span>
      </div>

      <div className="toolbar">
        <label>
          <span className="visually-hidden">Filter by status</span>
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            {STATUS_FILTERS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>

        <label>
          <span className="visually-hidden">Filter by severity</span>
          <select value={severity} onChange={(event) => setSeverity(event.target.value)}>
            <option value="">All severities</option>
            {SEVERITIES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>

        <button type="button" onClick={() => void load()}>
          Refresh
        </button>
      </div>

      {error && <ErrorPanel message={error} onRetry={() => void load()} />}
      {!error && incidents === null && <LoadingList rows={4} />}

      {!error && incidents?.length === 0 && (
        <EmptyPanel title={status === "active" ? "Nothing needs attention" : "No incidents match"}>
          <p>
            {status === "active"
              ? "Every incident has been resolved or dismissed."
              : "Try widening the filters above."}
          </p>
        </EmptyPanel>
      )}

      {!error && incidents && incidents.length > 0 && (
        <>
          <div className="card-list">
            {incidents.map((incident) => (
              <IncidentRow key={incident.id} incident={incident} />
            ))}
          </div>
          {nextCursor && (
            <div style={{ marginTop: 16, textAlign: "center" }}>
              <button type="button" onClick={() => void loadMore()}>
                Load more
              </button>
            </div>
          )}
        </>
      )}
    </Layout>
  );
}
