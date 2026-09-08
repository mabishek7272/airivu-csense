# Pipeline Execution Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close the gap `backend/tenant_api/app/api/pipeline_assignments.py`'s own docstring
already names honestly: *"This records intent, not execution... nothing pulls a camera's
stream and runs the assigned pipeline against it yet."* This is the actual core of the
product — continuously watching an assigned camera, running the model, and turning a real
detection into a real incident, with zero human posting anything by hand.

**Everything downstream of a detection already works and is not touched by this plan**:
rules, incidents, evidence capture, notifications, mobile app, exports. This plan builds
exactly one new thing — the loop that makes `POST /ingest/detections` (or
`csense_shared.pipeline.ingest.ingest_detection` directly) get called automatically for a
camera with an active pipeline assignment, instead of only ever being called by a human or
a test script.

**Explicitly out of scope, stated plainly:** `runtime_target="edge"` assignments. The edge
agent (built earlier this phase) deliberately does not do inference — this plan only
executes `runtime_target="cloud"` assignments, matching CLAUDE.md's own capacity math,
which is entirely about the central server's own core budget.

---

## Decisions made before any code — do not silently revisit

**Real RTSP frame decode, no shortcuts.** `cv2.VideoCapture` (OpenCV's ffmpeg backend)
against a URL built from the *resolved* IP, not the hostname — the same DNS-rebinding
discipline `camera_probe.py`'s `_probe_stream` already established (`resolve_public_endpoint`
first, dial the resolved address, never let the media client re-resolve). RTSP servers are
device-bound, not virtually hosted the way HTTP is, so connecting by IP directly is the
*correct* shape here, not a compromise. **TCP transport only** — CLAUDE.md's own measured
finding: "UDP over the public internet smears frames."

**Detection ingestion goes through the shared library function directly, not HTTP.**
`csense_shared.pipeline.ingest.ingest_detection` — the exact function both the single and
batch `/ingest/detections` endpoints already call — is invoked in-process, inside a
`tenant_session` scoped to the camera's own tenant, the same way `import_legacy_models.py`
and other operator-side tooling already call shared functions directly rather than round-
tripping through this service's own public HTTP API. No fake device credential is minted.

**This worker needs the platform DB role** — it serves every tenant's cameras from one
process, the exact "dispatch legitimately spans every tenant" justification
`notification_worker`/`webhook_dispatch` already established. `tenant_session(factory,
camera.tenant_id)` per detection call (not `platform_session()`) is still correct and
sufficient: the *connection* is platform-scoped so it can see every tenant's
`pipeline_assignments`, but each actual write still goes through the normal tenant-scoped
RLS path with that camera's own `tenant_id` — no detection is ever written cross-tenant.

**One frame at a time per camera, on its own schedule — no shared frame buffer, no
batching across cameras.** Each active assignment gets its own asyncio task looping at its
own sample interval. Simpler than a shared scheduler, and correct: cameras have
independent, unrelated cadences (a pipeline's `resource_profile.sample_fps`, tenant-
overridable per assignment).

**Sampling rate defaults to CLAUDE.md's own measured recommendation**: mainstream,
keyframe-only, 0.5 fps (~0.08 cores/camera) — the cheapest real option on the no-GPU
production box. `resource_profile` (already a real, currently-unused `dict` column on
`pipeline_versions`) carries `sample_fps` and `confidence`; `tenant_overrides` (already a
real, schema-checked column on `pipeline_assignments`) can override either per camera —
both fields already exist for exactly this purpose, per `pipelines.py`'s own
`allowed_overrides_schema` mechanism. No new columns, no new tables beyond what this plan's
own tasks name explicitly.

**Idempotency**: `source_event_id` is synthesized as
`f"pipeline-runtime:{camera_id}:{frame_captured_at.isoformat()}"` — deterministic, so a
frame that somehow gets processed twice (a crash-and-retry, an overlapping poll) can never
create two detections, using the exact `(tenant_id, source_event_id)` uniqueness
`ingest_detection`'s own docstring already leans on.

**A camera that's unreachable is a health fact, not a crash.** A failed connection attempt
logs, backs off, and retries — it must feed the *existing* camera health/probe machinery's
reasoning (a camera going `offline` is already a modeled, alertable condition) rather than
taking the whole worker down. This plan does not touch `camera_probe.py`'s own health-event
writing; it only needs to not crash alongside it.

---

### Task 1: Real RTSP frame grab, reusing the established connection pattern

**Files:** Create `backend/shared/csense_shared/cameras/frame_grab.py`; test
`backend/tests/test_frame_grab.py`.

