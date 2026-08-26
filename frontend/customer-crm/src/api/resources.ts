import { apiFetch } from "./client";
import type { Detection, IncidentDetail, IncidentSummary, Page } from "./types";

export interface DetectionFilters {
  camera_id?: string;
  site_id?: string;
  event_type?: string;
  since?: string;
  min_confidence?: number;
  with_evidence_only?: boolean;
  limit?: number;
  cursor?: string;
}

function query(params: Record<string, unknown>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    // Skip empties rather than sending `?camera_id=` — the server would reject a blank
    // UUID, and an unset filter should mean "no filter".
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

export function listDetections(filters: DetectionFilters = {}): Promise<Page<Detection>> {
  return apiFetch<Page<Detection>>(`/api/v1/tenant/detections${query({ ...filters })}`);
}

export function getDetection(id: string): Promise<Detection> {
  return apiFetch<Detection>(`/api/v1/tenant/detections/${id}`);
}

export interface IncidentFilters {
  status?: string;
  severity?: string;
  camera_id?: string;
  limit?: number;
  cursor?: string;
}

export function listIncidents(filters: IncidentFilters = {}): Promise<Page<IncidentSummary>> {
  return apiFetch<Page<IncidentSummary>>(`/api/v1/tenant/incidents${query({ ...filters })}`);
}

export function getIncident(id: string): Promise<IncidentDetail> {
  return apiFetch<IncidentDetail>(`/api/v1/tenant/incidents/${id}`);
}

export interface TransitionResult {
  id: string;
  previous_status: string;
  status: string;
}

/** Closing an incident requires a resolution code — the server rejects it otherwise, and
 *  "why was this closed" is the question a report has to answer months later. */
export function transitionIncident(
  id: string,
  action: "acknowledge" | "investigate" | "resolve" | "dismiss",
  body: { reason?: string; resolution_code?: string } = {},
): Promise<TransitionResult> {
  return apiFetch<TransitionResult>(`/api/v1/tenant/incidents/${id}/${action}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}
