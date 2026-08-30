import { apiFetch } from "./client";

/** This tenant's own visibility into support grants against its account, and its own
 *  right to end one early. Mirrors backend/tenant_api/app/api/support.py.
 */

export interface SupportGrant {
  id: string;
  developer_email: string;
  ticket_reference: string;
  purpose: string;
  requested_scopes: string[];
  status: "requested" | "approved" | "denied" | "active" | "revoked" | "expired";
  starts_at: string | null;
  expires_at: string;
}

export function listActiveSupportGrants() {
  return apiFetch<SupportGrant[]>("/api/v1/tenant/support-grants?active_only=true");
}

export function revokeSupportGrant(id: string, reason: string) {
  return apiFetch<SupportGrant>(`/api/v1/tenant/support-grants/${id}/revoke`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}
