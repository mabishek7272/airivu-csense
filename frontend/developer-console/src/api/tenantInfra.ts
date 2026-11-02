import { apiFetch } from "./client";

/** A tenant's operational setup, managed on their behalf by AIRIVU staff — mirrors
 * `backend/admin_api/app/api/tenant_{sites,zones,cameras,edge_devices,rules,
 * pipeline_assignments}.py`, all mounted at `/api/v1/admin/tenants/{tenant_id}/...`.
 * One file, not six: every resource here follows the identical tenant-scoped-path shape,
 * and splitting them into separate files would just be six copies of the same `BASE`
 * pattern with no other benefit.
 */

function base(tenantId: string) {
  return `/api/v1/admin/tenants/${tenantId}`;
}

/* --------------------------------------------------------------------------- Sites */

export interface Site {
  id: string;
  name: string;
  code: string;
  timezone: string;
  address: Record<string, string> | null;
  latitude: number | null;
  longitude: number | null;
  status: string;
  camera_count: number;
  device_count: number;
  created_at: string;
}

export interface SiteInput {
  name: string;
  code: string;
  timezone: string;
}

export function listSites(tenantId: string) {
  return apiFetch<Site[]>(`${base(tenantId)}/sites`);
}

export function createSite(tenantId: string, body: SiteInput) {
  return apiFetch<Site>(`${base(tenantId)}/sites`, { method: "POST", body: JSON.stringify(body) });
}

export function deleteSite(tenantId: string, siteId: string) {
  return apiFetch<void>(`${base(tenantId)}/sites/${siteId}`, { method: "DELETE" });
}

export function listTimezones(tenantId: string) {
  return apiFetch<string[]>(`${base(tenantId)}/sites/meta/timezones`);
}

/* --------------------------------------------------------------------------- Zones */

export interface Zone {
  id: string;
  site_id: string;
  site_name: string | null;
  name: string;
  zone_type: string;
  privacy_level: string;
  polygon: number[][];
  status: string;
  area_fraction: number;
  rule_count: number;
  created_at: string;
}

export interface ZoneInput {
  site_id: string;
  name: string;
  zone_type: string;
  privacy_level: string;
  polygon: number[][];
}

export function listZones(tenantId: string, siteId?: string) {
  const qs = siteId ? `?site_id=${siteId}` : "";
  return apiFetch<Zone[]>(`${base(tenantId)}/zones${qs}`);
}

export function createZone(tenantId: string, body: ZoneInput) {
  return apiFetch<Zone>(`${base(tenantId)}/zones`, { method: "POST", body: JSON.stringify(body) });
}

export function deleteZone(tenantId: string, zoneId: string) {
  return apiFetch<void>(`${base(tenantId)}/zones/${zoneId}`, { method: "DELETE" });
}

/* ------------------------------------------------------------------------- Cameras */

export interface Camera {
  id: string;
  site_id: string;
  site_name: string | null;
  zone_id: string | null;
  name: string;
  code: string;
  hostname: string | null;
  rtsp_port: number | null;
  main_stream_path: string | null;
  sub_stream_path: string | null;
  username: string | null;
  status: string;
  has_credentials: boolean;
  last_probed_at: string | null;
  last_error: string | null;
  created_at: string;
}

export interface CameraInput {
  site_id: string;
  name: string;
  code: string;
  hostname?: string;
  rtsp_port?: number;
  main_stream_path?: string;
  sub_stream_path?: string;
  username?: string;
}

export interface ProbeResult {
  reachable: boolean;
  detail: string;
  codec: string | null;
  width: number | null;
  height: number | null;
  framerate: number | null;
}

export function listCameras(tenantId: string, siteId?: string) {
  const qs = siteId ? `?site_id=${siteId}` : "";
  return apiFetch<Camera[]>(`${base(tenantId)}/cameras${qs}`);
}

export function createCamera(tenantId: string, body: CameraInput) {
  return apiFetch<Camera>(`${base(tenantId)}/cameras`, { method: "POST", body: JSON.stringify(body) });
}

export function deleteCamera(tenantId: string, cameraId: string) {
  return apiFetch<void>(`${base(tenantId)}/cameras/${cameraId}`, { method: "DELETE" });
}

export function setCameraCredentials(tenantId: string, cameraId: string, username: string | undefined, password: string) {
  return apiFetch<Camera>(`${base(tenantId)}/cameras/${cameraId}/credentials`, {
    method: "PUT",
    body: JSON.stringify({ username, password }),
  });
}

export function probeCamera(tenantId: string, cameraId: string, stream: "main" | "sub" = "main") {
  return apiFetch<ProbeResult>(`${base(tenantId)}/cameras/${cameraId}/probe?stream=${stream}`, { method: "POST" });
}

/* ---------------------------------------------------------------------- Edge devices */

