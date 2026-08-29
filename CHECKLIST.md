# AIRIVU CSense — Build Checklist

Execution tracker for turning the spec pack in [docs/](docs/) into a running product.
Source of truth for phase scope: [docs/06_IMPLEMENTATION_PLAN.md](docs/06_IMPLEMENTATION_PLAN.md).

**Execution model note:** the source plan assumes a 20-person team over 44 weeks, real
camera/NVR hardware, a real legacy system to migrate, and a contracted pilot tenant. None
of that exists here — this is a solo, AI-driven, local-Docker build. This checklist keeps
the same phase structure and architecture, but re-scopes each phase to what a single
engineer-equivalent can actually build and verify locally. Items that generically require
real-world resources (hardware certification lab, penetration test vendor, production
cloud account, contracted SMS/push providers, real pilot tenant, legal/privacy review) are
marked **[NEEDS HUMAN/EXTERNAL INPUT]** and are tracked, not silently skipped.

Legend: `[ ]` not started · `[~]` in progress · `[x]` done · `[!]` blocked on external input

---

## Phase 0 — Discovery & Baseline

- [x] Read and internalize all 6 spec documents (PRD, TRD, Flows, UX brief, Schema, Plan)
- [x] Confirm architecture baseline: FastAPI + Next.js14/React18-Vite, Traefik, MediaMTX,
      PostgreSQL16, MongoDB7, Redis7, MinIO, Docker Compose (per [docs/00_DOCUMENT_INDEX.md](docs/00_DOCUMENT_INDEX.md))
- [x] Confirm deployment target for this build: **local Docker Compose** (user decision)
- [x] Establish monorepo layout, git repo, requirement-ID traceability convention
- [x] Legacy CSense codebase inventory — **RESOLVED 2026-08-25.** Legacy system located
      at `/var/www/csense` on `103.118.158.92` (12 GB). Model estate inventoried and
      migrated; remaining W9 migration work (users, tenants, cameras, credentials,
      detections, snapshots) is now unblocked.
- [!] Pilot tenant, target countries/privacy jurisdiction, camera/NVR hardware list —
      **[NEEDS HUMAN INPUT]**, see [CLARIFICATIONS.md](CLARIFICATIONS.md)
- [x] Write this checklist and CLARIFICATIONS.md

## Phase 1 — Engineering Foundation

- [x] Monorepo skeleton: `backend/`, `frontend/`, `edge/`, `infra/`, `docs/`, `.github/`
- [x] `infra/docker-compose.yml`: Traefik v3, PostgreSQL 16, MongoDB 7, Redis 7, MinIO,
      MediaMTX, tenant-api, admin-api, developer-console, customer-crm
- [x] `backend/shared` internal library: config, DB sessions (Postgres async/Mongo/Redis),
      TenantContext + RLS session binding, security (Argon2id, JWT, token audiences),
      structured logging, correlation IDs, audit/outbox helpers
- [x] Alembic migration baseline: identity, organizations, tenants, memberships, roles,
      permissions, role_permissions, membership_resource_scopes, audit_events,
      outbox_events, processed_events (core of [docs/05_BACKEND_SCHEMA.md](docs/05_BACKEND_SCHEMA.md) §5, §11)
- [x] Tenant API (FastAPI): health/liveness/readiness, full `/api/v1/auth/*`
      (register creates org+tenant+owner, login, rotating refresh, logout), correlation
      ID + tenant context middleware, non-root read-only container
- [x] Admin API (FastAPI): health endpoints, platform-audience-only auth, permission-
      gated `/api/v1/admin/organizations` (real Phase 2 head start, not a stub)
- [x] Row-level security (`memberships`, `membership_resource_scopes`, `audit_events`)
      with the platform bypass gated on **database role membership**, not just a session
      flag — so a compromised Tenant API cannot escalate by setting `app.is_platform`
      itself. Three separate DB identities (owner / `csense_api` / `csense_platform_api`),
      created and verified by `backend/migrations/bootstrap_roles.py`.
- [x] `audit_events` append-only enforced by database GRANTs (UPDATE/DELETE revoked from
      application roles), not application convention alone — SCH §11.1
- [x] Automated tenant-isolation suite, 7 tests
      ([backend/tests/test_tenant_isolation.py](backend/tests/test_tenant_isolation.py)),
      including a guard that fails loudly if the test role could bypass RLS (which would
      make the whole suite vacuous)
- [x] Developer Console shell (Next.js 14): auth screen, route guard, dashboard calling
      the live admin API
- [x] Customer CRM shell (React 18 + Vite): register/login screen, route guard, dashboard
- [x] CI skeleton (GitHub Actions): backend lint/migrate/test against a real Postgres
      service container, both frontends lint/typecheck/build, gitleaks secret scan
- [x] `.env.example`, `.gitignore`, `.dockerignore`, root `README.md`
- [x] `scripts/generate_dev_secrets.sh` — verified working (fixed a Windows/mingw64
      openssl + apostrophe-in-path bug found while testing it)
- [x] **Exit gate IMP-G1** (self-verified, not independently audited):
  - [x] Two test tenants prove Postgres RLS isolation (automated test, see above)
  - [x] Customer token rejected by Admin API; platform token has no implicit tenant
        access — unit-tested in `backend/tests/test_tokens.py`, verified live against the
        running stack, and enforced structurally by separate audiences/services/DB roles
  - [x] `docker compose config` validates; local secrets generated and confirmed working
  - [x] Full stack builds and boots (`docker compose up --build`), all 10 services
  - [ ] CI actually green on a real runner — **pending first push to a Git host**
        **[NEEDS HUMAN INPUT: no GitHub/GitLab remote configured yet]**

### Phase 1 verification log

All verified live on 2026-08-25 against the local Docker stack, not just by inspection:

