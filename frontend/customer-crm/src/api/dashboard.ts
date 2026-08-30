import { apiFetch } from "./client";

/** This tenant's own summary counts. Mirrors backend/tenant_api/app/api/dashboard.py. */

export interface Dashboard {
  sites_count: number;
  cameras_count: number;
  incidents_open_count: number;
  incidents_total_count: number;
  team_members_count: number;
}

export function getDashboard() {
  return apiFetch<Dashboard>("/api/v1/tenant/dashboard");
}
