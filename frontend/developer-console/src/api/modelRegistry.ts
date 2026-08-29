import { apiFetch } from "./client";

/** The model registry - browsing and promoting versions.
 *
 *  Mirrors backend/admin_api/app/api/models.py exactly. That endpoint never returns an
 *  artifact's bytes, object key, bucket, or a presigned URL - only metadata (digest,
 *  size, framework, license, state) - and nothing here should ever add a field that
 *  changes that. The registry stays centralized and the artifacts stay off every screen
 *  by construction, not by convention this file has to remember to uphold.
 */

export const STATES = [
  "uploaded",
  "validating",
  "validated",
  "staging",
  "production",
  "deprecated",
  "revoked",
] as const;
export type ModelState = (typeof STATES)[number];

export const CLASSIFICATIONS = ["standard", "biometric", "regulated"] as const;
export type Classification = (typeof CLASSIFICATIONS)[number];

// A state a version can actually be picked up from by a pipeline - mirrors
// DEPLOYABLE_STATES in models.py. Used only to decide when the promote dialog's
// biometric-acknowledgement notice is relevant; the server enforces the real gate.
export const DEPLOYABLE_STATES: ReadonlySet<string> = new Set(["validated", "staging", "production"]);

// Mirrors VALID_TRANSITIONS in backend/admin_api/app/api/models.py. Used only to populate
// the promote dialog's dropdown so an operator can't even attempt an illegal hop from the
// UI - the server re-validates every request regardless, so a stale mirror here can only
// ever offer an option the server then rejects, never bypass the real state machine.
export const VALID_TRANSITIONS: Record<string, string[]> = {
  uploaded: ["validating", "revoked"],
  validating: ["validated", "uploaded", "revoked"],
  validated: ["staging", "deprecated", "revoked"],
  staging: ["production", "validated", "deprecated", "revoked"],
  production: ["deprecated", "revoked"],
  deprecated: ["staging", "revoked"],
  revoked: ["validating"],
};

export interface ModelVersionOut {
  id: string;
  model_name: string;
  task_code: string;
  version_label: string;
  state: string;
  access_classification: string;
  framework: string;
  runtime: string;
  artifact_sha256: string;
  size_bytes: number;
  license: string | null;
  state_reason: string | null;
  // The most recent golden-dataset validation run for this version, if any - read-only
  // summary from model_validation_runs. null means no run has ever been recorded, which
  // is itself meaningful (distinct from a run that failed).
  latest_validation_status: "passed" | "failed" | null;
  latest_validation_metrics: Record<string, unknown> | null;
  latest_validation_report_key: string | null;
}

export interface PromoteInput {
  target_state: string;
  reason: string;
  acknowledge_biometric?: boolean;
}

const BASE = "/api/v1/admin";

export function listModelVersions(
  params: { state?: string; classification?: string } = {},
) {
  const search = new URLSearchParams();
  if (params.state) search.set("state", params.state);
  if (params.classification) search.set("classification", params.classification);
  const qs = search.toString();
  return apiFetch<ModelVersionOut[]>(`${BASE}/models${qs ? `?${qs}` : ""}`);
}

export function promoteModelVersion(versionId: string, body: PromoteInput) {
  return apiFetch<ModelVersionOut>(`${BASE}/model-versions/${versionId}/promote`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export interface ModelGroup {
  modelName: string;
  taskCode: string;
  versions: ModelVersionOut[];
}

// Lower index = higher priority in the grouped view.
const STATE_PRIORITY: Record<string, number> = Object.fromEntries(
  ["production", "staging", "validated", "validating", "uploaded", "deprecated", "revoked"].map(
    (s, i) => [s, i],
  ),
);

/** Groups the flat version list by model - the API has no per-model detail endpoint, and
 *  with the registry's current size (~14 models) this is simpler than adding one for a
 *  view the flat list already carries every field for. Revisit if the registry grows
 *  enough that pulling the whole list on every load stops being cheap.
 *
 *  Within a group, versions are ordered by lifecycle-state priority (production first),
 *  not raw recency - an operator scanning the registry cares most about what's live, and
 *  a recently-touched-but-revoked version should not crowd out the production one at a
 *  glance. Ties keep the server's own order (Model.name, ModelVersion.created_at desc).
 */
export function groupByModel(versions: ModelVersionOut[]): ModelGroup[] {
  const order: string[] = [];
  const groups = new Map<string, ModelGroup>();

  for (const version of versions) {
    if (!groups.has(version.model_name)) {
      groups.set(version.model_name, {
        modelName: version.model_name,
        taskCode: version.task_code,
        versions: [],
      });
      order.push(version.model_name);
    }
    groups.get(version.model_name)!.versions.push(version);
  }

  for (const group of groups.values()) {
    group.versions.sort((a, b) => {
      const pa = STATE_PRIORITY[a.state] ?? 99;
      const pb = STATE_PRIORITY[b.state] ?? 99;
      return pa - pb;
    });
  }

  return order.map((name) => groups.get(name)!);
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = -1;
  do {
    value /= 1024;
    unit++;
  } while (value >= 1024 && unit < units.length - 1);
  return `${value.toFixed(value >= 10 || unit < 0 ? 0 : 1)} ${units[unit]}`;
}
