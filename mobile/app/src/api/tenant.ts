import { apiRequest } from './client';
import type {
  CameraOut,
  DashboardOut,
  IncidentDetail,
  IncidentPage,
  IncidentTransitionResult,
  LicenseOut,
  ResolveRequest,
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

export function listCameras(): Promise<CameraOut[]> {
  return apiRequest<CameraOut[]>('/api/v1/tenant/cameras');
}

/** Null is a valid, common answer (no license assigned yet) - the same "not an error"
 * contract the real endpoint documents (license.py's own docstring). */
export function getLicense(): Promise<LicenseOut | null> {
  return apiRequest<LicenseOut | null>('/api/v1/tenant/license');
}
