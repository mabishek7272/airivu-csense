import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiRequestError } from "../api/client";
import { getDetection, getIncident, transitionIncident } from "../api/resources";
import { NEXT_STATUSES } from "../api/types";
import type { Detection, IncidentDetail, IncidentStatus } from "../api/types";
import { CountBadge, SeverityBadge, StatusBadge } from "../components/Badges";
import { EvidenceThumb } from "../components/EvidenceThumb";
import { Layout, SiteTime, relativeTime } from "../components/Layout";
import { ErrorPanel } from "../components/States";

/** The action that produces each target status. Closing actions are separated because
 *  they require a resolution code, and the state machine treats them as terminal. */
const ACTIONS: Record<string, { action: "acknowledge" | "investigate" | "resolve" | "dismiss"; label: string; closing: boolean }> = {
  acknowledged: { action: "acknowledge", label: "Acknowledge", closing: false },
  investigating: { action: "investigate", label: "Start investigating", closing: false },
  resolved: { action: "resolve", label: "Resolve", closing: true },
  dismissed: { action: "dismiss", label: "Dismiss", closing: true },
};

const RESOLUTION_CODES = [
  { value: "confirmed_true_positive", label: "Confirmed — action taken" },
  { value: "false_positive", label: "False positive" },
  { value: "duplicate", label: "Duplicate of another incident" },
  { value: "no_action_required", label: "Genuine, no action required" },
];

