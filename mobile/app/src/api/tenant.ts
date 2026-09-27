import { apiRequest } from './client';
import type {
  CameraOut,
  DashboardOut,
  DetectionOut,
  DetectionPage,
  DevicesOut,
  IncidentDetail,
  IncidentPage,
  IncidentTransitionResult,
  LicenseOut,
  MfaStatusOut,
  ProfileOut,
  ResolveRequest,
  TotpConfirmOut,
  TotpEnrollOut,
  TransitionRequest,
} from './types';

export function getDashboard(): Promise<DashboardOut> {
  return apiRequest<DashboardOut>('/api/v1/tenant/dashboard');
}

export function listIncidents(params: { status?: string | null; cursor?: string | null } = {}): Promise<IncidentPage> {
  const query = new URLSearchParams();
  query.set('status', params.status ?? 'active');
  if (params.cursor) query.set('cursor', params.cursor);
  return apiRequest<IncidentPage>(`/api/v1/tenant/incidents?${query.toString()}`);
}

export function getIncident(incidentId: string): Promise<IncidentDetail> {
  return apiRequest<IncidentDetail>(`/api/v1/tenant/incidents/${incidentId}`);
}

export function acknowledgeIncident(incidentId: string, reason?: string | null): Promise<IncidentTransitionResult> {
  const body: TransitionRequest = { reason: reason ?? null };
  return apiRequest<IncidentTransitionResult>(`/api/v1/tenant/incidents/${incidentId}/acknowledge`, { method: 'POST', body });
}

export function investigateIncident(incidentId: string, reason?: string | null): Promise<IncidentTransitionResult> {
  const body: TransitionRequest = { reason: reason ?? null };
  return apiRequest<IncidentTransitionResult>(`/api/v1/tenant/incidents/${incidentId}/investigate`, { method: 'POST', body });
}

export function resolveIncident(incidentId: string, resolutionCode: string, reason?: string | null): Promise<IncidentTransitionResult> {
  const body: ResolveRequest = { resolution_code: resolutionCode, reason: reason ?? null };
  return apiRequest<IncidentTransitionResult>(`/api/v1/tenant/incidents/${incidentId}/resolve`, { method: 'POST', body });
}

export function dismissIncident(incidentId: string, resolutionCode: string, reason?: string | null): Promise<IncidentTransitionResult> {
  const body: ResolveRequest = { resolution_code: resolutionCode, reason: reason ?? null };
  return apiRequest<IncidentTransitionResult>(`/api/v1/tenant/incidents/${incidentId}/dismiss`, { method: 'POST', body });
}

/** No `status` filter here - unlike an incident, a raw detection has no status concept,
 * so unlike `listIncidents` this takes only `cursor`. The backend supports several other
 * optional filters (`camera_id`, `min_confidence`, etc) but nothing in this v1 screen's
 * scope needs them exposed. */
export function listDetections(params: { cursor?: string | null } = {}): Promise<DetectionPage> {
  const query = new URLSearchParams();
  if (params.cursor) query.set('cursor', params.cursor);
  const suffix = query.toString();
  return apiRequest<DetectionPage>(`/api/v1/tenant/detections${suffix ? `?${suffix}` : ''}`);
}

export function getDetection(detectionId: string): Promise<DetectionOut> {
  return apiRequest<DetectionOut>(`/api/v1/tenant/detections/${detectionId}`);
}

export function listCameras(): Promise<CameraOut[]> {
  return apiRequest<CameraOut[]>('/api/v1/tenant/cameras');
}

export interface LiveSession {
  path: string;
  protocol: string;
  token: string;
  play_url: string;
  expires_in: number;
}

/** `body` is a plain object, not a pre-stringified string - `apiRequest` does the one
 * `JSON.stringify` itself (see client.ts's own `rawFetch`), matching every other
 * function in this file (e.g. `acknowledgeIncident` above). */
export function startLiveSession(cameraId: string, protocol: 'hls' | 'webrtc' = 'hls'): Promise<LiveSession> {
  const body = { protocol };
  return apiRequest<LiveSession>(`/api/v1/tenant/cameras/${cameraId}/live-session`, { method: 'POST', body });
}

/** Null is a valid, common answer (no license assigned yet) - the same "not an error"
 * contract the real endpoint documents (license.py's own docstring). */
export function getLicense(): Promise<LicenseOut | null> {
  return apiRequest<LicenseOut | null>('/api/v1/tenant/license');
}

export function listDevices(): Promise<DevicesOut> {
  return apiRequest<DevicesOut>('/api/v1/tenant/devices');
}

export function removeDevice(deviceId: string): Promise<void> {
  return apiRequest<void>(`/api/v1/tenant/devices/${deviceId}`, { method: 'DELETE' });
}

/** The signed-in user's own profile - session-authenticated only, no special permission
 * required. A 404 (no active membership for the caller's own tenant) is a real, if
 * unlikely, error case surfaced as an `ApiError` like any other. */
export function getProfile(): Promise<ProfileOut> {
  return apiRequest<ProfileOut>('/api/v1/tenant/profile');
}

export function getMfaStatus(): Promise<MfaStatusOut> {
  return apiRequest<MfaStatusOut>('/api/v1/tenant/auth/mfa/status');
}

/** 201 on success; 409 `ConflictError` if already enrolled - a real, reachable error
 * (double-tapping "Enroll", or a stale re-fetch), not a crash - surfaced as an
 * `ApiError` for the screen to display. */
export function enrollTotp(): Promise<TotpEnrollOut> {
  return apiRequest<TotpEnrollOut>('/api/v1/tenant/auth/mfa/totp/enroll', { method: 'POST' });
}

/** `body` is a plain object, not a pre-stringified string - `apiRequest` does the one
 * `JSON.stringify` itself (see client.ts's own `rawFetch`). 409 (no pending enrollment)
 * and 401 (wrong code) are both real, reachable errors, not crashes. */
export function confirmTotp(code: string): Promise<TotpConfirmOut> {
  return apiRequest<TotpConfirmOut>('/api/v1/tenant/auth/mfa/totp/confirm', { method: 'POST', body: { code } });
}

/** Matches `PublicBrandingOut` exactly (backend/tenant_api/app/api/public_branding.py) -
 * shared verbatim by `branding.py`'s authenticated `/tenant/branding/effective` too (its
 * own docstring: "Reuses public_branding.py's own response shape"), so one type here
 * covers both `fetchPublicBranding` and `fetchEffectiveBranding` below. */
export interface PublicBranding {
  slug: string;
  display_name: string;
  logo_url: string | null;
  favicon_url: string | null;
  color_primary: string | null;
  color_accent: string | null;
  color_canvas: string | null;
}

/** Unauthenticated, slug-addressed lookup for the pre-login screen - `auth: false` since
 * there is no session yet (see public_branding.py's own docstring on the trust
 * boundary this sits on). */
export function fetchPublicBranding(slug: string): Promise<PublicBranding> {
  return apiRequest<PublicBranding>(`/api/v1/public/branding/${slug}`, { auth: false });
}

/** The authenticated counterpart - resolves the caller's own session's organization,
 * never a client-supplied slug. `null` is the normal "unbranded tenant" answer, not an
 * error (see branding.py's own docstring). */
export function fetchEffectiveBranding(): Promise<PublicBranding | null> {
  return apiRequest<PublicBranding | null>('/api/v1/tenant/branding/effective');
}
