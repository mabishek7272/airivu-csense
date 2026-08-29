import { apiFetch } from "./client";

/** The pipeline registry - authoring, publishing, and deprecating pipeline versions.
 *
 *  Mirrors backend/admin_api/app/api/pipelines.py exactly. Only one stage type is
 *  interpreted by anything at runtime today (`infer`), so a version is built here from a
 *  single model dropdown rather than a general-purpose stage editor - see that module's
 *  own docstring for why, and for what `pipeline_assignments` (a separate, tenant-facing
 *  API this console doesn't have a page for yet) does and doesn't do once a version is
 *  published.
 */

export const VERSION_STATES = ["draft", "published", "deprecated"] as const;
export type PipelineVersionState = (typeof VERSION_STATES)[number];

// Mirrors VERSION_TRANSITIONS in backend/admin_api/app/api/pipelines.py. Same discipline
// as modelRegistry.ts's own VALID_TRANSITIONS mirror: this only shapes which action a
// button offers, never bypasses the server's own check.
export const VERSION_TRANSITIONS: Record<string, string[]> = {
  draft: ["published"],
  published: ["deprecated"],
  deprecated: [],
};

export interface InferStage {
  type: "infer";
  model_name: string;
  min_model_state: string;
}

export interface PipelineVersionOut {
  pipeline_id: string;
  code: string;
  name: string;
  use_case: string;
  pipeline_status: string;
  // Null when the pipeline has no versions yet - GET /pipelines outer-joins so a
  // freshly created, still-versionless pipeline appears rather than being invisible
  // until someone creates its first version.
  version_id: string | null;
  version_number: number | null;
  state: string | null;
  definition_json: { stages: InferStage[] } | null;
  definition_sha256: string | null;
  runtime_target: string | null;
  allowed_overrides_schema: Record<string, string> | null;
  created_at: string | null;
  approved_at: string | null;
}

export interface CreatePipelineInput {
  code: string;
  name: string;
  use_case: string;
  description?: string;
  owner_team?: string;
}

export interface CreateVersionInput {
  stages: InferStage[];
}

export interface TransitionInput {
  reason: string;
}

const BASE = "/api/v1/admin";

export function listPipelines(params: { state?: string } = {}) {
  const search = new URLSearchParams();
  if (params.state) search.set("state", params.state);
  const qs = search.toString();
  return apiFetch<PipelineVersionOut[]>(`${BASE}/pipelines${qs ? `?${qs}` : ""}`);
}

export function createPipeline(body: CreatePipelineInput) {
  return apiFetch<{ id: string; code: string; name: string }>(`${BASE}/pipelines`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function createPipelineVersion(pipelineId: string, body: CreateVersionInput) {
  return apiFetch<PipelineVersionOut>(`${BASE}/pipelines/${pipelineId}/versions`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function publishPipelineVersion(versionId: string, body: TransitionInput) {
  return apiFetch<PipelineVersionOut>(`${BASE}/pipeline-versions/${versionId}/publish`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function deprecatePipelineVersion(versionId: string, body: TransitionInput) {
  return apiFetch<PipelineVersionOut>(`${BASE}/pipeline-versions/${versionId}/deprecate`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** A version row that is actually a version - every field the outer join can leave null
 *  is guaranteed present, because `groupByPipeline` only ever puts a row here after
 *  checking `version_id !== null`. */
export type PipelineVersionRow = PipelineVersionOut & {
  version_id: string;
  version_number: number;
  state: string;
  definition_json: { stages: InferStage[] };
  definition_sha256: string;
  runtime_target: string;
  allowed_overrides_schema: Record<string, string>;
  created_at: string;
};

export interface PipelineGroup {
  pipelineId: string;
  code: string;
  name: string;
  useCase: string;
  versions: PipelineVersionRow[];
}

const STATE_PRIORITY: Record<string, number> = { published: 0, draft: 1, deprecated: 2 };

/** Groups the flat version list by pipeline - same rationale as modelRegistry.ts's own
 *  groupByModel: no per-pipeline detail endpoint, and at this registry's size that's
 *  simpler than adding one. Published sorts first within a group - an operator scanning
 *  the console cares most about what's actually assignable right now. */
export function groupByPipeline(versions: PipelineVersionOut[]): PipelineGroup[] {
  const order: string[] = [];
  const groups = new Map<string, PipelineGroup>();

  for (const row of versions) {
    if (!groups.has(row.pipeline_id)) {
      groups.set(row.pipeline_id, {
        pipelineId: row.pipeline_id,
        code: row.code,
        name: row.name,
        useCase: row.use_case,
        versions: [],
      });
      order.push(row.pipeline_id);
    }
    // A version-less row (outer-joined so the pipeline itself still appears) only
    // creates the group above - there is no version to add to its list.
    if (row.version_id !== null) {
      groups.get(row.pipeline_id)!.versions.push(row as PipelineVersionRow);
    }
  }

  for (const group of groups.values()) {
    group.versions.sort((a, b) => {
      const pa = STATE_PRIORITY[a.state] ?? 99;
      const pb = STATE_PRIORITY[b.state] ?? 99;
      return pa - pb || b.version_number - a.version_number;
    });
  }

  return order.map((id) => groups.get(id)!);
}
