import { apiFetch } from "./client";

/** This tenant's own summary counts. Mirrors backend/tenant_api/app/api/dashboard.py. */

export interface Dashboard {
  sites_count: number;
  cameras_count: number;
  incidents_open_count: number;
  incidents_total_count: number;
  team_members_count: number;
}

export function getDashboard() {
  return apiFetch<Dashboard>("/api/v1/tenant/dashboard");
}

export interface CameraTile {
  camera_id: string;
  camera_name: string;
  camera_status: string;
  // Presigned, short-lived (~10 minutes). null means this camera has no evidence yet.
  thumbnail_url: string | null;
  captured_at: string | null;
}

export interface CameraWall {
  tiles: CameraTile[];
}

export function getCameraThumbnails(limit = 12) {
  return apiFetch<CameraWall>(`/api/v1/tenant/dashboard/camera-thumbnails?limit=${limit}`);
}
