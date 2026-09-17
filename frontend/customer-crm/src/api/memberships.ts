import { apiFetch } from "./client";

/** Team membership management. Mirrors backend/tenant_api/app/api/memberships.py. */

export interface Membership {
  id: string;
  user_id: string;
  email: string;
  display_name: string;
  role_name: "tenant_owner" | "tenant_operator" | "tenant_member" | "tenant_viewer";
  status: "invited" | "active" | "suspended" | "revoked";
  site_scope_mode: "all" | "selected" | "none";
  site_ids: string[];
  invited_at: string | null;
  accepted_at: string | null;
}

export interface InviteInput {
  email: string;
  display_name: string;
  role_name: "tenant_owner" | "tenant_operator" | "tenant_member" | "tenant_viewer";
  site_scope_mode?: "all" | "selected" | "none";
  site_ids?: string[];
}

export interface InviteResult extends Membership {
  // Set only when the invitation email could not actually be sent - see the backend's
  // own docstring for why this is the one case the token is ever returned at all.
  invitation_link: string | null;
}

export interface MembershipPatch {
  role_name?: "tenant_owner" | "tenant_operator" | "tenant_member" | "tenant_viewer";
  status?: "active" | "suspended" | "revoked";
  site_scope_mode?: "all" | "selected" | "none";
  site_ids?: string[];
}

const BASE = "/api/v1/tenant/memberships";

export function listMemberships() {
  return apiFetch<Membership[]>(BASE);
}

export function inviteMember(body: InviteInput) {
  return apiFetch<InviteResult>(BASE, { method: "POST", body: JSON.stringify(body) });
}

export function updateMembership(id: string, body: MembershipPatch) {
  return apiFetch<Membership>(`${BASE}/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}
