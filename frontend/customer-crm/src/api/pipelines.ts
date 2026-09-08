import { apiFetch } from "./client";

/** Pipeline assignment: pointing one of this tenant's own cameras at a published pipeline
 *  version. Mirrors `backend/tenant_api/app/api/pipeline_assignments.py` — see that
 *  module's own docstring for what this tenant surface deliberately does and doesn't
 *  expose (e.g. `AssignableVersion` is narrower than the Developer Console's own
 *  `PipelineVersionOut` in `frontend/developer-console/src/api/pipelines.ts`: no
 *  `owner_team`, no pipeline-level `status`, and the server never returns a `draft` or
 *  `deprecated` version here at all).
 *
 *  **This records intent, not execution** — the same point the backend module docstring
 *  makes. Nothing in this codebase yet pulls a camera's stream and runs the assigned
 *  pipeline against it.
 */

/** A published pipeline version a tenant may assign right now. */
export interface AssignableVersion {
  pipeline_version_id: string;
  pipeline_code: string;
  pipeline_name: string;
  version_number: number;
  use_case: string;
  description?: string | null;
  resource_profile: Record<string, unknown> | null;
  // Simple key -> JSON-type-name map ("string" | "number" | "boolean"), the same
  // stage-1 simplification `pipeline_assignments.py`'s own `_TYPE_CHECKS` uses server
  // side — not full JSON-Schema-draft validation.
  allowed_overrides_schema: Record<string, "string" | "number" | "boolean">;
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
  effective_from: string;
  effective_to?: string | null;
  created_at: string;
}

export interface AssignmentInput {
  pipeline_version_id: string;
  tenant_overrides?: Record<string, unknown>;
  runtime_location?: "cloud" | "edge";
  priority?: number;
}

const TENANT_BASE = "/api/v1/tenant";

export function listAssignablePipelines() {
  return apiFetch<AssignableVersion[]>(`${TENANT_BASE}/pipelines/assignable`);
}

export function listCameraAssignments(cameraId: string) {
  return apiFetch<Assignment[]>(`${TENANT_BASE}/cameras/${cameraId}/pipeline-assignments`);
}

export function createAssignment(cameraId: string, body: AssignmentInput) {
  return apiFetch<Assignment>(`${TENANT_BASE}/cameras/${cameraId}/pipeline-assignments`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function revokeAssignment(assignmentId: string) {
  return apiFetch<Assignment>(`${TENANT_BASE}/pipeline-assignments/${assignmentId}/revoke`, {
    method: "POST",
  });
}