export interface EdgeDevice {
  id: string;
  site_id: string | null;
  site_name: string | null;
  name: string;
  serial_number: string | null;
  device_type: string;
  role: string;
  status: string;
  online: boolean;
  connectivity_method: string | null;
  vpn_address: string | null;
  lan_cidr: string | null;
  has_wireguard_key: boolean;
  camera_count: number;
  last_seen_at: string | null;
  created_at: string;
}

export interface DeviceInput {
  name: string;
  site_id?: string;
  device_type: string;
  role: string;
  serial_number?: string;
}

export interface EnrolmentToken {
  device_id: string;
  token: string;
  expires_at: string;
  instructions: string;
}

export interface VpnProvisionResult {
  device: EdgeDevice;
  server_peer_config: string;
  client_config: string;
  warnings: string[];
}

export function listDevices(tenantId: string) {
  return apiFetch<EdgeDevice[]>(`${base(tenantId)}/edge/devices`);
}

export function createDevice(tenantId: string, body: DeviceInput) {
  return apiFetch<EdgeDevice>(`${base(tenantId)}/edge/devices`, { method: "POST", body: JSON.stringify(body) });
}

export function deleteDevice(tenantId: string, deviceId: string) {
  return apiFetch<void>(`${base(tenantId)}/edge/devices/${deviceId}`, { method: "DELETE" });
}

export function issueEnrolmentToken(tenantId: string, deviceId: string) {
  return apiFetch<EnrolmentToken>(`${base(tenantId)}/edge/devices/${deviceId}/enrolment-token`, { method: "POST" });
}

export function provisionVpn(tenantId: string, deviceId: string, lanCidr?: string) {
  return apiFetch<VpnProvisionResult>(`${base(tenantId)}/edge/devices/${deviceId}/vpn-provision`, {
    method: "POST",
    body: JSON.stringify({ lan_cidr: lanCidr || null }),
  });
}

/* ---------------------------------------------------------------------------- Rules */

export interface Rule {
  id: string;
  site_id: string;
  site_name: string | null;
  camera_id: string | null;
  camera_name: string | null;
  zone_id: string | null;
  zone_name: string | null;
  name: string;
  type_code: string;
  alertable_classes: string[];
  min_confidence: number;
  severity: string;
  cooldown_seconds: number;
  status: string;
  warnings: string[];
  created_at: string;
}

export interface RuleInput {
  site_id: string;
  camera_id?: string;
  zone_id?: string;
  name: string;
  type_code: string;
  alertable_classes: string[];
  min_confidence: number;
  severity: string;
  cooldown_seconds?: number;
}

export function listRules(tenantId: string, siteId?: string) {
  const qs = siteId ? `?site_id=${siteId}` : "";
  return apiFetch<Rule[]>(`${base(tenantId)}/rules${qs}`);
}

export function createRule(tenantId: string, body: RuleInput) {
  return apiFetch<Rule>(`${base(tenantId)}/rules`, { method: "POST", body: JSON.stringify(body) });
}

export function deleteRule(tenantId: string, ruleId: string) {
  return apiFetch<void>(`${base(tenantId)}/rules/${ruleId}`, { method: "DELETE" });
}

export function updateRule(tenantId: string, ruleId: string, patch: Partial<RuleInput> & { status?: string }) {
  return apiFetch<Rule>(`${base(tenantId)}/rules/${ruleId}`, { method: "PATCH", body: JSON.stringify(patch) });
}

/* --------------------------------------------------------------- Pipeline assignments */

export interface AssignableVersion {
  pipeline_version_id: string;
  pipeline_code: string;
  pipeline_name: string;
  version_number: number;
  use_case: string;
  description: string | null;
  allowed_overrides_schema: Record<string, string>;
}

export interface Assignment {
  id: string;
  camera_id: string;
  pipeline_version_id: string;
  pipeline_code: string;
  pipeline_version_number: number;
  runtime_location: string;
  tenant_overrides: Record<string, unknown>;
  status: string;
  priority: number;
  created_at: string;
}

export function listAssignablePipelines(tenantId: string) {
  return apiFetch<AssignableVersion[]>(`${base(tenantId)}/pipelines/assignable`);
}

export function listAssignments(tenantId: string, cameraId: string) {
  return apiFetch<Assignment[]>(`${base(tenantId)}/cameras/${cameraId}/pipeline-assignments`);
}

export function createAssignment(
  tenantId: string,
  cameraId: string,
  body: { pipeline_version_id: string; runtime_location: "cloud" | "edge"; priority?: number; tenant_overrides?: Record<string, unknown> },
) {
  return apiFetch<Assignment>(`${base(tenantId)}/cameras/${cameraId}/pipeline-assignments`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function revokeAssignment(tenantId: string, assignmentId: string) {
  return apiFetch<Assignment>(`${base(tenantId)}/pipeline-assignments/${assignmentId}/revoke`, { method: "POST" });
}