Read `backend/tenant_api/app/services/camera_probe.py`'s `_probe_stream` in full first —
this task's connection-establishment half (tunnel allowlist, resolve-then-dial, credential
handling) must mirror it exactly, not reinvent it.

- [x] `resolve_camera_endpoint(settings, session, *, tenant_id, camera_id, hostname, port,
      path, username, secret_purpose) -> tuple[str, int]` — thin wrapper factoring out
      `_probe_stream`'s own `tunnel_networks()` + `resolve_public_endpoint()` call pair
      into a shared helper both `camera_probe.py` and this new module call, so there is
      one place this logic can be wrong, not two. Update `camera_probe.py` to use it too
      (a real, small refactor — confirm `scripts/e2e_camera_onboarding.py` and the existing
      probe tests still pass unchanged after).
      Shipped as `resolve_camera_endpoint(session, *, camera_id, hostname, port) ->
      tuple[str, int]` in `csense_shared/cameras/connection.py` — narrower than the
      signature above. `settings`/`tenant_id`/`path`/`username`/`secret_purpose` were
      dropped because the factored-out logic (`tunnel_networks()` +
      `resolve_public_endpoint()`) never touches them; those params exist elsewhere in
      `_probe_stream` only for credential decryption and URI text, which stayed put.
      Spec-reviewed and confirmed as a defensible simplification, not a gap.
- [x] `grab_frame(rtsp_url: str, *, timeout_seconds: float = 10.0) -> np.ndarray | None` —
      opens `cv2.VideoCapture` against the URL (built from the *resolved* IP per the
      Decisions section — never the original hostname), forces `cv2.CAP_PROP_...` /
      FFmpeg options for **TCP transport** (not UDP), reads exactly one frame, releases the
      capture unconditionally (a leaked `VideoCapture` holds an open socket + decoder
      thread — this must not be possible even on an exception path). Returns `None` on any
      failure (unreachable, timeout, undecodable) — never raises for a merely-offline
      camera, matching this codebase's "a probe failure is information, not an exception"
      convention already established in `camera_probe.py`.
      TDD against a real, local RTSP source: this dev stack's own `mediamtx` service is
      already running and already used by `scripts/e2e_live_view.py`/
      `e2e_camera_onboarding.py` for exactly this — read those scripts for how they publish
      a real test stream into it, and reuse that pattern rather than inventing a new one.
      Tests, all against a real published stream (no mocked `cv2`, per this project's own
      "verify for real" standard for exactly this kind of I/O boundary):
      - a real published stream yields a real, non-empty frame
      - an unreachable URL returns `None`, doesn't raise, doesn't hang past the timeout
      - the capture is verifiably released after both success and failure paths (check the
        process's open file descriptors / socket count before and after, or an equivalent
        real check — not just "the function returned")
