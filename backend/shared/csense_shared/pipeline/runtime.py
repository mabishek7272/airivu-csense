"""The pipeline execution loop's own core: given an active `pipeline_assignments` row,
decide whether/what to do about one camera, right now - the gap
`backend/tenant_api/app/api/pipeline_assignments.py`'s own docstring names honestly
("This records intent, not execution"). Everything here is pure logic with every I/O
boundary either a real, narrow DB read (`active_cloud_assignments`) or fully injected
(`run_one_cycle`'s `grab_frame_fn`/`http_infer_fn`/`ingest_fn`) - no camera is dialed and
no detection is written from inside this module. Wiring real callables together into a
running per-camera loop is Task 3 (`backend/pipeline_runtime/`), not this one.

**`runtime_target` lives on `pipeline_versions`, not `pipeline_assignments`.** Confirmed
against `csense_shared.db.models.PipelineVersion` and migration 0031 before writing this
query - `pipeline_assignments` has its own, differently-named `runtime_location`
('cloud'/'edge', see its own module docstring: "Always NULL... deployment_id" and
`AssignmentIn.runtime_location`), which is a separate, currently-unused field. This module
filters on the *version's* `runtime_target`, exactly as the plan specified.

**Confidence and sample rate are merged once, here, not re-derived by every caller.**
`pipeline_versions.resource_profile` (the pipeline's own defaults) and
`pipeline_assignments.tenant_overrides` (a tenant's per-camera overrides, already
type-checked against `allowed_overrides_schema` at assignment-creation time by
`pipeline_assignments.py`'s `_validate_overrides`) both may carry `confidence`/
`sample_fps`; the override wins whenever both are present. `Assignment` carries only the
*already-merged* effective values, so `run_one_cycle` never has to know the merge rule
exists.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.pipeline.rules import DetectedObject

logger = logging.getLogger(__name__)

# CLAUDE.md's own measured recommendation for the cheapest viable no-GPU config
# (mainstream, keyframe-only sampling, ~0.08 cores/camera) - used whenever a pipeline
# version ships with no `resource_profile` at all (the column is nullable).
DEFAULT_SAMPLE_FPS = 0.5

# Not specified anywhere else (CLAUDE.md, the schema docs) - chosen to match
# `csense_shared.pipeline.rules.Rule`'s own default `min_confidence`, so a pipeline
# version with no `resource_profile` behaves the same as this codebase's other
# "reasonable default" for confidence. Documented here per this project's own convention
# of writing down decisions with no explicit spec at the point they're made.
DEFAULT_CONFIDENCE = 0.5


@dataclass(frozen=True)
class Assignment:
    """Everything one polling cycle needs for one camera: connection details, which model
    to run, and the *effective* (already-merged) confidence/sample-rate config.
    """

    assignment_id: uuid.UUID
    tenant_id: uuid.UUID
    site_id: uuid.UUID
    camera_id: uuid.UUID
    hostname: str | None
    rtsp_port: int | None
    main_stream_path: str | None
    username: str | None
    connection_mode: str
    model_name: str
    confidence: float
    sample_fps: float
    effective_from: dt.datetime
    effective_to: dt.datetime | None


def merge_resource_profile(resource_profile: dict | None, tenant_overrides: dict | None) -> dict:
    """The effective per-camera config: `resource_profile` (the pipeline version's own
    defaults, falling back to this module's own defaults for anything it doesn't set)
    with `tenant_overrides` layered on top - override wins on any key both define.

    This is a plain dict-merge, deliberately not tied to the two keys this module happens
    to read (`confidence`, `sample_fps`) - `allowed_overrides_schema` already validated
    that a stored `tenant_overrides` only contains sanctioned, correctly-typed keys at
    assignment-creation time (`pipeline_assignments.py`'s `_validate_overrides`); nothing
    before this ever merged that value against `resource_profile`, since nothing executed
    a pipeline yet.
    """
    merged: dict[str, Any] = {"sample_fps": DEFAULT_SAMPLE_FPS, "confidence": DEFAULT_CONFIDENCE}
    merged.update(resource_profile or {})
    merged.update(tenant_overrides or {})
    return merged


# Joins the one stage type any runtime interprets today (`infer` - see
# backend/admin_api/app/api/pipelines.py's own module docstring) straight out of
# `definition_json` rather than duplicating a "what does this pipeline run" lookup
# elsewhere. `c.deleted_at IS NULL` mirrors `pipeline_assignments.py`'s own
# `_require_own_camera` guard - a soft-deleted camera must never be polled again even if
# its assignment row was never explicitly revoked.
_SELECT_ACTIVE_CLOUD = """
    SELECT pa.id, pa.tenant_id, pa.camera_id, c.site_id,
           c.hostname, c.rtsp_port, c.main_stream_path, c.username, c.connection_mode,
           pv.definition_json -> 'stages' -> 0 ->> 'model_name' AS model_name,
           pv.resource_profile, pa.tenant_overrides,
           pa.effective_from, pa.effective_to
    FROM pipeline_assignments pa
    JOIN pipeline_versions pv ON pv.id = pa.pipeline_version_id
    JOIN cameras c ON c.id = pa.camera_id
    WHERE pa.status = 'active'
      AND pv.state = 'published'
      AND pv.runtime_target = 'cloud'
      AND c.deleted_at IS NULL
"""


async def active_cloud_assignments(session: AsyncSession) -> list[Assignment]:
    """Every camera this process should be polling right now.

    **Platform-scoped.** The caller's session must already carry `app.is_platform = true`
    plus membership in the `csense_platform` database role (see
    `csense_shared.db.postgres.platform_session`, or `notification_worker`'s own
    `_as_platform` helper for the equivalent "set it on an already-open session" shape) -
    this dispatch legitimately spans every tenant, the exact justification already
    established for `notification_worker`/`webhook_dispatch`. Only this read is
    platform-scoped; nothing here writes, and the per-detection write path (`ingest_fn`,
    injected into `run_one_cycle` below, wired to the real `ingest_detection` by Task 3)
    still goes through a normal tenant-scoped session for that camera's own tenant - no
    detection is ever written cross-tenant.
    """
    rows = (await session.execute(text(_SELECT_ACTIVE_CLOUD))).all()
    assignments: list[Assignment] = []
    for row in rows:
        merged = merge_resource_profile(row[10], row[11])
        assignments.append(
            Assignment(
                assignment_id=row[0],
                tenant_id=row[1],
                camera_id=row[2],
                site_id=row[3],
                hostname=row[4],
                rtsp_port=row[5],
                main_stream_path=row[6],
                username=row[7],
                connection_mode=row[8],
                model_name=row[9],
                confidence=float(merged["confidence"]),
                sample_fps=float(merged["sample_fps"]),
                effective_from=row[12],
                effective_to=row[13],
            )
        )
    return assignments


async def run_one_cycle(
    camera_assignment: Assignment,
    *,
    grab_frame_fn: Callable[[Assignment], Any],
    http_infer_fn: Callable[..., Awaitable[list[DetectedObject]]],
    ingest_fn: Callable[..., Awaitable[Any]],
    now: dt.datetime,
) -> str:
    """One polling cycle for one camera. All I/O is injected, so this function never
    touches a real camera, a real network call, or a real database:

      - `grab_frame_fn(camera_assignment) -> frame | None` - production wiring (Task 3)
        resolves the camera's dial-safe address
        (`csense_shared.cameras.connection.resolve_camera_endpoint`), decrypts its
        credential fresh, builds the RTSP URL, and calls
        `csense_shared.cameras.frame_grab.grab_frame` (Task 1). All of that is async and
        DB-bound, so it lives entirely behind this one seam rather than inside this
        function - tests pass a synchronous fake instead.
      - `http_infer_fn(model_name, frame, *, confidence) -> list[DetectedObject]` -
        production calls ai-runtime's real `/internal/v1/infer`; injected so no test makes
        a real network call.
      - `ingest_fn(**kwargs) -> IngestResult` - production is
        `csense_shared.pipeline.ingest.ingest_detection`, called in-process inside a
        `tenant_session` scoped to this camera's own tenant (per the plan's own decision:
        the discovery read above is platform-scoped, but every write stays tenant-scoped).

    Returns `"detected"` / `"clean"` / `"unreachable"` / `"skipped_not_due"` for the loop
    wrapper's own logging/metrics - never raises for an ordinary "camera didn't answer" or
    "nothing detected" outcome, matching `camera_probe.py`'s own established convention
    that an offline camera is information, not an exception.

    **`"skipped_not_due"`** is this function's own defensive check that `now` actually
    falls within `[effective_from, effective_to)`. Nothing in today's
    `pipeline_assignments` API lets a tenant schedule a future `effective_from`, so this
    is dead code against the current API surface - it exists because
    `active_cloud_assignments` already carries both columns for free, and returning a real
    status for "this assignment doesn't apply yet/anymore" is cheap insurance against a
    future feature (scheduled assignments, or a revoke racing a poll) silently running
    early/late instead of loudly skipping. Not specified anywhere in the plan beyond
    naming it as one of the four possible outcomes; documented here as the decision this
    module made without one.
    """
    if camera_assignment.effective_from > now or (
        camera_assignment.effective_to is not None and camera_assignment.effective_to <= now
    ):
        return "skipped_not_due"

    frame = grab_frame_fn(camera_assignment)
    if frame is None:
        return "unreachable"

    objects = await http_infer_fn(
        camera_assignment.model_name, frame, confidence=camera_assignment.confidence
    )

    qualifies = any(o.confidence >= camera_assignment.confidence for o in objects)
    if not qualifies:
        return "clean"

    # Deterministic and idempotent by construction: the same (camera, moment) pair always
    # synthesizes the same id, so a frame processed twice - a crash-and-retry, an
    # overlapping poll - can never create two detections. `ingest_detection` upserts on
    # (tenant_id, source_event_id) exactly to make this the safe default (see its own
    # module docstring: "Idempotency comes from the edge, not from us").
    source_event_id = (
        f"pipeline-runtime:{camera_assignment.camera_id}:{now.isoformat()}"
    )

    await ingest_fn(
        tenant_id=camera_assignment.tenant_id,
        site_id=camera_assignment.site_id,
        camera_id=camera_assignment.camera_id,
        source_event_id=source_event_id,
        captured_at=now,
        objects=objects,
        frame=frame,
    )
    return "detected"
