import { apiFetch } from "./client";

/** Zones — the polygons that decide where a rule applies.
 *
 *  Coordinates are normalised 0..1 against the frame in both directions, so a zone means
 *  the same thing at any stream resolution. Nothing here ever holds pixels.
 */

export type ZonePoint = [number, number];

export interface Zone {
  id: string;
  site_id: string;
  site_name?: string | null;
  name: string;
  zone_type: string;
  privacy_level: string;
  polygon: ZonePoint[];
  status: string;
  /** Fraction of the frame covered. A zone far smaller than its author believed is the
   *  usual answer to "why does this rule never fire". */
  area_fraction: number;
  /** Rules pointing at this zone. Redrawing changes all of them. */
  rule_count: number;
  created_at: string;
}

export interface ZoneInput {
  site_id: string;
  name: string;
  zone_type?: string;
  privacy_level?: string;
  polygon: ZonePoint[];
}

const BASE = "/api/v1/tenant/zones";

export function listZones(params: { site_id?: string } = {}) {
  const search = new URLSearchParams();
  if (params.site_id) search.set("site_id", params.site_id);
  const qs = search.toString();
  return apiFetch<Zone[]>(`${BASE}${qs ? `?${qs}` : ""}`);
}

export function createZone(body: ZoneInput) {
  return apiFetch<Zone>(BASE, { method: "POST", body: JSON.stringify(body) });
}

export function updateZone(
  id: string,
  body: Partial<ZoneInput> & { status?: string },
) {
  return apiFetch<Zone>(`${BASE}/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}

export function deleteZone(id: string) {
  return apiFetch<void>(`${BASE}/${id}`, { method: "DELETE" });
}
