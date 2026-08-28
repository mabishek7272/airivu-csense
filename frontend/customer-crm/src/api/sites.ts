import { apiFetch } from "./client";

export interface SiteAddress {
  line1?: string;
  line2?: string;
  city?: string;
  state?: string;
  postal_code?: string;
  country?: string;
}

export interface Site {
  id: string;
  name: string;
  code: string;
  timezone: string;
  address?: SiteAddress | null;
  latitude?: number | null;
  longitude?: number | null;
  status: string;
  /** Shown in the listing so the cost of removing a site is visible before anyone tries. */
  camera_count: number;
  device_count: number;
  created_at: string;
}

export interface SiteInput {
  name: string;
  code: string;
  timezone: string;
  address?: SiteAddress;
  latitude?: number;
  longitude?: number;
}

const BASE = "/api/v1/tenant/sites";

export function listSites() {
  return apiFetch<Site[]>(BASE);
}

export function createSite(body: SiteInput) {
  return apiFetch<Site>(BASE, { method: "POST", body: JSON.stringify(body) });
}

export function updateSite(id: string, body: Partial<SiteInput> & { status?: string }) {
  return apiFetch<Site>(`${BASE}/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}

export function deleteSite(id: string) {
  return apiFetch<void>(`${BASE}/${id}`, { method: "DELETE" });
}

/** The zones the server will accept, served from its own IANA database.
 *
 *  Fetched rather than hardcoded so the picker cannot offer a value the validator
 *  rejects — a dropdown that produces a 422 is worse than a free-text field, because the
 *  user has no reason to suspect their choice was the problem. */
export function listTimezones() {
  return apiFetch<string[]>(`${BASE}/meta/timezones`);
}
