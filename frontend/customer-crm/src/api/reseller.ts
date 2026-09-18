import { apiFetch } from "./client";

/** Reseller aggregate rollup. Mirrors backend/tenant_api/app/api/reseller.py's
 *  GET /child-tenants/rollup. */

export interface ChildTenantRollup {
  tenant_id: string;
  display_name: string;
  tenant_status: string;
  site_count: number;
  camera_count: number;
  active_incident_count: number;
  license_status: string | null;
  // List price only - no payment/invoicing system exists to know what was actually
  // billed (see migration 0058/0059). `null` means no active/grace license, or a
  // license whose plan has no price on record - not "$0".
  list_price_cents: number | null;
  currency: string | null;
}

export interface RollupSummary {
  child_tenant_count: number;
  total_sites: number;
  total_cameras: number;
  total_active_incidents: number;
  // Sum of every child's list_price_cents, NULLs counted as 0.
  total_monthly_list_price_cents: number;
  tenants: ChildTenantRollup[];
}

export function getChildTenantRollup() {
  return apiFetch<RollupSummary>("/api/v1/tenant/child-tenants/rollup");
}

/** Mirrors CreateChildTenantIn/CreateChildTenantOut in
 *  backend/tenant_api/app/api/reseller.py exactly. Only a reseller organization can call
 *  this - a non-reseller tenant gets a real `403 not_a_reseller`, surfaced by the caller
 *  the same way any other ApiRequestError is, not hidden client-side. */

export interface CreateChildTenantInput {
  organization_name: string;
  owner_email: string;
  owner_display_name: string;
}

export interface CreateChildTenantResult {
  tenant_id: string;
  organization_id: string;
  display_name: string;
  status: string;
  created_at: string;
  owner_email: string;
  // Set only when the invitation email could not actually be sent - see the backend's
  // own docstring (and memberships.ts's InviteResult, which the same case mirrors).
  invitation_link: string | null;
}

export function createChildTenant(body: CreateChildTenantInput) {
  return apiFetch<CreateChildTenantResult>("/api/v1/tenant/child-tenants", {
    method: "POST",
    body: JSON.stringify(body),
  });
}
