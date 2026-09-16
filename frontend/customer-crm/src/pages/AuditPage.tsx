import { useEffect, useState } from "react";
import type { AuditEvent } from "../api/audit";
import { listAuditEvents } from "../api/audit";
import { ApiRequestError } from "../api/client";
import { Layout, relativeTime } from "../components/Layout";
import { EmptyPanel, ErrorPanel, LoadingRows } from "../components/States";

/** A tenant's own audit trail - `audit.read`, owner-only. Append-only: nothing here is
 *  editable, this is a read of exactly what `record_audit_and_outbox` has written since
 *  the tenant was created - every feature in this product writes through it.
 */
export function AuditPage() {
  const [events, setEvents] = useState<AuditEvent[] | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [action, setAction] = useState("");
  const [outcome, setOutcome] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function load(reset: boolean) {
    if (reset) {
      setLoading(true);
      setError(null);
    } else {
      setLoadingMore(true);
    }
    try {
      const page = await listAuditEvents({
        action: action || undefined,
        outcome: outcome || undefined,
        cursor: reset ? undefined : (nextCursor ?? undefined),
      });
      setEvents((current) => (reset ? page.items : [...(current ?? []), ...page.items]));
      setNextCursor(page.next_cursor);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }

  useEffect(() => {
    void load(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [action, outcome]);

  return (
    <Layout>
      <div className="page-header">
        <div>
          <h1>Audit log</h1>
          <p className="muted">Every recorded action on this tenant, append-only.</p>
        </div>
      </div>

      <div className="filter-bar">
        <label className="visually-hidden" htmlFor="audit-action">
          Filter by action
        </label>
        <input
          id="audit-action"
          placeholder="Filter by action (e.g. camera.manage)"
          value={action}
          onChange={(e) => setAction(e.target.value)}
        />
        <label className="visually-hidden" htmlFor="audit-outcome">
          Filter by outcome
        </label>
        <select id="audit-outcome" value={outcome} onChange={(e) => setOutcome(e.target.value)}>
          <option value="">Any outcome</option>
          <option value="success">Success</option>
          <option value="failure">Failure</option>
        </select>
      </div>

      {loading ? (
        <LoadingRows rows={6} columns={5} />
      ) : error ? (
        <ErrorPanel
          message={error instanceof ApiRequestError ? error.body.message : "Could not load the audit log."}
          onRetry={() => void load(true)}
        />
      ) : events && events.length === 0 ? (
        <EmptyPanel title="No matching events" icon="◇">
          <p>Nothing recorded yet matches this filter.</p>
        </EmptyPanel>
      ) : (
        <>
          <div style={{ overflowX: "auto" }}>
            <table className="data-table">
              <caption className="visually-hidden">Audit events</caption>
              <thead>
                <tr>
                  <th scope="col">When</th>
                  <th scope="col">Action</th>
                  <th scope="col">Actor</th>
                  <th scope="col">Target</th>
                  <th scope="col">Outcome</th>
                  <th scope="col">Reason</th>
                </tr>
              </thead>
              <tbody>
                {(events ?? []).map((event) => (
                  <tr key={event.id}>
                    <td title={event.occurred_at}>{relativeTime(event.occurred_at)}</td>
                    <td className="mono">{event.action}</td>
                    <td>
                      {event.actor_display_name ? (
                        <>
                          {event.actor_display_name}
                          <span className="mono muted"> · {event.actor_type}</span>
                        </>
                      ) : (
                        <span className="mono">
                          {event.actor_type}
                          {event.actor_id ? ` · ${event.actor_id.slice(0, 8)}…` : ""}
                        </span>
                      )}
                    </td>
                    <td className="mono">
                      {event.target_type ? `${event.target_type} · ${(event.target_id ?? "").slice(0, 8)}…` : "—"}
                    </td>
                    <td>
                      <span className={`badge ${event.outcome === "success" ? "badge-low" : "badge-critical"}`}>
                        {event.outcome}
                      </span>
                    </td>
                    <td>{event.reason ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {nextCursor && (
            <button
              type="button"
              className="btn-quiet"
              style={{ marginTop: 16 }}
              disabled={loadingMore}
              onClick={() => void load(false)}
            >
              {loadingMore ? "Loading…" : "Load more"}
            </button>
          )}
        </>
      )}
    </Layout>
  );
}
