/**
 * Every type here mirrors a real backend Pydantic model field-for-field, read directly
 * from backend/tenant_api/app/api/*.py - not inferred from documentation, which can
 * drift. This is the same contract mobile/android's own (now-superseded, see git
 * history) native Kotlin app reverse-engineered - re-expressed in TypeScript for this
 * Expo/React Native app.
 */

// --- Auth (backend/tenant_api/app/api/auth.py) --------------------------------------

export interface LoginRequest {
  email: string;
  password: string;
}

/**
 * Mirrors AuthResponse exactly. Deliberately does NOT carry a refresh token field - the
 * real backend never puts one in this body. It sets `csense_session`/`csense_refresh`
 * as httpOnly cookies, path-scoped to `/api/v1/auth`, instead - see
 * src/auth/cookies.ts for how this app relies on the platform's own native cookie jar
 * (not something JS can or should read directly) for that half of the flow.
 */
export interface AuthResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  tenant_id: string | null;
}

// --- Errors (csense_shared.errors.ProblemResponse) -----------------------------------

/** Every error this backend returns has this shape (docs/08_API_GUIDE.md's own "Error
 * shape" section). `code` is the stable, machine-readable field to match on. */
export interface ProblemResponse {
  code: string;
  message: string;
  details: unknown;
  correlation_id: string | null;
  retryable: boolean;
}

// --- Dashboard (backend/tenant_api/app/api/dashboard.py) -----------------------------

export interface DashboardOut {
  sites_count: number;
  cameras_count: number;
  incidents_open_count: number;
  incidents_total_count: number;
  team_members_count: number;
}

// --- Incidents (backend/tenant_api/app/api/incidents.py) -----------------------------

export interface IncidentSummary {
  id: string;
  incident_number: number;
  type_code: string;
  severity: string;
  status: string;
  title: string;
  summary: string | null;
  camera_id: string;
  site_id: string;
  detection_count: number;
  first_detected_at: string;
  last_detected_at: string;
  acknowledged_at: string | null;
}

export interface IncidentPage {
  items: IncidentSummary[];
  next_cursor: string | null;
}

export interface IncidentEventOut {
  event_type: string;
  actor_type: string;
  actor_id: string | null;
  previous_status: string | null;
  new_status: string | null;
  occurred_at: string;
  payload: unknown;
}

/** Mirrors IncidentDetail(IncidentSummary) - flattened, since the wire shape already is
 * (Pydantic inheritance just merges fields at the top level). */
export interface IncidentDetail extends IncidentSummary {
  resolution_code: string | null;
  resolution_summary: string | null;
  metadata: unknown;
  events: IncidentEventOut[];
  detection_ids: string[];
}

export type IncidentStatusFilter = 'active' | 'resolved' | 'dismissed' | null;

/** Body for POST .../acknowledge and .../investigate. */
export interface TransitionRequest {
  reason?: string | null;
}

/** Body for POST .../resolve and .../dismiss. */
export interface ResolveRequest {
  resolution_code: string;
  reason?: string | null;
}

/** The ad-hoc `{id, previous_status, status}` dict every transition endpoint returns
 * (incidents.py's own `_transition` helper - not a named Pydantic model on the backend,
 * but a stable, real shape worth naming here). */
export interface IncidentTransitionResult {
  id: string;
  previous_status: string | null;
  status: string;
}

// --- Cameras (backend/tenant_api/app/api/cameras.py) ---------------------------------

export interface CameraOut {
  id: string;
  site_id: string;
  site_name: string | null;
  zone_id: string | null;
  name: string;
  code: string;
  vendor: string | null;
  model: string | null;
  hostname: string | null;
  rtsp_port: number | null;
  main_stream_path: string | null;
  sub_stream_path: string | null;
  username: string | null;
  rtsp_transport: string;
  status: string;
  has_credentials: boolean;
  stream_profile: unknown;
  last_probed_at: string | null;
  last_frame_at: string | null;
  last_error: string | null;
  created_at: string;
}

// --- License (backend/tenant_api/app/api/license.py) ---------------------------------

export interface EntitlementOut {
  entitlement_code: string;
  value_type: string;
  limit_numeric: number | null;
  enabled_boolean: boolean | null;
  value_json: unknown;
}

export interface QuotaUsageOut {
  quota_code: string;
  limit_value: number;
  reserved_value: number;
  consumed_value: number;
}

/** `status` is one of scheduled/active/grace/suspended/expired/revoked, freshly synced
 * server-side on every read (csense_shared.licensing.lifecycle) - never stale by the
 * time this app sees it. */
export interface LicenseOut {
  id: string;
  plan_code: string;
  plan_name: string;
  status: string;
  starts_at: string;
  expires_at: string | null;
  grace_ends_at: string | null;
  entitlements: EntitlementOut[];
  quota_usage: QuotaUsageOut[];
}
