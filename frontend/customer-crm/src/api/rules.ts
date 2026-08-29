import { apiFetch } from "./client";

/** Detection rules — what turns a detection into an incident.
 *
 *  `warnings` is computed server-side from the values already on the rule (confidence too
 *  high for night, no zone, no cooldown) and is not something the client derives itself -
 *  so it can never drift from what the rule actually does.
 */

export interface RuleInput {
  site_id: string;
  camera_id?: string | null;
  zone_id?: string | null;
  name: string;
  type_code?: string;
  alertable_classes: string[];
  min_confidence?: number;
  severity?: string;
  min_roi_overlap?: number;
  min_consecutive_frames?: number;
  cooldown_seconds?: number;
  active_from_hour?: number | null;
  active_to_hour?: number | null;
}

export interface Rule {
  id: string;
  site_id: string;
  site_name?: string | null;
  camera_id?: string | null;
  camera_name?: string | null;
  zone_id?: string | null;
  zone_name?: string | null;
  name: string;
  type_code: string;
  alertable_classes: string[];
  min_confidence: number;
  severity: string;
  min_roi_overlap: number;
  min_consecutive_frames: number;
  cooldown_seconds: number;
  active_from_hour: number | null;
  active_to_hour: number | null;
  site_timezone?: string | null;
  status: string;
  warnings: string[];
  created_at: string;
}

const BASE = "/api/v1/tenant/rules";

export function listRules(params: { site_id?: string; camera_id?: string; status?: string } = {}) {
  const search = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v) search.set(k, String(v));
  }
  const qs = search.toString();
  return apiFetch<Rule[]>(`${BASE}${qs ? `?${qs}` : ""}`);
}

export function createRule(body: RuleInput) {
  return apiFetch<Rule>(BASE, { method: "POST", body: JSON.stringify(body) });
}

export function updateRule(id: string, body: Partial<RuleInput> & { status?: string }) {
  return apiFetch<Rule>(`${BASE}/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}

export function deleteRule(id: string) {
  return apiFetch<void>(`${BASE}/${id}`, { method: "DELETE" });
}
