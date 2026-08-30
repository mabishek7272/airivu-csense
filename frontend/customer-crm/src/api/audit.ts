import { apiFetch } from "./client";

/** A tenant's own audit trail - read-only. Mirrors backend/tenant_api/app/api/audit.py. */

export interface AuditEvent {
  id: string;
  actor_type: string;
  actor_id: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  outcome: "success" | "failure";
  reason: string | null;
  occurred_at: string;
}

export interface AuditEventPage {
  items: AuditEvent[];
  next_cursor: string | null;
}

export function listAuditEvents(params: {
  action?: string;
  target_type?: string;
  outcome?: string;
  cursor?: string;
} = {}) {
  const search = new URLSearchParams();
  if (params.action) search.set("action", params.action);
  if (params.target_type) search.set("target_type", params.target_type);
  if (params.outcome) search.set("outcome", params.outcome);
  if (params.cursor) search.set("cursor", params.cursor);
  const qs = search.toString();
  return apiFetch<AuditEventPage>(`/api/v1/tenant/audit-events${qs ? `?${qs}` : ""}`);
}
