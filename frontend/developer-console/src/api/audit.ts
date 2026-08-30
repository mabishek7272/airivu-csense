import { apiFetch } from "./client";

/** Cross-tenant audit trail for platform operators. Mirrors
 *  backend/admin_api/app/api/audit.py.
 */

export interface AuditEvent {
  id: string;
  tenant_id: string | null;
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
  tenant_id?: string;
  action?: string;
  outcome?: string;
  cursor?: string;
} = {}) {
  const search = new URLSearchParams();
  if (params.tenant_id) search.set("tenant_id", params.tenant_id);
  if (params.action) search.set("action", params.action);
  if (params.outcome) search.set("outcome", params.outcome);
  if (params.cursor) search.set("cursor", params.cursor);
  const qs = search.toString();
  return apiFetch<AuditEventPage>(`/api/v1/admin/audit-events${qs ? `?${qs}` : ""}`);
}
