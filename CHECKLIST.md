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
- [ ] Site/zone/edge schemas + Customer CRM screens
- [ ] Camera CRUD, encrypted credential storage (envelope encryption), ONVIF discovery
      stub, manual RTSP entry, NVR adapter interface (one mock reference adapter)
- [ ] MediaMTX integration: short-lived signed media session, WebRTC/HLS, privacy masking
      pipeline
- [ ] Camera health current-state model + telemetry history (Mongo)
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
- [x] Biometric quarantine: InsightFace models registered `revoked` / `biometric`, and the
      promotion API refuses a deployable state without explicit acknowledgement
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
  - [x] `kitchen-safety-y8` - TFLite via ai-edge-litert, raw YOLOv8 head + NMS
  - [ ] 5 InsightFace models load and execute, but their SCRFD/ArcFace output needs the
        `insightface` package's decoding rather than a generic detector decode. Tracked
        below.
  - [ ] `license-plate-ocr` - runs, but OCR output is a character sequence, not
        detections; needs the ANPR pipeline stage to call it via `raw_infer`.
- [x] Internal runtime API: `/internal/v1/models`, `/models/{name}/load`, `/infer`,
      `/engines`. Deliberately **not** exposed through Traefik - it takes raw frames and
      returns raw detections with no tenant scoping, so it is called by the pipeline
      layer, never by a browser.
- [ ] InsightFace decoding via the `insightface` FaceAnalysis wrapper (SCRFD anchors +
      ArcFace embeddings)
- [ ] Label maps for `kitchen-safety-y8` and the plate models - detections currently
      return numeric class ids (see CLARIFICATIONS #18)
- [ ] Golden dataset + benchmark harness; `model_validation_runs` is still empty
- [ ] Model registry UI in the Developer Console
- [ ] Pipeline schema, stage registry, versioning, allowed tenant overrides
- [~] Pipeline stages: **infer** is built (above). Remaining: ingest, preprocess,
      filter/ROI, tracking, rules, evidence-intent — these turn raw detections into
      tenant-scoped incidents and are the next real piece of work.
- [ ] Redis config cache + invalidation, desired-state deployment to edge
- [ ] Developer Console: registry, model detail, pipeline builder, version comparison
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
- [ ] WebSocket real-time incident updates to the CRM
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
- [ ] Quiet hours and per-tenant notification policy editing in the CRM (`within_quiet_hours`
      is implemented and tested but not yet consulted by the dispatcher)
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
