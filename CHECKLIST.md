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

- [ ] Edge enrollment token issuance + device cert bootstrap (mTLS)
- [ ] Device heartbeat, observed/desired state, signed commands with expiry/idempotency
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
- [x] Registry regression tests (8) — immutability, duplicate-digest rejection, malformed
      digest, biometric non-deployability, licence/provenance presence
- [ ] Model family/version registry UI in the Developer Console
- [ ] Pipeline schema, stage registry, versioning, allowed tenant overrides
- [ ] Pipeline schema, stage registry, versioning, allowed tenant overrides
- [ ] Python asyncio AI runtime skeleton: ingest → preprocess → infer → filter/ROI →
      track → rules → evidence-intent, using an open-source YOLO/ONNX model by default
- [ ] Redis config cache + invalidation, desired-state deployment to edge
- [ ] Developer Console: registry, model detail, pipeline builder, version comparison
- [ ] Golden dataset + benchmark harness for at least one reference use case
- [ ] **[NEEDS EXTERNAL INPUT]** GPU/edge hardware for real profiling; default to CPU/ONNX
      Runtime reference numbers otherwise
- [ ] **[NEEDS DECISION]** YOLOv8/AGPL-3.0 licensing for commercial hosting — see
      CLARIFICATIONS.md #15. Affects 5 of the 14 migrated models.

## Phase 5 — Incident, Evidence, and Notification MVP

- [ ] Detection normalization + idempotent ingestion (Mongo `detections`)
- [ ] Rule correlation, cooldown, duplicate suppression, late-event policy
- [ ] Incident state machine, timeline, comments, assignment, ack/resolve
- [ ] Evidence storage (MinIO), masked variant, checksum, access authorization
- [ ] Customer incident inbox/detail + WebSocket real-time updates
- [ ] Notification policy versions, recipient groups, in-app/email/webhook adapters
      (pluggable provider interface; concrete provider selection pending —
      see [CLARIFICATIONS.md](CLARIFICATIONS.md))
- [ ] Retry/backoff, delivery status, escalation, ack-cancels-escalation
- [ ] Initial incident/response reports
- [ ] Vertical-slice test: camera event → detection → incident → evidence → WebSocket →
      notification → ack → resolve → full audit trail

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
