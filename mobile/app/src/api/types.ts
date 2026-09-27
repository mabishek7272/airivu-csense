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
  client?: 'web' | 'mobile';
  device_id?: string;
  device_name?: string;
  platform?: string;
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

/**
 * Mirrors `LoginOut` (backend/tenant_api/app/api/auth.py) exactly - the real response
 * shape for both `POST /api/v1/auth/login` and `POST /api/v1/auth/login/mfa-verify`.
 * `login()` on the backend ALWAYS routes through the MFA gate (a TOTP-enrolled user
 * verifies with their authenticator app, everyone else gets a one-time emailed code) -
 * there is no direct-to-`authenticated` path from `/login` itself, only from
 * `/login/mfa-verify` once the challenge is satisfied. The token/tenant fields are only
 * ever populated when `status === 'authenticated'`; the `mfa_*` fields only when
 * `status === 'mfa_required'` - never both at once.
 */
export interface LoginResult {
  status: 'authenticated' | 'mfa_required';
  access_token?: string;
  token_type?: string;
  expires_in?: number;
  tenant_id?: string | null;
  mfa_ticket?: string;
  mfa_factor?: 'totp' | 'email_otp';
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

// --- Detections (backend/tenant_api/app/api/detections.py) ---------------------------

/** Normalised 0..1 against the frame - no client-side bounding-box overlay in v1, but the
 * shape is carried through so a future screen can add one without another backend read. */
export interface BoundingBox {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface DetectedObjectOut {
  class_name: string;
  confidence: number;
  bbox: BoundingBox;
  track_id: string | null;
  attributes: unknown;
}

/** Mirrors the backend's `LocationOut` for a detection's nested site/zone summary - named
 * `DetectionLocationSummary` (not a bare `LocationOut`, which doesn't exist elsewhere in
 * this file today but could collide with something else added later) to keep it
 * unambiguous. */
export interface DetectionLocationSummary {
  site_id: string;
  site_name: string;
  site_code: string;
  zone_name: string | null;
  address: unknown;
  latitude: number | null;
  longitude: number | null;
  timezone: string;
}

/** Mirrors the backend's `CameraOut` nested inside a detection - a small summary, NOT the
 * same shape as the top-level `CameraOut` above (that one is `id/name/code/...` for the
 * Cameras list). Named `DetectionCameraSummary` to avoid colliding with that export. */
export interface DetectionCameraSummary {
  camera_id: string;
  camera_name: string;
  camera_code: string;
  vendor: string | null;
  model: string | null;
}

/** `variant` is `"annotated" | "masked" | "original"` - list/detail only ever return the
 * first two unless the caller holds `evidence.download` (mobile users won't). `url` can be
 * null if the presign itself failed (object missing) - a real case, guard image rendering
 * on this. */
export interface EvidenceOut {
  evidence_id: string;
  variant: string;
  url: string | null;
  sha256: string;
  captured_at: string;
}

/** Mirrors `DetectionOut` exactly. `captured_at` (when the event happened at the edge) is
 * the primary timestamp to show, per the backend docstring's own reasoning -
 * `cloud_received_at` (when the platform found out) can differ after an offline spool
 * replay. */
export interface DetectionOut {
  detection_id: string;
  event_type: string;
  source_event_id: string;
  confidence: number;
  captured_at: string;
  cloud_received_at: string;
  objects: DetectedObjectOut[];
  camera: DetectionCameraSummary;
  location: DetectionLocationSummary;
  evidence: EvidenceOut[];
  incident_id: string | null;
  incident_number: number | null;
}

export interface DetectionPage {
  items: DetectionOut[];
  next_cursor: string | null;
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

// --- Profile (backend/tenant_api/app/api/profile.py) ---------------------------------

/** Mirrors `ProfileOut` exactly - the signed-in user's own profile, plus a couple of
 * tenant-level fields for display (organization name). `404` (no active membership for
 * the caller's own tenant) is a real, if unlikely, error case - not modeled as a nullable
 * field here since the backend raises `NotFoundError` for it rather than returning a
 * shape with blanks. */
export interface ProfileOut {
  user_id: string;
  email: string;
  display_name: string;
  locale: string | null;
  timezone: string | null;
  tenant_id: string;
  organization_display_name: string;
}

// --- MFA (backend/tenant_api/app/api/mfa.py) ------------------------------------------

/** Mirrors `StatusOut`. Named `MfaStatusOut` (not a bare `StatusOut`) - too generic a
 * name for this shared types namespace. */
export interface MfaStatusOut {
  enrolled: boolean;
  recovery_codes_remaining: number;
}

/** Mirrors `EnrollOut` - returned by `POST .../totp/enroll` (201). No QR code rendering
 * in v1 (per the plan); `secret`/`otpauth_uri` are shown as manual-entry text. */
export interface TotpEnrollOut {
  secret: string;
  otpauth_uri: string;
}

/** Mirrors `ConfirmOut` - the 10 recovery codes are shown exactly once, by design (never
 * retrievable again after this response). */
export interface TotpConfirmOut {
  recovery_codes: string[];
}

// --- Devices (backend/tenant_api/app/api/devices.py) -------------------------------------

export interface DeviceOut {
  id: string;
  name: string | null;
  platform: string | null;
  first_seen_at: string;
  last_seen_at: string;
}

export interface DevicesOut {
  /** `null` = the plan sets no limit. */
  device_limit: number | null;
  web_session_limit: number | null;
  devices: DeviceOut[];
}
