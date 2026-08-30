"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import type { AuditEvent } from "@/api/audit";
import { listAuditEvents } from "@/api/audit";
import { useAuth } from "@/auth/AuthContext";
import { Layout } from "@/components/Layout";
import { EmptyPanel, ErrorPanel, LoadingRows } from "@/components/States";

/** Cross-tenant audit trail - `audit.read`, `platform_admin`. Read-only, append-only:
 *  exactly what `record_audit_and_outbox` has written across every tenant, optionally
 *  narrowed to one.
 */
export default function AuditPage() {
  const { isAuthenticated, isLoading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!isLoading && !isAuthenticated) router.replace("/login");
  }, [isAuthenticated, isLoading, router]);

  const [events, setEvents] = useState<AuditEvent[] | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [tenantId, setTenantId] = useState("");
  const [action, setAction] = useState("");
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
        tenant_id: tenantId || undefined,
        action: action || undefined,
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
    if (isAuthenticated) void load(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated, tenantId, action]);

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
          <h1>Audit log</h1>
          <p>Every recorded action, across every tenant, append-only.</p>
        </div>
      </div>

      <div className="filter-bar">
        <label className="visually-hidden" htmlFor="audit-tenant">
          Filter by tenant id
        </label>
        <input
          id="audit-tenant"
          placeholder="Tenant id (blank = all)"
          value={tenantId}
          onChange={(e) => setTenantId(e.target.value)}
        />
        <label className="visually-hidden" htmlFor="audit-action">
          Filter by action
        </label>
        <input
          id="audit-action"
          placeholder="Filter by action (e.g. model.promote)"
          value={action}
          onChange={(e) => setAction(e.target.value)}
        />
      </div>

      {loading ? (
        <LoadingRows rows={6} columns={6} />
      ) : error ? (
        <ErrorPanel
          message="Could not load the audit log."
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
                  <th scope="col">Tenant</th>
                  <th scope="col">Action</th>
                  <th scope="col">Actor</th>
                  <th scope="col">Target</th>
                  <th scope="col">Outcome</th>
                </tr>
              </thead>
              <tbody>
                {(events ?? []).map((event) => (
                  <tr key={event.id}>
                    <td>{new Date(event.occurred_at).toLocaleString()}</td>
                    <td className="mono">{event.tenant_id ? `${event.tenant_id.slice(0, 8)}…` : "platform"}</td>
                    <td className="mono">{event.action}</td>
                    <td className="mono">
                      {event.actor_type}
                      {event.actor_id ? ` · ${event.actor_id.slice(0, 8)}…` : ""}
                    </td>
                    <td className="mono">
                      {event.target_type ? `${event.target_type} · ${(event.target_id ?? "").slice(0, 8)}…` : "—"}
                    </td>
                    <td>
                      <span className={`badge ${event.outcome === "success" ? "badge-low" : "badge-critical"}`}>
                        {event.outcome}
                      </span>
                    </td>
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
