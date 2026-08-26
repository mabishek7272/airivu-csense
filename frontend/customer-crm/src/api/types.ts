// Shapes returned by the Tenant API. Kept hand-written for now; once the OpenAPI
// document is stable these should be generated from it (TRD §10.1) so the two cannot
// drift silently.

export interface BoundingBox {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface DetectedObject {
  class_name: string;
  confidence: number;
  bbox: BoundingBox;
  track_id: string | null;
}

export interface Camera {
  camera_id: string;
  camera_name: string;
  camera_code: string;
  vendor: string | null;
  model: string | null;
}

export interface SiteLocation {
  site_id: string;
  site_name: string;
  site_code: string;
  zone_name: string | null;
  address: Record<string, string> | null;
  latitude: number | null;
  longitude: number | null;
  timezone: string;
}

/** `annotated` carries the boxes, `masked` blurs faces, `original` is unmasked and only
 *  returned to callers holding `evidence.download`. */
export type PrivacyVariant = "annotated" | "masked" | "original";

export interface Evidence {
  evidence_id: string;
  variant: PrivacyVariant;
  /** Short-lived presigned URL, or null when the object could not be reached. */
  url: string | null;
  sha256: string;
  captured_at: string;
}

export interface Detection {
  detection_id: string;
  event_type: string;
  source_event_id: string;
  confidence: number;
  /** Source capture time — when it actually happened. */
  captured_at: string;
  /** When the platform received it; differs after an offline spool replay. */
  cloud_received_at: string;
  objects: DetectedObject[];
  camera: Camera;
  location: SiteLocation;
  evidence: Evidence[];
  incident_id: string | null;
  incident_number: number | null;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export type IncidentStatus =
  | "open"
  | "acknowledged"
  | "investigating"
  | "escalated"
  | "resolved"
  | "dismissed";

export type Severity = "info" | "low" | "medium" | "high" | "critical";

export interface IncidentSummary {
  id: string;
  incident_number: number;
  type_code: string;
  severity: Severity;
  status: IncidentStatus;
  title: string;
  summary: string | null;
  camera_id: string;
  site_id: string;
  detection_count: number;
  first_detected_at: string;
  last_detected_at: string;
  acknowledged_at: string | null;
}

export interface IncidentEvent {
  event_type: string;
  actor_type: string;
  actor_id: string | null;
  previous_status: string | null;
  new_status: string | null;
  occurred_at: string;
  payload: Record<string, unknown>;
}

export interface IncidentDetail extends IncidentSummary {
  resolution_code: string | null;
  resolution_summary: string | null;
  metadata: Record<string, unknown>;
  events: IncidentEvent[];
  detection_ids: string[];
}

/** Statuses an incident can move to from here. Mirrors the server's state machine so the
 *  UI only offers valid actions — the server still enforces it, this just avoids
 *  presenting a button that is guaranteed to fail. */
export const NEXT_STATUSES: Record<IncidentStatus, IncidentStatus[]> = {
  open: ["acknowledged", "investigating", "escalated", "resolved", "dismissed"],
  acknowledged: ["investigating", "escalated", "resolved", "dismissed"],
  investigating: ["escalated", "resolved", "dismissed"],
  escalated: ["investigating", "resolved", "dismissed"],
  resolved: [],
  dismissed: [],
};