export function IncidentDetailPage() {
  const { incidentId } = useParams<{ incidentId: string }>();
  const navigate = useNavigate();

  const [incident, setIncident] = useState<IncidentDetail | null>(null);
  const [detections, setDetections] = useState<Detection[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [resolutionCode, setResolutionCode] = useState(RESOLUTION_CODES[0].value);
  const [note, setNote] = useState("");

  const load = useCallback(async () => {
    if (!incidentId) return;
    setError(null);
    try {
      const detail = await getIncident(incidentId);
      setIncident(detail);

      // Fetch a handful of linked detections for the evidence strip. An incident can
      // accumulate hundreds; loading them all would make the page unusable to show what
      // is essentially the same scene repeatedly.
      const sample = detail.detection_ids.slice(0, 6);
      const loaded = await Promise.allSettled(sample.map((id) => getDetection(id)));
      setDetections(
        loaded
          .filter((r): r is PromiseFulfilledResult<Detection> => r.status === "fulfilled")
          .map((r) => r.value),
      );
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.body.message : "Unexpected error.");
    }
  }, [incidentId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function runAction(target: IncidentStatus) {
    if (!incidentId) return;
    const spec = ACTIONS[target];
    if (!spec) return;

    setBusy(true);
    setActionError(null);
    try {
      await transitionIncident(incidentId, spec.action, {
        reason: note || undefined,
        ...(spec.closing ? { resolution_code: resolutionCode } : {}),
      });
      setNote("");
      await load();
    } catch (err) {
      // A 409 here means someone else moved the incident first. Reloading shows the
      // current truth rather than leaving a stale set of buttons on screen.
      setActionError(err instanceof ApiRequestError ? err.body.message : "Unexpected error.");
      if (err instanceof ApiRequestError && err.status === 409) await load();
    } finally {
      setBusy(false);
    }
  }

  if (error) {
    return (
      <Layout>
        <ErrorPanel message={error} onRetry={() => void load()} />
        <p style={{ marginTop: 16 }}>
          <button type="button" className="link" onClick={() => navigate("/incidents")}>
            Back to incidents
          </button>
        </p>
      </Layout>
    );
  }

  if (!incident) {
    return (
      <Layout>
        <div className="card state-panel" aria-busy="true">
          Loading incident…
        </div>
      </Layout>
    );
  }

  const available = NEXT_STATUSES[incident.status] ?? [];
  const closing = available.some((s) => ACTIONS[s]?.closing);
  const site = detections[0]?.location;

  return (
    <Layout>
      <div className="page-head">
        <div>
          <p style={{ marginBottom: 4 }}>
            <Link to="/incidents">← Incidents</Link>
          </p>
          <h1>
            #{incident.incident_number} · {incident.title}
          </h1>
          <p>{incident.summary}</p>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <SeverityBadge severity={incident.severity} />
          <StatusBadge status={incident.status} />
          <CountBadge count={incident.detection_count} noun="detection" />
        </div>
      </div>

      <div className="detail-grid">
        <div className="detail-main">
          <section className="card" style={{ padding: "var(--space-4)" }} aria-labelledby="evidence-heading">
            <h2 id="evidence-heading" style={{ fontSize: 15, marginTop: 0 }}>
              Evidence
            </h2>
            {detections.length === 0 ? (
              <p style={{ color: "var(--text-muted)", margin: 0 }}>
                No snapshots are linked to this incident.
              </p>
            ) : (
              <div
                style={{
                  display: "grid",
                  gap: "var(--space-3)",
                  gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))",
                }}
              >
                {detections.map((detection) => (
                  <figure key={detection.detection_id} style={{ margin: 0 }}>
                    <EvidenceThumb
                      evidence={detection.evidence}
                      alt={`Snapshot from ${detection.camera.camera_name}, ${detection.objects.length} objects outlined`}
                    />
                    <figcaption style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 4 }}>
                      <SiteTime iso={detection.captured_at} timezone={detection.location.timezone} />
                    </figcaption>
                  </figure>
                ))}
              </div>
            )}
          </section>

          <section className="card" style={{ padding: "var(--space-4)" }} aria-labelledby="timeline-heading">
            <h2 id="timeline-heading" style={{ fontSize: 15, marginTop: 0 }}>
              History
            </h2>
            <ol className="timeline">
              {incident.events.map((event, index) => (
                <li key={index} className={index === incident.events.length - 1 ? "is-current" : ""}>
                  <strong>{event.event_type.replace("incident.", "")}</strong>
                  {event.previous_status && (
                    <span style={{ color: "var(--text-muted)" }}>
                      {" "}
                      · {event.previous_status} → {event.new_status}
                    </span>
                  )}
                  <time dateTime={event.occurred_at}>
                    {relativeTime(event.occurred_at)} · by {event.actor_type}
                  </time>
                  {typeof event.payload?.reason === "string" && (
                    <p style={{ margin: "4px 0 0" }}>{event.payload.reason}</p>
                  )}
                </li>
              ))}
            </ol>
          </section>
        </div>

        <aside className="detail-aside">
          <section className="card" style={{ padding: "var(--space-4)" }} aria-labelledby="where-heading">
            <h2 id="where-heading" style={{ fontSize: 15, marginTop: 0 }}>
              Where and when
            </h2>
            <dl className="meta-grid" style={{ gridTemplateColumns: "1fr" }}>
              <div className="meta-item">
                <dt>First detected</dt>
                <dd>
                  <SiteTime iso={incident.first_detected_at} timezone={site?.timezone} />
                </dd>
              </div>
              <div className="meta-item">
                <dt>Last detected</dt>
                <dd>
                  <SiteTime iso={incident.last_detected_at} timezone={site?.timezone} />
                </dd>
              </div>
              {detections[0] && (
                <>
                  <div className="meta-item">
                    <dt>Camera</dt>
                    <dd>
                      {detections[0].camera.camera_name}
                      <div className="mono" style={{ color: "var(--text-muted)" }}>
                        {detections[0].camera.camera_code}
                      </div>
                    </dd>
                  </div>
                  <div className="meta-item">
                    <dt>Site</dt>
                    <dd>
                      {detections[0].location.site_name}
                      {detections[0].location.zone_name && ` · ${detections[0].location.zone_name}`}
                    </dd>
                  </div>
                </>
              )}
              <div className="meta-item">
                <dt>Incident ID</dt>
                <dd className="mono">{incident.id}</dd>
              </div>
            </dl>
          </section>

          <section className="card" style={{ padding: "var(--space-4)" }} aria-labelledby="actions-heading">
            <h2 id="actions-heading" style={{ fontSize: 15, marginTop: 0 }}>
              Actions
            </h2>

            {available.length === 0 ? (
              <p style={{ color: "var(--text-muted)", margin: 0 }}>
                This incident is {incident.status} and cannot be reopened. A recurrence
                creates a new incident, so each occurrence keeps its own history.
              </p>
            ) : (
              <>
                <div className="field">
                  <label htmlFor="note">Note (optional)</label>
                  <input
                    id="note"
                    type="text"
                    value={note}
                    onChange={(event) => setNote(event.target.value)}
                    placeholder="What did you find?"
                    maxLength={500}
                  />
                </div>

                {closing && (
                  <div className="field">
                    <label htmlFor="resolution">Resolution</label>
                    <select
                      id="resolution"
                      value={resolutionCode}
                      onChange={(event) => setResolutionCode(event.target.value)}
                    >
                      {RESOLUTION_CODES.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </div>
                )}

                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {available
                    .filter((target) => ACTIONS[target])
                    .map((target) => (
                      <button
                        key={target}
                        type="button"
                        className={target === "acknowledged" ? "primary" : undefined}
                        disabled={busy}
                        onClick={() => void runAction(target)}
                      >
                        {ACTIONS[target].label}
                      </button>
                    ))}
                </div>
              </>
            )}

            {actionError && (
              <p role="alert" style={{ color: "var(--critical)", marginBottom: 0 }}>
                {actionError}
              </p>
            )}
          </section>
        </aside>
      </div>
    </Layout>
  );
}
