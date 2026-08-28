import { apiFetch } from "./client";

/** Cameras and edge devices.
 *
 *  Note what is absent from `Camera`: there is no password field, in either direction of
 *  a read. The API never returns one, so the client has no shape to put one in — the type
 *  is the record of that decision, not merely a reflection of it. Credentials are written
 *  through `setCameraCredentials` and are never read back.
 */

export interface Camera {
  id: string;
  site_id: string;
  site_name?: string | null;
  zone_id?: string | null;
  name: string;
  code: string;
  vendor?: string | null;
  model?: string | null;
  hostname?: string | null;
  rtsp_port?: number | null;
  main_stream_path?: string | null;
  sub_stream_path?: string | null;
  username?: string | null;
  rtsp_transport: string;
  status: string;
  /** Whether a credential is stored — never the credential itself. */
  has_credentials: boolean;
  stream_profile: Record<string, unknown>;
  last_probed_at?: string | null;
  last_frame_at?: string | null;
  last_error?: string | null;
  created_at: string;
}

export interface CameraInput {
  site_id: string;
  name: string;
  code: string;
  vendor?: string;
  model?: string;
  hostname?: string;
  rtsp_port?: number;
  main_stream_path?: string;
  sub_stream_path?: string;
  username?: string;
  rtsp_transport?: string;
}

export interface ProbeResult {
  reachable: boolean;
  detail: string;
  codec?: string | null;
  width?: number | null;
  height?: number | null;
  framerate?: number | null;
  transport?: string | null;
}

const BASE = "/api/v1/tenant/cameras";

export function listCameras(params: { site_id?: string; status?: string } = {}) {
  const search = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v) search.set(k, String(v));
  }
  const qs = search.toString();
  return apiFetch<Camera[]>(`${BASE}${qs ? `?${qs}` : ""}`);
}

export function getCamera(id: string) {
  return apiFetch<Camera>(`${BASE}/${id}`);
}

export function createCamera(body: CameraInput) {
  return apiFetch<Camera>(BASE, { method: "POST", body: JSON.stringify(body) });
}

export function updateCamera(id: string, body: Partial<CameraInput> & { status?: string }) {
  return apiFetch<Camera>(`${BASE}/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}

export function deleteCamera(id: string) {
  return apiFetch<void>(`${BASE}/${id}`, { method: "DELETE" });
}

export function setCameraCredentials(
  id: string,
  body: { username?: string; password: string; label?: string },
) {
  return apiFetch<Camera>(`${BASE}/${id}/credentials`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function clearCameraCredentials(id: string) {
  return apiFetch<void>(`${BASE}/${id}/credentials`, { method: "DELETE" });
}

export function probeCamera(id: string, stream: "main" | "sub" = "main") {
  return apiFetch<ProbeResult>(`${BASE}/${id}/probe?stream=${stream}`, { method: "POST" });
}

/* ------------------------------------------------------------------ edge devices */

export interface EdgeDevice {
  id: string;
  site_id?: string | null;
  site_name?: string | null;
  name: string;
  serial_number?: string | null;
  device_type: string;
  role: string;
  status: string;
  online: boolean;
  hardware: Record<string, unknown>;
  capabilities: Record<string, unknown>;
  os_name?: string | null;
  os_version?: string | null;
  agent_version?: string | null;
  connectivity_method?: string | null;
  connectivity_reason?: string | null;
  vpn_address?: string | null;
  camera_count: number;
  enrolled_at?: string | null;
  last_seen_at?: string | null;
  last_error?: string | null;
  created_at: string;
}

export interface EdgeDeviceInput {
  name: string;
  site_id?: string;
  device_type?: string;
  role?: string;
  serial_number?: string;
}

export interface EnrolmentToken {
  device_id: string;
  token: string;
  expires_at: string;
  instructions: string;
}

const EDGE = "/api/v1/tenant/edge";

export function listEdgeDevices(params: { site_id?: string; role?: string } = {}) {
  const search = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v) search.set(k, String(v));
  }
  const qs = search.toString();
  return apiFetch<EdgeDevice[]>(`${EDGE}/devices${qs ? `?${qs}` : ""}`);
}

export function createEdgeDevice(body: EdgeDeviceInput) {
  return apiFetch<EdgeDevice>(`${EDGE}/devices`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function updateEdgeDevice(id: string, body: Partial<EdgeDeviceInput>) {
  return apiFetch<EdgeDevice>(`${EDGE}/devices/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteEdgeDevice(id: string) {
  return apiFetch<void>(`${EDGE}/devices/${id}`, { method: "DELETE" });
}

export function issueEnrolmentToken(id: string) {
  return apiFetch<EnrolmentToken>(`${EDGE}/devices/${id}/enrolment-token`, {
    method: "POST",
  });
}

export interface HealthEvent {
  level: string;
  check_name: string;
  status: string;
  detail?: string | null;
  metrics: Record<string, unknown>;
  observed_at: string;
  received_at: string;
}

export function listDeviceHealth(id: string, level?: string) {
  const qs = level ? `?level=${level}` : "";
  return apiFetch<HealthEvent[]>(`${EDGE}/devices/${id}/health${qs}`);
}
