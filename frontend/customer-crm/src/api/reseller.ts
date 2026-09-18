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
}

export interface RollupSummary {
  child_tenant_count: number;
  total_sites: number;
  total_cameras: number;
  total_active_incidents: number;
  tenants: ChildTenantRollup[];
}

export function getChildTenantRollup() {
  return apiFetch<RollupSummary>("/api/v1/tenant/child-tenants/rollup");
}