| Check | Result |
|---|---|
| `docker compose config` | valid |
| `scripts/generate_dev_secrets.sh` | generates JWT keypair + `.env` (fixed an openssl/mingw64 path bug — CLARIFICATIONS #12) |
| Full stack `up --build` | 10/10 services running; postgres/mongo/redis/minio healthy |
| `bootstrap_roles.py` | creates 3 roles, verifies none can bypass RLS |
| `alembic upgrade head` | migrations 0001–0005 applied clean on a fresh volume |
| `pytest` | **14 passed** (3 password, 4 token, 7 tenant-isolation) |
| Register → tenant created | 201, org+tenant+owner membership written under RLS |
| Login → token issued | 200, via SECURITY DEFINER membership lookup |
| Refresh rotation | 200, new token issued |
| Refresh replay (rotated token reused) | 401 + session revoked |
| Customer token → Admin API | 401 |
| Platform login → cross-tenant org list | 200, 14 orgs |
| Audit + outbox atomicity | `tenant.created` audit row and `tenant.created.v1` outbox row committed together |
| Customer CRM via Traefik | 200, SPA fallback on `/dashboard` works |
| Developer Console via Traefik | 200 |

**Security finding fixed during this phase.** The isolation test initially failed and
exposed that RLS was completely inert: the Postgres image creates `POSTGRES_USER` as a
*superuser*, and superusers bypass row-level security even with `FORCE ROW LEVEL
SECURITY`. Every policy in the schema was configured but doing nothing. Fixed by
introducing separate non-superuser roles (migrations 0004/0005 + `bootstrap_roles.py`),
then hardened further so the platform bypass requires database-role membership rather
than a session flag any role could set. This is exactly the class of defect the gate
exists to catch, and it would have been invisible without running the test for real.

A second, related bug was caught the same way: `SET LOCAL app.tenant_id = :param` is a
PostgreSQL syntax error (SET takes no bind parameters), so tenant scoping would have
failed at runtime on the first tenant-scoped query. Now uses `set_config(..., true)`.

## Phase 2 — Tenant, License, and Operator Foundation

- [ ] Organization/tenant creation + owner invitation flow
- [ ] Memberships, system roles, granular permissions, site scopes (seed data + API)
- [ ] Reseller relationship + child tenant foundation
- [ ] License plans, terms, entitlements, quota ledgers, concurrent reservation (row-lock
      pattern from [docs/02_TECHNICAL_REQUIREMENTS_DOCUMENT.md](docs/02_TECHNICAL_REQUIREMENTS_DOCUMENT.md) §9)
- [ ] Principal Administrator org/license screens (Developer Console)
- [ ] Customer guided onboarding + tenant settings (Customer CRM)
- [ ] Central append-only audit query/search foundation
- [ ] Step-up authentication for high-risk actions
- [ ] Vertical-slice test: reseller → child tenant → MFA enrollment → empty dashboard →
      quota-exceeded rejection

## Phase 3 — Edge, Camera, and Live Media Alpha

- [x] **Edge section** — device registry for Raspberry Pi, Jetson Nano/Orin, DGX Spark and
      Windows/Linux PCs. `role` (`gateway` / `inference` / `hybrid`) is the field that
      decides where CPU cost lands: a Pi costs the server ~0.5 cores per camera, a Jetson
      running models locally costs almost nothing.
  - [x] Enrolment tokens: single-use, 48h, serial-pinned on redemption, stored as a
        digest, every failure returning one identical message so tokens cannot be probed.
  - [x] A separate long-lived agent credential issued at enrolment — the enrolment token
        travels (USB stick, read aloud to an installer), so reusing it as the ongoing
        credential would make that path a permanent way in.
  - [x] Migrations 0025/0026: two narrow `SECURITY DEFINER` lookups. RLS scopes by tenant,
        but resolving a device credential is what *discovers* the tenant; the Tenant API's
        role deliberately still cannot bypass RLS. `search_path` pinned on both.
  - [ ] mTLS device certificates — the bearer credential is the interim step
- [x] **Device heartbeat and health** — three tiers (infrastructure/service/quality).
      Current state overwritten on the device row, history written only for transitions
      and failures: every-beat storage would be ~2,880 rows/device/day recording that
      nothing happened. `observed_at` and `received_at` are both kept, because a device
      reports cached events on reconnect and the gap is what an outage investigation needs.
- [ ] Signed commands with expiry/idempotency (desired-state push to the device)
- [x] **Connectivity model + SSRF guard correction** — `connection_mode` per camera
      (`direct` / `vpn` / `edge` / `cloud_relay`). The guard previously refused every
      private address, which blocked the *recommended* production path (camera at
      `10.0.0.2` via WireGuard). Now allowlists only what a tenant has provisioned: the
      peer's `/32` and the site LAN it routes.
  - [x] Peer allowlisted as `/32`, never the `/24` — every peer shares one WireGuard
        interface, so a subnet entry would expose every other tenant's cameras.
  - [x] `validate_allowlist_candidate` rejects ranges overlapping the host's own networks.
        A tenant declaring `172.18.0.0/16` looks ordinary but is Docker's bridge: traffic
        takes the local route and reaches our Postgres, not their tunnel.
  - [x] **The deployment guide's own templates closed, not just documented as risky.**
        `AllowedIPs = 10.0.0.0/24` on every client, paired with a blanket server-side
        FORWARD accept, let one tenant's peer route to another's cameras - and the guide's
        `10.0.0.2` was hardcoded, so a second site collided with the first. Neither is a
        "be careful when editing the template" problem now: `POST
        /devices/{id}/vpn-provision` ([edge.py](backend/tenant_api/app/api/edge.py)) is
        the only path that produces a peer entry, and it cannot reproduce either mistake.
    - [x] [vpn_pool.py](backend/shared/csense_shared/security/vpn_pool.py) allocates a
          fleet-wide-unique `/32` from a 10.8.0.0/16 pool (65k+ addresses vs. the guide's
          ~253) - the existing DB unique index is the backstop, this is what makes hitting
          it rare rather than routine.
    - [x] The rendered server peer stanza's `AllowedIPs` is always exactly that device's
          own `/32` (plus its own site LAN, never anyone else's) - never the `/24` every
          peer used to share. This is what actually stops cross-tenant routing; a bigger
          pool alone would just be the same flaw with more room to collide in.
    - [x] **A second, previously-undiscovered flaw closed alongside it**: nothing stopped
          two different tenants declaring overlapping site LANs (`192.168.1.0/24` is
          common), which on a shared WireGuard server means the second peer configured
          silently steals routing for the first - a functional bug, and a way for a
          malicious tenant to hijack another's camera traffic. `edge_vpn_pool_snapshot()`
          (migration 0029, `SECURITY DEFINER` - the same narrow-escape-hatch pattern as
          `edge_enrolment_lookup`) lets the overlap check see the whole fleet without
          widening row-level security anywhere, and returns only addresses/ranges, never
          which tenant they belong to.
    - [x] **A default that would have silently defeated this on deploy, fixed underneath
          it**: `reserved_local_networks`' default value was effectively all of RFC1918 -
          wired into a real check for the first time here, it would have rejected every
          legitimate site LAN a tenant could ever declare. Narrowed to the deployment's
          actual Docker bridge ranges.
    - [x] A device's WireGuard public key is now validated as one (`^[A-Za-z0-9+/]{43}=$`)
          at enrolment, not just length-bounded - it is later embedded verbatim into a
          peer stanza an operator pastes into the platform's one shared server config, and
          an unvalidated value could smuggle a newline and a forged second `[Peer]` block.
    - [x] Verified through two separate tenants, because the property that matters -
          "tenant A's provisioning can never collide with tenant B's" - cannot be shown
          from inside one tenant's own view:
          [scripts/e2e_vpn_provisioning.py](scripts/e2e_vpn_provisioning.py) (20 checks) and
          [scripts/e2e_vpn_tunnel_dialog.py](scripts/e2e_vpn_tunnel_dialog.py) (browser).
          7 new unit tests in
          [test_vpn_pool.py](backend/tests/test_vpn_pool.py); suite at 185 passing.
    - [x] CRM: a "Tunnel" action on the Edge page
          ([EdgePage.tsx](frontend/customer-crm/src/pages/EdgePage.tsx)) allocates and
          renders both config blocks, copy-to-clipboard, disabled until a device has
          reported a WireGuard key.
- [x] Site/zone/edge schemas + Customer CRM screens — stale, found while scoping "next":
      done across three earlier commits this branch already has (`e0371e3` sites,
      `8da6fb5` zones + a keyboard-operable polygon editor that surfaced a real timezone
      bug, `7e1964c` edge). `SitesPage.tsx`/`ZonesPage.tsx`/`EdgePage.tsx` all exist and are
      wired up; this line just never got checked off.
- [~] Camera CRUD, encrypted credential storage (envelope encryption), manual RTSP entry —
      done (`cameras.py`: full CRUD, credential set/clear, a real digest-auth RTSP/SDP
      probe in `camera_probe.py`). **ONVIF discovery stub and NVR adapter interface are
      still genuinely missing** — checked for both by name, neither exists yet. Splitting
      this line out since it was previously all-or-nothing.
- [ ] ONVIF discovery stub + NVR adapter interface (one mock reference adapter) — split out
      of the line above; ONVIF discovery in particular blocks on real hardware/simulators to
      test against (**[NEEDS EXTERNAL INPUT]**, see below), but a stub interface with a mock
      adapter doesn't.
- [ ] MediaMTX integration: short-lived signed media session, WebRTC/HLS, privacy masking
      pipeline — confirmed still not built: infra config exists (`infra/mediamtx/`) but
      nothing in tenant_api issues a media session or serves live video yet.
- [ ] Camera health current-state model + telemetry history (Mongo) — note: MongoDB was
      removed from the stack (CLARIFICATIONS #19/#20); this will land in PostgreSQL like
      detections did, not Mongo as originally spec'd. Line stays open; wording is stale.
- [ ] **[NEEDS EXTERNAL INPUT]** real camera/NVR hardware or RTSP test feeds for actual
      onboarding validation — will build against RTSP test streams / simulators otherwise

## Phase 4 — AI Registry, Pipeline Runtime, and Control Plane

**Started early**, out of phase order, because the legacy model estate became available
(server access provided 2026-08-25) and migrating it needed somewhere to land.

- [x] Model family/version registry, immutable MinIO artifact storage, digest/provenance
      (migration 0006: `models`, `model_versions`, `model_validation_runs`, `stored_objects`)
- [x] Content-addressed artifact storage — `csense-models/global/models/{model}/{version}/{sha256}.ext`,
      unique digest constraint, DB trigger rejecting any change to identity/artifact columns
- [x] Legacy model migration: 14 unique artifacts (547 MB) transferred, SHA-256 verified,
      imported with licence + provenance metadata
      ([backend/migrations/legacy_model_manifest.py](backend/migrations/legacy_model_manifest.py),
      [import_legacy_models.py](backend/migrations/import_legacy_models.py) — idempotent, re-runnable)
- [x] Biometric quarantine: InsightFace models imported `revoked` / `biometric`, and the
      promotion API refuses a deployable state without explicit acknowledgement. That
      acknowledgement was subsequently given - **all 5 are `production` today**, promoted
      2026-08-26 by owner direction alongside the rest of the legacy estate (CLARIFICATIONS
      #16). The `access_classification=biometric` tag is retained through promotion rather
      than cleared, so it stays visible for a future privacy review rather than becoming
      indistinguishable from any other production model.
- [x] Admin API: `GET /api/v1/admin/models`, `POST /api/v1/admin/model-versions/{id}/promote`
      with a validated state machine, permission gating, and full audit + outbox events
- [x] Registry regression tests (10) — immutability, duplicate-digest rejection, malformed
      digest, biometric classification retention, audited promotion, licence/provenance
- [x] **Models wired up and running** (2026-08-26). AI Runtime service
      ([backend/ai_runtime/](backend/ai_runtime/)) loads artifacts from MinIO with
      SHA-256 verification, keeps them resident in an LRU pool, and runs inference across
      three frameworks. Verified live against a real photograph:
  - [x] 6 Ultralytics models - detection + pose with keypoints (177ms-720ms warm, CPU)
  - [x] `license-plate-detector` - ONNX end-to-end decoder (6- and 7-column layouts)
    - [x] **The decoder had the score and class columns swapped**, found while building a
          demo site around this exact model: it returned zero detections on every real
          frame tried, at any confidence down to 0.01, which is itself the tell (a
          genuine zero-candidate frame from an end2end export doesn't look like that).
          Probing the raw ONNX output directly (`OnnxEngine._decode`'s bypass,
          `raw_infer`) showed the column read as "score" was a constant `0.0` on every
          candidate box the export had already chosen to keep, while the column read as
          "class" varied plausibly with framing (0.03-0.15 on a small, distant plate at
          this camera's native 640x480; 0.80 on the same plate cropped in close) - i.e.
          class and score were transposed. Fixed in
          [engines.py](backend/ai_runtime/app/engines.py), pinned with six new decoder
          tests exercising the real column layout, the below-threshold and empty-output
          cases, and the raw-head fallback
          ([test_ai_runtime_decoder.py](backend/tests/test_ai_runtime_decoder.py)).
    - [x] **Fixing the decoder wasn't enough on its own for a privacy-safe blur pass.**
          Even correctly decoded, this model's own confidence values run low at this
          camera's actual resolution (a real, plainly-visible plate scored 0.06) — a
          usual operating threshold (0.4) would still miss real plates. Blurring now
          runs at a low, recall-favouring confidence (0.03) with every candidate kept
          regardless of category; a single degenerate candidate covering ~90% of the
          frame that shows up at that confidence is rejected by box-area, not by raising
          the confidence back up (which would silently drop the real low-confidence plate
          again too). Verified against a real curated frame: the plate visible on a
          parked car went from fully unblurred (missed at confidence 0.4) to correctly
          detected and blurred (`scripts/build_demo_assets.py`).
  - [x] `kitchen-safety-y8` - TFLite via ai-edge-litert, raw YOLOv8 head + NMS. Carries a
        real label map (`glove`, `hairnet`, `maskoff`, `maskon`, `no_glove`,
        `no_hairnet`) - CLARIFICATIONS #18 and this checklist both said this was still
        missing; it wasn't, found while checking the OCR item below against the real
        database rather than trusting the doc.
  - [~] 5 InsightFace models load; 2 now decode correctly. All 5 are `production`
        (CLARIFICATIONS #16), so this was reachable through the real internal API before
        this work, just returning `OutputContractUnknownError` - decoding closes a real gap,
        it doesn't newly expose anything. `insightface-buffalo-l-detect` (SCRFD) and
        `insightface-buffalo-l-recognition` (ArcFace) now decode via a new
        `InsightFaceEngine`, dispatched by `task_code` rather than a new `runtime` value so
        every other `onnxruntime` model (plate detector, plate OCR) is untouched. Delegates
        the actual decode to the `insightface` package itself - re-deriving SCRFD's
        multi-scale anchor maths or ArcFace's alignment by hand would be exactly the
        guessed-decode failure mode `OutputContractUnknownError` exists to avoid, doubly so
        here where a wrong guess means a wrong face match rather than a misplaced box.
        Verified against the real artifacts through the real internal API, not just
        synthetic unit tests: `insightface-buffalo-l-detect` found all 6 faces in a real
        multi-face photo (insightface's own bundled test image, never committed here) at
        0.87-0.92 confidence with correct 5-point landmarks; `insightface-buffalo-l-
        recognition`'s embeddings scored self-similarity 1.0 (same face re-embedded) against
        0.064 for a different face in the same photo - real evidence the alignment and
        embedding are actually correct, not just shaped correctly.
        `insightface-buffalo-l-genderage` and the two landmark models are deliberately left
        undecoded: no caller/use case for them exists anywhere in the codebase yet (unlike
        detection+recognition, which any face-matching use case would need), so there is
        nothing to build against; the same pattern (`InsightFaceEngine`, `task_code`
        dispatch) extends to them later with no new design work. **This is decode only** -
        nothing here adds a pipeline stage, persists an embedding, or exposes a new API
        route; `embed()` is an in-process escape hatch (mirrors `OnnxEngine.raw_infer`), not
        wired to any endpoint.
        ([engines.py](backend/ai_runtime/app/engines.py),
        [test_insightface_engine.py](backend/tests/test_insightface_engine.py))
  - [~] **`license-plate-ocr` didn't actually run at all** - found while wiring it in.
        Two real bugs in `OnnxEngine`, both in shared preprocessing code every ONNX
        model goes through, not something specific to this one model:
    - [x] **Wrong input layout.** `_preprocess`/`_target_size` assumed NCHW (channels
          first) unconditionally - true for every other ONNX artifact in this estate
          (the plate detector, all 5 InsightFace models), false for this one: its real
          input is `[-1, 64, 128, 3]`, channels *last*. Reading that as NCHW resizes the
          frame to 3 pixels wide (width read off the channel axis) before the model ever
          sees it. Fixed with a layout check based on which axis actually looks like a
          channel count (1/3/4) rather than a per-model exception, so the next NHWC
          export this codebase picks up is handled the same way, not silently assumed
          away again.
    - [x] **Wrong dtype.** The model's own ONNX input metadata declares `tensor(uint8)`
          - it normalises internally - but `_preprocess` always cast to normalised
          float32. Found because onnxruntime rejects the mismatch outright (a real
          error, not a silent wrong answer); fixed by reading the artifact's own
          declared input type instead of assuming float32 for every model.
    - [x] Both fixed and pinned with 6 new decoder tests
          ([test_ai_runtime_decoder.py](backend/tests/test_ai_runtime_decoder.py))
          covering NCHW/NHWC detection and dtype handling directly, not just this one
          model's shape.
    - [ ] **Text decode is not implemented**, on purpose. With the layout/dtype bugs
          fixed, the model runs and returns a well-formed `(1, 9, 37)` output - 9
          character positions, each a 37-way softmax (blank/pad + 10 digits + 26
          letters is the one class count that fits). But the actual index-to-character
          mapping is unverified: every real plate crop from this camera's 640x480
          source tried during this work was too low-resolution for a human or the model
          to confidently read it (10-40% per-position confidence, trailing positions
          converging on what looks like blank/pad). Shipping a guessed charset mapping
          would be exactly the failure mode `OutputContractUnknownError` exists to avoid
          elsewhere in this same file - a wrong guess produces a plausible-looking plate
          number that is silently wrong. Needs either the model's original training
          config or a clearer reference image with a known answer to check against; not
          guessed at here. Documented in `raw_infer`'s own docstring
          ([engines.py](backend/ai_runtime/app/engines.py)).
- [x] Internal runtime API: `/internal/v1/models`, `/models/{name}/load`, `/infer`,
      `/engines`. Deliberately **not** exposed through Traefik - it takes raw frames and
      returns raw detections with no tenant scoping, so it is called by the pipeline
      layer, never by a browser.
- [~] InsightFace decoding: detection (SCRFD) + recognition (ArcFace) done via the
      `insightface` package's own `model_zoo`, not `FaceAnalysis` (which expects a
      directory-based model pack; `model_zoo.get_model()` works directly against this
      estate's content-addressed artifact paths). Landmarks/genderage not yet decoded - see
      the detailed entry above. Runtime dependency note: `insightface`'s own declared
      dependency is `opencv-python` (the GUI build), which cannot coexist with
      `opencv-python-headless` (used everywhere else in this image, and confirmed
      empirically - not assumed - that having both installed and then removing either
      leaves `cv2` unimportable); installed with `--no-deps` in the Dockerfile instead,
      with its real non-cv2 dependencies listed explicitly in `requirements.txt`.
- [x] ~~Label maps for `kitchen-safety-y8` and the plate models~~ — stale: checked
      against the real database rather than trusting this line, and `kitchen-safety-y8`
      and `license-plate-detector` both already carry real label maps (see above). What
      was actually still missing turned out to be `license-plate-ocr`'s output decode,
      tracked in its own entry above under a more accurate description than "a label
      map" - a character-position softmax isn't the same shape of problem as a
      class-id map, and conflating them here was itself part of what made this stale.
- [ ] Golden dataset + benchmark harness; `model_validation_runs` is still empty
- [x] **Model registry UI in the Developer Console** — the registry (`GET
      /api/v1/admin/models`, `POST /model-versions/{id}/promote`) was fully built and
      tested since the AI runtime work, but the console had zero UI for it: a login
      screen and one page listing organizations. Built alongside a first design-system
      pass for the console itself (it had no CSS or navigation at all before this - a
      trimmed port of the Customer CRM's tokens/components, not a new system).
  - [x] `/models`: every version, grouped by model, filterable by state and
        classification (server-side, matching the API's own query params). A biometric
        version is flagged with the word itself next to a warning icon, never colour
        alone.
  - [x] Promote dialog: the target-state dropdown only ever offers what the server's own
        `VALID_TRANSITIONS` table allows from the version's current state - an operator
        cannot even attempt an illegal hop from the form. The biometric-acknowledgement
        notice appears only when it would matter (biometric + a deployable target),
        mirrors the server's own explanation word-for-word, and leaving it unchecked
        still submits - the resulting 422 is what actually proves the gate is real, not
        the checkbox's presence.
  - [x] **A real, pre-existing bug found while building this, unrelated to the feature
        itself**: the console's login response sets no cookie at all
        (`Set-Cookie: None`), so a full page reload or a bookmarked deep link silently
        logs a platform admin back out - the in-memory access token only survives
        client-side (`next/link`) navigation. Doesn't block normal use (nobody hard-
        refreshes mid-session by habit) but is a real reliability gap. **Not fixed here** -
        flagged for a decision on whether the console should call the admin API same-
        origin (matching how the Customer CRM avoids this entirely) or the cookie needs
        `SameSite=None` treatment for its current cross-origin (`console.localhost` ->
        `localhost`) setup.
  - [x] Verified against the real registry, not a fixture double —
        [scripts/e2e_model_registry.py](scripts/e2e_model_registry.py) (14 checks):
        renders all 14 real legacy models, promotes two throwaway versions through the
        actual state machine, proves the biometric gate is server-enforced, confirms the
        audit/outbox trail recorded the acknowledgement, and - the concrete form of why
        this all exists to be centralized - asserts no download URL, presigned URL,
        object key, or bucket name ever appears in a response body or the rendered page
        across the whole run.
- [x] **Public demo site** (`frontend/demo-site/`, `demo.localhost`) — a standalone,
      unauthenticated marketing surface, separate from both the Customer CRM and the
      Developer Console, so sales can show a prospect real detections without exposing
      either. Built around a real, out-of-repo reference: SSH access to the production
      server (granted 2026-08-25) turned up the still-live legacy deployment's own
      camera archive — one real customer's vehicle-lot camera, 203,000+ real snapshots —
      which is where every frame on this site actually comes from.
  - [x] **Offline-rendered, not live inference.** AI Runtime's `/internal/v1/infer` is
        deliberately not exposed publicly (see its own docstring, and the Phase 4 note
        above it). `scripts/build_demo_assets.py` runs once, against curated real
        frames, and bakes the finished, redacted JPEGs into the static build
        (`public/showcase/{category}/`, plus a `manifest.json` the page reads instead of
        hardcoding frame counts) — the deployed site calls no backend at all.
  - [x] Three categories shipped: Vehicle & Object Detection (`yolov8n-general`, 12
        frames), Person Detection (`yolov8n-person`, 2 frames — the archive turned out
        vehicle-heavy, so this category shipped smaller rather than force weak examples
        in), License Plate Detection (`license-plate-detector`, 5 frames) — framed as a
        feature ("detected — and automatically redacted"), not just a privacy fix.
        Fire/smoke, PPE, and kitchen-safety have no real footage available yet and wait
        for a later pass. Biometric models are excluded regardless, per the same
        governance as the registry UI above.
  - [x] **General/person frames keep the legacy system's own drawn boxes as-is**, rather
        than this project re-rendering them. Re-running inference and drawing a second
        box style on top was the first thing tried; rendered and actually looked at, it
        was a cluttered double layer, not a professional single one. The legacy boxes
        are kept instead — legitimately, since the weights that drew them were migrated
        1:1 into this project's own registry (SHA-256-verified) — and only the plates
        category adds this project's own `draw_detections` output, styled in muted grey
        specifically to read as distinct from the legacy green.
  - [x] **A real bug in `license-plate-detector` found and fixed while building this** —
        see the decoder note under the model's own entry above. Reliable plate-blurring
        across every category (not just the plates showcase) depends on it; verified
        with a real curated frame where a visible plate went from fully unblurred to
        correctly detected and blurred.
  - [x] Auto-advancing crossfade carousel (`ModelShowcase.tsx`) — keyboard nav
        (arrows), pause on hover/focus, dot navigation, and `prefers-reduced-motion`
        genuinely stops the auto-advance timer, not just its transition (CSS alone can
        silence a fade; it can't stop a `setInterval`).
  - [x] Closing CTA ("Want to see this on your own site?") — a `mailto:` link to two
        real inboxes given directly for this purpose (`aron.morgan@airivu.ai`,
        `ak@irairf.com`), not a placeholder alias invented for the page. Deliberately not
        a form: this site has no backend to submit one to, and a fake-looking form that
        silently goes nowhere is worse than an honest mailto.
  - [x] Verified with a real browser, not just a build —
        [scripts/e2e_demo_site.py](scripts/e2e_demo_site.py) (10 checks): categories
        switch, real images render, keyboard nav advances the frame, reduced motion is
        respected in both directions, the CTA resolves to a real address, and — the check
        specific to this site's privacy requirement — samples pixel-luminance variance
        inside the flagship plate frame's actual detected bbox versus a same-size region
        right beside it, confirming the region is *measurably* blurred rather than merely
        boxed.
  - [x] A dark-mode contrast bug (hero heading rendering dark-on-dark, caught in a manual
        screenshot review rather than by typecheck/lint/the e2e script) fixed before
        shipping — `--text-inverse` flips per theme for other uses on this page, but the
        hero's own background never does, so it needed its own theme-independent token.
  - [x] Checked for a second Person Detection example beyond the 2 shipped — reran person
        detection against both curated candidate pools already on disk (48 frames, down
        to confidence 0.1): nothing new. The archive is genuinely vehicle-lot-heavy;
        finding more would mean a fresh, wider pull from the server, not local curation.
      Real, uncurated source frames for this (`curate-review/`, `smoke-test*/`) live
      outside the repo entirely and are gitignored as a safety net; only the finished,
      reviewed, redacted output under `public/showcase/` reaches git.
- [x] **Pipeline schema, stage registry, versioning, tenant assignment** (migration
      0031/0032; SCH §8.4, §8.5, §8.8). The one thing genuinely missing before this: a
      `pipelines`/`pipeline_versions` concept at all. `detections.pipeline_version_id`
      had sat as a bare nullable column with no FK since migration 0012 - deliberately,
      for this migration to close, which it now does.
  - [x] `pipelines` / `pipeline_versions` — platform-global, same pattern as
        `models`/`model_versions` (no RLS, role grants only). A version is immutable
        after creation (a DB trigger mirroring `model_versions_immutable` rejects any
        change to `definition_json` or its digest), content-addressed the same way an
        artifact is (`definition_sha256`, over a canonical serialization - identical
        definitions can't become two versions), and moves `draft → published →
        deprecated` — simpler than the model ladder on purpose, since there's no
        validation-run step yet (`pipeline_test_runs`, deferred below).
  - [x] **Only one stage type is interpreted anywhere in this codebase: `infer`.** The
        TRD's own pipeline diagram (§16) has more (preprocess, filter, tracking...), and
        `definition_json` is shaped to hold them later, but nothing executes a pipeline
        at all yet — see the next point. Version creation validates the one real thing:
        the named model has a version in a deployable state (the same
        `DEPLOYABLE_STATES` bar the registry's own promote endpoint uses).
  - [x] `pipeline_assignments` — tenant-owned, RLS, a published version bound to one of
        the tenant's own cameras. **This records intent, not execution**: nothing pulls
        a camera's stream and runs the assigned pipeline against it yet — that's a
        materially different, larger piece of work (a gateway device pulling RTSP, or
        the cloud doing so, and calling `/infer` continuously) than the registry itself,
        and is still the real gap behind "models never leave the central server" being
        proven for the registry/runtime but not yet *enforced* end-to-end. Tracked here,
        not silently implied by the assignment endpoint existing. A partial unique index
        on `(camera_id, priority) WHERE status = 'active'` enforces SCH §8.8's overlap
        constraint (a simplified form of it — a full overlapping-time-range exclusion
        would need `btree_gist` and buys nothing yet, since nothing reads
        `effective_to`).
  - [x] `pipeline.publish` and `pipeline.assign` were already seeded in migration 0007,
        ahead of any table that made them do anything — but only ever granted to
        `platform_admin`, and `pipeline.assign` is a *tenant* action
        (`POST /api/v1/tenant/cameras/{id}/pipeline-assignments`, named in the TRD).
        Migration 0032 adds the `tenant_owner` grant that was missing, plus the two
        genuinely new permissions (`pipeline.read`, `pipeline.manage`).
  - [x] **Developer Console pipeline builder** (`/pipelines`) — mirrors `/models`
        closely: grouped-by-pipeline cards, state badges, publish/deprecate dialogs. The
        "builder" is a model dropdown, not a stage editor — there's exactly one stage
        type to configure right now, so a general-purpose DAG UI would be building
        controls for stage types nothing executes. No Customer CRM page this pass —
        tenant-facing assignment is API-only for now, verified by the e2e script, not
        wrapped in a CRM page yet.
    - [x] **A pipeline with no versions yet was invisible in the list** — found while
          writing the e2e script: `GET /pipelines` inner-joined versions, so a pipeline
          had nowhere to appear until its first version existed, and the "New version"
          button that would create one lived inside the (non-rendering) pipeline
          section. Fixed with an outer join and a nullable version half of the response
          shape; the frontend then briefly double-counted a version (an optimistic
          update appending the real version without dropping the version-less
          placeholder row already in local state) — caught by the same e2e run, fixed,
          and pinned with its own check.
  - [x] Verified against the real registry and a real camera —
        [scripts/e2e_pipeline_registry.py](scripts/e2e_pipeline_registry.py) (16
        checks): author and publish a version against one of the 14 real registered
        models, confirm immutability holds at the database level (not just the UI),
        assign it to a real camera through the tenant API, confirm the overlap
        constraint refuses a second active assignment at the same priority, and confirm
        deprecating a version blocks a new assignment without touching an existing one.
  - [~] Deliberately deferred, each because it depends on something that doesn't exist
        yet or is a scale optimization with no current need:
    - Actually executing an assignment against live camera frames (see above)
    - `pipeline_deployments` (canary/rollout across a fleet) — no real fleet to canary
          across yet; `pipeline_assignments.deployment_id` stays NULL throughout, which
          SCH §8.8 itself treats as a valid shape
    - `pipeline_test_runs` / golden-dataset benchmark harness — same gap
          `model_validation_runs` already has, tracked below
    - Redis 3-layer distributed cache (TRD §16) — Postgres-authoritative direct reads
          are enough at this scale, same reasoning already used for the WS-realtime
          feature's relay decision
    - Full JSON-Schema-draft validation of `tenant_overrides` against
          `allowed_overrides_schema` — a simple key/type check today
- [ ] Redis config cache + invalidation, desired-state deployment to edge
- [ ] Golden dataset + benchmark harness for at least one reference use case
- [ ] **[NEEDS EXTERNAL INPUT]** GPU/edge hardware for real profiling; default to CPU/ONNX
      Runtime reference numbers otherwise
- [ ] **[NEEDS DECISION]** YOLOv8/AGPL-3.0 licensing for commercial hosting — see
      CLARIFICATIONS.md #15. Affects 5 of the 14 migrated models.


## Phase 5 — Incident, Evidence, and Notification MVP

- [x] Rule evaluation engine ([backend/shared/csense_shared/pipeline/rules.py](backend/shared/csense_shared/pipeline/rules.py)):
      class filter, confidence threshold, ROI containment by **true polygon overlap area**
      (Sutherland–Hodgman clip, not centre-point), minimum duration, cooldown, and
      overnight-wrapping schedules. Pure functions — no DB, no clock — so the thresholds
      operators tune are directly testable. Every rejection carries a reason, so
      "why didn't this alert?" is answerable.
- [x] Incident creation with **deduplication enforced in the database**
      ([incidents.py](backend/shared/csense_shared/pipeline/incidents.py)): a partial
      unique index on (tenant, camera, type, correlation_key) for non-closed incidents
      means a retry, a concurrent worker, or a redelivered event cannot create a
      duplicate. Verified: 25 consecutive frames → 1 incident.
- [x] Per-tenant gap-free incident numbers from a locked counter (tenants see
      "Incident 42", not a global id leaking other tenants' volume)
- [x] Incident state machine + append-only `incident_events` history (DB-enforced:
      UPDATE/DELETE revoked from application roles)
- [x] Schema: sites, zones (normalised ROI polygons), cameras, incidents,
      incident_events, incident_detection_links — all RLS-protected with the same
      group-membership-gated policy as Phase 1 (migration 0009)
- [x] Tenant API incident endpoints: cursor-paginated inbox, detail with full history,
      acknowledge / investigate / resolve / dismiss, permission-gated, each transition
      audited and emitting an outbox event
- [x] Recovered legacy label maps (migration 0008) — kitchen-safety and plate models were
      returning numeric class ids. Also recovered `VIOLATION_CLASSES`: only 3 of the 6
      kitchen classes are alertable, so `maskon`/`glove` no longer raise incidents.
- [x] **End-to-end verified** ([scripts/e2e_detection_to_incident.py](scripts/e2e_detection_to_incident.py)):
      real photograph → 6 detections → rule filters to 2 matches with 4 distinct
      rejection reasons → 10 firings produce 1 incident → tenant API → full lifecycle →
      illegal transition refused with 409.
- [x] Detection persistence in **PostgreSQL**, not MongoDB
      ([detections.py](backend/shared/csense_shared/pipeline/detections.py), migration 0012)
      — a deliberate departure from the spec's two-datastore design, see CLARIFICATIONS #19.
      Idempotent by unique `(tenant_id, source_event_id)`, so edge retries and offline-spool
      replay record once. Keeps all five timestamps distinct (TRD-DATA-005), which is what
      makes a six-hour offline backlog diagnosable. Retention swept by
      `delete_expired_detections()` since PostgreSQL has no TTL index; null `expires_at`
      keeps legal-hold rows out of its reach.
- [x] MongoDB removed from the stack entirely — 9 services instead of 10, one datastore to
      back up, restore and patch. Detection tenant isolation is now enforced by row-level
      security instead of an application-level filter.
- [x] Evidence capture ([evidence.py](backend/shared/csense_shared/pipeline/evidence.py)):
      stores **two objects** — original and blurred variant — rather than masking at
      render time, so unmasked bytes are never what a normal read path returns. SHA-256
      verified by reading back from storage before the row is committed (SCH §19).
      Tenant-scoped presigned URLs; originals marked `restricted`.
- [x] **Detection listing with full context** — `GET /api/v1/tenant/detections`
      ([detections.py](backend/tenant_api/app/api/detections.py)) returns, per detection:
      annotated screenshot URL, boundary coordinates, detection id, capture timestamp,
      camera name/code, and site name + address + lat/long + timezone. Filters by camera,
      site, event type, time range, confidence; keyset-paginated.
- [x] **Annotated evidence variant** (migration 0014): boxes and labels drawn on the
      *masked* image, so a face stays blurred underneath its own box. Rule matches drawn
      in alert colour, rejected detections muted — an operator sees what the model saw and
      what the rule decided. Skipped when there are no boxes, since it would be a
      byte-identical duplicate of the masked variant.
- [x] Unmasked `original` withheld from roles lacking `evidence.download` — verified live
      against a `tenant_member` account, which receives only `annotated` and `masked`
- [x] **WebSocket real-time incident updates to the CRM.** The Incidents inbox and detail
      page update live — a new incident from the real pipeline, or a status change from
      the API, appears with no manual refresh.
  - [x] **Short-lived, single-use WS tickets**
        ([ws_tickets.py](backend/shared/csense_shared/security/ws_tickets.py)), not the
        real access token, on the WS handshake — a browser's native WebSocket API cannot
        set the `Authorization` header this API otherwise requires for every other call,
        and the two common workarounds (a query-string token, or putting the long-lived
        token there instead) both put something worth protecting somewhere a log or
        browser history can capture it. A ticket is good for 20 seconds, exactly one
        connection attempt, and is deleted from Redis the moment it's read (`GETDEL`),
        so a captured ticket is already useless by the time anyone could replay it.
  - [x] **Polls `outbox_events` per-tenant inside that tenant's own `tenant_session()`,
        not a Redis pub/sub relay** — even though `OutboxEvent`'s own docstring describes
        that shape. The Tenant API's database role deliberately cannot read across
        tenants (not a member of the `csense_platform` group — see the role table
        above), and reaching for the Admin API's `platform_session()` from here would
        cross the exact boundary its own docstring rules out. Scoping the poll to one
        tenant, inside its own RLS session, needs no privileged role at all, and it
        naturally costs nothing for a tenant with no open tab — nobody's watching, so
        nothing polls. A relay is the right call *if* this ever needs to fan out across
        multiple API replicas serving the same tenant; building it now would have meant
        opening a privileged cross-tenant read path for a feature that doesn't need one.
  - [x] Incident **creation** now writes an `incident.created.v1` outbox event
        ([ingest.py](backend/shared/csense_shared/pipeline/ingest.py)) alongside the
        `incident_events` row it already wrote — that row is this incident's own
        append-only history, not something anything outside it polls, so creation had no
        outbox event at all before this and a brand new incident would never have
        reached a live inbox.
  - [x] `ModelShowcase`-style discipline carried over: a visible **connection
        indicator** (text-labelled, never colour-only), reconnect with exponential
        backoff, and events collapse into one debounced list reload rather than one
        fetch per event landing in the same second.
  - [x] Verified against the real pipeline in a real browser, not a mock socket —
        [scripts/e2e_incident_realtime.py](scripts/e2e_incident_realtime.py) (7 checks):
        a detection posted through the actual ingestion endpoint appears in the inbox
        with **no `page.reload()` anywhere in the script**, an API-driven acknowledgement
        updates the same row live, and — the security properties, not just the happy
        path — a ticket cannot be reused for a second connection, a connection with no
        ticket is refused, and a connection from an origin outside the CORS allowlist is
        refused (exercised via a raw `new WebSocket(...)` from inside the browser,
        bypassing the app's own hook, so the check doesn't just trust the UI to reflect
        what actually happened at the protocol level).
- [x] **Notification policies, recipient groups, provider adapters, escalation** — the
      dispatcher, retry policy and provider adapters existed but nothing drove them, so
      no alert could ever leave the building. Now closed end to end:
  - [x] `notification-worker` service ([backend/notification_worker/](backend/notification_worker/)) —
        polls every 5s, one transaction *per delivery* so a provider timeout cannot roll
        back sends that already happened, and a stalled-delivery sweep that recovers rows
        a killed worker left in `sending`. No ports: it accepts no requests.
  - [x] Policy resolution ([policies.py](backend/shared/csense_shared/notifications/policies.py))
        — parses tenant-written JSON defensively (a malformed policy must not stop
        alerting for every other tenant) and falls back to a flat default so a tenant who
        configured nothing is still told.
  - [x] Escalation ladder written up front, not promoted rung by rung — a worker outage
        then delays alerts instead of silently losing them.
  - [x] **Acknowledgement cancels escalation**, wired into `transition_incident` in the
        same transaction as the status change, and re-checked at send time to close the
        claim→send window. All five human-driven statuses stop the ladder.
  - [x] Annotated snapshots attached to email (never the unmasked `original`).
  - [x] **The same snapshot reaches WhatsApp, not just email.** `Message.media_urls` and
        the WhatsApp provider's `/send/media` path (checks `if message.media_urls`) had
        existed since this section was built - nothing ever populated `media_urls`, so
        every WhatsApp alert had silently been text-only. `load_media_urls`
        ([worker.py](backend/notification_worker/app/worker.py)) presigns the annotated
        evidence object against the *internal* MinIO endpoint (the gateway is a container
        on this network, not a browser - a browser-signed URL would not resolve for it).
        Verified live against a real WhatsApp number: the gateway's own log shows it
        fetching the presigned URL and sending a real `ImageMessage`, with a delivery
        receipt (`Receipt received ... type sender`) coming back.
  - [x] Migration 0017: `cancelled` added to `delivery_status`. Filing acknowledged
        alerts under `abandoned` would have made a success dashboard report failures
        during exactly the incidents handled best.
  - [x] Verified live: [scripts/e2e_notification.py](scripts/e2e_notification.py) opened
        an incident and the worker container sent a real email via Resend
        (`accepted provider=resend`). 35 new tests; suite at 154 passing.
  - [x] **Ingestion wired.** `POST /api/v1/tenant/ingest/detections` is the production
        caller; the chain runs in one transaction so a detection can never be recorded
        without its incident.
- [x] **Vertical slice closed** — [scripts/e2e_ingest_to_alert.py](scripts/e2e_ingest_to_alert.py)
      posts a detection to the HTTP API as an `edge_device` identity and verifies, in one
      run: incident opened, three evidence variants stored, escalation scheduled, a real
      email sent by the worker, a replayed `source_event_id` producing nothing, and
      acknowledgement clearing the ladder. 18 new tests; suite at 172 passing.
  - [x] Migration 0018: `detection_rules`, tenant-scoped with RLS. Camera-or-site scope so
        a site rule is written once, not once per camera; thresholds constrained in the
        database so nonsense cannot be stored whatever a future UI does.
  - [x] Migration 0019: `detection.ingest` and an `edge_device` role holding it and nothing
        else — a credential taken from a device in a plant room cannot read incidents or
        download evidence.
  - [x] Rule schedules evaluated in **site-local** time. Evaluating an overnight rule
        against UTC shifts it by the site's offset, which reads as the rule being broken.
- [ ] **Edge auth is interim.** Ingestion uses a bearer token scoped to one permission;
      Phase 3 replaces it with per-device mTLS issued at enrolment.
- [x] **Rule CRUD API and CRM editor** — the last gap in "configure everything from the
      UI, no SQL" for the detection pipeline. Camera→zone→rule→incident is now fully
      operator-driven.
  - [x] [rules.py](backend/tenant_api/app/api/rules.py): create/list/update/delete, scoped
        to a site with optional camera/zone narrowing. A camera on a different site than
        the rule is refused with 422 before saving, not left to fail silently at
        evaluation time.
  - [x] Server-side warnings, not silent acceptance: a confidence threshold above 0.45
        is flagged as likely to miss detections at night (grounded in the NVR IR-darkness
        benchmarking from Phase 2 — persons measured 0.09-0.21 confidence in the dark); a
        rule with no zone, no camera, no cooldown, or an unusually high consecutive-frame
        requirement is explained, not just accepted.
  - [x] Delete is a hard delete (no FK references `detection_rules`, confirmed via
        `pg_constraint`) — unlike cameras/sites, which soft-delete.
  - [x] [RulesPage.tsx](frontend/customer-crm/src/pages/RulesPage.tsx): cascading
        site→camera/zone pickers, object-class chip selector, live night-confidence
        warning shown while the threshold is being typed (not just after submit),
        enable/disable toggle.
  - [x] **Verified against the real pipeline**, not just the API surface —
        [scripts/e2e_rule_to_incident.py](scripts/e2e_rule_to_incident.py): a rule built
        over HTTP opens an incident when a matching detection lands in its zone, stays
        silent for one outside it, and stops firing once disabled.
  - [x] **Verified in the browser** —
        [scripts/e2e_rule_editor.py](scripts/e2e_rule_editor.py): empty state explains
        the consequence of having no rules, the night-confidence warning appears while
        setting the threshold, and enable/disable is reflected in the listing.
- [x] **Recipient groups and notification policies — the last "configure alerting from
      the UI" gap closed, and quiet hours actually wired in.** `within_quiet_hours` was
      implemented and tested since Phase 4 but never called from anywhere; both
      `recipient_groups` and `notification_policies` have existed since the dispatcher
      was built but were SQL-only.
  - [x] [recipient_groups.py](backend/tenant_api/app/api/recipient_groups.py): group and
        member CRUD. A member's own `active_schedule` is their quiet hours - not the
        group's, not the policy's - because two people in the same escalation step can
        legitimately want to be reached at different hours.
  - [x] [notification_policies.py](backend/tenant_api/app/api/notification_policies.py):
        policy CRUD plus `POST .../versions` to publish - the only path that ever writes
        an escalation ladder, matching the immutable-version design from migration 0015.
        A policy with no published version is a deliberate draft: `resolve_policy` inner
        -joins to `active_version_id`, so it exists but reaches nobody until published.
  - [x] **Quiet hours wired into the dispatcher**: `_quiet_hours_delay` in
        [dispatcher.py](backend/shared/csense_shared/notifications/dispatcher.py) holds a
        recipient's delivery until their window ends - `high` and `critical` always
        bypass it (migration 0015's own words: "a fire alarm ignores them; a housekeeping
        alert should not"). `quiet_hours_end` (new, alongside `within_quiet_hours`) computes
        exactly when to release it, correct across a wrap-midnight window and a
        recipient's own IANA timezone. Moved to a new `schedule.py` module shared by
        `policies.py` and `dispatcher.py` without a circular import.
  - [x] **A second gap closed alongside it**: `load_recipients` never actually checked
        `recipient_groups.status` - archiving a group (now possible for the first time)
        would not have stopped it notifying anyone.
  - [x] Verified through the real ingestion pipeline, not just the API surface —
        [scripts/e2e_notification_config.py](scripts/e2e_notification_config.py): a
        medium-severity incident holds a quiet-hours recipient's delivery and does not
        touch an always-reachable one; a high-severity incident reaches both regardless.
        Plus the CRUD guards: a group referenced by a published policy cannot be deleted,
        a policy with delivery history cannot be deleted (disable instead).
  - [x] Browser coverage —
        [scripts/e2e_notification_pages.py](scripts/e2e_notification_pages.py): empty
        states, adding a member with quiet hours, publishing an escalation step,
        enable/disable reflected in the listing.
  - [x] CRM: **Recipients** and **Notifications** pages — group/member management with a
        quiet-hours picker (reusing the timezone list built for Sites), and a policy
        editor with a step builder (delay, channels, recipient groups).
- [x] **Customer CRM UI** — incident inbox, incident detail with evidence strip and
      history timeline, detections feed with annotated thumbnails. Design tokens with
      light/dark, WCAG 2.2 AA contrast, keyboard focus, skip link, reduced-motion support.
      Severity and status always carry a text label, never colour alone.
- [x] Browser-driven verification ([scripts/screenshot_crm.py](scripts/screenshot_crm.py)):
      logs in, walks every screen, asserts evidence images actually load, acknowledges an
      incident and waits for the badge to change, and checks the page does not scroll
      horizontally at 420px. Seed data via
      [scripts/seed_demo_tenant.py](scripts/seed_demo_tenant.py).

## Phase 6 — Resilience, APIs, Reporting, Privileged Support

- [ ] Edge encrypted offline spool, reconnect cursor, batch resync, dedup
- [ ] WireGuard/relay integration design + time-limited diagnostic access
- [ ] Support grant request/approve/active-banner/expiry/revoke + audit
- [ ] Scoped API keys, rate limits, usage metering, developer API docs
- [ ] Webhook signing, verification, replay protection
- [ ] SMS/web-push provider adapters — **[NEEDS HUMAN INPUT: no provider contracted]**
- [ ] Async reports/exports with time-limited download
- [ ] License grace/restriction + renewal flow
- [ ] Camera health use cases: offline, obstruction, glare/night-vision, low FPS, network

## Phase 7 — Migration Tooling and Pilot Beta

Legacy system access provided 2026-08-25, so this is partially unblocked.

- [x] Model estate migration (idempotent, digest-verified) — done ahead of schedule as
      part of Phase 4 above
- [ ] Legacy source inventory frozen and mapping approved (users, tenants, cameras,
      credentials, detections, snapshots)
- [ ] Idempotent migration tools for the remaining entity types
- [ ] Password migration or forced-reset strategy (legacy uses SQLite `csense_users.db`)
- [ ] Snapshot-to-MinIO digest verification
- [ ] DDNS/edge protocol compatibility or edge upgrade package
- [ ] Per-tenant reconciliation dashboard/report
- [!] Pilot cutover still needs a **named pilot tenant and an agreed cutover window**
      — [NEEDS HUMAN INPUT]

## Phase 8 — Production Hardening

- [ ] SAST/SCA/secret/container/IaC scan wired into CI (automatable now)
- [ ] DAST baseline scan against local stack (automatable now)
- [!] Independent penetration test — **[NEEDS HUMAN/EXTERNAL INPUT]** requires a
      contracted third party; not something I can perform or substitute for
- [ ] Load/spike/endurance/failure-injection test suite (automatable, local-scale)
- [ ] Backup automation + restore-exercise scripts (automatable against local MinIO/PG/Mongo)
- [!] Real capacity/SLO validation — needs real traffic; only synthetic benchmarks
      possible locally

## Phase 9 — Production Candidate and Wave Rollout

- [!] **[NEEDS HUMAN/EXTERNAL INPUT]** entirely dependent on a real cloud/production
      environment, DNS, TLS certificates, and business go/no-go — not applicable to a
      local-only build until a target environment is chosen

## Phase 10 — Stabilization and Handover

- [ ] Admin manual, operator manual, API guide, incident runbooks (can draft now)
- [!] Pilot defect resolution, false-positive tuning — needs real pilot data first

---

## How this checklist is used

- Phases 0–1 are being executed now, fully, as real working code.
- Phases 2–6 will be executed next, in order, as working code against the local Docker
  stack — no camera/GPU/provider hardware required to make real progress on each.
- Phases 7, 9, and parts of 8 are structurally blocked on things only a human/business can
  supply (real legacy system, pilot customer, cloud account, contracted vendors, security
  auditor). I will build every part of them that doesn't require those, and flag the rest
  here rather than claim completion.
- This file is updated as each item completes.