- [x] Full suite + `ruff check backend scripts`. Rebuild `tenant-api` if `camera_probe.py`
      changed; confirm existing camera e2e scripts still pass. Commit.
      Committed as `e25c413`. Full suite: 491 passed / 266 skipped / 1 unrelated
      pre-existing failure (`test_site_timezones.py::test_unusable_values_are_refused
      [asia/kolkata]` — a macOS case-insensitive-filesystem artifact, not caused by this
      task; out of scope, tracked separately). `ruff check backend scripts` clean.
      `tenant-api` container confirmed running the new code by introspecting it live.
      Two pre-existing `test_model_registry.py` failures surfaced by this same full-suite
      run (unrelated to this task — a biometric-classification policy gap and a missing
      audit trail from the original legacy import) were fixed separately in `a786354`.

      **Two-stage review: both passed.** Spec review: ✅ compliant. Code quality review
      found two Important gaps (undeclared `psutil` test dependency that would break CI
      collection; the "TCP transport is forced" docstring claim wasn't actually
      distinguished from OpenCV's default UDP negotiation by any test) — fixed in
      `f4f769b` with a real MediaMTX-session-observed TCP/UDP test, then independently
      re-verified by the reviewer (including a negative-control run proving the new test
      genuinely fails under UDP, not just passes vacuously). Final assessment: Approved.
      Task 1 closed.

---

### Task 2: The execution loop core, real-DB and injected-frame tested

**Files:** Create `backend/shared/csense_shared/pipeline/runtime.py`; test
`backend/tests/test_pipeline_runtime.py`.

- [x] `active_cloud_assignments(session) -> list[Assignment]` — real DB query, platform-
      scoped: every `pipeline_assignments` row with `status='active'` joined to a
      `pipeline_versions` row that is `published` (a `draft`/`deprecated` version must
      never run), filtered to `runtime_target='cloud'` on the version (not the assignment —
      confirm which table actually carries `runtime_target` by reading the schema again
      before assuming; the plan's own research found it on `pipeline_versions`, verify
      this still holds). Returns everything the loop needs: camera connection details,
      `model_name`, merged `resource_profile` + `tenant_overrides` (overrides win, matching
      `allowed_overrides_schema`'s existing validation semantics).
      Confirmed against migration 0031 / `csense_shared.db.models.PipelineVersion`:
      `runtime_target` is on `pipeline_versions`, exactly as the plan expected.
      `pipeline_assignments` carries its own, differently-named, currently-unused
      `runtime_location` — a real, distinct column, not a naming accident; this filters on
      the version's `runtime_target` per the plan. Merge is a plain dict-merge
      (`merge_resource_profile`) with `{sample_fps: 0.5, confidence: 0.5}` module defaults
      for a pipeline version shipped with no `resource_profile` at all — not specified
      anywhere else, documented in-module as the decision it is (0.5 fps matches
      CLAUDE.md's cheapest no-GPU recommendation; 0.5 confidence matches
      `csense_shared.pipeline.rules.Rule`'s own default).
- [x] `run_one_cycle(camera_assignment, *, http_infer_fn, ingest_fn, now) -> str` — the
      single-camera unit: grab a frame (Task 1), call `http_infer_fn` (injected — real
      `ai-runtime` `/infer` in production, a fake in tests, matching this session's
      established "no real network call in a unit test" discipline), and if any detection
      clears the pipeline's `confidence` threshold, call `ingest_fn` (injected — real
      `ingest_detection` in production) with the synthesized `source_event_id`. Returns a
      status string (`"detected"` / `"clean"` / `"unreachable"` / `"skipped_not_due"`) for
      the loop wrapper's own logging/metrics.
      Shipped with one addition beyond the literal signature: `grab_frame_fn` is also an
      injected keyword-only parameter (the plan's own text allowed this — "injected via a
      parameter too if that's cleaner for testing"). It takes the whole `Assignment`, not
      just an RTSP URL, so the real resolve-address / decrypt-credential / build-URL /
      `grab_frame` sequence (all async, DB-bound) can live entirely behind Task 3's own
      production callable rather than inside this pure-logic function; every test here
      passes a synchronous fake. `"skipped_not_due"` (named in the plan as one of the four
      outcomes but never specified further) is implemented as an `effective_from`/
      `effective_to` bounds check — dead code against today's assignment-creation API
      (nothing lets a tenant schedule a future `effective_from` yet) but free, real
      insurance against a later scheduled-assignment feature silently running early/late.
      Tests, injected fakes, no real network or real camera needed here (Task 1 already
      proved the real I/O boundary separately):
      - a frame with a clearing-confidence detection results in exactly one `ingest_fn`
        call with the synthesized, deterministic `source_event_id`
      - a frame with no detection above threshold never calls `ingest_fn`
      - an unreachable camera (frame grab returns `None`) never calls `http_infer_fn` or
        `ingest_fn`, and reports `"unreachable"`
      - the exact same `(camera, moment)` pair run twice produces the identical
        `source_event_id` both times (the idempotency property this whole design rests on)
      - `resource_profile.confidence` and a `tenant_overrides` confidence override are both
        honoured, with the override winning when both are present
      Also proved against the real dev-stack Postgres (`TEST_POSTGRES_DSN`, same fixture
      shape as `test_pipeline_ingest.py`): a draft version's assignment, a deprecated
      version's assignment, an edge-`runtime_target` assignment, and a revoked assignment
      are each excluded; a genuinely active+published+cloud assignment is included with
      the right merged `resource_profile`/`tenant_overrides` (0.6 default overridden to 0.9
      by `tenant_overrides`, `sample_fps` left at the version's own 1.0 with no override).
      A deliberate negative-control run (removing the `pv.state = 'published'` filter)
      confirmed the two state-exclusion tests actually fail without that filter, rather
      than passing vacuously.
- [x] Full suite + ruff. Commit.
      696 passed / 73 skipped / 1 unrelated pre-existing failure
      (`test_site_timezones.py::test_unusable_values_are_refused[asia/kolkata]`, the same
      macOS filesystem artifact Task 1 already tracked as out of scope). `ruff check
      backend scripts` clean.

---

### Task 3: The service — `backend/pipeline_runtime/`, one asyncio task per active camera

**Files:** Create `backend/pipeline_runtime/app/{__init__,main}.py`,
`backend/pipeline_runtime/{Dockerfile,requirements.txt}`; test
`backend/tests/test_pipeline_runtime_service.py`. Read `backend/notification_worker/app/
main.py` and `webhook_dispatch.py` first — same two-part shape (a thin per-service loop
wrapper around already-tested `csense_shared` functions) and the same loop-isolation
discipline (one camera's task dying must never take down every other camera's task, or the
whole service).

- [ ] **Per-camera task, not a shared poll loop.** On each pass over
      `active_cloud_assignments`, spawn or continue one `asyncio.Task` per camera, each
      sleeping to its own `sample_fps`-derived interval between cycles. A newly-assigned
      camera gets picked up within one discovery-poll interval (make this interval real
      and short — seconds, not minutes; a tenant assigning a pipeline reasonably expects it
      to start working soon). A revoked/deprecated assignment's task is cancelled cleanly,
      not left running against a pipeline that no longer applies — test this specifically
      (revoke an assignment mid-run, confirm its task stops within one discovery cycle and
      makes no further `ingest_fn` calls).
- [ ] **One camera's exception never kills another's**, nor the discovery loop itself —
      the same `_isolated`/loop-isolation shape `notification_worker/app/main.py`'s
      `_webhook_dispatch_never_takes_alerts_down_with_it` already established for exactly
      this reason. Reuse that pattern's *shape*, not a copy-paste — write the equivalent
      for "per-camera task" instead of "per-loop-type task."
- [ ] `Dockerfile`: needs `opencv-python-headless` + its real ffmpeg/RTSP shared-library
      dependencies (unlike the edge agent, this service legitimately needs OpenCV — it is
      not fighting the same ARM/no-compiler constraint the edge agent was built around,
      since this runs on the full x86 production box). Confirm the image actually starts
      and can decode a real RTSP frame inside the container, not just on the host.
- [ ] Wire into `infra/docker-compose.yml` (and `docker-compose.prod.yml` — this one
      **does** belong there, unlike the edge agent; it runs on infrastructure we own).
      Platform DB role, same as `notification-worker`.
- [ ] Full suite + ruff. Rebuild and confirm the new container starts clean. Commit.

---

### Task 4: Real e2e — a live stream to a real incident, nobody posting anything by hand

**Files:** Create `scripts/e2e_pipeline_execution.py`. Read `scripts/e2e_live_view.py` and
`scripts/e2e_camera_onboarding.py` first for the established real-RTSP-via-mediamtx
pattern, and `scripts/e2e_edge_spool.py`/`e2e_diagnostic_access.py` for this session's
real-container e2e conventions.

- [ ] Must prove, against the live stack, with the new `pipeline-runtime` container
      actually running (not imported as a module):
      1. Publish a real video into `mediamtx` containing something `yolov8n-general`
         (already `production`) will genuinely detect (a real clip with a person/vehicle —
         check what test media this repo's other camera e2e scripts already use before
         sourcing a new one).
      2. Real tenant, real camera pointed at that stream, a real published pipeline
         version (`yolov8n-general`, `runtime_target="cloud"`), a real active assignment.
      3. **Nobody posts a detection by hand.** Poll the tenant's own `GET /incidents` (or
         equivalent) with a real timeout and assert a real incident appears on its own,
         with real evidence attached — the single assertion that proves this plan's whole
         point.
      4. Revoke the assignment; confirm no *further* incidents appear from continued
         frames (the per-camera task actually stopped, not just stopped mattering).
      5. Point the camera at an unreachable address; confirm the service logs it and keeps
         running (doesn't crash, doesn't take other cameras' tasks down) — reuse whatever
         real health/probe signal already exists rather than inventing a new one to check.
      6. Full cleanup; PASS/FAIL summary; non-zero exit on failure.
- [ ] Run until genuinely passing. `ruff check backend scripts`. Commit.

---

### Task 5: `CHECKLIST.md`

- [ ] Flip `pipeline_assignments`' own "records intent, not execution" sub-bullet (Phase 4,
      currently `[x]` with the gap named as a sub-point — read the current exact text
      before editing, it may have shifted). Name what shipped: the frame-grab module and
      its DNS-rebinding-safe connection reuse, the per-camera-task execution loop, the new
      `pipeline-runtime` service, real e2e proof of a fully automatic camera-to-incident
      path. Name the honest remainder: `runtime_target="edge"` is still not executed
      (edge inference is its own, separate, not-yet-started body of work); no admission
      control / core-budget enforcement across concurrently-running cameras yet (real
      capacity planning is still blocked on real traffic, per this file's own existing
      Phase 8 entry); the confidence-threshold/night-detection caveat `CLAUDE.md` already
      names for `yolov8n-general` applies here for real now, for the first time, since this
      is the first code path that runs that model continuously rather than on a single
      pushed frame.
- [ ] Commit.
