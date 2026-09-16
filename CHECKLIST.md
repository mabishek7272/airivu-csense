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
- [~] Pilot tenant, target countries/privacy jurisdiction, camera/NVR hardware list —
      **[NEEDS HUMAN INPUT]**, see [CLARIFICATIONS.md](CLARIFICATIONS.md)
  - [x] **2026-09-09: two real named customers and their real camera hardware now
        exist in the system** (see the Phase 3 camera/NVR entry for the full detail) —
        `abelamm57@bigpond.com` (7 cameras, live-probed, one actively running the real
        pipeline execution loop) and `paresh@aptiservices.com` (1 camera, correctly
        SSRF-refused pending a real edge device on their LAN). What's still genuinely
        open: **target countries/privacy jurisdiction was not stated for either
        customer** - still needed before any real reliance on this for those tenants -
        and **an agreed pilot cutover window** (tracked separately, Phase 7).
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

- [x] Organization/tenant creation — was already done (`/api/v1/auth/register`, used by
      every e2e script all session) but never checked off; owner invitation flow is new,
      see the detailed entry below.
- [~] **Memberships + invitation flow.** The schema (migration 0001: `memberships`,
      `membership_resource_scopes`, `status: invited/active/suspended/revoked`,
      `site_scope_mode: all/selected/none`) existed since Phase 1 with zero API against
      it until now.
  - [x] `POST /api/v1/tenant/memberships` (invite), `GET` (list), `PATCH` (role/status/
        scope) — `membership.manage`, `tenant_owner`-only (migration 0035) - identity/
        access control is more sensitive than any resource permission granted to
        `tenant_member` so far.
  - [x] Real invitation emails via the existing Resend integration
        (`csense_shared.notifications.bootstrap`/`ResendEmailProvider` - already verified
        sending, CLARIFICATIONS #26) - reused, not a new email path. A new Redis-backed
        token (`invitation_tickets.py`, sibling to `ws_tickets.py` - long-lived (7 days)
        and single-use, deliberately unlike a WS ticket's 20-second lifetime) is only ever
        returned in the API response when sending actually failed - otherwise it goes out
        in the email alone and the API never holds it again.
  - [x] `POST /api/v1/auth/accept-invitation` sets the real password and activates the
        membership, then logs the person straight in (issues real tokens), same as a
        fresh registration does.
  - [x] **Zero-owners lockout guard**: nothing in the schema itself stops a tenant
        revoking or demoting its last active `tenant_owner`, which would lock the tenant
        out of its own account permanently. Refused with a clear `422`; proven against
        the real running API, not just as a schema constraint (`scripts/e2e_memberships.py`
        - refuses with one owner, succeeds once a second exists).
  - [x] Customer CRM: `/team` (list, invite dialog, role/status actions,
        `ConfirmDialog` for revoke) and `/accept-invitation` (the one page in this app
        reachable without already being signed in).
  - [x] Verified for real, not simulated: `scripts/e2e_memberships.py` sends a real
        invitation through Resend, reads the real token out of Redis (never asked of the
        API - matches how the token behaves once an email actually sends), accepts it,
        confirms the new member can log in independently, confirms a reused token is
        refused (401), and confirms the lockout guard both blocks and un-blocks correctly
        over real HTTP calls.
  - [ ] **Deliberately deferred**: `site_scope_mode: selected` (per-site scoping via
        `membership_resource_scopes` - real schema, no picker UI); finer role granularity
        than the existing `tenant_owner`/`tenant_member` pair; a separate read permission
        for members who aren't owners to see their own team roster (today `membership.
        manage` gates both read and write).
- [~] Reseller relationship + child tenant foundation
  - [x] Schema needed no migration (`organization_relationships`, `organization_type`'s
        `reseller`/`reseller_customer` values - migration 0001); this shipped its first
        API + real provisioning mechanism against it. New: migration 0037 -
        `reseller.manage_children`, granted broadly to `tenant_owner` like
        `membership.manage` is - the business rule ("only an actual reseller org may use
        this") is enforced at the endpoint via `organizations.organization_type`, not by
        which roles hold the permission.
  - [x] `csense_shared.tenancy.provisioning` (new, shared) - creates an organization +
        tenant with an *invited* (not active) owner. Neither caller (a platform admin, or
        a reseller) ever learns the new owner's password, unlike `/register` - the owner
        activates through the existing, unmodified `POST /api/v1/auth/accept-invitation`,
        which needed zero changes to support this.
  - [x] `POST /api/v1/admin/organizations` (TRD §10.2's own representative endpoint,
        previously unbuilt) - a platform admin provisions a `direct_customer` or
        `reseller` organization. This is how a reseller comes into being in the first
        place (TRD §8.2).
  - [x] `POST` / `GET /api/v1/tenant/child-tenants` - a reseller creates and lists its own
        `reseller_customer` child tenants, each an independent tenant boundary (TRD §8.2:
        "records are not stored in a shared reseller tenant") linked via a real
        `organization_relationships` row. A non-reseller tenant_owner (a `direct_customer`,
        or even a `reseller_customer` itself) is refused with a clear `403
        not_a_reseller`, not a bare permission denial.
  - [x] **Real bug found and fixed during e2e verification**: the child-tenant list
        endpoint's first draft joined `memberships` to show each child's owner email -
        `memberships` is RLS-protected per-tenant, so that join was silently emptied by
        RLS under the reseller's own tenant scope, making every child tenant invisible in
        its own parent's list. Fixed by dropping the cross-tenant join entirely (TRD
        §8.2's "aggregate views are computed from authorized child relationships" -
        `organizations`/`tenants`/`organization_relationships` carry no RLS at all, so the
        list reads only what a reseller is actually authorized to see); owner email is
        returned once, at creation time, from data already in hand.
  - [x] Verified for real: `scripts/e2e_reseller.py` - a platform admin provisions a
        reseller, its invited owner accepts and creates a child tenant, the child's own
        invited owner accepts independently and lands in the *child* tenant (not the
        reseller's), the child tenant (a `reseller_customer`) is refused when it tries to
        create children of its own, an ordinary `direct_customer` tenant is refused the
        same way, and the `organization_relationships` row is confirmed by direct query.
        Full PASS.
  - [ ] **Deliberately deferred, and not silently**: no UI yet for any of this - the two
        related, still-open CHECKLIST lines below (`Principal Administrator org/license
        screens`, `Customer guided onboarding`) are exactly where that belongs, not
        duplicated here. No reseller aggregate rollups (usage/billing across children) -
        blocked on the licensing/quota item below existing first.
- [~] License plans, terms, entitlements, quota ledgers, concurrent reservation (row-lock
      pattern from [docs/02_TECHNICAL_REQUIREMENTS_DOCUMENT.md](docs/02_TECHNICAL_REQUIREMENTS_DOCUMENT.md) §9)
  - [x] No schema existed for this before now (unlike memberships/reseller) - migration
        0038 adds `license_plans` (platform-global), `licenses`, `license_entitlements`,
        `quota_ledgers`, `quota_reservations` (docs/05_BACKEND_SCHEMA.md §6.1-6.5), all
        tenant-owned tables FORCE-RLS'd with the current tenant-match-or-platform-group
        policy (migration 0009's, not migration 0001's older boolean-only one).
        `usage_buckets` (§6.6, metering reconciliation) deliberately not created - a
        genuinely separate concern with no metering pipeline to write it yet, mirroring
        migration 0031's own deferral of tables with no writer.
  - [x] `csense_shared.licensing.quota.reserve_quota` (new, shared) - the real TRD §9
        row-lock pattern (`SELECT ... FOR UPDATE` on the tenant's `quota_ledgers` row,
        verify, increment, all inside the caller's own transaction alongside the resource
        it's gating). **No quota_ledgers row for a (tenant, quota_code) means unlimited,
        not zero** - the property that let this be wired into cameras.py, an
        already-shipped, already-tested creation flow, without breaking every tenant that
        predates licensing.
  - [x] migration 0039: `license.manage` (platform_admin) covers plan CRUD + issuance;
        `license.read` (both `tenant_owner` and `tenant_member` - informational, not
        access control) covers a tenant's own view.
  - [x] `POST`/`GET /api/v1/admin/license-plans`, `POST`/`GET /api/v1/admin/licenses` -
        issuing a license merges the plan's `default_entitlements` with the request's
        `entitlement_overrides` (overrides win) into real `license_entitlements` rows, and
        seeds a `quota_ledgers` row for every `limit_numeric` entitlement. One effective
        (active/grace) license per tenant enforced both by a partial unique index and a
        clear `409` at the API layer.
  - [x] `GET /api/v1/tenant/license` - a tenant's own effective license, entitlements, and
        live quota usage. No license is `null`, not a 404 - the correct, unremarkable
        answer for most tenants today.
  - [x] **First real quota-gated flow, wired into `cameras.py`**: `camera.count` is
        reserved before the `INSERT INTO cameras`, in the same transaction: within quota
        succeeds, over quota gets a real `402 quota_exceeded` naming the limit and current
        usage, and a tenant with no license/quota row is unaffected (verified for real,
        not assumed).
  - [x] Verified for real: `scripts/e2e_licensing.py` - a platform admin creates a plan
        and issues a `camera.count=1` license, the tenant's own view shows the real
        entitlement and zero usage, the first camera succeeds, the second is refused
        (402), the tenant's own quota view then shows `consumed_value=1`, a second license
        for the same tenant is refused (409), and a *separate*, unlicensed tenant creates
        two cameras with no restriction at all - the regression check. Full PASS (one
        transient flake on a container that had just restarted, not reproduced on retry,
        confirmed correct by both a manual re-check and a full clean second run).
  - [ ] **Deliberately deferred**: no UI (belongs to the two still-open lines below); no
        license upgrade/downgrade/supersede flow (a second license for an already-licensed
        tenant is refused outright, not migrated); no reseller-allocation
        (`parent_license_id`) flow wired to anything yet - the column exists, nothing
        issues through it; the two-phase `reserved_value` → `consumed_value` path and
        `quota_reservations` rows stay unused - real schema for a future long-running
        create, not needed by the one synchronous flow (`camera.count`) this pass gates.
- [x] Principal Administrator org/license screens (Developer Console)
  - [x] `/dashboard` rebuilt from its Phase 1 placeholder into three real sections:
        Organizations (list + "Create organization" - both `direct_customer` and
        `reseller`, mirroring `POST /api/v1/admin/organizations` exactly), License plans
        (list + "Create plan" with a numeric-entitlement editor - `boolean`/`json`
        entitlement types have no UI yet, named as such in the dialog itself), and
        Licenses (list + "Issue license", tenant picked from the organizations already
        loaded rather than a raw id paste).
  - [x] **Real gap found and closed along the way**: `GET /api/v1/admin/organizations`
        never returned `tenant_id` - there was no way to find the tenant a license needs
        from the organization list alone. Added via an outer join to `tenants` (nullable
        in the response, defensively - organizations and tenants are 1:1 in practice but
        nothing enforces it at the schema level).
  - [x] Issuing a license is step-up-gated (migration from the step-up authentication
        item above) - rather than detecting a `403 step_up_required` reactively, the
        Issue dialog always collects a current MFA code alongside the tenant/plan and
        verifies immediately before issuing, in one submit. A `401` from that step names
        itself clearly when the admin isn't enrolled yet, pointing at Settings.
  - [x] New `/settings` page: this platform developer's own MFA enroll/confirm (secret +
        otpauth URI, real recovery codes shown once)/remove (itself step-up-gated) -
        `components/States.tsx`'s `InlineSpinner` re-added for this (it was deliberately
        trimmed from the Phase 1 port, with a note to add it back "if a future page needs
        it" - this is that page; the CSS for it was already sitting unused).
  - [x] Verified for real: `npm run typecheck`/`lint`/`build` clean, all four new/changed
        routes (`/dashboard`, `/settings` on both apps) confirmed serving `200` through
        Traefik after rebuild; the `tenant_id` addition and org-creation flow exercised
        directly against the real running Admin API (not just the type layer).
- [x] Customer guided onboarding + tenant settings (Customer CRM)
  - [x] New `GET /api/v1/tenant/dashboard` (TRD §10.2's own representative endpoint,
        previously unbuilt - migration 0040, `dashboard.read`) with a real Customer CRM
        page at `/dashboard`, now the default landing route after login/accept-invitation
        (previously fell through to `/incidents`, with no home page at all). Real stat
        tiles (sites/cameras/open incidents/team members), each linking to its own page.
  - [x] A guided-onboarding checklist renders under the stat tiles whenever any step is
        incomplete, computed entirely from data the API already reports (dashboard
        counts + MFA status) - there is no separate "onboarding progress" record to drift
        out of sync with what's actually true. Disappears on its own once every step is
        done.
  - [x] New `/settings` page: this tenant's own license (plan, status, quota usage as
        real progress bars) - `null` renders as "no license assigned yet", not an error -
        and this account's own MFA enroll/confirm/remove, the identical mechanism the
        Developer Console's own Settings page uses, mirrored for the Tenant API's session
        shape.
  - [x] Verified for real: `npm run typecheck`/`lint`/`build` clean, `/dashboard` and
        `/settings` confirmed serving `200` through Traefik after rebuild.
- [~] Central append-only audit query/search foundation
  - [x] `audit.read` permission (migration 0036), granted to both `tenant_owner`
        (customer) and `platform_admin` (platform) - the same permission code safely
        shared across audiences since `TenantContext`/`PlatformContext` are resolved from
        separate audience-scoped JWTs and `require_permission()` only ever checks the
        caller's own resolved context.
  - [x] `GET /api/v1/tenant/audit-events` - one tenant's own history, filters `action`/
        `target_type`/`outcome`/`since`/`until`, keyset-paginated (same cursor shape as
        `incidents.py`: base64 `{occurred_at, id}`, `ORDER BY occurred_at DESC, id DESC`).
  - [x] `GET /api/v1/admin/audit-events` - same shape, platform-wide, plus an optional
        `tenant_id` filter to narrow to one tenant without leaking others' rows (proven,
        not assumed - `scripts/e2e_audit_log.py` checks every returned row matches).
  - [x] Both endpoints query `audit_events` as it already stood - no backfill, no schema
        change. Every feature built this session already writes through
        `record_audit_and_outbox()`, so real history existed before either endpoint did.
  - [x] Customer CRM `/audit` (action/outcome filters, "Load more" cursor pagination) and
        Developer Console `/audit` (tenant_id/action filters) - both confirmed serving
        `200` through Traefik after rebuild.
  - [x] Verified for real: `scripts/e2e_audit_log.py` registers a tenant (a real
        `tenant.created` event), confirms the tenant's own endpoint sees it, confirms the
        `action` filter narrows, confirms a `limit=1` cursor page returns a different row
        next time, bootstraps a throwaway platform admin, confirms the admin endpoint
        finds the same event when `tenant_id`-filtered with no cross-tenant leakage - full
        PASS against the real running stack.
  - [ ] **Deliberately deferred**: no friendly actor-name join (actor shown as
        type + id, not display name); `previous_hash`/`event_hash` hash-chain integrity
        columns remain unpopulated (a pre-existing gap, not introduced or closed this
        slice - tamper-evidence would need a backfill + a hashing point on write, both
        out of scope here).
- [~] Step-up authentication for high-risk actions
  - [x] `csense_shared.security.totp` (new) - RFC 6238 TOTP implemented directly against
        the stdlib (`hmac`/`hashlib`/`base64`/`struct`), not a dependency - conformance
        proven against RFC 6238 Appendix B's own published test vector, not just
        self-consistency. `csense_shared.security.step_up_tickets` (new) - a
        checked-not-consumed Redis recency marker (unlike `invitation_tickets.py`'s
        single-use shape - a step-up must cover several actions in its window, not be
        burned on the first one), 5-minute TTL.
  - [x] `backend/admin_api/app/api/mfa.py` (new): `GET /status`, `POST /totp/enroll`,
        `POST /totp/confirm` (returns 10 hashed-at-rest, Argon2id recovery codes, shown
        once), `POST /verify` (TOTP code or a single-use recovery code), `DELETE /totp`
        (itself requires a fresh step-up). The TOTP secret is stored through the same
        `encrypted_secrets` envelope-encryption path every other secret in this codebase
        uses (`tenant_id=NULL` for a platform-owned secret - already-supported, not a new
        capability).
  - [x] **Scoped to TOTP + recovery codes, stated plainly**: WebAuthn/passkeys (TRD
        §7.1's *preferred* option) needs a browser-side ceremony this pass doesn't build;
        TOTP is explicitly "supported", not merely a fallback. Available and enforced as
        a step-up gate on **one real high-risk mutation** (`POST /api/v1/admin/licenses`)
        proving TRD-SEC-010 against something real, not a strawman endpoint - **not yet
        mandatory at login for every platform session** (TRD §6.2's "mandatory" is a
        separate, larger UX/policy decision: what happens to a developer who has never
        enrolled - deferred, not silently answered).
  - [x] Verified for real: `scripts/e2e_mfa.py` - enroll with a real secret, confirm with
        a code computed the same way an authenticator app would, a wrong code is refused,
        removing MFA before any step-up is refused (enrolling itself doesn't count as
        one), issuing a license before any step-up is refused the same way, a correct
        TOTP code verifies and the identical license request then fails downstream
        instead (proving the gate specifically lifted), a recovery code verifies and is
        confirmed single-use, and removing MFA succeeds once a fresh step-up exists. Full
        PASS. `scripts/e2e_licensing.py` updated to step up before issuing - still full
        PASS, confirming no regression on the already-shipped licensing flow.
  - [x] `backend/tests/test_totp.py` (7 tests, including the RFC vector) and
        `test_step_up_tickets.py` (5 tests) - both real unit-level coverage, not only
        e2e.
  - [x] `backend/tenant_api/app/api/mfa.py` (new) - the identical TOTP + recovery-code
        mechanism mirrored for tenant users (`GET`/`POST /api/v1/tenant/auth/mfa/...`),
        not shared as one router with the Admin API's since the two run under genuinely
        different session shapes (`TenantContext`/`db_session_for_tenant` vs
        `PlatformContext`/`platform_db_session` - TRD §7.2). The TOTP secret is stored
        `tenant_id`-scoped this time, not `NULL` - a customer's own credential, under the
        same RLS every other tenant-owned secret already gets. Enrollment/verification/
        removal all work for real; this pass doesn't yet gate any specific *tenant*
        mutation behind a step-up (nothing in the Tenant API is identified as
        TRD-SEC-010 "high-risk" yet) - the mechanism is real and checkable the moment one
        is.
  - [x] Verified for real: `scripts/e2e_tenant_mfa.py` - enroll, confirm with a real
        computed code, a wrong code refused, remove-before-step-up refused, a TOTP code
        verifies, a recovery code verifies and is confirmed single-use, a second, separate
        tenant owner is unaffected (no shared state), remove succeeds once a fresh
        step-up exists. Full PASS (two transient flakes on a container that had just
        restarted, not reproduced on retry or in isolated manual re-checks - the same
        class of flake the licensing slice already documented, not a code bug).
  - [ ] **Deliberately deferred**: WebAuthn/passkeys; mandatory-MFA-at-login policy; any
        other high-risk mutation besides license issuance (model promotion to production,
        organization creation - same gate, just not wired to them yet); no tenant-side
        mutation gated behind tenant MFA yet either.
- [x] Vertical-slice test: reseller → child tenant → MFA enrollment → empty dashboard →
      quota-exceeded rejection
  - [x] Along the way this also built `GET /api/v1/tenant/dashboard` (TRD §10.2's own
        representative endpoint, previously unbuilt) - real counts (sites, cameras, open/
        total incidents, active team members), gated by a new `dashboard.read`
        (migration 0040, granted broadly like `license.read`). A brand-new tenant's
        dashboard renders as real zeros (plus the owner's own membership), not an error
        or a missing field - the "empty dashboard" step needed something real to check
        against, not a stub.
  - [x] Verified for real, chained through the actual running stack in one script:
        `scripts/e2e_vertical_slice.py` - a platform admin provisions a reseller, its
        owner accepts and creates a child tenant, the child's own owner accepts
        independently, the fresh dashboard reads all real zeros, the child owner enrolls
        TOTP MFA on their own account, the admin issues a one-camera license (itself
        step-up-gated), the child owner creates a site and one camera within quota, a
        second camera is refused with a real `402 quota_exceeded`, and the dashboard
        afterward reflects the real usage. Every step here is a feature this session
        shipped and separately e2e-verified in isolation
        (`e2e_reseller.py`/`e2e_tenant_mfa.py`/`e2e_mfa.py`/`e2e_licensing.py`) - this is
        the proof they compose into one real customer journey, not just that each works
        alone. Full PASS, first run, no flakes.

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
- [x] Signed commands with expiry/idempotency (desired-state push to the device)
  - [x] `device_commands` (SCH §7.5, previously spec'd but never built - migration 0042),
        plus the `desired_state_version`/`observed_state_version` counters SCH §7.3
        already specifies for `edge_devices`. **Now genuinely driven, not just present**:
        the offline-spool phase built the real edge agent this section originally noted
        didn't exist, and the device-level desired-state config push
        (`POST /devices/{id}/config`) built on top of it is these counters' first real
        writer - see that entry for the real, measured (30s -> 15s) cadence-change proof.
        `(edge_device_id, idempotency_key)` unique at the database level - a retried
        issuance genuinely cannot create a second row.
  - [x] `csense_shared.security.signed_commands` (new) - reuses the exact same RS256
        keypair `tokens.py` already signs access tokens with, rather than a second signing
        mechanism. `exp`/`nbf` enforced by the JWT library itself on verify, the same way
        access tokens already get expiry for free.
  - [x] `backend/tenant_api/app/api/edge.py`: `POST`/`GET /devices/{id}/commands`
        (operator, `edge.manage`/`edge.read`) to issue/list; `GET /commands/pending` and
        `POST /commands/{id}/ack` (device-authenticated via `current_agent` - the same
        credential `/heartbeat` already uses) to poll and report result. Polling a
        command marks it delivered in the same call - a device that asks "what do you
        have for me" and gets an answer has, by definition, just received it.
  - [x] **Originally shipped server-side only, with the gap named plainly** ("an edge
        agent to consume it" didn't exist - `edge/agent/` was an empty directory at the
        time). That gap is closed: `backend/edge_agent/` (built in the offline-spool
        phase) is a real consumer, polling `/commands/pending` and acking via
        `/commands/{id}/ack` on every command cycle in production operation, not just in
        the e2e script below.
  - [x] Verified for real: `backend/tests/test_signed_commands.py` (6 tests) - a real
        RS256 round trip, a tampered payload (one byte changed after signing) fails
        verification, an expired command is refused, a not-yet-valid command is refused,
        the wrong audience is refused, a command signed by a different key entirely is
        refused. `scripts/e2e_device_commands.py` - against the real running API and two
        real enrolled devices: an operator issues a signed command, the envelope
        genuinely verifies against the deployment's own running public key (not just in
        the unit tests), re-issuing with the same idempotency key returns the identical
        command and envelope rather than a duplicate, the real device polls and receives
        it (marked delivered), a second poll doesn't see it again, a *different* device's
        ack attempt is refused (404), and the real device's own ack marks it completed
        with its reported result. Full PASS. No regression on the pre-existing edge
        enrolment/heartbeat e2e script.
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
- [x] Camera CRUD, encrypted credential storage (envelope encryption), manual RTSP entry,
      ONVIF discovery stub, NVR adapter interface — done (`cameras.py`: full CRUD,
      credential set/clear, a real digest-auth RTSP/SDP probe in `camera_probe.py`; NVR/
      ONVIF below).
- [x] NVR adapter interface (one mock reference adapter) + ONVIF discovery stub —
      `nvr_adapter.py`'s `NVRAdapter` protocol + `MockNVRAdapter` (never opens a socket,
      by design - real vendor NVRs speak wildly different channel-listing protocols and
      exactly one, mock, adapter was scoped for this pass), wired to a real
      `POST /nvr/discover` (`nvr.py`) that turns discovered channels into real cameras
      through the existing `POST /cameras` - demoable today, no hardware needed
      (`scripts/e2e_nvr_discovery.py`, `NvrDiscoveryDialog.tsx`).
  - [x] **ONVIF discovery is honestly stubbed, not faked.** WS-Discovery is UDP multicast,
        bound to one network segment - FLOW-05 already says discovery is an edge
        responsibility for exactly this reason, and `edge/agent/` is empty (Phase 3+, not
        started). A central endpoint that pretended to discover a customer's real LAN would
        be actively misleading. `POST /sites/{id}/discover-cameras` says so plainly -
        `available: false` with a real reason - rather than an empty list (reads as "no
        cameras found", a different and false claim) or canned fake results.
  - [x] The real WS-Discovery probe/ProbeMatch logic (`csense_shared/onvif/discovery.py`)
        is built and unit-tested against real WS-Discovery/ONVIF XML shapes anyway -
        ready for the edge agent to use directly once that phase starts, not unverified
        code someone would otherwise trust later without ever having run it. Not called
        from any Tenant API route (would only ever scan this container's own Docker
        network, which is worthless for a real customer).
  - [x] `camera.discover` permission (migration 0034), elevated, same tier and grant
        pattern as `camera.probe` - both make the server open a connection to an address
        the caller supplied.
- [x] MediaMTX integration: short-lived signed media session, HLS + WebRTC live view.
  - [x] **HLS is the primary path, not WebRTC** - a scope reversal from the first pass at
        this plan, caught before any code was written: the deployment's real NVR streams
        H.265 only ([[nvr-h265-constraint]]), which no mainstream browser's WebRTC stack
        can decode (confirmed - MediaMTX's own WebRTC endpoint 400s an H.265 source), but
        which HLS carries natively, no transcoding, no extra cost. `POST
        /api/v1/tenant/cameras/{id}/live-session` (`backend/tenant_api/app/api/media.py`)
        relays the mainstream through a MediaMTX path MediaMTX only pulls from
        `sourceOnDemand` (nothing watched, nothing costs anything).
  - [x] **WebRTC transcodes the substream, never the mainstream** - a real, measured cost
        difference (~0.23 vs ~1.7 CPU cores/camera, [[nvr-h265-constraint]]), driven by a
        MediaMTX `runOnDemand` ffmpeg process that only starts once a viewer actually
        connects. Needed the `bluenviron/mediamtx:1.20.1-ffmpeg` image variant - the plain
        tag has no shell or ffmpeg at all (confirmed directly).
  - [x] **Two real auth-webhook bugs found only by testing the real system**, both
        corrected the same session: MediaMTX's `authMethod: http` by default gates its own
        Control API (`action: api`) behind the same webhook meant for viewers - excluded
        explicitly (`mediamtx.yml`'s `authHTTPExclude`), or `media.py` could not configure
        any path at all. It also gates the WebRTC transcode's own internal loopback
        *publish* - a first pass assumed this was implicitly trusted and was wrong; fixed
        with a narrowly-scoped exclusion (`publish` on `*-webrtc` paths only, not `publish`
        generally), and RTSP (8554) was found to need no host port published at all once
        that was understood, narrowing the exclusion's real reach to this compose network.
  - [x] Session tokens (`csense_shared/security/media_sessions.py`) are deliberately
        **not** `ws_tickets.py`'s single-use shape - a live view needs to survive repeated
        auth-webhook checks over a real viewing duration, so this checks-not-consumes and
        relies on a TTL instead; carries `protocol` so an `hls` token can't authorize a
        `webrtc` path or vice versa (they cost genuinely different amounts).
  - [x] `camera.view_live` permission (migration 0033), elevated, granted to both
        `tenant_owner` and `tenant_member` - watching a camera is more routine than editing
        one (`camera.manage`, owner-only), matching `camera.probe`'s existing precedent.
  - [x] **No live masking, deliberately** - real-time per-frame masking would need a
        separate transcode pipeline the no-GPU production server can't really support, and
        would undercut live monitoring's actual purpose. Confirmed with you explicitly
        before building. Masking stays an evidence/retention-time concern, unchanged.
  - [x] **Verified against the real NVR, not just structurally** - `scripts/e2e_live_view.py`
        drives the real API end to end and, when given real credentials (never hardcoded,
        never persisted), runs a real `ffprobe` through the real MediaMTX and confirms
        actual video: the hls path carries real HEVC from the real camera, the webrtc
        path's transcode actually produces real H.264 - not just a plausible-looking config.
  - [ ] **Deliberately deferred**: zone-privacy-level gating (`zones.privacy_level` is
        still CRUD-only, read by nothing); a concurrency/CPU guardrail on the WebRTC
        transcode path (no cap on simultaneous transcodes - a real risk on a fixed-core
        box, not building a limiter without a real policy to build it against); HLS
        fallback via hls.js is built, but no feature-detection fallback exists for a
        browser that can decode neither HEVC-over-HLS nor gets to try WebRTC - the UI
        names the limitation rather than silently failing (`useHlsPlayer.ts`).
- [x] Camera health current-state model + telemetry history — landed in PostgreSQL like
      detections did (MongoDB was removed from the stack, CLARIFICATIONS #19/#20), not
      Mongo as originally spec'd.
  - [x] **Current-state already existed, it turned out**: migration 0020 had already
        given `cameras` `last_probed_at`/`last_error`/`stream_profile`, populated by
        every `POST /cameras/{id}/probe`. What was actually missing was the *history*
        behind that single row - every probe overwrote the last one. Migration 0041 adds
        `camera_health_events`, append-only, written by the same probe call - no new
        probing mechanism, no edge dependency, just making an already-real signal
        durable.
  - [x] `GET /cameras/{id}/health` (new, `camera.read`) - `current_status` derived from
        the existing `cameras` columns (not duplicated into a second current-state
        table, which would just be two places for the same fact to disagree), plus the
        real event history, newest first.
  - [x] Verified for real against the deployment's own real NVR (no mock, no stub -
        `scripts/e2e_camera_health.py`): before any probe, health is a real `unknown`
        with no history; a real probe against the real host (`autotek-dorani-nvr
        .dyndns.org`) writes a real event whose status matches what actually happened
        (this run: `reachable=false`, "requires authentication" - a real, honest network
        round trip); a second probe adds a second event, newest first; an SSRF-refused
        probe attempt (127.0.0.1) writes nothing at all - a blocked attempt isn't a
        health signal. Full PASS.
- [~] **[NEEDS EXTERNAL INPUT]** real camera/NVR hardware or RTSP test feeds for actual
      onboarding validation — partially resolved: a real NVR (`autotek-dorani-nvr
      .dyndns.org`, see [[nvr-h265-constraint]]) was available and used to validate live
      view end-to-end (`scripts/e2e_live_view.py`). The NVR adapter interface is scoped
      mock-only for this pass (see above) so real hardware wouldn't add anything there
      either way. What's still genuinely blocked: a real ONVIF-speaking device to validate
      `csense_shared/onvif/discovery.py`'s WS-Discovery probe against, and the edge agent
      it's waiting for.
  - [x] **2026-09-09: two real named customers onboarded, through the real API, not a
        script.** `abelamm57@bigpond.com` (org "Abelamm", site "Autotek Dorani Site") —
        the same reference NVR above, now the real customer's own hardware, not a test
        fixture. Registered a real account, real site, 7 real cameras (channels c1-c7;
        c8 probed and confirmed to have no camera attached), real encrypted RTSP
        credentials via `PUT .../credentials` (never written to a plain column, never
        committed anywhere). All 7 probed live and reachable
        (`POST /cameras/{id}/probe` → `reachable: true`, real H.265/20fps detected).
        A real published pipeline (`autotek-zone-watch`, `yolov8n-general`,
        `runtime_target=cloud`) assigned to channel 1 - confirmed `pipeline-runtime`
        picked it up within one discovery cycle (`camera_task_started`) and made a real
        successful call to `ai-runtime`'s `/internal/v1/infer` against a genuinely
        decoded frame from this live external feed (`HTTP/1.1 200 OK`, logged). No
        detection cleared threshold on the frames observed - real footage, not staged,
        so an empty loading bay at that moment is a real and expected outcome, not a
        failure.
        `paresh@aptiservices.com` (org "Apti Services") — one real camera
        (`10.0.0.2:554/stream1`, RTSP credentials stored encrypted the same way). Probed
        for real and correctly refused: `address_not_permitted`, the exact behavior the
        customer's own "only works on server" already predicted - a private-LAN camera
        with no edge device/tunnel provisioned is exactly what this SSRF guard exists to
        refuse. This is the security boundary working correctly, not a gap; validating
        this camera for real needs either a real edge device provisioned on that LAN, or
        running this stack on a host that is itself on `10.0.0.0/24` (matching "only
        works on server").
        One environment-specific finding, not a product bug: this sandbox's own DNS
        resolver synthesizes a NAT64 IPv6 address for the NVR's public DDNS hostname
        alongside its real IPv4 (`64:ff9b::/96` + `206.148.37.112` - confirmed via `dig`
        and `socket.getaddrinfo`), which the SSRF policy's own "every resolved address
        must pass" rule correctly refuses (a synthesized address, reserved-range-shaped).
        Worked around here only by temporarily pointing the camera records at the literal
        IPv4 - correct for continued validation in *this* sandbox, wrong for real
        production (this NVR's IP is dynamic; that's the entire reason it has a DDNS
        name) - noted here so nobody mistakes the workaround for the real config. A
        normal production host without DNS64 configured (the actual Phase 9 target)
        would never hit this in the first place.

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
  - [~] **2026-09-08: found and fixed a real drift between this database and this
        checklist's own claim below.** This machine's Postgres volume was lost mid-session
        (unrelated to the biometric decision itself) and re-seeded from
        `import_legacy_models.py` alone, which lands every model at its conservative
        manifest default (`revoked` for the biometric set) - the *separate*
        owner-directed promotion walk (`promote_legacy_models.py`, run once already in
        commit `c1a7a1b`) never got re-applied. All 5 InsightFace models sat at `revoked`
        in the live database for some period even though this checklist and
        CLARIFICATIONS.md #16 both still (correctly) said `production`. Re-ran
        `promote_legacy_models.py --target production` for real (confirmed via dry-run
        first) after explicit user confirmation given the sensitivity - restores the
        already-decided, already-audited state, not a new decision. 5 more
        `model.promote` audit_events rows now exist recording this second promotion
        explicitly; `test_model_registry.py` (9/9) reconfirmed passing after.
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
- [x] Golden dataset + benchmark harness (one reference use case) — see the detailed
      entry below; `model_validation_runs` has its first real row.
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
        `definition_json` is shaped to hold them later. Version creation validates the
        one real thing: the named model has a version in a deployable state (the same
        `DEPLOYABLE_STATES` bar the registry's own promote endpoint uses). **2026-09-08:
        this stage type is now genuinely executed**, not just interpreted at
        publish-validation time — see the next point.
  - [x] `pipeline_assignments` — tenant-owned, RLS, a published version bound to one of
        the tenant's own cameras. **2026-09-08: this used to record intent, not
        execution — it now does both.** A partial unique index on
        `(camera_id, priority) WHERE status = 'active'` enforces SCH §8.8's overlap
        constraint (a simplified form of it — a full overlapping-time-range exclusion
        would need `btree_gist` and buys nothing yet, since nothing reads
        `effective_to`).
    - [x] **The execution gap this bullet used to name is closed** — a real, running
          camera-to-incident loop, not just the registry proving it *could* be enforced.
          Built across 4 tasks
          ([plan](docs/superpowers/plans/2026-09-08-pipeline-execution-runtime.md)):
          `csense_shared.cameras.frame_grab`/`connection` (a real, TCP-forced, fd-leak-
          proof RTSP frame grab reusing `camera_probe.py`'s own DNS-rebinding-safe
          resolve-then-dial path, not a second copy of it);
          `csense_shared.pipeline.runtime` (the pure-logic execution cycle — grab, infer,
          ingest on a qualifying detection, with a deterministic `source_event_id` so a
          retried/overlapping cycle can never double-ingest); the new
          [backend/pipeline_runtime/](backend/pipeline_runtime/) service (one
          `asyncio.Task` per active `runtime_target="cloud"` camera, a 5s discovery poll,
          one camera's exception or a whole failed discovery poll never taking another
          camera's task or the service itself down); and a real end-to-end proof
          ([scripts/e2e_pipeline_execution.py](scripts/e2e_pipeline_execution.py)) that a
          real published video, with nobody posting a detection by hand, produces a real
          incident with real evidence on its own within ~95s, that revoking actually
          stops the camera's task (not just stops it mattering), and that an unreachable
          camera never takes another camera's own progress down.
          **The honest remainder, named rather than silently implied fixed:**
      - `runtime_target="edge"` assignments are still not executed. The edge agent
            (built earlier this phase) deliberately does not run inference — executing
            an edge-targeted assignment is its own, separate, not-yet-started body of
            work, not a small extension of this one.
      - [x] **2026-09-09: a hard cap on concurrently-running camera tasks now exists —
            `pipeline_runtime_max_concurrent_cameras` (default 160), enforced inside
            `run_discovery_loop` itself.** 160 is CLAUDE.md's own measured number for the
            *default* config, not a live measurement: ~0.08 cores/camera at the default
            mainstream-keyframe-only 0.5fps sampling, against ~13 usable cores on the
            production box. At capacity, a newly-discovered active assignment is turned
            away rather than spawned — a per-camera DEBUG line
            (`camera_admission_refused_capacity`) plus one INFO-level aggregate summary
            per discovery cycle (`camera_admission_refused_capacity_summary`, a count of
            how many were refused) so an operator can see *why* cameras aren't running
            without the per-camera line flooding INFO logs at fleet scale — code-quality
            review caught the first version of this logging at INFO per refused
            candidate, per cycle, unbounded; fixed before this was ever exercised near
            its real cap. Already-running tasks are never disturbed by a new candidate
            showing up at capacity (no cancel-and-respawn thrashing), and recomputing
            "who's running vs. who's active" every discovery cycle is exactly what makes
            a freed slot (a revoke, a deprecated pipeline) pick a turned-away assignment
            back up on the very next cycle with no special-case code — confirmed for real
            with an `asyncio`-real test
            (`test_a_turned_away_assignment_is_admitted_on_the_next_cycle_after_a_slot_frees`
            in `backend/tests/test_pipeline_runtime_service.py`), not assumed. A small
            `rotate_for_admission` rotation keeps a saturated cap from always favoring the
            same waiting candidates cycle after cycle for a slot that just freed, without
            ever preempting an already-running camera to do it.
            **The honest remainder, named rather than silently implied fixed**:
        - This is a flat cap on task *count*, not a weighted core budget. It says
              nothing about each assignment's *actual* cost — `sample_fps`, resolution,
              whether live view is concurrently active for that camera — none of which
              factor in today. A fleet where every camera ran at, say, 2fps full-decode
              mainstream (~0.72 cores/camera per CLAUDE.md's own table) would exhaust the
              real 13-core budget at ~18 cameras, well before this count-based cap of 160
              ever engages. A proper weighted admission control — summing each
              assignment's actual `sample_fps`-derived cost against a real core budget —
              is still future work, not attempted here. Real capacity planning stays
              blocked on real traffic — tracked separately, this file's own existing
              Phase 8 entry on real capacity/SLO validation.
        - **A *changed* assignment (not revoked — a `tenant_overrides` edit, a
              pipeline version promotion) can lose its own slot at saturation and not get
              it back.** It's stopped and re-added to the candidate pool the same as a
              brand-new arrival; at a saturated cap, the one slot its own change just
              freed is up for grabs by `rotate_for_admission` on equal footing with every
              other waiting candidate, not reserved for the camera that owned it a moment
              ago. Found in code-quality review, documented in `run_discovery_loop`'s own
              docstring, not fixed — the honest fix (priority for a just-self-freed slot
              before rotation considers anyone else) needs more care than there was time
              for tonight.
      - **CLAUDE.md's own night/IR detection confidence caveat for `yolov8n-general`
            (0.09–0.21 measured, broken at a 0.5 threshold) now applies for real**, for
            the first time — this is the first code path that runs that model
            continuously against a live camera rather than on a single manually-pushed
            frame. Not a new problem this work introduced; a dormant, already-documented
            one that now has a real execution path to actually surface through.
    - [x] **2026-09-08: closed the "never browse the catalogue" stance.** This module's
          own docstring used to say a tenant reads `pipeline_versions` only to validate
          an assignment target, never to list or browse it — reasonable while nothing
          consumed it, but it doesn't survive a real self-service assignment page: a
          tenant cannot pick a `pipeline_version_id` blind. Added
          `GET /api/v1/tenant/pipelines/assignable`, deliberately still narrower than
          the admin catalogue (`published` versions only, never `draft`/`deprecated`;
          no `owner_team`, no pipeline-level `status`) — the docstring itself now
          records this as the corrected, current scope rather than "never".
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
        controls for stage types nothing executes.
  - [x] **2026-09-08: Customer CRM assignment page** (`/pipelines`,
        `PipelineAssignmentsPage.tsx`) — closes the gap the line above used to name
        ("tenant-facing assignment is API-only for now"). Lists a tenant's own cameras
        with their current assignment (or "Not assigned"), assigns from the new
        `GET .../pipelines/assignable` list with dynamic override fields built from the
        chosen version's `allowed_overrides_schema`, and revokes — same empty/no-
        results/loading/error discipline as `CamerasPage.tsx`. Verified against the
        live stack with a real browser
        ([scripts/e2e_pipeline_assignments_crm.py](scripts/e2e_pipeline_assignments_crm.py)):
        register a tenant, add a site and camera, confirm the page starts at "Not
        assigned", confirm the Assign dropdown never shows a draft/deprecated version,
        assign for real, confirm the pill and Revoke button appear, revoke for real,
        confirm it's back to "Not assigned" and "Assign pipeline".
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
- [~] Redis config cache + invalidation, desired-state deployment to edge
  - [ ] **The Redis cache half stays deferred, deliberately** - the same TRD §16 3-layer
        distributed cache already deferred for pipeline config above (line ~935) -
        Postgres-authoritative direct reads are enough at this scale, same reasoning as
        the WS-realtime relay decision. Not revisited by this pass.
  - [x] **The desired-state-deployment half is built**, now that a real edge agent exists
        to push to and verify against (the offline-spool work, earlier in this phase).
        `POST /api/v1/tenant/edge/devices/{id}/config` pushes a signed, versioned config
        over the *existing* `device_commands` transport (migration 0042's signed-command
        mechanism, unmodified - no parallel channel built) - `edge_devices.
        desired_state_version`/`observed_state_version` get their first real writers.
        **Scope deliberately bounded to device-level operational config** (heartbeat
        interval, spool row/byte caps, backoff parameters) - not model/pipeline weights,
        the same boundary the edge agent itself held to (Phase 4 AI-runtime territory).
        Honors FLOW-13's conflict policy exactly: cloud desired state wins, an already-
        expired command is never applied (proven against the *real* existing `expires_at`
        filter, not a new one), and "artifact verification" for this narrow payload means
        the agent validates against the same bounds `config.py` already enforces at its
        own startup - shared, not duplicated with different numbers - so an out-of-bounds
        push is refused and logged, never coerced or silently ignored.
  - [x] **Applied to the running process, not just on next restart** - a device needing a
        power cycle to pick up a heartbeat-interval change isn't "deployed." Two real bugs
        found and fixed by code review before this was called done: the spool's live
        cap-lowering call was blocking the agent's single event loop (a violation of
        `spool.py`'s own documented contract, on the exact SD-card-class hardware this
        project's storage reasoning is built around) - now runs off-loop; and the
        heartbeat's server-side cadence-widening advisory (`next_interval_seconds`, "so
        the cadence can be widened during an incident without shipping firmware") was
        being silently and permanently disabled by *any* config push, even one that never
        touched the heartbeat interval - now pinned only when a push actually sets that
        field.
  - [x] Verified for real, against the live stack, with a real `edge-agent` container:
        `scripts/e2e_edge_desired_state.py` - a real config push measurably halves the
        agent's *actual* heartbeat cadence (30.03s avg -> 15.02s avg, real elapsed
        wall-clock time between real heartbeats, not an echoed version number),
        `observed_state_version` converges to `desired_state_version` via the real API,
        an already-expired push is genuinely never applied (version gap stays visible,
        cadence unaffected), and the server refuses two out-of-bounds pushes with 422
        before either ever reaches the device. Full PASS.
- [x] **Golden dataset + benchmark harness (one reference use case): `license-plate-detector`.**
      TRD §15.2 has nine validation gates - this covers gates 2-4 (load/shape
      compatibility, golden dataset functional tests, accuracy against declared
      thresholds), not the other six (malware scan, adversarial/malformed-input testing,
      thermal profile, edge hardware compatibility, signed release manifest, canary/shadow
      deployment) - named explicitly rather than implied as done.
  - [x] **Golden set: 10 real curated frames, hand-verified by actually looking at each
        one** (`backend/tests/fixtures/golden/license-plate-detector/`), not inferred from
        which curated-review folder they came from - two came from the `person`/`general`
        folders and turned out to show a clearly visible plate anyway, one from the
        `plates` folder turned out not to (a distant night frame, no plate legible at any
        size). Ground truth is presence-only (`has_plate: true/false`), not bounding
        boxes - pixel-accurate boxes by hand aren't something to claim confidence in, so
        the metric is recall/false-positive-rate, not IoU.
  - [x] **Real measured result, run for real against the real model** (not simulated):
        **recall 1.0 (6/6), false positive rate 0.25 (1/4)** at this deployment's own
        established confidence (0.03) - the one false positive was the same distant
        night-IR frame already flagged as marginal during ground-truth review. Mean
        inference 129ms, p95 662ms (CPU, no GPU in this environment - see the
        `[NEEDS EXTERNAL INPUT]` line below). Recorded as a real
        `model_validation_runs` row against the real, already-`production`
        `license-plate-detector` version (`scripts/run_model_validation.py`), with the
        full per-image report uploaded to MinIO alongside it.
  - [x] **`validating → validated` promotion now requires a passing run on record** -
        confirmed safe before adding: nothing previously promoted through that specific
        transition via the API (`scripts/e2e_model_registry.py` only ever exercises
        `uploaded → validating`; the DB-level immutability tests write state directly,
        bypassing the API). Proven against the real running admin-api, not just unit
        tests: refused with no run, still refused after a `failed` one, succeeds only
        after a `passed` one (`scripts/e2e_model_validation_gate.py`).
  - [x] Developer Console `/models` shows each version's latest validation status +
        recall inline (`ValidationBadge`) - deliberately does **not** surface the raw
        report object key/link, matching this same file's own established rule that the
        registry UI never exposes an object key or bucket, only metadata.
  - [ ] Deliberately not covered: every other of the 14 migrated models still has zero
        validation runs (all reached `production` by owner direction, CLARIFICATIONS
        #15/#16, never through this gate); a UI-driven way to *trigger* a run (today it's
        a script, matching `import_legacy_models.py`'s own precedent for operator
        tooling); the other six TRD §15.2 gates named above.
- [~] **[NEEDS EXTERNAL INPUT]** GPU/edge hardware for real profiling; default to CPU/ONNX
      Runtime reference numbers otherwise. A Raspberry Pi 5 became available 2026-09-02 -
      real arm64 hardware for the edge agent (already cross-build-verified for
      `linux/arm64` in the offline-spool work); still no GPU for the runtime's own
      profiling numbers, which is the part this line is actually about.
- [x] **[DECIDED]** YOLOv8/AGPL-3.0 licensing for commercial hosting — was already resolved
      2026-08-26, before this checklist line was last updated to say otherwise (see
      CLARIFICATIONS.md #15: owner accepted the position given these models' production
      use since 2022, and directed all 5 be wired up). They already are -
      `backend/migrations/legacy_model_manifest.py`'s `_AGPL` license_metadata is set on
      all 5, all reached `production` status, none gated. Reconfirmed 2026-09-02: owner
      directed retrained-on-our-data weights be used and wired in on the same basis.
      **Retraining on proprietary data does not itself change the license** - AGPL-3.0
      attaches to Ultralytics' architecture/training code, not cured by different weights
      - so this remains the same accepted, recorded position CLARIFICATIONS.md #15
      already carries, not a new legal fact. `license_metadata` stays the visible record
      if the position is ever revisited.
- [~] **18 models pulled in `uploaded` (2026-09-03/09-05: 15 `uniface-zoo` + 3 Airivu
      intern-trained) - real validation against a real golden set, not a rubber stamp.**
      Owner instruction was "validate and ship tha too quick"; this pass is the honest
      version of that - real per-model evidence, real findings (two real problems found,
      not promoted), not everything flipped to `production` on request.
  - [x] **New TRD §15.2 gate 2-4 path added**: `/internal/v1/validate-infer` in
        `backend/ai_runtime/app/main.py` + `get_by_version_id`/`VALIDATABLE_STATES` in
        `registry.py`. The existing `run_model_validation.py` path
        (`/internal/v1/infer`, by model name) only ever reaches `DEPLOYABLE_STATES`
        (`validated`/`staging`/`production`) - which is exactly backwards for a genuinely
        new upload: gate 2-4 evidence has to be producible *before* promotion, not only
        reproducible after it. The new route is addressed by `version_id` (not name) and
        allows `uploaded`/`validating` too, but still refuses `revoked`/`deprecated` - same
        boundary the by-name path always enforced, just not artificially blocking a
        version that has simply never been promoted yet.
  - [x] **Real golden set built for the 3 intern models**: 8 hand-verified images,
        `backend/tests/fixtures/golden/intern-{abuse-detection,child-adult-detection,
        classroom-hazard-detection}/manifest.json` - 7 pulled live from the real onboarded
        customer's own Autotek NVR (channels c1/c2/c3/c4/c5/c7/c8, 2026-09-09), 1 a stock
        photo already sitting in this platform's own MinIO evidence bucket as a reused
        placeholder capture frame (discovered while looking for real evidence - documented
        rather than silently swapped for something else). **No real photograph of a real
        child was used or exists anywhere in this repository** - the only ones this project
        ever had were deleted earlier this session on the owner's own explicit instruction,
        so `Child`/`Abuse`/hazard-positive recall is `null` (unmeasured) everywhere, never
        guessed at. Committing real customer-site frames into tracked, pushed git history
        was itself confirmed with the owner before doing it (2026-09-09).
  - [x] **`run_intern_model_validation.py` - real runs recorded for all 3, real per-class
        metrics, via the real Admin API + a real service account, report uploaded to
        MinIO.** Threshold used: `max_false_positive_rate=0.0` on every class (stricter
        than `run_model_validation.py`'s general 0.5 default - deliberate: there is zero
        recall evidence for any of these 3 to weigh a looser tolerance against, and two are
        alarm classes where a false trigger on provably-calm footage is itself the finding).
    - [x] **`intern-classroom-hazard-detection`: PASSED** - 0.0 FPR on all 4 classes
          (`hazardous_object`/`wet_floor`/`fire`/`smoke`) across every unambiguous frame.
          Promoted `uploaded -> validating -> validated`. **Not pushed further to
          `staging`/`production` here** - recall is still `null` (no real hazard event was
          ever tested against it, only its false-positive behaviour on calm footage), and
          this is a brand-new intern-trained model with undocumented training-data
          provenance (`review_required: true` in `intern_model_manifest.py`). Going live
          in a real customer site is a real decision, left for the owner rather than
          pushed through on a thin, FPR-only pass.
    - [x] **`intern-child-adult-detection`: FAILED - a real, safety-relevant finding.**
          `Child` false-positive rate 0.25 (2/8): it tagged one real adult (a person
          partially cropped at a frame edge in an otherwise-correct 3-adult street photo)
          *and* an entirely empty room with zero people in it as containing a "Child".
          `Adult` scored recall 1.0 / FPR 0.0 on what little was tested. Promoted only to
          `validating` - the Admin API's own gate (`validating -> validated` requires a
          passing run on record) correctly refuses to let this go further, and it should
          not be forced past that. **Not promoted, not shipped** - a model this unreliable
          on its safety-critical class is a real problem, not a formality to route around.
    - [x] **`intern-abuse-detection`: FAILED - same story.** `Abuse` false-positive rate
          0.375 (3/8): it fired on three frames with nothing happening at all (two empty
          rooms, one blank scene). Promoted only to `validating`, not further. Recall was
          never measurable either way (no real abuse footage exists in this project, and
          none was staged to manufacture a positive example - see the manifest's own note
          on why that would be its own bad practice).
  - [~] **The 15 `uniface-zoo` ONNX models are a materially bigger job - gate 2 (load/
        shape) done for real for all 15; gates 3-4 (accuracy) now done for real for 13 of
        them: the 6 low-risk models (all passed real testing) and the 4 cross-check models
        (3 passed - 1 promoted to `validated`, 2 blocked at `validating` by the biometric-
        acknowledgement gate; 1 failed and correctly not promoted further) on 2026-09-11,
        plus the 3 face detectors (all 3 passed, all 3 at `validating`) on 2026-09-16. Only
        the 2 no-guess models remain undecoded, deliberately.** Checked
        `backend/ai_runtime/app/engines.py`: `OnnxEngine._decode` only understands the
        end-to-end YOLO 6/7-column layout the plate detector uses. None of these 15 models
        are YOLO-shaped - **zero decode logic exists anywhere in this codebase for any of
        them.** A wrong guess at a decode fails silently - exactly the failure class this
        project has already been bitten by twice (the plate detector's class/score column
        swap; the child/adult label_map that was backwards in its first draft) - so nothing
        below was decoded on a guess.
    - [x] **Real gate-2 evidence for all 15**: loaded every actual artifact via
          `onnxruntime.InferenceSession` inside the real `ai-runtime` container (fetched
          from the real `csense-models` MinIO bucket, not a copy) and recorded its true
          input/output tensor names, shapes and dtypes. All 15 load cleanly - no artifact
          is corrupt or unopenable. Full recorded shapes in this session's own working
          notes; the takeaway that matters going forward:
          - **6 are low-risk to decode**: `adaface`/`edgeface`/`mobileface`/`sphereface`
            (all a single `(batch, 512)` embedding, two even self-name their output
            `embedding`) and `facemesh` (`(batch, 468, 3)` + a presence score - MediaPipe's
            own public, stable 468-point spec) and `modnet` (`(batch, 1, H, W)`, a single
            alpha matte matching MODNet's own documented single output). Straightforward,
            well-documented, verifiable against a real face crop without guessing.
          - **4 need a public-repo cross-check before decoding, not a blind guess**:
            `fairface` (named `race_output`/`gender_output`/`age_output` - the *column
            order within each* is the public FairFace repo's own documented convention,
            not self-describing from the ONNX graph alone), `minifasnet` (3-class
            real/spoof - order matches the public Silent-Face-Anti-Spoofing repo's own
            convention), `mobilegaze` (`yaw`/`pitch` each `(1, 90)` - a 90-bin
            classification-to-angle convention matching the public L2CS-Net/6DRepNet
            approach, needs a softmax-weighted-expectation decode, not argmax), `pipnet`
            (`cls_map`/`offset_x`/`offset_y`/`nb_x`/`nb_y` at `98` points × `980` (=98×10)
            neighbours - the exact signature of the public PIPNet repo's own 98-point
            scheme). Decodable with real confidence, but only by matching the original
            public architecture's documented convention and then checking the result
            against a real face crop - not from the ONNX graph in isolation.
          - **3 are anchor-based detectors where the anchor-generation math itself must
            exactly match training config**: `blazeface` (896 anchors × 16, the classic
            BlazeFace regressor/score split), `centerface` (heatmap + scale + offset +
            landmarks, CenterNet-style), `retinaface` (`loc`/`conf`/`landmarks`, the
            classic 5-point-landmark RetinaFace head). Same family of risk already named
            in this file for `license-plate-detector`'s NMS/anchor handling - a wrong
            anchor grid produces plausible-looking but silently wrong boxes. *(All 3
            decoded and validated 2026-09-16, see the dedicated entry below. One
            correction this tiering got wrong and the later work caught: `centerface` is
            NOT anchor-based at all - it is anchor-free/CenterNet-style, established by
            reading the real reference source rather than inheriting this grouping.)*
          - **2 should not be decoded on a guess at all, and are not**: `bisenet-parsing`
            exports 3 output tensors named `output`/`414`/`424` - the last two are raw
            ONNX-export node IDs, not semantic names, and nothing in the delivered artifact
            says which is the real per-pixel class map vs. an auxiliary training-only head.
            `faceattribnet` exports one `(batch, 5)` `probability` tensor with **no
            per-column label at all** - 5 of *what*, in *what order*, is not recoverable
            from the graph. Exactly the `license-plate-ocr` situation already on record in
            this file ("shipping a guessed mapping would be exactly the failure mode
            `OutputContractUnknownError` exists to avoid") - needs the original training
            config or a labelled reference to resolve, not guessed at here.
    - [x] **New validation-only path added to support this**: `/internal/v1/validate-infer`
          (`backend/ai_runtime/app/main.py`) + `get_by_version_id`/`VALIDATABLE_STATES`
          (`registry.py`) - addressed by `version_id`, reaches `uploaded`/`validating`
          versions the by-name `/internal/v1/infer` route (deployable-states only)
          structurally cannot, so gate 2-4 evidence is producible *before* promotion. Built
          and proven working against the 3 intern models above before this probing pass.
    - [x] **Decode logic written and validated for the 6 low-risk models** (`adaface`/
          `edgeface`/`mobileface`/`sphereface`-recognition, `facemesh`-landmark, `modnet`-
          matting) - `UnifaceEmbeddingEngine`/`UnifaceFaceMeshEngine`/`UnifaceMattingEngine`
          in `engines.py`, following `InsightFaceEngine`'s own established pattern (a
          dedicated method per non-box output shape - `embed()`/`landmarks()`/`matte()` -
          rather than forcing a `Detection`/`infer()` fit; `infer()` raises
          `OutputContractUnknownError` for all three, same as `InsightFaceEngine`'s ArcFace
          path). The 4 cross-check models (fairface/minifasnet/mobilegaze/pipnet) were a
          separate agent's own concurrent worktree, not touched here; the 3 anchor-based
          detectors and the 2 no-guess models remain untouched, per the original risk
          tiering above.
      - [x] **All 6 real gate-2 shapes independently re-probed this session** (not trusted
            from the write-up above), matched exactly:
            `adaface`/`mobileface`/`sphereface` output `output` `(batch/1,512)`,
            `edgeface` output `embedding` `(batch,512)`, `facemesh` `landmarks`
            `(batch,468,3)` + `score` `(batch,1)`, `modnet` `output` `(batch,1,H,W)`.
      - [x] **Preprocessing/alignment/postprocessing ported from the real public
            reference implementation these weights ship with**
            (`github.com/yakhyo/uniface`, MIT - the real clone sitting at `uniface-main/`
            in this repo's working tree, cross-checked line-for-line against the same
            files fetched fresh from GitHub this session), not reimplemented from papers
            or guessed from the ONNX graph - the same discipline this file already applies
            everywhere else a wrong guess would produce a plausible-but-silently-wrong
            output. One specific claim was independently verified against the real
            installed package, not just read from source: `uniface.face_utils.
            reference_alignment` (the 5-point 112x112 ArcFace template uniface's own
            alignment uses) is byte-for-byte identical to `insightface.utils.face_align.
            arcface_dst`, confirmed live via `docker exec` against the real `ai-runtime`
            image - so the 4 recognition engines reuse `InsightFaceEngine`'s already-
            production `norm_crop` alignment path rather than re-deriving uniface's own
            `estimate_norm`/`face_alignment`, and this is not an assumed equivalence.
      - [x] **A real dispatch collision was found and fixed, not just theorised**: 4 of
            the 6 models (`mobileface`/`sphereface`/`adaface`/`edgeface`-recognition) are
            registered with `task_code="face_recognition"` - the exact task_code
            `InsightFaceEngine` already owned for `insightface-buffalo-l-recognition`
            (confirmed against the real manifests). The engine dispatch in `build_engine`
            (`engines.py`) and `ModelPool.get` (`pool.py`) was changed from task_code-
            keyed to exact-`model_name`-keyed for both `InsightFaceEngine` and these 3 new
            engines, closing the collision - a regression test
            (`test_build_engine_face_recognition_task_code_does_not_reach_insightface` in
            the new `backend/tests/test_uniface_engines.py`) pins this so it can't silently
            regress.
      - [x] **New `/internal/v1/validate-infer-uniface` endpoint** (`main.py`) - the
            sibling of `/internal/v1/validate-infer` for outputs that aren't a `Detection`
            list at all (embedding/landmarks/matte), same `version_id`/`VALIDATABLE_STATES`
            scoping. For the 5 face-shaped models it first runs the platform's own already-
            verified, `production`-state SCRFD detector (`insightface-buffalo-l-detect`,
            confirmed live) over the frame, then decodes the target model per detected
            face - reusing the real production face cropper rather than building a new one,
            per this session's own instructions.
      - [~] **This new endpoint could not be validated through the live HTTP API - written
            to code, not yet deployed, honestly reported rather than silently skipped.**
            The real `csense-ai-runtime-1` container on this stack was started from an
            image built before this work and does not have this code; rebuilding/
            restarting it was explicitly off-limits this session (a real client demo was
            running on this exact Docker stack). Real fallback validation was performed
            instead, the way the task's own instructions named: `docker cp`'d the
            candidate `engines.py` into a **throwaway path** inside the real running
            container (`/tmp/uniface_validate/`, never the container's actual
            `/app/app/engines.py`) and imported it directly against real MinIO-fetched
            artifacts and a real image, using the container's own already-installed
            onnxruntime/insightface/opencv - `scripts/_uniface_validate_in_container.py`
            is what runs inside the container; `scripts/run_uniface_model_validation_lowrisk.py`
            drives it and records the result. This is real, gate-2/3 evidence (real
            artifacts, real ported preprocessing code, real image) - honestly short of
            being the actual deployed HTTP path, which remains a named next step once the
            owner restarts `ai-runtime`.
      - [x] **Real golden manifests built for all 6**
            (`backend/tests/fixtures/golden/uniface-{adaface,edgeface,mobileface,
            sphereface}-recognition/`, `uniface-facemesh-landmark/`, `uniface-modnet-
            matting/manifest.json`) - shaped differently from the intern-* manifests
            (there is no class/presence label to score for an embedding/landmark/matte
            model) but same honesty discipline. **Only one image anywhere under
            `backend/tests/fixtures/golden/*/` has real, usable human faces**
            (`stock_streetscene_3adults.jpg`, already in the repo) - every Autotek NVR
            frame pulled for the 3 intern models is night/blank/vehicle/empty-room with no
            face in it, and no fresh live camera frames were pulled this session (the live
            demo, and a second agent already using the platform's one real NVR, per this
            session's own instructions). The platform's own production SCRFD detector
            found 2 of the 3 visible real faces at its default threshold. **No same-
            identity pair exists anywhere in this project's fixtures**, so true recognition
            recall (same person, two frames, embeddings actually match) is `null` -
            unmeasured, stated plainly in every manifest's own note, not padded over with a
            staged/fabricated positive pair (the task's own explicit instruction, and the
            same discipline `run_intern_model_validation.py` already established for the
            missing-child-photo gap).
      - [x] **Real per-model measured results, all genuinely passed gate 2-3** (shape/
            dtype/finiteness/non-degeneracy, and for facemesh, geometric correctness -
            landmarks land within the detector's own bbox for both real faces): all 4
            embedding models produced finite 512-d vectors, not identical between the two
            different real people (adaface/edgeface/mobileface/sphereface pairwise cosine
            similarity -0.03/0.01/0.08/0.04 - reported as a weak signal only, since high-
            dimensional random vectors also produce near-zero cosine similarity by
            concentration of measure; this is NOT claimed as proof of discriminative
            power). One real, checkpoint-specific finding recorded rather than smoothed
            over: `adaface`'s raw ONNX output is already exactly L2-normalised (measured
            full-precision norm = 1.0 on both real faces) while `edgeface`/`mobileface`/
            `sphereface`'s raw outputs are not (3.48-3.70 / 1.77-1.95 / 0.29-0.38) - the
            decode deliberately returns the raw value in all 4 cases (matching
            `InsightFaceEngine.embed()`'s own raw convention), so this is a fact about the
            AdaFace checkpoint's own final layer, not something the decode code did.
            `facemesh` produced 468x3 finite landmarks per face landing within (or a few
            px beyond, at jaw/forehead - expected) each face's own detector bbox for both
            real faces. `modnet` produced a finite, correctly-shaped, bounded [0,1] matte
            (mean alpha 0.063, ~6% coverage - a plausible, non-degenerate result for a wide
            street scene, honestly *not* MODNet's own tuned portrait use case, and not
            scored against a pixel-level ground truth none exists for).
      - [x] **Real `model_validation_runs` rows recorded via the real Admin API** for all
            6 (`scripts/run_uniface_model_validation_lowrisk.py`, same service account/Admin API
            flow as `run_intern_model_validation.py`) - all 6 status=`passed` on the honest
            gate-2/3 basis above, `metrics.method_note` on every row stating plainly that
            this ran via the docker-cp/local-import fallback, not the (not-yet-deployed)
            HTTP endpoint.
      - [x] **Promotion, and a real, load-bearing discovery about it.** All 6 promoted
            `uploaded -> validating` for real. **5 of the 6 (all but `modnet`) could NOT be
            promoted further to `validated`, and this is not a bug or an oversight**: all 5
            face-recognition/landmark models are `access_classification=biometric`, and
            `validated` is itself one of `DEPLOYABLE_STATES` in `admin_api/app/api/
            models.py` - so the *existing* biometric-acknowledgement gate (unrelated to
            this session's work, already in the codebase) refuses `validating -> validated`
            for a biometric model without `acknowledge_biometric=true`, the same as it
            would refuse `validated -> staging`. This was verified for real, not reasoned
            about in the abstract: the actual Admin API returned a real `422
            biometric_promotion_requires_acknowledgement` for all 5. Per this session's own
            explicit instructions, `acknowledge_biometric=true` was never passed - that
            acknowledgement is the project owner's call. **`uniface-modnet-matting` is
            `access_classification=standard`, not biometric** (a portrait alpha matte is
            not an identity template - confirmed against the real registry row before
            assuming otherwise), so it has no such gate in its way and is now genuinely
            `validated`. Net real state: 5 models sit at `validating` (as far as they can
            go without the owner's biometric sign-off), 1 (`modnet`) at `validated`. No
            model was pushed to `staging`/`production`.
      - [x] **13 new unit tests** (`backend/tests/test_uniface_engines.py`) pin the dispatch
            table (including a regression test for the exact collision found above), the
            AdaFace-vs-everyone-else BGR/RGB preprocessing split, and the FaceMesh ROI/
            inverse-affine geometry, with synthetic inputs and known-correct answers - same
            discipline as the existing `test_ai_runtime_decoder.py`. Full existing suite
            (`test_insightface_engine.py`, `test_ai_runtime_decoder.py`) still green
            alongside them (44 passed total); `ruff check` clean on every touched file.
    - [~] **The 4 cross-check models (`fairface`/`minifasnet`/`mobilegaze`/`pipnet`)
          decoded and validated for real, 2026-09-11, in an isolated worktree** (the 6
          low-risk models were dispatched to a separate worktree/agent and are not this
          entry's scope). Every decode is grounded in this project's own real upstream
          artifact source per `backend/migrations/uniface_model_manifest.py`'s own
          `legacy_paths` (not the ONNX graph alone, and not the generic public repo
          guessed at random) - each cross-checked against **two independent real
          sources** (the artifact's actual upstream repo, plus the original repo that
          upstream itself re-implements) before any code was written:
        - **`uniface-fairface-attributes`**: column order for `race_output`(7)/
          `gender_output`(2)/`age_output`(9) confirmed against `github.com/dchen236/
          FairFace`'s own `predict.py` (`race_outputs = outputs[:7]` etc, its own
          hardcoded label lists) *and* this artifact's real source, `github.com/yakhyo/
          fairface-onnx` (`legacy_paths` in the manifest), whose `models/predictor.py`
          defines the identical three label lists in the identical order independently.
          Preprocessing (224x224, RGB, ImageNet mean/std) confirmed from the same
          `predict.py`. Face crop: aligned via this platform's own already-production
          ArcFace-style 5-point alignment (`insightface.utils.face_align.norm_crop`, the
          same primitive `InsightFaceEngine.embed()` already uses) rather than FairFace's
          own `dlib.get_face_chips` (dlib is not part of this stack) - a disclosed,
          good-faith approximation, not a claimed pixel-exact match, documented in
          `_crop_scrfd_aligned_chip`'s own docstring.
        - **`uniface-minifasnet-antispoofing`**: index 1 = real/live confirmed against
          `github.com/minivision-ai/Silent-Face-Anti-Spoofing`'s own `test.py`
          (`label = np.argmax(prediction); if label == 1: ... "Real Face"`) *and* this
          artifact's real source, `github.com/yakhyo/face-anti-spoofing` (`minifasnet_v2_
          MiniFASNetV2.onnx` per the manifest), whose `main.py` has the identical
          `"Real" if label_idx == 1 else "Fake"`. Crop geometry (scale=2.7, symmetric
          box-centred expansion) replicated exactly from that same repo's own
          `utils.crop_face` - `2.7` is its own documented constant for the "v2" variant,
          matched against the artifact's own `local_name`.
        - **`uniface-mobilegaze-estimation`**: 90-bin softmax-weighted-expectation
          (`yaw = sum(softmax(logits) * bin_index) * 4 - 180`) confirmed against this
          artifact's real source, `github.com/yakhyo/gaze-estimation` (built on L2CS-Net,
          `resnet18_gaze.onnx` per the manifest) *and* independently against the original
          `github.com/Ahmednull/L2CS-Net`'s own `test.py`/`train.py`, which use the
          identical `* 4 - 180` formula for their own 90-bin Gaze360 config. Crop: raw
          detector bbox, no margin - confirmed from the same `yakhyo/gaze-estimation`
          source (`frame[y_min:y_max, x_min:x_max]` directly, no padding).
        - **`uniface-pipnet-landmark`**: heatmap-peak + offset + neighbour-vote-average
          decode replicated from the original `github.com/jhb86253817/PIPNet`
          (`lib/functions.py::forward_pip`, `lib/demo.py`'s merge step, `lib/
          data_utils.py::get_meanface` for the reverse-index neighbour table - the WFLW
          98-point `meanface.txt` copied verbatim into `engines.py` as `_WFLW98_MEANFACE`)
          *and* independently confirmed against this artifact's real source, `github.com/
          yakhyo/pipnet-onnx` (`pipnet_r18_wflw_98.onnx` per the manifest), whose own
          numpy port implements the identical argmax-peak/offset-gather/reverse-index-
          merge algorithm and the identical asymmetric 1.2x crop (`pad=0.1`,
          shrink-top/expand-others - "remove a part of top area for alignment" per the
          original paper). One disclosed, non-source-confirmed assumption: `cls_map` is
          passed through a sigmoid purely to report a 0..1 confidence per point: peak
          *location* (argmax) is invariant to that choice, so only the reported
          confidence number - never landmark position - depends on it.
        - **Correctness fix beyond just adding a decode**: `build_engine()` now dispatches
          these 4 by exact `model_name` (a new, small `pool.py` change threads
          `registered.model_name` through), not by `task_code` - `task_code` alone is
          ambiguous (`face_attribute` is shared with the deliberately-not-decoded
          `faceattribnet`; `face_landmark` is shared with the separately-decoded
          `facemesh`). **Confirmed live and empirically, not assumed**: calling the real,
          still-running `/internal/v1/validate-infer` against `uniface-fairface-
          attributes` *before* this fix returns `detection_count: 0` with **no error at
          all** - the old `OnnxEngine._decode` misreads `race_output`'s `(1, 7)` shape as
          a 7-column end2end YOLO box row purely by coincidence (7 classes = 7 columns)
          and silently produces zero boxes instead of erroring. A live example of exactly
          the silent-wrong-decode failure class this project has already been bitten by
          twice (the plate detector's class/score swap; the backwards child/adult label
          map) - closed by routing these 4 away from `OnnxEngine` entirely; each new
          engine's own `infer()` now raises `OutputContractUnknownError` loudly instead,
          pointing at its real dedicated method (`predict_attributes`/`predict_liveness`/
          `estimate_gaze`/`predict_landmarks`), the same "dedicated method, not shoehorned
          into `Detection`" shape `InsightFaceEngine.embed()` already established. Three
          of the four need a real face crop: rather than a new cropper, they take a full
          frame plus a `Detection` from the paired, already-`production` face_detection
          model (`insightface-buffalo-l-detect`/SCRFD), the same two-stage-pipeline shape
          the plate detector/OCR and face detection/recognition pairs already use.
        - **Could not be validated through the live running API - and correctly stopped
          short of the container restart that would fix that, per this session's own
          explicit constraint** (a real client demo was running on this exact `ai-
          runtime` container). The new decode code lives only in the worktree, not the
          running image. Instead, validated by importing `engines.py` directly (by file
          path - the module is self-contained, stdlib + numpy only at import time) into a
          separate local Python 3.12 venv (this dev machine's own system Python was 3.9.6
          via Xcode's toolchain and too old for current `onnxruntime`/`insightface`
          wheels), with `onnxruntime`/`opencv-python-headless`/`insightface`/`minio`
          installed fresh from PyPI - genuinely exercising the real, final decode code
          against the real artifact bytes (fetched from the real `csense-models` MinIO
          bucket over its published `localhost:9000` port, sha256-verified against the
          registry's own `artifact_sha256` before use) and a real face crop from the
          real, already-`production` SCRFD detector - not a reimplementation, not a
          separate test double. `scripts/run_uniface_model_validation_crosscheck.py` documents this
          in full, including exactly how it differs from `run_intern_model_validation.py`
          (which calls the live HTTP API by design) and how to re-run it end-to-end
          through the real service once `ai-runtime` is next rebuilt.
        - **Real golden evidence, and its honest limit**: exactly one real, usable source
          image exists anywhere in this repo's committed fixtures with clearly visible
          human faces - `stock_streetscene_3adults.jpg` (already used by
          `intern-child-adult-detection`), 2 real adult male faces (a third person is
          cropped at the frame edge and wasn't detected by SCRFD). Checked directly, not
          assumed: every other committed golden frame (7 Autotek NVR night/interior
          frames, 9 license-plate-detector vehicle photos) has no face at a resolution a
          face detector could use. New manifests at `backend/tests/fixtures/golden/
          uniface-{fairface-attributes,minifasnet-antispoofing,mobilegaze-estimation,
          pipnet-landmark}/manifest.json` match faces to ground truth by nearest detected
          face-centre (not assumed output order), and are honest about what is and is not
          gradable from 2 faces in 1 photo:
          - **`uniface-fairface-attributes`: PASSED, 4/4** - gender correct on both faces
            (Male/Male, confidence 0.73/1.00) and age_bucket both landed in the adult
            range (`30-39`, not a child bucket). Race is recorded in every result
            (`Middle Eastern` top-1 on both, `White` a distant second on both) but
            **never scored** - nobody photographed in a public street scene has a
            knowable ground-truth self-identified race from visual inspection, and
            grading it would be exactly the invented-precision `intern-child-adult-
            detection`'s own manifest already declined to do for age/ethnicity. FairFace
            is CC BY 4.0 - attribution is required wherever its output is ever surfaced
            to a user (recorded in `uniface_model_manifest.py`'s own `_CC_BY` metadata);
            that obligation belongs to whatever UI eventually renders this, not to this
            decode-only engine. Promoted `uploaded -> validating`; blocked at
            `validating -> validated` by the real `biometric_promotion_requires_
            acknowledgement` gate (422, confirmed live) - correctly not overridden,
            `acknowledge_biometric` is the owner's call per this task's own instruction.
          - **`uniface-minifasnet-antispoofing`: FAILED, 0/2 - a real finding, not a bug
            in the harness.** Both real photographed faces were classified `fake` at
            99.3-99.4% confidence. Verified this is not a crop-geometry bug before
            reporting it as a model finding: re-ran both faces at `scale` values from 1.0
            (tight box) to 4.0 and with the raw unscaled detector bbox - **identical
            "fake" result to 3 decimal places at every scale**, so the decode logic
            itself is not the variable. The most likely real cause, named rather than
            hidden: both subjects wear opaque sunglasses (a materially harder case for a
            model that leans on eye-region texture/reflections) and both faces are small
            in the source photo (native ~35-40px wide, upscaled to the model's 80x80
            input, losing exactly the fine texture detail anti-spoofing relies on).
            **Spoof-class recall remains entirely unmeasured** - this project has no real
            print/replay-attack photograph and none was staged to manufacture one (same
            reasoning `intern-abuse-detection`'s manifest already recorded for not
            staging a fake positive); what this run actually measures is real-class
            recall, and on the 2 real faces available it is 0/2. Promoted only to
            `validating`, same as the two FAILED intern models - **not promoted further,
            not shipped**, this is `access_classification=standard` (see note below) so
            the biometric gate would not have blocked it, but a 0/2 real-class recall
            earns exactly the same "not promoted" outcome the FPR-driven intern failures
            got, on its own real merits.
          - **`uniface-mobilegaze-estimation`: PASSED, 2/2** on a deliberately coarse
            plausibility check (`|yaw|<=60deg`, `|pitch|<=60deg` - a bound against a
            physically-absurd angle, not a claim of measured accuracy; no ground-truth
            gaze angle is recoverable from a static photo without eye-tracking
            equipment). Measured: yaw 3.9deg/-15.4deg, pitch -28.1deg/-9.7deg - well
            inside the envelope and directionally sane for two pedestrians walking
            forward. Promoted all the way to `validated` (`access_classification=
            standard` per the real manifest/DB - see note below - so no biometric gate
            applied).
          - **`uniface-pipnet-landmark`: PASSED, 2/2** on plausibility (all 98 points'
            bounding box falls within the paired detector's own bbox, expanded by a
            stated 20% tolerance - not point-by-point, since no hand-labelled 98-point
            reference exists in this repo and fabricating one would be its own guess
            dressed up as ground truth). Measured agreement was tight even without the
            tolerance: face 0's landmark bbox `[270.8,436.7,306.8,471.6]` px against the
            detector's own `[271.0,420.0,307.2,472.0]` px - the top edge sits inset by
            ~16px, matching PIPNet's own deliberate forehead-exclusion crop convention
            almost exactly, not a loose miss. A meaningful test of the neighbour-vote
            decode specifically, not just peak-finding: both subjects' eyes are covered
            by sunglasses, so the eye-region points had to come from neighbour votes, not
            a directly visible feature. Promoted `uploaded -> validating`; blocked at
            `validating -> validated` by the same real biometric gate as fairface.
        - **Correction to this task's own stated premise, caught by checking the live DB
          rather than taking it on faith**: only `fairface` and `pipnet` are actually
          `access_classification=biometric` in this registry - `minifasnet` and
          `mobilegaze` are `standard` (confirmed both in `model_versions` directly and in
          `uniface_model_manifest.py`'s own `LegacyModel(...)` calls, which simply omit
          `access_classification` for those two, unlike every other entry in the file).
          `mobilegaze` reaching `validated` above is that real classification working as
          designed, not an oversight.
        - Not touched here: the 6 low-risk models (dispatched separately) and the 3
          anchor-based detectors / 2 no-guess models (still correctly excluded, per the
          reasoning already on record above).
    - [~] **The 3 face detectors (`blazeface`/`centerface`/`retinaface`) decoded and
          validated for real, 2026-09-16, in an isolated worktree** - the highest-risk
          group of the 15 by this file's own tiering ("a wrong anchor grid produces
          plausible-looking but silently wrong boxes"). All 3 passed; all 3 sit at
          `validating`. `BlazeFaceEngine`/`CenterFaceEngine`/`RetinaFaceEngine` in
          `engines.py`, registered in `_UNIFACE_ENGINES_BY_MODEL_NAME`. Unlike the other 9
          uniface engines, these 3 DO produce the ordinary `Detection` list, so they need
          no special endpoint - they run through the existing `/internal/v1/validate-infer`
          unchanged.
      - [x] **Real gate-2 shapes re-probed live, not trusted from this file's own summary
            above**: a fresh `onnxruntime.InferenceSession` on each artifact inside the
            real running `ai-runtime` container, each fetched from the real `csense-models`
            MinIO bucket and **sha256-verified against its own registry row before
            loading** (all 3 matched). Recorded:
            `blazeface` input `input` `(batch,3,128,128)` f32 -> `regressors`
            `(batch,896,16)` + `scores` `(batch,896,1)`;
            `centerface` input `(batch,3,height,width)` f32 *fully dynamic* -> `heatmap`
            `(b,1,h/4,w/4)` + `scale` `(b,2,...)` + `offset` `(b,2,...)` + `landmarks`
            `(b,10,...)`;
            `retinaface` input `(batch,3,height,width)` f32 *fully dynamic* -> `loc`
            `(b,N,4)` + `conf` `(b,N,2)` + `landmarks` `(b,N,10)`.
      - [x] **Two of those probes are real evidence about the anchor maths, not just shape
            bookkeeping** - the anchor count is the one thing a wrong training config
            cannot fake:
          - **RetinaFace returned N=16800 priors at a 640x640 input**, and the ported
            anchor generator produces exactly 16800 (strides 8/16/32 over 640px = 80x80 +
            40x40 + 20x20 = 8400 cells, 2 `min_sizes` each). A different stride set or
            anchor-size count lands on a different number, so this is a genuine match
            between the artifact and the config the decode assumes, not a coincidence.
            The engine also asserts this at inference time and raises
            `OutputContractUnknownError` on a mismatch rather than decoding anyway. It is
            also why RetinaFace runs at a **fixed** 640x640 despite the graph's H/W being
            dynamic: the priors are a function of the input size, so the size the anchors
            were built for and the size fed to the model must be the same.
          - **BlazeFace's head is literally `(896, 16)`**, and the ported MediaPipe SSD
            anchor config generates exactly 896 (16x16x2 at stride 8 + 8x8x6 at stride 16);
            `_blazeface_anchors` raises rather than returning a mismatched grid. `16` = 4
            box terms + 6 keypoints x 2, which is what fixes the keypoint count at **6**
            rather than the 5 every other face model here uses.
      - [x] **Every constant ported line-for-line from the real reference these weights
            ship with** - `github.com/yakhyo/uniface` (MIT), the clone at `uniface-main/`
            in this repo's working tree, per each model's own `legacy_paths` in
            `uniface_model_manifest.py`: `uniface/detection/{blazeface,centerface,
            retinaface}.py` plus `uniface/common.py`'s `generate_anchors`/`decode_boxes`/
            `decode_landmarks`/`non_max_suppression`/`resize_image`. Not re-derived from
            the papers, not inferred from the ONNX graph. The specific things that would
            each have been a silent wrong-box bug if guessed, all taken from the source and
            documented at the point of the code:
          - RetinaFace's face score is `conf[:, 1]`, **not** `conf[:, 0]` (column 0 is
            background - reading it inverts every score); its preprocessing subtracts the
            Caffe BGR mean `(104,117,123)` with **no** RGB swap and no `/255`; its
            letterbox pastes at the **top-left** of a zero canvas, not centred, which is
            why the inverse transform is a plain divide with no pad offset.
          - CenterFace's box size is `exp(scale) * 4` - log-space, so dropping the `exp`
            yields few-pixel boxes that still look like plausible detections; its landmark
            pairs are stored **(dy, dx)**, not (dx, dy), which on a roughly-square face
            would still land inside the box if swapped; and each side is rounded up to a
            multiple of 32 **independently**, so the two axis scale factors genuinely
            differ and must be applied per axis.
          - BlazeFace normalises to **[-1, 1]** RGB (not `[0,1]`, not the Caffe mean), and
            uses MediaPipe's **weighted** NMS - overlapping candidates are score-averaged
            into the winner rather than discarded, so neither this file's existing
            `cv2.dnn.NMSBoxes` path nor the ported plain NMS can be substituted. Upstream's
            `merge[0] = True` guard is kept too (a zero-area box has IoU 0 against itself,
            so relying on self-overlap loops forever).
      - [x] **A real premise correction, from reading the source rather than trusting the
            grouping**: `centerface` is **not anchor-based**, despite sitting in this
            file's "3 anchor-based detectors" tier. It is anchor-free/CenterNet-style -
            faces are peaks in a stride-4 heatmap, with scale/offset/landmark maps read at
            each peak's own cell. The live gate-2 probe agrees (four outputs named
            `heatmap`/`scale`/`offset`/`landmarks` at exactly a quarter resolution). There
            is no anchor grid to get wrong; its equivalent traps are the ones listed above.
      - [x] **Real gate 3-4 through the REAL deployed HTTP route** - a genuine step up from
            how the previous 10 had to be validated. `POST /internal/v1/validate-infer`,
            served by a container actually running the code under test, against the real
            golden images. ai-runtime has **no Traefik route by design**
            (`infra/docker-compose.yml`: raw frames in, raw detections out, no tenant
            scoping of its own - TRD §16), so there is no host port to call and the HTTP
            client runs *inside* the container:
            `scripts/_uniface_detector_validate_in_container.py`, driven by
            `scripts/run_uniface_model_validation_detectors.py`. **The shared running
            `csense-ai-runtime-1` was deliberately not patched** (a second agent was
            working on this same stack and the same file); instead a sidecar
            (`csense-ai-runtime-validate`) was created from the same image with the
            candidate `engines.py` deployed into it. `--runtime-container` selects which,
            and every recorded run's own `method_note` says which one served it - never
            silently.
      - [x] **Recall scored by cross-check against another already-validated detector, on
            the same frame** - the sanity check that two unrelated architectures agree on
            where a face is. The reference is the platform's own already-`production`
            SCRFD (`insightface-buffalo-l-detect`). Measured, all at confidence 0.5:
            | model | recall vs SCRFD | mean IoU | min IoU | FPR |
            |---|---|---|---|---|
            | `uniface-retinaface-detect` | 1.0 (2/2) | 0.862 | 0.818 | 0.0 |
            | `uniface-centerface-detect` | 1.0 (2/2) | 0.864 | 0.862 | 0.0 |
            | `uniface-blazeface-detect` | 1.0 (2/2)* | 0.755 | 0.715 | 0.0 |
            *BlazeFace's recall is graded on a short-range crop - see its own entry below.
            Stated plainly: IoU is measured against SCRFD's box, **not** a hand-drawn
            ground truth, and 0.5 is deliberately a loose "same face, same place" bar
            because these detectors genuinely use slightly different box conventions around
            the chin/hairline. It is evidence that both decoded the frame correctly, not a
            claim that SCRFD's box is truth.
      - [x] **False-positive rate is a real measurement, not an untested zero**: 3 real
            Autotek NVR frames with no human face in them (interior storage room, garage
            with parked vehicles, night IR blank) are committed as negative controls in
            each golden set. **All 3 models returned zero boxes on all 3 frames.**
      - [x] **Landmark decode confirmed structurally against SCRFD's own points**, which is
            what actually catches a swapped coordinate order:
          - `retinaface` and `centerface` 5-point outputs land within **0.0003-0.003
            normalised** (~1-3 px) of SCRFD's own 5 points on both real faces, in the same
            order. CenterFace's `(dy, dx)` storage order is therefore confirmed correct -
            a swap would have mirrored every point about the box diagonal.
          - `blazeface`'s 6 points were checked against SCRFD **re-run on the same crop**
            (the harness was fixed mid-work to record crop-space reference points rather
            than full-frame ones, which had made the stored comparison look like a gross
            error when it was only a change of coordinate frame): point 3 (mouth centre)
            = `(0.5245, 0.5880)` vs the midpoint of SCRFD's two mouth corners
            `(0.5214, 0.5884)` - 0.003 apart in x, 0.0004 in y; points 0/1 bracket SCRFD's
            two eyes; points 4/5 sit laterally **outside** that eye span at eye height,
            i.e. ears. A wrong stride or offset in the 16-column regressor would not
            reproduce that arrangement.
      - [x] **BlazeFace returns ZERO detections on the full street scene - established as
            a real capability limit by measurement, not excused as one.** On the full frame
            each face is ~5% of the frame width (~6px once letterboxed to 128px). Rather
            than assume "short-range model, must be fine", a detection-range sweep was run
            and is recorded in the model's own validation run: BlazeFace holds the same
            real face down to **8.7% of frame width** (confidence 0.59, still correctly
            located) and loses it by 6.0%, while CenterFace and RetinaFace still find it at
            5.1%. So the decode demonstrably works and the zero is the documented ~2m
            short-range design boundary. Its golden manifest grades recall on a fixed 3.5x
            crop for that reason, applied identically to both faces (not tuned per face to
            make something pass), and records the full-frame zero as a finding rather than
            scoring it. **Practical consequence worth stating: BlazeFace would find nothing
            in a typical wide-angle CCTV frame. It is validated as a correct decode, NOT
            recommended as a site detector.**
      - [x] **BlazeFace's 6 keypoints are structurally incompatible with every alignment
            path in this platform, and fail loudly rather than silently** - it emits
            MediaPipe's 6 points (right eye, left eye, nose tip, mouth centre, right ear,
            left ear), not the 5-point ArcFace template, and upstream marks the same
            distinction as `supports_alignment = False`. Checked against the real guards
            rather than assumed: both `InsightFaceEngine.embed()` and
            `UnifaceEmbeddingEngine.embed()` already require exactly 5 keypoints, so a
            BlazeFace `Detection` raises `OutputContractUnknownError` there instead of
            producing a mis-aligned crop. A new unit test pins that specific 6-point case.
      - [x] **Real `model_validation_runs` rows recorded via the real Admin API** for all 3
            (`run_uniface_model_validation_detectors.py`, same service account / Admin API
            flow as the earlier suites), reports uploaded to MinIO, all 3 `status=passed`.
            All 3 promoted `uploaded -> validating` for real. **All 3 blocked at
            `validating -> validated` by the pre-existing biometric-acknowledgement gate
            (real `422 biometric_promotion_requires_acknowledgement`, confirmed live)** -
            all 3 are `access_classification=biometric` and `validated` is a
            `DEPLOYABLE_STATE`. Correctly not overridden: `acknowledge_biometric` is the
            owner's call. Net state: 3 at `validating`, none at `staging`/`production`.
      - [x] **A real stale test was caught rather than quietly edited around**:
            `test_build_engine_falls_through_to_generic_onnx_for_unknown_model_name` used
            `uniface-retinaface-detect` as its stand-in for "a model_name no engine claims".
            Registering RetinaFace made that false and the test failed - correctly, since a
            registered model reaching the generic `OnnxEngine` is precisely the silent-
            wrong-decode bug that suite exists to catch. The assertion was right and only
            its example had gone stale; it now uses a name that will never be registered.
      - [x] **17 new unit tests** (`backend/tests/test_uniface_engines.py`, 29 in that file
            now) pin the anchor counts against the live artifacts' own real head dimensions,
            the RetinaFace box/landmark decode algebra (zero offset must recover the prior
            exactly - which pins both variances and the log-space size term), the weighted-
            vs-plain NMS distinction, the deliberate clip-boxes-but-not-keypoints
            asymmetry, the dispatch table, and CenterFace's per-axis 32-alignment. One of
            those tests failed first with a wrong premise of its own - it assumed the real
            704x576 substream runs at native size, when 704 exceeds the 640 cap so it is
            scaled to 640x544; the engine was right, the test was wrong, and the corrected
            test now pins that real production behaviour instead of deleting the case.
            Full targeted suite green: **66 passed** (`test_uniface_engines.py`,
            `test_ai_runtime_decoder.py`, `test_insightface_engine.py`,
            `test_ai_runtime_loader.py`), `ruff check` clean on every touched file.
      - [~] **Named gaps, not smoothed over.** (1) **Landmark ACCURACY is unverified for
            all 3.** The points are structurally correct and positionally very close to
            SCRFD's, but no hand-labelled 5-point ground truth exists anywhere in this
            repo, so per-point error is not scored in any manifest - "agrees with another
            detector" is not the same claim as "is accurate". (2) **The entire positive set
            is 2 upright adult faces in 1 photo** - still the only committed image in this
            repo with usable human faces. No crowded, small-face, rotated, or occluded-face
            recall is measured. (3) **No night/IR face has ever been tested** against any of
            the 3 - the one night frame here is a negative control containing no face, so
            it tests false positives only; CLAUDE.md's standing warning about night
            thresholds being unvalidated applies to these 3 unchanged. (4) The sidecar used
            to serve the HTTP validation is not the deployed compose service - the code is
            real and the route is real, but `csense-ai-runtime-1` still needs a rebuild
            after this branch merges before these 3 are reachable in the normal stack.
    - [x] **Both worktrees merged into `phase-1-foundation`, by hand, and `ai-runtime`
          rebuilt/restarted for real (2026-09-11)** - the two agents independently
          inserted their new engine classes at the same point in `engines.py` and
          extended `build_engine()`'s dispatch, so this was resolved manually rather than
          trusting an automatic 3-way merge: both class blocks kept in full, dispatch
          combined into one table, the two agents' same-named validation scripts renamed
          (`run_uniface_model_validation_lowrisk.py` / `_crosscheck.py`), CHECKLIST.md's
          same bullet combined rather than one write-up silently winning. Re-verified
          independently after merging, not just after each agent's own report: full
          targeted test suite green (46 tests), `ruff` clean, and the load-bearing factual
          claims (the ArcFace alignment template match, MiniFASNet's real/spoof index)
          re-confirmed directly against the real installed packages a second time. Once
          rebuilt, confirmed live through the real running API - not just re-imported
          locally: `uniface-modnet-matting`, `uniface-adaface-recognition`, and
          `uniface-facemesh-landmark` all return real, correct-shaped results from
          `POST /internal/v1/validate-infer-uniface` against a real image (matte
          coverage_fraction 0.063, 3 real embeddings/landmark sets with real detector
          confidences).
      - [x] **Closed, 2026-09-11**: `/internal/v1/validate-infer-uniface` now handles all
            10 models (extended `_UNIFACE_VALIDATION_MODELS` + 4 new response shapes -
            `FaceAttributesOut`/`LivenessOut`/`GazeOut`/`Landmark98Out` - and 4 new
            branches in `_run_uniface_inference`, same "one dedicated shape per real
            output type" pattern the first 6 already established, not forced into
            `Detection`). Rebuilt `ai-runtime` and confirmed all 4 live against a real
            image, not just re-imported: `uniface-fairface-attributes` (`race="Middle
            Eastern"`), `uniface-minifasnet-antispoofing` (`is_real=false,
            confidence=0.9937` - matches the 99.3-99.4% the offline validation run
            measured, same real finding, not a different number from a different path),
            `uniface-mobilegaze-estimation` (`yaw_deg=3.91` - matches the offline run's
            `3.9deg`), `uniface-pipnet-landmark` (98 real points returned). Full targeted
            test suite (46 tests) and `ruff` still clean after this change.


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
- [x] **Edge auth is no longer only the interim path.** Phase 3 shipped a real per-device
      credential (enrolment token → long-lived, individually-revocable, hashed agent
      credential - not literal mTLS certificates, but the same property that matters:
      one device, one identity, independently revocable, distinct from the enrolment
      secret that created it) for `/heartbeat` and the signed-command endpoints.
      Detection ingestion (`POST /ingest/detections`) now accepts *either* that real
      device credential *or* the original scoped-token path - not a replacement, a second
      real path with its own reason to exist (see [ingest.py](backend/tenant_api/app/api/ingest.py)'s
      own docstring): the scoped-token path stays available for anything that
      authenticates as a tenant member rather than an enrolled device, and its
      `edge_device_id` remains self-reported since it has no device credential to check
      against. When a real device credential authenticates, `edge_device_id` is taken
      from the credential itself - a device can no longer claim to be reporting on
      behalf of a different device it isn't, closing that half of the original gap for
      real.
  - [x] Verified for real: `scripts/e2e_ingest_dual_auth.py` - the legacy scoped-token
        path still accepts a detection and still trusts the body's `edge_device_id`
        exactly as before (no regression); a real enrolled device's own credential posts
        a detection too, and the stored row's `edge_device_id` is the credential's own
        device even when the request body deliberately claims a different one; no
        credential at all gets one uniform `401`. Full PASS. No regression on the
        pre-existing `e2e_edge_enrolment.py`; full backend suite (369 tests) still green -
        nothing downstream of ingestion (rules/incidents/evidence/notifications) was
        touched by this change.
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

- [x] Edge encrypted offline spool, reconnect cursor, batch resync, dedup
  - [x] **First, the edge agent itself had to exist** - `backend/edge/agent/` had never been
        built (previous sessions built only the server-side enrolment/heartbeat/command
        contract, `deps_agent.py` and migration 0022/0026/0042). `backend/edge_agent/`
        (new service, distinct from that empty legacy path) is a from-scratch process
        implementing `docs/03_APPLICATION_FLOWS.md` §15 FLOW-13 - offline detection, and
        resync on reconnect.
  - [x] **Runtime and dependency footprint chosen against the real target**: Python, shipped
        as a container, built and proven for `linux/arm64` (Pi/Jetson-class hardware -
        user's explicit direction). The agent does **not** install `csense_shared` - that
        package's sqlalchemy/asyncpg/redis/minio/argon2-cffi tree needs a C toolchain on
        ARM, and a process whose entire interaction with the platform is HTTP has no use
        for any of it. The agent's whole dependency set is `httpx` + `cryptography` +
        stdlib (`sqlite3` for the spool, `http.server` for the local detection listener) -
        both third-party packages publish prebuilt `aarch64` wheels, confirmed by a real
        `docker buildx build --platform linux/arm64` (succeeded, ~6s, no QEMU needed on
        Docker Desktop's buildx node), not merely asserted.
  - [x] **The spool has its own device-local key, not the platform KEK.** Shipping
        `master_v1.key` - the key that decrypts every tenant's camera credentials, TOTP
        secrets and webhook secrets - to a box sitting in a customer's building would be a
        real security regression, not a convenience worth taking. `edge_agent/app/crypto.py`
        generates a 32-byte key on the device at first run (`0600`, refuses a
        group/world-**writable** key file, mirrors and cites `csense_shared.security.
        envelope`'s own permission reasoning without reusing its code, since that module's
        AAD binds database-row concepts a local spool row doesn't have). AES-256-GCM, fresh
        12-byte nonce per row, AAD binds the row's own id so a captured ciphertext can never
        be relocated to another row.
  - [x] `edge_agent/app/spool.py`: SQLite via stdlib (no dependency), one table, payload
        column holds only the sealed blob. **`synchronous=FULL`, not the usual WAL-mode
        `NORMAL` advice** - `NORMAL` doesn't fsync on commit, only at a checkpoint, so a
        power cut (the exact scenario a Pi-class box in the field faces) leaves the file
        consistent but silently short by the last commits, which are precisely the events
        no other copy exists of. Oldest-first drain by `(captured_at, id)` (FLOW-13's
        "original event identity/timestamps are preserved"). **Nothing is ever deleted
        before the server acknowledges it** - the property that makes at-least-once
        delivery plus the server's existing `(tenant_id, source_event_id)` uniqueness
        (migration 0012) into effectively-once. Bounded size, oldest-evicted on overflow
        with a persistent dropped counter (named tradeoff: the earliest events of a long
        outage are the ones lost, but the counter reaching the server via heartbeat is what
        stops that being silent). Verified genuinely encrypted, not merely claimed: a test
        writes a known string and asserts it is absent from *every* file SQLite touches,
        including the `-wal` sidecar - checked without closing first, since closing
        checkpoints the WAL and would hide exactly this class of bug.
  - [x] **`POST /api/v1/tenant/ingest/detections/batch`** (new) - a device draining a
        day-long spool one HTTP round trip at a time is what FLOW-13's "uploads batches"
        step exists to avoid. Capped at 100 items; runs every item through the *same*
        `ingest_detection` pipeline the single endpoint uses (dedup, rules, incidents,
        evidence, notifications all behave identically, verified against a real DB); one
        item's transaction failure never rolls back or blocks its neighbours (own
        tenant-scoped transaction per item, sequential - reasoned in the endpoint's own
        docstring: a request-wide transaction would be poisoned by any single item's DB
        error anyway, so it couldn't continue regardless of rollback semantics). **A
        malformed item gets its own error entry rather than 422ing the whole request** -
        found and fixed after the first version typed the body as `list[DetectionIn]`,
        which let pydantic reject the entire batch before the handler ever ran; one
        malformed row in a spool would have poisoned every batch it was drained in. Each
        rejection carries a `retryable` flag the item's own code sets (matching
        `ProblemResponse.retryable`'s semantics) rather than a client-side guess, so an
        already-deployed agent fleet classifies a server error code it has never seen as
        retryable-by-default instead of silently discarding real evidence.
  - [x] Heartbeat (`HeartbeatIn`) now carries `spool_depth`/`spool_dropped`, stored in the
        existing `health` JSONB rather than a migration (per-device gauges with nothing in
        common across hardware classes are exactly what that column is for, migration
        0026's own reasoning). **A device is marked `degraded` on new drops only - the
        delta against the previous heartbeat's counter, never the raw cumulative value** -
        a device that evicted one event a fortnight ago cannot latch `degraded` for the
        rest of its service life; a device that stops dropping recovers on its very next
        heartbeat with no flag to clear. Found and fixed along the way: `DeviceOut` exposed
        neither `health` nor `health_status` at all, so the telemetry
        `docs/04_UI_UX_DESIGN_BRIEF.md` already documents as shown to an operator was
        write-only until this pass. Also found and fixed: injecting the `spool` block
        before the payload size check meant the server's own ~62 added bytes could push a
        device sitting legitimately at its budget into a 422 - a healthy box would have
        started looking offline the day this field shipped. The regression test that
        proves the fix caught a real self-inflicted near-miss: its first version left 200
        bytes of slack and passed just as happily with the bug present: re-sized to land
        exactly on the boundary, confirmed by reintroducing the bug and watching it fail.
  - [x] `edge_agent/app/sync.py`: online delivers immediately without spooling; a duplicate
        the server reports is acked and removed like any other success, not retried (a
        duplicate is a successful outcome); a permanently-rejected row is discarded and
        counted as a drop rather than retried for 24h; a fresh event always goes out
        immediately regardless of backlog size (the live path never queues behind a large
        drain), documented tradeoff being that an older backlogged event can correlate into
        its own incident later, squarely inside FLOW-13's own conflict policy. **A
        whole-batch 4xx (something even per-item validation can't catch - a malformed body,
        a proxy-level rejection) is isolated by bisection, not by discarding the batch** -
        and bisection's own blind spot was found and closed: nothing originally
        distinguished "this one row is poison" from "every request gets rejected for a
        reason unrelated to content" (a client-side bug, a hostile proxy), so a systemic
        cause could have bisected all the way down and discarded an entire backlog as
        individually "unrecoverable" - exactly the silent evidence loss the whole spool
        exists to prevent, via a different door. Bounded to 3 leaf discards per bisection
        episode; past that the engine backs off instead of trusting every remaining leaf is
        real poison. A real regression test (a fake sender that rejects *everything*
        regardless of content) proves the backlog survives rather than being drained to
        zero by discard.
  - [x] `edge_agent/app/main.py`: enrolment, heartbeat, command and sync loops run
        concurrently, modelled directly on `notification_worker/app/main.py`'s established
        shape - including its loop-failure-isolation pattern (a dying sync/command loop is
        logged and swallowed, never silently taking heartbeat down with it, so the box
        never "goes dark" over an event-pipeline bug). That isolation guarantee shipped
        without a test at first, exactly the property this task was told to carry over from
        an already-tested sibling; closed with the same three-case shape
        `test_worker_loop_isolation.py` already established (a dying guarded loop is
        swallowed while its sibling keeps running, cancellation still propagates so
        shutdown works, a healthy loop returns cleanly).
  - [x] **Scope boundary, stated where a future reader will find it**: the agent does not do
        inference in this pass. `source.py` is a narrow, explicit interface (one real
        implementation: a local, loopback-by-default HTTP listener a co-located inference
        process hands events to) rather than a fake detector implying the agent sees.
        Building real ARM inference is separate, larger work that belongs with the Phase 4
        AI runtime, not something to half-build as a side effect of the spool.
  - [x] Container: `backend/edge_agent/Dockerfile`, non-root, self-contained build context
        (no `COPY shared`, matching the no-`csense_shared` decision above). Wired into
        `infra/docker-compose.yml` (dev only) with its **own explicit, minimal environment**
        rather than reusing the platform's shared env/secrets anchors - confirmed via
        `docker compose config` that no Postgres/Redis/MinIO/JWT/master-KEK value reaches
        it. Deliberately **not** added to `docker-compose.prod.yml`: that file deploys this
        platform onto infrastructure we own; the edge agent's entire purpose is running on
        hardware we don't. State (spool + device key) lives on a named volume so it
        survives a container restart - confirmed live: a second start loads the existing
        device key rather than generating a new one.
  - [x] Verified for real, against the live stack, with the agent running as a real
        container (not imported as a Python module): `scripts/e2e_edge_spool.py` - a real
        enrolment, a live event delivered directly while online, then the container
        genuinely cut off the network (`docker network disconnect`, not a mocked flag) with
        events spooling locally and provably nothing reaching the server, then reconnected
        with **no manual drain trigger** - the backlog empties on its own, every event's
        `captured_at` survives the outage untouched (not the delivery time, confirmed
        against FLOW-13's own conflict policy), and exactly one detection row exists per
        `source_event_id` despite the drain's own retry machinery - the one assertion no
        unit test could make, since it depends on the batch endpoint's idempotency and the
        agent's ack-only-after-confirm spool actually composing correctly together. Full
        PASS, run twice for consistency, real cleanup confirmed leaving no residue.
  - [ ] **Deliberately deferred, stated plainly**: the command channel's poll-marks-delivered
        design is at-most-once, not at-least-once - a command handed to an agent that
        crashes before acting is never re-offered. Acceptable today because the agent only
        executes `ping` (inert, no side effect) and acks everything else as
        `unsupported_command`; stops being acceptable the moment a real command handler
        (config-apply, reboot) is added, since `device_commands.command_type` is
        unvalidated free text at the DB layer and nothing stops one being issued today. No
        Customer CRM UI shows spool depth or the new `degraded` health verdict yet, even
        though both now arrive over the API. No `scripts/e2e_ingest_batch.py` dedicated to
        the batch endpoint's own contract in isolation - its idempotency and per-item
        validation are covered by `test_ingest_batch.py` and exercised for real inside
        `e2e_edge_spool.py`, but not by a standalone e2e script the way most other
        endpoints in this file have one.
- [x] WireGuard/relay integration design + time-limited diagnostic access
  - [x] **"Integration design" was already substantively done** by earlier work this
        phase (`CLAUDE.md`'s documented WireGuard peer-isolation/fleet-address-allocation/
        cross-tenant-overlap fixes). Confirmed by grep that `cloud_relay`/`port_forward`
        are connectivity modes a device self-*reports*, never backing infrastructure we
        operate - correct, since a relay is the customer's own setup, not ours to run.
        So the only real gap was diagnostic access itself.
  - [x] **Scoped deliberately, by owner direction (2026-09-02): read-only - logs + health
        snapshot. No shell, no exec, nothing that mutates device state.** Closed without
        building any new relay/session-broker service at all - it rides entirely on
        infrastructure already shipped this phase: the heartbeat channel (agent ->
        `tenant_api`) and support-grant elevation (an active `support_grants` row turning
        a platform-audience token into a real, scope-limited `TenantContext`). This is the
        second real consumer of that elevation mechanism, proving it generalizes rather
        than being special-cased to the incident-access work it first shipped for.
  - [x] New `diagnostic.read` permission (migration 0053, customer-audience, granted to
        `tenant_owner`/`tenant_member`) - deliberately **not** added to
        `DANGEROUS_SUPPORT_SCOPES` (the denylist that keeps a support grant from minting
        persistent access): read-only telemetry is exactly the class that denylist exists
        to distinguish from things like `membership.manage`. The same permission and the
        same `GET /devices/{id}/diagnostics` route serve both an ordinary tenant admin
        self-diagnosing their own device and a platform developer under an elevated grant
        - the route doesn't know or care which, by design.
  - [x] The edge agent now reports a real, bounded (~4 KiB), redacted log tail on every
        heartbeat (`backend/edge_agent/app/logbuf.py`) - a `logging.Handler` subclass, not
        a log-shipping subsystem. **Found and fixed during review, not shipped as written**:
        the first redaction regex (any 20+ character token-shaped run) was also silently
        eating this package's own real event names 20+ characters long
        (`spool_row_permanently_undeliverable` and 21 others) - would have destroyed the
        one thing every log entry exists to carry. Fixed with a shape-based heuristic
        (digit, hyphen, or mixed case - properties every real secret in this codebase has
        and a `snake_case` event name never does). Also found: the tail's first version
        only captured `record.getMessage()`, and this package's own logging convention
        puts nearly all real diagnostic detail (`str(exc)`, error codes) in `extra=`,
        which the handler didn't read - shipped, it would have surfaced bare event names
        with none of the detail an operator needs. Fixed with a small, explicit allowlist
        of `extra` keys this package's own call sites actually use, through the same
        redaction path.
  - [x] Verified for real, against the live stack, with a real elevated session:
        `scripts/e2e_diagnostic_access.py` - a real device heartbeats real content-bearing
        logs; the tenant owner and a support-grant-elevated platform developer both read
        *identical* diagnostics through the *same* route; **the single assertion that
        proves read-only is real, not just intended**: the still-active elevated session
        is refused (403) both an unrelated device command and a device PATCH, since the
        grant's `requested_scopes` never included `edge.manage`; the diagnostics response
        never leaks a command's `payload`/`signed_envelope` even under elevation (a
        review-caught over-exposure, fixed before this shipped - `diagnostic.read` alone
        must not incidentally see more than diagnostics); and revoking the grant stops
        elevation immediately. Full PASS.
  - [ ] **Deliberately deferred, stated plainly**: full remote shell/exec was explicitly
        declined as its own dedicated project (real security stakes - session recording,
        command auditing, a much larger attack surface if the support-grant layer above it
        is ever bypassed), not a corner cut. No Customer CRM UI surfaces diagnostics or
        the config-push mechanism yet, even though both are fully usable via the API.
- [x] Support grant request/approve/active-banner/expiry/revoke + authorization + audit
  - [x] `support_grants` (SCH §5.10, previously spec'd but never built - migration 0043).
        `support_grant_id` had already existed on `TenantContext`/`PlatformContext` and
        `record_audit_and_outbox` since early in this build, with nothing to populate it
        until now - this is the table those fields were always meant to point at.
  - [x] Migration 0044: `support.request`/`support.approve` (platform-only, `critical`
        risk for approve); `support.revoke`/`support.read` granted to **both** audiences
        - the same safe sharing `audit.read` (migration 0036) already established, since
        a permission code is just a string each audience's own `require_permission()`
        checks against its own already-resolved context.
  - [x] `backend/admin_api/app/api/support.py` (new): request/list/approve/deny/revoke.
        **Self-approval is refused in code, deliberately** - a platform developer cannot
        approve their own request; a real peer must. `backend/tenant_api/app/api/
        support.py` (new): a tenant's own read (`?active_only=true` is what the "active
        support session" banner polls) and its **own real right to revoke a session
        early**, independent of the platform side's own revoke.
  - [x] **Expiry is lazy, not a scheduled job**: every read first runs a cheap
        `UPDATE ... WHERE status='active' AND expires_at < now()` before returning
        results - `status` in the database is never stale by more than the time until
        the next request, without needing a cron job or worker this pass doesn't have
        anywhere to run.
  - [x] Customer CRM: a real, persistent top banner (`SupportGrantBanner.tsx`, mirroring
        the existing offline banner's own shape and reasoning - a standing condition, not
        a dismissible toast) shown on every authenticated page whenever a grant is active
        against the tenant, naming who and which ticket, with an "End session now"
        button wired to the tenant's own revoke. Polled every 60s - no realtime channel
        exists for a platform-side event like this yet.
  - [x] Verified for real: `scripts/e2e_support_grants.py` - request, a real peer's
        approval, self-approval refused (403), the tenant's own banner data appearing
        the moment a grant goes active, a *different* tenant seeing nothing (RLS
        isolation), the tenant's own revoke clearing the banner immediately with the
        platform side agreeing, denial as a real alternative to approval, and lazy expiry
        (an active grant past `expires_at` flips to `expired` in the database on the very
        next read, not merely hidden). Full PASS, first run. `npm run typecheck`/`lint`/
        `build` clean; the running container confirmed serving `200` afterward.
  - [x] **Authorization**: an active grant now genuinely elevates a platform developer's
        request into real tenant data access - the deferred half above, closed in a
        dedicated pass rather than as a side effect of the governance workflow. Migration
        0051 adds `support_grant_lookup()`, a narrow `SECURITY DEFINER` function (mirrors
        `csense_active_membership_for_user()` migration 0005 and `edge_vpn_pool_snapshot()`
        migration 0029) callable from a `bootstrap_session()` before any tenant RLS context
        exists - it returns an active, unexpired grant's id plus its `requested_scopes`
        intersected against real `customer`-audience-grantable permission codes (honoring
        `role_permissions.effect = 'allow'`, not a raw echo of whatever the developer typed
        at request time). `csense_shared.security.support_elevation.elevate_from_grant`
        wraps that call for direct testability (7 real-DB tests). `current_tenant_context`
        (`backend/tenant_api/app/deps.py`) - the single choke point all 22 of tenant_api's
        tenant-scoped routes already depend on - now also accepts a `csense-platform`
        audience token paired with `X-CSense-Support-Tenant-Id` and a real active grant,
        building a `TenantContext` whose `permissions` come **only** from the grant's own
        scopes (never the developer's ambient platform permissions) and whose
        `membership_id` is `None` (widened to `UUID | None` - no real memberships row
        exists for a platform developer acting against a tenant they don't belong to).
        Every existing tenant-scoped route picks this up with **zero changes of its own** -
        `require_permission()` is a plain set-membership check against whatever
        `context.permissions` holds. `support_grant_id` is now threaded through all 17
        `record_audit_and_outbox` call sites in `tenant_api/app/api/` (`None` for every
        ordinary customer action, unchanged; populated only under an elevated context) -
        the represented-actor audit trail this feature's own governance half always
        anticipated (`audit_events.support_grant_id`, migration 0001) is now real.
  - [x] Verified for real: `scripts/e2e_support_grant_authorization.py` - a real elevated
        read actually returns the seeded tenant's real incident data (not just a 200), the
        same call refused (401) with no header, before approval, and for a different
        tenant's id; revoking a grant refuses the identical elevated call immediately
        (not eventually); and a real elevated write's own audit row is confirmed (via a
        direct DB read, since neither the tenant nor platform audit-read endpoints expose
        `support_grant_id` in their response model yet - named below) to carry the
        approving grant's own id. Full PASS against the live stack. `ruff check backend
        scripts` clean.
  - [x] **Dangerous scopes refused at request time**: a final holistic review of this
        whole feature (not any single task's own review) surfaced a real gap -
        `requested_scopes` was never checked against permission codes that let an
        elevated, *temporary, revocable* session mint **persistent** access outliving the
        grant itself. `membership.manage` (can promote to `tenant_owner`),
        `api_client.manage` (a new long-lived API credential), `webhook.manage` (a new
        outbound data-delivery destination), `reseller.manage_children` (a whole new
        tenant), and `support.revoke` (ending a *different* developer's own active grant
        on the same tenant) are now refused with `422` by
        `reject_dangerous_scopes()`/`DANGEROUS_SUPPORT_SCOPES` in
        `backend/admin_api/app/api/support.py`, called from `request_support_grant`
        immediately after `require_permission(context, "support.request")` - **before**
        the grant row is even created, so a dangerous request never reaches a peer for
        approval at all (not merely "the elevated session couldn't do anything with it"
        later, at elevation time). Verified for real: `scripts/
        e2e_support_grant_authorization.py` requests `["membership.manage"]` and asserts
        the request itself is refused with `422`. Full PASS against the live stack
        (`admin_api` rebuilt); `backend/tests/test_support_grants_dangerous_scopes.py`
        (7 tests, pure function, no DB) plus the full backend suite and `ruff check
        admin_api shared tests` both clean.
  - [ ] **Deliberately deferred, stated plainly**: `support_grant_id` exists on
        `audit_events` and is now populated for real, but neither `AuditEventOut`
        (tenant_api) nor its admin-side equivalent surfaces that column in their response
        models yet - a tenant (or platform operator) reading their own audit trail via the
        API cannot currently see *which* support grant touched a given row, only that some
        action happened; the e2e script proves the column is correct by reading the
        database directly rather than through the API for this reason. A small, contained
        follow-up (add the field to both response models) - named rather than silently
        left unconsidered, per this file's own discipline. No Developer Console UI for any
        of the support-grant lifecycle yet either, authorization included - the same
        deferral the governance half above already named.
- [x] Scoped API keys, rate limits, usage metering, developer API docs
  - [x] `api_clients`/`api_keys` (SCH §10.5, previously spec'd but never built - migration
        0047). Tenant-owned only this pass (SCH's `tenant_id null` implies a future
        platform-level client, not needed by anything today - widening it later is a
        small additive migration). `rate_policy_id` is shipped as a direct
        `rate_limit_per_minute` column instead of a not-yet-existing shared policy table
        - the same simplification `webhook_endpoints` already made for its own
        `rate_policy` field (migration 0045).
  - [x] `api_key_lookup` (migration 0047) mirrors `edge_agent_lookup` (0026) exactly - a
        `SECURITY DEFINER` prefix-lookup function, because resolving a credential is what
        discovers the tenant and RLS cannot scope that. `deps_api_client.py` (new) is the
        same shape as `deps_agent.py`: constant-time digest comparison, "every failure
        looks the same" 401, its own `ApiClientContext` type (not folded into
        `TenantContext`/`PlatformContext` - `require_permission()` is deliberately typed
        to only those two).
  - [x] **Scopes are capped at issuance**: a key's `scopes` must be a subset of the
        issuing user's own `context.permissions` at creation time (`api_clients.py`,
        `create_api_client`) - a key can never be issued more power than the person
        issuing it currently holds. What that person holds later is not re-checked (the
        same relationship any OAuth token already has to its grantor), named rather than
        silently assumed.
  - [x] Real Redis-backed fixed-window rate limiting + usage metering
        (`csense_shared.security.rate_limit`, new) - `enforce_rate_limit` is a FastAPI
        dependency every API-key route opts into explicitly, returning `429` with
        `retry_after_seconds` in `details` once a client's own configured
        `rate_limit_per_minute` is exceeded. Fixed-window imprecision (up to ~2x burst at
        a minute boundary) is a named, accepted tradeoff over a sliding-window/token-
        bucket limiter - real cost this pass doesn't need to pay, since the limit is a
        per-client operator-set ceiling, not a platform-wide guarantee.
  - [x] `GET /api/v1/tenant/integrations/whoami` (new, isolated demonstrator route) proves
        the full credential -> rate-limit -> usage-metering path end to end without
        touching any existing tested endpoint - deliberate, not a stub: it is exactly the
        shape a real integration route would take once one exists.
  - [x] "Developer API docs" was already satisfied for free - FastAPI's own
        `/api/v1/tenant/docs` and `/api/v1/tenant/openapi.json` are live with zero
        additional code (confirmed via the e2e script).
  - [x] `backend/tests/test_api_keys.py` (4 tests), `backend/tests/test_rate_limit.py` (7
        tests, real Redis). `scripts/e2e_api_clients.py`: scope-exceeds-issuer refused,
        real key issuance/list/whoami, tampered and unknown keys refused identically,
        the real configured rate limit (3/minute) actually hit over real HTTP (3
        succeed, 2 refused with 429), usage metering counts every call including the
        refused ones, immediate revocation, gap-free second-key rotation, both docs
        endpoints live. Full PASS. `ruff check backend scripts` clean; full backend
        pytest suite green (379 passed, 30 skipped).
- [x] Webhook signing, verification, replay protection, automatic outbox-driven delivery
  - [x] `webhook_endpoints`/`webhook_deliveries` (SCH §10.6/§10.7, previously spec'd but
        never built - migration 0045). Both the URL and the signing secret are
        envelope-encrypted through the same `encrypted_secrets` path every other
        sensitive value in this codebase already uses (camera credentials, TOTP secrets)
        - SCH's own `url_encrypted` field name already signals the URL itself is
        sensitive (a webhook URL can embed a path-based token some receivers use as
        their own auth, the way Slack's own incoming-webhook URLs work).
  - [x] `csense_shared.security.webhooks` (new) - the same timestamp+HMAC-SHA256 shape
        Stripe/GitHub-style webhook signing already uses, not reinvented. **Replay
        protection is the timestamp tolerance window itself** (default 5 minutes, both
        directions) - a captured, replayed request becomes unverifiable the moment it
        ages past the window.
  - [x] `backend/tenant_api/app/api/webhooks.py` (new, `webhook.manage`, `tenant_owner`-
        only): create/list/update/rotate-secret/delete, plus a real `POST /{id}/test`
        that signs and sends an actual HTTP POST through the same SSRF guard camera
        probing already uses - checked fresh both at creation *and* at every delivery
        attempt (a public DNS record can be repointed at a private address after an
        endpoint was created). Both secrets are write-only, the same discipline
        `cameras.py`'s own credential handling already established.
  - [x] Verified for real: `backend/tests/test_webhooks.py` (8 tests) - a real round
        trip, a tampered body, the wrong secret, an expired signature, a
        signature-from-the-future, and a malformed header all handled correctly.
        `scripts/e2e_webhooks.py` against the real running API and a real public
        destination (`httpbin.org` - the same "prove it against something real" standard
        already applied to the real NVR and real Resend email delivery this session): a
        real signed HTTP POST actually reaches it and gets a real `200` back with a real
        round-trip time; creating an endpoint pointed at a private address is refused
        (422) rather than silently accepted; the list view never shows the URL or secret;
        rotating issues a genuinely different secret; deleting an endpoint leaves no
        orphaned encrypted secrets behind. Full PASS. Backend: 377 passed, 21 skipped.
  - [x] **The deferred half is now built**: `csense_shared/webhooks/dispatcher.py` (new)
        drives real automatic delivery off the outbox in two passes.
        `fan_out_due_outbox_events` turns each not-yet-seen `outbox_events` row into one
        `webhook_deliveries` row per matching active endpoint on that row's tenant.
        Progress is tracked in `processed_events` (a multi-consumer idempotency table
        that has existed since migration 0001 and nothing had used) rather than
        `outbox_events.published_at`: `realtime.py`'s per-tenant WebSocket consumer
        already owns that column for its own purpose, and two consumers racing to stamp
        one column would silently break whichever lost - the kind of failure that shows
        up as missing alerts, not as an error.
  - [x] `claim_and_send_one_delivery` claims one due row (`FOR UPDATE SKIP LOCKED`,
        mirroring `notifications.dispatcher.claim_due_deliveries`), decrypts URL and
        signing secret, re-runs the SSRF guard fresh, signs, POSTs, records the result.
        **Claim and send happen in one transaction** - deliberately simpler than
        `notification_worker`'s own claim-then-send split, which exists so several worker
        *replicas* never block on a slow provider call while holding a shared lock. This
        is a single loop at far lower volume, and a crash mid-POST simply leaves the row
        `pending` for the next pass, so no stalled-row sweep is needed at all. Retries
        back off `[1, 5, 30, 120, 720]` minutes and the delivery is abandoned after
        `MAX_DELIVERY_ATTEMPTS` (6 attempts spanning 876 minutes = ~14.6h) - long enough to
        ride out a receiver's deploy, short enough that a permanently broken endpoint stops
        inside a day.
  - [x] **A 24h age window** (`webhook_dispatch_max_event_age_seconds`) bounds fan-out, so
        a first deploy never blasts a database's entire outbox history at a newly created
        endpoint and a long worker outage never floods a receiver with stale events. This
        is a deliberate departure from the platform's usual "late alert beats no alert"
        stance (`MAX_DELAY_SECONDS`, the escalation ladder): a webhook is an integration
        feed consumed by software, not a human alert, and a day-old event delivered now
        is worse than not delivered - named here because the departure is real.
  - [x] Runs as a **second loop inside the existing `notification-worker` container**, not
        a new service (`notification_worker/app/webhook_dispatch.py`, wired in `main.py`)
        - it needs the exact platform DB role that container already has, for the exact
        "dispatch legitimately spans every tenant" reason its own worker already states,
        and `CLAUDE.md`'s resource math for the 16-core box treats "reuse infra, don't
        proliferate containers" as the default. The two loops are isolated so a crashing
        webhook loop can never take life-safety alert dispatch down with it; three tests
        pin exactly that (`backend/tests/test_worker_loop_isolation.py`).
  - [x] Migration 0052 indexes `outbox_events (occurred_at, id)` - for **two** consumers,
        not one: the new fan-out scan and `realtime.py`'s pre-existing per-open-browser-tab
        incident tail, which has been polling an unindexed table since it shipped.
        `ix_outbox_unpublished` serves neither. At today's row count the planner still
        correctly prefers a seq scan (76 rows, one page); with `enable_seqscan = off` both
        queries match the index as an `Index Cond`, confirming the shape is right for when
        the table is large - claimed as future headroom, not a measured win today.
  - [x] **The validated address is the address dialled** (`csense_shared/security/
        pinned_http.py`, new). Both send paths - the dispatcher and the on-demand
        `POST /{id}/test` - previously called `resolve_public_endpoint`, discarded the IPs
        it returned, and handed the *hostname* to httpx, which resolved it a second time.
        That is precisely the DNS-rebinding window `security/outbound.py`'s own docstring
        says this guard exists to close: one answer for the check, another for the connect.
        The request now goes to the IP literal, with the hostname carried in `Host` and in
        the `sni_hostname` request extension - the latter being the part that must not be
        got wrong, since it is what the TLS layer verifies the certificate against.
        Verified rather than assumed, because a botched pin silently disables certificate
        checking and that would be worse than the bug: against a real `httpbin.org` IP, an
        honest Host/SNI gets `200`, and the same socket with a mismatched hostname raises
        `CERTIFICATE_VERIFY_FAILED: Hostname mismatch` - so verification is exactly as
        strict as before. All of a name's addresses are kept as candidates, since pinning
        to only the first would have quietly given up the happy-eyeballs walk httpx used
        to do for free.
  - [x] **`getaddrinfo` runs on a thread**, not on the event loop. It blocks with no
        timeout of its own, and `notification_worker/app/main.py` runs webhook delivery and
        alert dispatch in *one* event loop under `asyncio.gather` - so one tenant's webhook
        pointed at a host with an unresponsive DNS server would have stalled life-safety
        alert delivery for every tenant on that replica. Same `asyncio.to_thread` treatment
        the container already gives its blocking MinIO calls, applied to the tenant API's
        webhook create/test handlers for the same reason.
  - [x] **`webhook_dispatch_max_event_age_seconds` is now actually wired.** It was dead
        configuration: nothing threaded it from `main.py` through
        `run_forever`/`run_once`, so fan-out always fell back to the module default and an
        operator tuning the env var would have seen no effect at all. Threaded end to end,
        with a test that fans out nothing under a ten-second window and the same event
        under the default one - so the claim is now checkable rather than merely written.
  - [x] Verified for real: `backend/tests/test_webhook_dispatcher.py` (12 real-DB tests),
        `test_webhook_dispatch_loop.py` (3), `test_pinned_http.py` (8, one of them gated
        behind `ALLOW_NETWORK_TESTS=1` because it drives a real TLS handshake),
        `test_worker_loop_isolation.py` (3).
        `scripts/e2e_webhook_dispatch.py` against the live stack: a real acknowledged
        incident emits `incident.acknowledged.v1`, it fans out of the outbox on its own,
        and the *deployed* notification-worker signs and POSTs it to a real
        `httpbin.org` endpoint - confirmed in the container's own logs, `200` on the first
        attempt. Exactly **one** delivery row survives the many worker passes that run
        during the poll window, which proves `processed_events` idempotency against the
        real running worker rather than only in a unit test, and a second endpoint whose
        `event_filters` don't match receives zero. Re-run unchanged after the IP-pinning
        rework above, which is the point: the delivery now goes to a validated address
        rather than a re-resolved name, and still lands. Full PASS, as does
        `e2e_webhooks.py` for the on-demand test-delivery path that shares the same helper.
        Backend suite: 429 passed, 59 skipped (plus the one known unrelated failure,
        `test_site_timezones.py::test_unusable_values_are_refused[asia/kolkata]`);
        `ruff check backend scripts` clean.
  - [x] **2026-09-09: the API half of "no UI for webhook delivery history" is closed;
        the UI half turned out to be a bigger gap than that line implied - now also
        closed, for the Customer CRM (see below for the one pre-existing, unrelated bug
        found while verifying it, and the one deliberately-unaddressed Developer Console
        note).**
    - [x] `GET /api/v1/tenant/webhooks/{webhook_id}/deliveries` (new, `webhook.manage`,
          `backend/tenant_api/app/api/webhooks.py`) - real, tenant-scoped, cursor-paginated
          on `(scheduled_at, id)` the same keyset shape `incidents.py`'s own list endpoint
          already uses (this table takes new rows continuously as retries land, so offset
          paging would skip or repeat). Returns `status`, `attempt_number`,
          `response_status`, `response_time_ms`, `next_attempt_at`, and the
          already-redacted `failure_summary_redacted` exactly as the dispatch worker wrote
          it - no redaction logic added here, by design. Filterable by status
          (comma-separated, same convention `incidents.py`'s own `status` param uses). The
          `webhook_id` is checked for ownership before anything else, same "missing vs. not
          yours stays indistinguishable" discipline `update_webhook`/`delete_webhook`/
          `rotate_webhook_secret`/`test_webhook` already apply in this file - a foreign
          tenant's endpoint id gets the identical 404 a nonexistent one would.
    - [x] Verified for real: `backend/tests/test_webhooks_api.py` (11 tests, new) against a
          live migrated Postgres, same two-DSN discipline
          `test_pipeline_assignments_api.py` established (`TEST_POSTGRES_DSN` sets up
          fixture rows with `BYPASSRLS`; the ASGI app under test is mounted on
          `TEST_POSTGRES_API_DSN`, the real `csense_api` role) - correct row shape,
          newest-first ordering, single and comma-separated status filters, cursor
          pagination with no gaps or repeats across pages, a malformed cursor refused
          (400) rather than 500'd, another tenant's delivery rows never appearing (proven
          against real RLS, not just the endpoint's own SQL filter), and a foreign-tenant
          `webhook_id` getting the exact same 404 code as a missing one.
    - [x] `scripts/e2e_webhook_dispatch.py` - previously read `webhook_deliveries` straight
          out of Postgres via `psql` for its delivery assertions, exactly because nothing
          exposed the table over the API; now calls the new endpoint instead (`psql`
          stays only for seeding the incident, which has no public creation endpoint, and
          for the `processed_events` idempotency check, which has no API surface). Re-run
          against the live stack with `tenant-api` rebuilt to pick up the new route: full
          PASS, including the idempotency and event-filter assertions now read through the
          real endpoint rather than a raw table scan. `scripts/e2e_webhooks.py` re-run
          unchanged: full PASS. `ruff check backend scripts` clean; full backend suite 783
          passed, 19 skipped (plus the one known unrelated failure,
          `test_site_timezones.py::test_unusable_values_are_refused[asia/kolkata]`).
    - [x] **2026-09-09: the Customer CRM management page now exists** -
          `frontend/customer-crm/src/pages/WebhooksPage.tsx` (new), routed at `/webhooks`
          and added to `Layout.tsx`'s nav next to Notifications. Full create/list/edit/
          rotate-secret/test/delete cycle plus the delivery-history view this gap was
          originally about, all against the real endpoints above - no mocked data, no
          workaround for anything the API doesn't support.
      - [x] `frontend/customer-crm/src/api/webhooks.ts` (new) mirrors `webhooks.py`
            exactly, including what it refuses to type: `signing_secret` exists only on
            `CreateWebhookResult`/`RotateSecretResult`, the two shapes that exist to show
            it once - there is no field anywhere else in the client that could carry a
            secret back out of a plain GET.
      - [x] The signing secret is shown exactly once, in a dedicated `SecretRevealDialog`
            (copy button with a clipboard-denied fallback, an explicit "this is shown
            once" warning) shared by both create and rotate-secret - mirrors
            `EdgePage.tsx`'s own `TokenDialog` for an enrolment token, the established
            precedent in this app for a write-only value. Rotate goes through a
            `ConfirmDialog` that says plainly that the old secret stops working
            immediately, matching `CamerasPage.tsx`'s own directness about consequential
            actions.
      - [x] Event filters are a plain comma-separated text input, not a picker -
            `CreateWebhookIn.event_filters` is freeform (`list[str]`, no server-side enum
            validation), so a picker would have to enumerate values the backend does not
            expose; over-building one wasn't worth it for a field the API itself treats as
            opaque strings.
      - [x] Delivery history is a dialog opened per-row (`DeliveryHistoryDialog`), not a
            separate route - the closest existing precedent is
            `NotificationPoliciesPage.tsx`'s own `EscalationDialog` (manage a per-row
            sub-resource without leaving the list). Real cursor pagination via a real
            "Load more" against the real `next_cursor`, the same convention
            `AuditPage.tsx` already established - not client-side paging of one fetched
            page. Delivery status badges (`DeliveryStatusBadge`, added to `Badges.tsx`)
            reuse the same four severity/status colours every other badge in this app
            already uses (pending→medium, succeeded→low, failed→critical,
            abandoned→neutral) rather than inventing a fifth palette.
      - [x] Verified for real: `npm run typecheck` / `lint` / `build` all clean in
            `frontend/customer-crm`. `scripts/e2e_webhooks_crm.py` (new, Playwright)
            against the live stack: registers a tenant, creates a webhook through the
            real form, sees the real one-time secret, sees it listed, fires a real test
            delivery and reads the real `TestDeliveryOut` result from a toast, opens the
            delivery-history dialog and sees that same test delivery, causes a second,
            *automatically*-dispatched delivery the same way `e2e_webhook_dispatch.py`
            does (seed an incident, acknowledge it through the real API) and confirms the
            dialog shows it after a genuine fresh page load, rotates the secret and
            confirms the new one differs from the first, deletes the endpoint and
            confirms it's gone. Full PASS.
      - [x] **The pre-existing refresh-rotation-race bug named above is now fixed**,
            addressed as its own follow-up task since the prior note explicitly flagged
            this exact area as out of scope for the webhooks work. Two things changed:
            - **Real fix, in `rotate_session`
              (`backend/shared/csense_shared/security/sessions.py`)**: a short (10s) grace
              window on the *immediately-previous* token hash, the standard
              "refresh-token-reuse-detection-interval" shape (as Auth0 and other
              real-world rotation systems describe it). When a presented token doesn't
              match the current stored hash but does match the hash that was current
              immediately before the last rotation, and that rotation happened within the
              grace window, the caller gets back the SAME current token the winning
              caller already established (via a Redis key with its own short, independent
              TTL - `_grace_key`), rather than being treated as a replay - so two
              concurrent legitimate callers (two tabs, a slow-network retry) converge on
              one valid session instead of one of them nuking it for both. Anything
              matching neither hash (forged, or a genuine replay once the grace window
              has closed) still deletes the whole session exactly as before - this path
              is deliberately unweakened. The compare/check-grace/rotate-or-reject
              decision runs as one atomic Redis `EVAL` (Lua script), not separate
              `HGETALL`/`HSET` round trips, because real concurrent callers (verified with
              `asyncio.gather`, not just sequential calls that happen to race in
              practice) can otherwise both observe the same pre-rotation state and both
              believe themselves the legitimate rotator. Full tradeoff (why 10s, what it
              does and doesn't weaken, TTL-renewal and logging decisions) documented in
              the module's own docstring.
            - **Frontend hygiene, in `AuthContext.tsx`**: the silent-refresh effect is now
              guarded by a `useRef` boolean so React.StrictMode's dev-only double-invoke
              can't fire it twice - the standard minimal fix for a non-idempotent effect,
              and a genuine win in production too (one fewer wasted duplicate network
              call on every real page load), not just a workaround for the dev symptom.
              This does **not** address two real separate browser tabs racing a refresh -
              that's a distinct, real backend concern, and the grace window above is what
              actually covers it.
            - **Verified for real**: `backend/tests/test_sessions.py` (new) against a real
              Redis - a genuinely stale/forged token (including one two rotations old,
              i.e. past its own grace window) still rejects and still destroys the
              session; a real `asyncio.gather` of two (and, separately, five) concurrent
              `rotate_session` calls presenting the same current token all succeed,
              converge on one identical currently-valid token, and leave the session
              intact; a real 1.5s wait (not a mocked clock) past a monkeypatched 1s grace
              window still correctly rejects the replay. Full backend suite: 549 passed,
              only the pre-existing unrelated `test_site_timezones.py` failure. `ruff
              check backend scripts` clean on everything this task touched (pre-existing,
              unrelated `F541` findings remain in `scripts/e2e_webhooks_crm.py`, out of
              scope here). Frontend `typecheck`/`lint`/`build` all clean. Beyond the
              Redis-backed unit tests, also rebuilt and restarted the real `tenant-api`/
              `admin-api` containers against the live Docker stack and fired two truly
              concurrent `curl` refresh calls at `POST /api/v1/auth/refresh` presenting
              the same real cookie from a real registered tenant: both returned 200 with
              the identical rotated refresh-token cookie, a follow-up refresh with it
              still succeeded, and replaying the original (by-then two-rotations-stale)
              token correctly got a 401 - the real end-to-end version of the race, not
              just the isolated module test.
            - **UPDATE (2026-09-09), real gap in the above found and closed**: an
              independent security review reproduced, against real Redis with a real
              `asyncio.gather` burst (10 callers on token-generation-0 racing 10 callers on
              generation-1, no mocks), that the fix above **still destroyed the session in
              4 of 8 trials**. Root cause: `_grace_key` held exactly **one overwritten
              slot** - the immediately-previous generation's hash and its current raw
              token. That's correct for exactly one rotation happening while stragglers of
              it are still in flight, but wrong the moment a *second* legitimate rotation
              (a third tab, a retry) lands before every straggler of the *first* one has
              been served: the second rotation's `HSET` on `grace_key` unconditionally
              clobbered the first rotation's still-live grace entry, so those
              first-generation stragglers then matched neither current nor grace and hit
              the unconditional-delete branch - killing the session for every caller,
              including ones holding the brand-new, genuinely-current token. Exactly the
              same "two rotations old" phrase in the verification note two bullets above
              was, in hindsight, evidence this was already latent: the old test asserting
              that exact case *rejected* was actually pinning the bug, not a real security
              property (see below).
              - **Fix, same file**: `grace_key` is now a Redis hash holding one field per
                still-live superseded generation (`field = that generation's token hash`,
                `value = that generation's own expiry timestamp`, stamped once and never
                touched by a later rotation) plus one reserved `__current__` field mirroring
                whatever the actual current raw token is right now. A later rotation only
                *adds* a field; it never overwrites an earlier one. Bounded by
                `MAX_GRACE_GENERATIONS = 5` (defense in depth against a caller rotating
                faster than the 10s `GRACE_WINDOW_SECONDS`, on top of each entry's own TTL)
                - oldest-expiring entries evicted first once exceeded. The whole
                lazy-expire/cap-evict/compare-current/check-every-generation/rotate-or-reject
                decision is still one atomic Lua `EVAL` - the already-verified atomicity
                property is unchanged, only the data structure it operates on grew from a
                single pair to a bounded set. Full design tradeoff (what this does and does
                NOT widen for a real replay) is in the module's own docstring.
              - **`backend/tests/test_sessions.py` updated**: the old
                `test_a_token_already_rotated_away_is_rejected_on_reuse` asserted that a
                token exactly two rotations stale is *always* rejected, with no elapsed
                time involved - that assertion encoded the bug (a single overwritten slot
                can only ever remember one generation back), not a real security property,
                so it's been replaced with
                `test_a_token_more_than_the_generation_cap_stale_is_rejected_on_reuse`
                (monkeypatches the cap down and does enough real rotations to exceed it,
                confirming eviction-past-the-bound still rejects and still destroys the
                session). Two new tests pin the actual fix:
                `test_generation_0_stragglers_survive_a_second_legitimate_rotation`
                (deterministic, sequential - two real rotations complete, then a
                generation-0 straggler is honoured and gets the true current token, not the
                stale intermediate one) and
                `test_generation_0_and_generation_2_stragglers_race_without_destroying_the_session`
                (the review's own reproduction: 25 trials, each a 20-way real
                `asyncio.gather` burst of generation-0 stragglers racing generation-2
                callers after two real sequential rotations - every trial passed, 0/25
                destroyed, 0/25 null results). All 12 tests in the file pass, run 5x in a
                row for determinism, plus ad hoc stress beyond the permanent suite: 8 trials
                each at 2/10, 3/15, and 5/10 generations/burst-size - 0 destroyed, 0 null
                results across all 24 of those trials too. Full backend suite: 551 passed,
                268 skipped (unrelated services not running in this shell), same one
                pre-existing `test_site_timezones.py::test_unusable_values_are_refused[asia/kolkata]`
                failure and nothing else. `ruff check backend scripts` clean on
                `sessions.py`/`test_sessions.py`; same pre-existing unrelated
                `e2e_webhooks_crm.py` `F541` findings, untouched.
              - **Real remaining limitation, named rather than hidden**: this closes the
                *specific* reproduction the review found (grace entries surviving
                subsequent legitimate rotations) but the fundamental tradeoff the module
                docstring already names is still true and still not eliminated by this
                fix - an attacker racing a legitimate client to present a stolen token
                *within* `GRACE_WINDOW_SECONDS` (10s) of any live generation still succeeds
                instead of revoking the session, exactly as before. Multi-generation makes
                this no *wider* (same 10s per generation, same 5-generation cap) but also
                doesn't make it narrower - closing that would need binding rotation to some
                caller-identity signal (IP/device fingerprint) this module doesn't have
                today, which is a materially bigger change than this task's scope.
              - **A second, independent security review of this exact fix** (not just the
                first fix) ran adversarial stress beyond the shipped suite (3-generation and
                5-generation-at-the-cap boundary bursts, 1500+ calls/run across multiple
                runs) and found the multi-generation design **Approved** - genuinely closes
                the reproduced gap, no new session-destruction or replay-widening path
                found. Two low-severity, non-blocking follow-ups from that review:
                (1) the docstring's "what this does NOT widen" section stated the *per-token*
                window honestly but didn't say the *aggregate* count of simultaneously-
                exploitable stale values grew from ≤1 to ≤5 (bounded, same order of
                magnitude, unavoidable side effect of fixing the real bug) - fixed by adding
                that sentence to the docstring directly, same commit family as this entry;
                (2) eviction tie-breaking uses `redis.call('TIME')[1]` (second-granularity
                only), so two rotations landing in the same wall-clock second get identical
                expiry timestamps and rely on Lua's `table.sort` (not guaranteed-stable) to
                order them - verified correct in every trial run (Redis hash field-insertion
                order + small-hash listpack encoding happens to make it deterministic in
                practice) but not guaranteed by construction. Not fixed - genuinely low
                severity (worst case is evicting a tied peer with identical remaining
                lifetime anyway) and left as a real, named follow-up rather than papered over.
      - [ ] The Developer Console (`frontend/developer-console/src/pages/`) still has no
            webhook-management page - not addressed here, and arguably not a real gap:
            webhook endpoints are a tenant's own integration config, not something AIRIVU
            staff manage on a tenant's behalf, so there is no obvious reason the internal
            console would ever need this. Named rather than silently ignored, in case that
            assumption turns out to be wrong.
  - [ ] `outbox_events` still has **no retention or pruning policy at all** - unrelated to
        delivery history, unchanged by the above - nothing anywhere deletes from it, so it
        grows monotonically for the life of the deployment. Migration 0052's index
        postpones that becoming a problem; it does not solve it. Whether outbox rows
        should be pruned after N days, archived to object storage, or kept forever as an
        event log is a real product/compliance decision nobody has made - named here so it
        gets made deliberately rather than discovered the day the table hurts.
- [ ] SMS/web-push provider adapters — **[NEEDS HUMAN INPUT: no provider contracted]**
- [~] Async reports/exports with time-limited download
  - [x] `export_jobs` (migration 0050) - tenant-RLS-isolated, the same `FORCE ROW LEVEL
        SECURITY` pattern every other tenant-owned table this session added already
        uses. No new permission: exporting incidents you can already read is scoped by
        the same `incident.read` the list/detail endpoints already require (migration's
        own docstring).
  - [x] `backend/tenant_api/app/api/exports.py` (new): `POST .../incidents` returns a
        `queued` job immediately (202); the query + CSV build + MinIO upload runs in a
        FastAPI `BackgroundTasks` callback, off the request path - no separate worker
        process this deployment has anywhere to run. **A real ordering fact confirmed
        directly against the installed FastAPI's own `routing.py`, not assumed**:
        background tasks run *before* a dependency's post-`yield` cleanup, so the
        request's own DB transaction is still open when the task starts - the endpoint
        commits explicitly before scheduling the task so the freshly-inserted job row is
        actually visible to the task's own, separate `tenant_session`.
  - [x] `csense_shared/exports/incidents.py` (new): pure, dependency-free CSV
        serialization (column order, `None`-to-empty-cell, UTF-8 BOM for Excel) - kept
        out of the API module so the one part of the pipeline with real formatting logic
        is unit-testable without a running stack, the same reasoning `cameras/health.py`
        already established. 6 real unit tests
        (`backend/tests/test_exports.py`).
  - [x] Download is a presigned URL minted fresh on every `GET .../{id}`
        (15-minute TTL, same reasoning as evidence images), gated by the job's own
        longer-lived `expires_at` (24h after completion) - **lazily flipped to `expired`
        on read**, the same precedent `sync_license_status` and support-grant reads
        already established rather than a scheduled cleanup job this deployment has
        nowhere to run.
  - [x] Verified for real, against the live stack, not just unit-tested:
        `scripts/e2e_exports.py` - a real tenant, real site/camera via the real API,
        five seeded incidents, a real export job that reaches `completed` with the
        right `row_count`, a real presigned URL that downloads real CSV bytes matching
        every seeded incident, a status filter that narrows the export correctly, an
        invalid filter refused (400) before any job is created, the job list ordered
        newest-first, a bogus job id giving a real 404, and - the property that matters
        most - a second, unrelated tenant getting a 404 (not another tenant's data) for
        the first tenant's job id, with its own job list empty (RLS isolation, not just
        an authorization check in application code). Full PASS. Backend: 277 passed,
        164 skipped; `ruff check` clean.
  - [ ] **Deliberately scoped to one export type this pass**: incidents only, as CSV.
        The same `export_jobs`/background-task/presigned-download mechanism would cover
        detections, audit events, or camera health history without changes - just a new
        `export_type` and its own query-building function - named as a real, easy next
        slice rather than silently left unconsidered. No Developer Console or Customer
        CRM UI yet - fully usable and exercised via the API and the e2e script.
- [x] License grace/restriction + renewal flow
  - [x] `csense_shared.licensing.lifecycle` (new): `sync_license_status` computes and
        persists `active -> grace -> expired` purely against `expires_at`/`grace_ends_at`
        (SCH's own columns, migration 0038 - no new migration needed), **lazily on the
        next read** - the same "flips to expired on the very next read, not merely
        hidden" pattern this session already established for support-grant expiry, so no
        new background worker/cron process is needed. `suspended`/`revoked` are left
        alone by the clock (a human explicitly set those). `current_license` looks up
        the tenant's most recent license row regardless of status - dropping the
        `active`/`grace` filter deliberately, since a license that already lazily
        flipped to `expired` has to stay findable by the next call, or restriction would
        silently stop applying the moment nobody was looking.
  - [x] **Restriction, wired into the one place quota enforcement already lives**
        (`cameras.py`'s `create_camera`, alongside `reserve_quota`) rather than
        blast-radius across every write endpoint this late in the session -
        `require_license_not_restricted` refuses `expired`/`suspended`/`revoked` with a
        real, distinct `402 license_restricted` (not `quota_exceeded` - a tenant past
        that point should see "your license lapsed", not a limit-shaped error suggesting
        raising a quota would help). `grace` and "no license at all" both pass through
        un-restricted - grace is a reduced-friction warning window, not a hard stop, and
        the same "no plan assigned = unlimited" fallback `reserve_quota` already uses.
  - [x] **Renewal**: `POST /api/v1/admin/licenses/{id}/renew` (admin_api, `license.manage`,
        same TRD-SEC-010 step-up gate `issue_license` already requires). Updates the
        *same* license row rather than issuing a new one - quota usage already recorded
        against it stays intact, which a fresh `issue_license` call would lose. Also
        extends the tied `quota_ledgers.period_end` to the new expiry - without this, the
        ledger's own "does this row currently cover now()" check in `reserve_quota` would
        stop finding it once the old expiry passed, silently reading back as unlimited
        rather than the tenant's real, renewed limit. `revoked` is a deliberate terminal
        state, refused (409) rather than renewed.
  - [x] `issue_license` now also accepts `grace_days` (default 14) and computes/stores
        `grace_ends_at`; `GET /api/v1/tenant/license` and `GET /api/v1/admin/licenses`
        both surface the freshly-synced status and `grace_ends_at` instead of the
        `active`/`grace`-filtered, possibly-stale view they had before - a tenant whose
        license lapsed can now actually see that, not have the endpoint quietly answer
        `null` again as if nothing had ever been issued.
  - [x] Customer CRM's `SettingsPage.tsx` license card: severity-correct status badge
        (active=low, grace/scheduled=medium, expired/suspended/revoked=critical - mirrors
        `TeamPage.tsx`'s own membership-status convention) and a "renew by" /
        "contact your reseller" hint tied to the real status. Developer Console gets the
        `renewLicense` API function and updated types, no dedicated renewal dialog yet -
        named as a deferral, same shape as support grants' own missing UI.
  - [x] `backend/tests/test_license_lifecycle.py` (13 tests, real DB): active-stays-
        active, grace flip (and that it actually persists, not just returns), expired
        flip both with and without a grace window, a term-less license never expires, a
        suspended license is left alone by the clock, `current_license` keeps finding an
        already-expired row, restriction passes no-license/grace and refuses
        expired/suspended with the right code. `scripts/e2e_license_lifecycle.py`: two
        real tenants, licenses issued already past expiry (no sleeps - the clock is
        driven by a past `expires_at` at issuance, not by waiting) - one lands in grace
        (still creates resources), one lands in expired (real 402, distinct from
        `quota_exceeded`); renewal restores active + resource creation immediately and
        the quota ledger period is verified extended via a direct query; a revoked
        license refuses renewal (409). Full PASS on both scripts, first clean run (one
        run of each hit an unrelated, already-known TOTP-window timing flake in
        `mfa_step_up` - passed cleanly on immediate retry, same class of flake noted
        elsewhere in this file). `ruff check backend scripts` clean; full backend pytest
        suite green (392 passed, 30 skipped). `customer-crm` and `developer-console`
        `typecheck`/`lint`/`build` all clean; both rebuilt containers confirmed serving
        `200` through Traefik.
- [~] Camera health use cases: offline, obstruction, glare/night-vision, low FPS, network
  - [x] **`offline`, `network`, `low FPS` - all real, all wired into the existing probe**
        (`POST /cameras/{id}/probe`, migration 0041's own mechanism), not a new probing
        path. `csense_shared.cameras.health.classify_health_events` (new, dependency-free
        - see its own docstring) turns one probe outcome into one or more
        `camera_health_events` rows: a `connectivity` event always (unchanged
        online/offline signal), plus a `network` event when the probe's own round-trip
        time exceeds 3s (measurable as of this pass - `camera_probe.probe_stream` now
        times the whole exchange, migration-free since it only adds a wrapper around the
        existing function), plus a `framerate` event when the stream's own SDP-reported
        fps drops below 5 - both thresholds named and reasoned about in the module's own
        docstring, not arbitrary.
  - [x] Migration 0049: `camera_health_events.status` gains `degraded` (mirrors
        `edge_health_events`' own ok/degraded/failed model, migration 0026) and a new
        `check_name` column (free text, deliberately not DB-constrained - mirrors
        `edge_health_events.check_name` exactly, for the same "the check set grows"
        reasoning). `GET /{camera_id}/health` now returns `check_name` per event and a
        computed `active_concerns` list (which named checks' most recent event isn't
        `online`) - not a new stored value, derived the same way `current_status`
        already is, so there is one place this fact can be wrong, not two.
  - [ ] **`obstruction` and `glare/night-vision` are deliberately not built this pass** -
        both need a decoded video frame to analyze (brightness/variance statistics), and
        `camera_probe.py`'s own long-standing design speaks RTSP directly rather than
        shelling out to ffmpeg specifically to avoid that dependency for connectivity
        checks; reversing that just for this would be the wrong place to add a
        frame-decode path. A real snapshot-based check is a legitimate, separate feature
        - and per [[nvr-h265-constraint]] (this deployment's own real NVR, tested
          earlier this session), any glare/obstruction thresholds would need real
        validation data against its H.265-only, IR-greyscale night imagery before being
        trusted, the same caution already recorded there for detection thresholds - not
        a stub built under time pressure this late in the session.
  - [x] `backend/tests/test_camera_health_classification.py` (9 tests, pure logic - no
        DB, no real camera, so the threshold-crossing branches are actually exercised,
        unlike a live-hardware-only e2e which cannot reliably force a slow or low-fps
        condition on demand): healthy/offline baselines, network and framerate degraded
        events fire past their thresholds and not at the boundary, both can fire on the
        same probe, an unreachable probe never gets network/framerate events, missing
        framerate data is never misread as 0fps. `scripts/e2e_camera_health.py` extended
        (not replaced) to prove the mechanism against the real NVR: a real measured
        `elapsed_ms`, exactly one `connectivity` event per probe (robust to any real
        degraded events a live camera might also trigger), every event's `check_name` is
        one of the known set. Full PASS against real hardware, both before and after a
        mid-slice refactor (moving the classification function into `csense_shared` to
        resolve a real `app`-module name collision across services in the shared test
        suite - caught by running the full suite, not just the new file in isolation).
        `ruff check backend scripts` clean; full backend pytest suite green (401 passed,
        30 skipped).

## Phase 7 — Migration Tooling and Pilot Beta

Legacy system access provided 2026-08-25, so this is partially unblocked.

- [x] Model estate migration (idempotent, digest-verified) — done ahead of schedule as
      part of Phase 4 above
- [~] Legacy source inventory frozen and mapping approved (users, tenants, cameras,
      credentials, detections, snapshots)
  - [x] **`users`** table: 61 rows total, 60 real + 1 excluded (`first_name='Test',
        last_name='User'`, `@example.com`) — frozen and mapped. See the migration tool
        below.
  - [ ] The rest of the estate is a **separate, not-yet-started inventory pass**. Real
        counts already known from the legacy pull, recorded here so they don't need
        rediscovering: `user_cameras` — 4 rows, look like placeholder/test data, not
        real camera assignments; `camera_ip_changes` — 0 rows; the separate legacy DDNS
        SQLite file's `hosts` table — 0 rows; ~15,225 real snapshot image files on the
        legacy server, ~1.5 GB total. None of these have a migration tool yet — nothing
        beyond counting them was attempted in this pass (scope was users only).
- [ ] Idempotent migration tools for the remaining entity types (`user_cameras`,
      `camera_ip_changes`, DDNS `hosts`, snapshots — see inventory counts above)
- [x] Password migration or forced-reset strategy (legacy uses SQLite `csense_users.db`)
  - **Force-reset, not re-hash** — decided by the account owner directly
    (CLARIFICATIONS.md #30, 2026-09-09). No legacy bcrypt hash is migrated, or ever read
    into memory beyond skipping past it.
  - `backend/migrations/import_legacy_users.py`: idempotent, verify-before-write, raw-
    psycopg operator script (house style of `import_legacy_models.py`). Creates one
    organization + tenant + `invited`/`password_hash=NULL` user + `invited` `tenant_owner`
    membership per real legacy user, plus a real `audit_events` row recording the legacy
    numeric id and that a hash existed but was never migrated. Run for real against the
    live legacy pull: **60 created, 1 test/placeholder account excluded, re-running
    creates 0** (idempotency verified against the real data, not just the test fixture).
  - `backend/migrations/send_legacy_migration_invitations.py`: the separate, deliberately
    manual step that actually issues the real invitation ticket + email for the accounts
    the import created — selection is narrowed to rows with a matching
    `user.legacy_import` audit event, so it can never touch an unrelated normal team
    invitation or reseller-provisioned account. Refuses to send anything without an
    explicit `--confirm-send` flag (dry run otherwise: prints exactly who would be
    emailed and the real link origin, zero side effects). **Run for real in dry-run mode
    against the live 60 migrated accounts — confirmed correct.**
  - **`--confirm-send` has deliberately not been run.** The real invitation emails to the
    60 migrated legacy customers are the account owner's own call to trigger, not
    something to fire as a side effect of building the tooling.
- [ ] Snapshot-to-MinIO digest verification
- [ ] DDNS/edge protocol compatibility or edge upgrade package
- [ ] Per-tenant reconciliation dashboard/report
- [!] Pilot cutover still needs a **named pilot tenant and an agreed cutover window**
      — [NEEDS HUMAN INPUT]

## Phase 8 — Production Hardening

- [~] SAST/SCA/secret/container/IaC scan wired into CI (automatable now)
  - [x] Secret scanning already existed (gitleaks, full history). Added: SAST (`bandit`
        against `backend`/`scripts`), SCA for Python (`pip-audit`, already existed) and
        for both frontend apps (`npm audit --audit-level=high`, new), and a Trivy
        filesystem + config scan covering dependency manifests across every service plus
        every Dockerfile/`docker-compose.yml` for real misconfigurations - container/IaC
        scanning without needing to build any image in CI (this workflow doesn't build
        service images at all; that happens in `infra/`, not here).
  - [x] **A real fix landed alongside wiring the scan, not just a report**: Developer
        Console's `next.js` was pinned at `14.2.5`, vulnerable to a real critical CVE
        (cache poisoning) plus several high-severity ones; bumped to `14.2.35` (the
        latest 14.x patch - a real, low-risk fix, not a side effect of forcing the audit
        tool's own suggestion). Verified: `typecheck`/`lint`/`build` all still pass, and
        the running container confirmed serving `200` through Traefik afterward.
  - [x] Every new scan step is **non-blocking**, the same discipline `pip-audit`'s own
        `TODO(Phase 8)` already established for this repo - a first run against a baseline
        nobody has triaged should surface real findings, not fail every future PR on day
        one. Each has its own TODO naming what's actually left in its baseline:
        `bandit`'s is mostly PATCH-endpoint false positives (a dynamic column list built
        from a Pydantic model's own fixed field names, which the tool can't tell apart
        from user-controlled input - confirmed by hand for the one specific line this
        session's own PATCH pattern touches, `zones.py`); the Customer CRM's `npm audit`
        baseline is `react-router` 6→7 and `vite` 6→8, both real fixes that need a
        breaking-change major-version bump and their own dedicated testing pass, not a
        side effect of wiring the scan; the Developer Console's remaining baseline is
        `eslint-config-next`'s own build-time tooling chain (`glob`/`minimatch`), fixed
        only by an `eslint-config-next` major bump.
  - [ ] **Deliberately deferred**: turning any of these blocking once triaged; uploading
        SARIF output to GitHub code scanning (findings currently only visible in the
        workflow's own log/artifact); a Dependabot/Renovate config to keep the frontend
        baselines from silently drifting further.
- [x] DAST baseline scan against local stack, two real findings found and fixed
  - [x] `scripts/dast_baseline.py`: OWASP ZAP's own `zap-api-scan.py`, driven from
        `tenant-api`/`admin-api`'s real, already-live OpenAPI documents, against the real
        running containers on the real docker network - not a mocked target. `-I`
        (non-blocking) matches this session's own CI security-scanning precedent
        (bandit/npm audit): findings are reported, not gated on. Reports land in
        `reports/dast/` (gitignored - point-in-time artifacts).
  - [x] **The first real run found two real, fixable issues**: `X-Content-Type-Options
        Header Missing` and `Cross-Origin-Resource-Policy Header Missing` (Low, on every
        endpoint of both services). A third finding, `Private IP Disclosure` (evidence
        `192.168.1.0`), was reviewed and confirmed a false positive - it's an `e.g.
        192.168.1.0/24` example string in a Pydantic field description
        (`edge.py`'s tunnel-network field), surfaced in the auto-generated OpenAPI
        document ZAP scanned, not a real leak of any actual internal address.
  - [x] **Fixed for real**: `SecurityHeadersMiddleware` (new, `csense_shared.middleware`,
        installed on both services) sets both headers on every response.
        `Cross-Origin-Resource-Policy: same-origin` is correct (not the more permissive
        `same-site`/`cross-origin`) specifically because Traefik serves each frontend and
        its own API on the same origin - the one legitimate cross-origin caller (a local
        Vite dev server) uses CORS `fetch()`, which CORP does not govern.
  - [x] **A real Starlette gotcha surfaced and was fixed properly, not routed around**:
        `BaseHTTPMiddleware` does not reliably see responses an `add_exception_handler`
        handler produces - the middleware was rewritten as a plain ASGI class (mutating
        the raw `http.response.start` message) so the headers apply to `ApiError`-typed
        error responses too (every 401/402/404/422/429 this session has built), not just
        200s. One genuine, named architectural boundary remains: a response from a
        catch-all `Exception`-keyed handler (this codebase's own
        `unhandled_exception_handler`, truly unexpected bugs) is routed by Starlette to
        `ServerErrorMiddleware`, which sits outside *all* `add_middleware` layers -
        structurally unreachable by any middleware, asserted explicitly in its own test
        rather than silently assumed away.
  - [x] `backend/tests/test_security_headers_middleware.py` (4 tests): both headers set
        on a normal response, both still set on an `ApiError`-shaped error response
        (the realistic case), and the catch-all-500 boundary is asserted as a known gap,
        not hidden. Full backend pytest suite green (405 passed, 30 skipped).
  - [x] **Verified twice against the real rebuilt/restarted services, individually
        (a combined-run script invocation hit real Docker Desktop RAM-pressure
        instability partway through a second full run - the same class of environment
        issue already documented earlier in this session, unrelated to the fix itself;
        each service was confirmed clean via its own separate scan instead)**: `tenant-api`
        went from 3 real+1 false-positive Low findings to exactly the 1 false positive;
        `admin-api` went from 2 Low findings to **zero** Low/Medium/High findings, 118/118
        checks passing.
- [!] Independent penetration test — **[NEEDS HUMAN/EXTERNAL INPUT]** requires a
      contracted third party; not something I can perform or substitute for
- [x] Load/spike/endurance/failure-injection test suite (automatable, local-scale), one
      real bug found and fixed by the failure-injection phase
  - [x] `scripts/load_test.py`: a plain `asyncio` worker-pool against a real throwaway
        tenant's own real, authenticated, DB-backed endpoints - no new load-testing tool
        pulled in. Concurrency/duration are named, honest local-scale numbers (10
        baseline / 30 spike / sustained 45s endurance), not what a dedicated-
        infrastructure production load test would use. Four phases: baseline (p50/p95/
        p99 + error rate), spike (a sudden 3x burst, a looser but still bounded error-
        rate ceiling), endurance (same load sustained, first-bucket-vs-last-bucket p95
        compared to catch drift a short burst can't), and failure injection (stops the
        real redis container mid-run, restarted in a `finally` so it's never left down).
  - [x] **The failure-injection phase found a real, fixable bug**: `GET /readyz` (built
        specifically to report a dependency outage gracefully) instead **hung for ~26
        seconds** when redis was down, traced to redis-py 8.x's own default connection
        retry policy (10 retries with exponential backoff) applying independently of
        `retry_on_timeout` - setting `socket_connect_timeout`/`socket_timeout` alone
        (the first fix attempted) was not enough, confirmed by timing a real failed
        connect before concluding retries were the actual cause, not guessed.
  - [x] **Fixed for real, in the one shared place** (`csense_shared.db.redis.
        create_redis_client`, used by both `tenant-api` and `admin-api`): `retry=Retry(
        NoBackoff(), 0)` plus `retry_on_timeout=False`/`retry_on_error=[]` - a Redis
        outage now fails in the configured 2s, not ~26s, for every caller of this shared
        client (the rate limiter and `/readyz` alike), not just the one endpoint that
        happened to surface it.
  - [x] Verified twice, honestly: first by direct reproduction from a plain Python
        process using the exact same client-construction code against the real stopped
        redis container (confirmed the bug at ~26s, then confirmed the fix at exactly
        2.0s) when repeated Docker Desktop instability (this dev machine's own recurring
        RAM-pressure issue this session - 12GB total, measured down to 0.66GB free mid-
        session, unrelated to the production server spec) blocked a full container
        rebuild; then for real end-to-end once the stack was rebuilt and stable - full
        PASS, all four phases, `readyz` confirmed reporting `degraded`
        with `redis: unavailable` (not a hang) and recovering to `ok` within 30s of
        restart. Full backend pytest suite green (405 passed, 30 skipped; one argon2
        `HashingError: Memory allocation error` seen during the same RAM-pressure window
        - confirmed a transient environment condition, not a code issue, on immediate
        retry). `ruff check backend scripts` clean.
- [x] Backup automation + restore-exercise scripts (MongoDB is no longer part of this
      stack, CLARIFICATIONS #19/#20 - Postgres and MinIO are what actually needs backing
      up now)
  - [x] `scripts/backup.py`: a real `pg_dump -Fc` taken *inside* the postgres container
        (`docker compose exec`, no host-side `pg_dump` binary, no version-skew risk
        between what took the dump and what would restore it), uploaded to the
        `csense-backups` bucket at the exact key layout SCH §14 already specifies
        (`{environment}/{store}/{date}/{artifact}`) - that bucket has existed in
        `csense_shared.storage.objects.ALL_BUCKETS` since MinIO was first wired up,
        unused until this script. Also builds a real MinIO object inventory manifest
        (key/size/ETag for every object in every other bucket) - **not** "MinIO backed up
        into itself" (copying within the same instance protects against nothing a real
        outage would take out); the manifest is the honest scope for a local exercise -
        real off-site replication needs a genuine second storage target this deployment
        does not have, named rather than faked.
  - [x] `scripts/restore_exercise.py`: finds the *latest* real backup, downloads it, and
        actually restores it (`pg_restore`) into a throwaway database on the same
        server - the real database is never touched, only read from for comparison. Row
        counts across five representative tables (organizations, tenants, users,
        cameras, incidents) are compared between source and restored database for real,
        not asserted from the dump's own metadata. A backup nobody has ever restored is
        a belief, not a backup - this is what actually proves the mechanism.
  - [x] Run for real against the real stack: **1,428,376-byte dump** (sha256 recorded),
        MinIO inventory of **451 evidence objects / 15 model objects / 573MB** across
        the live buckets, and a full restore-and-compare that matched on every table
        (3124/3079/825/2406/908 rows respectively) on the first real run. `ruff check
        scripts` clean.
- [!] Real capacity/SLO validation — needs real traffic; only synthetic benchmarks
      possible locally

## Phase 9 — Production Candidate and Wave Rollout

- [~] **2026-09-11: real production deploy to `103.118.158.92` (domain `3rdi.in`),
      genuinely done and verified - not just "the containers are running."**
  - [x] **Found the target server already ran a different, abandoned CSense deployment**
        (`csense-ai-engine`/`csense-frame-ingestor`/`csense-stream-mgr`/`csense-tenant-crm`
        - none of these service names match this codebase's actual architecture; 2 of its
        containers were already `unhealthy`). Confirmed with the owner before touching
        anything (not assumed) that it was safe to replace. **Backed up its data volumes
        in full before deleting anything** (`pgdata`/`redisdata`/`miniodata`/`mongodata` -
        the last one alone 17GB - tarred to `/home/ubuntu/old-csense-deployment-backup-
        2026-09-11/` on the server, `gzip -t` integrity-verified on all 4 before the
        originals were removed), per the owner's own explicit call to back up rather than
        delete outright.
  - [x] **The server is shared, not dedicated** - ~20 unrelated real client domains
        already running behind a system nginx (certbot-managed certs) that owns host
        ports 80/443. `docker-compose.prod.yml`'s own Traefik wants those same ports and
        its own Let's Encrypt HTTP-01 challenge - would have taken every other site on the
        box down. Built a real, documented, reusable second deployment path for exactly
        this shape (`infra/docker-compose.prod.behind-proxy.yml` +
        `infra/traefik/dynamic.prod.behind-proxy.yml` - see
        `docs/10_PRODUCTION_DEPLOYMENT_GUIDE.md` B.5's own new section). Found and fixed a
        real bug in the override before trusting it: Compose's list-merge *appends* to a
        base service's `ports`/`command`/`volumes` rather than replacing them - a first
        attempt left `0.0.0.0:80`/`:443` bound alongside the new internal-only
        `127.0.0.1:18080`, exactly the collision this whole path exists to avoid; fixed
        with the `!override` YAML tag on each of the three keys, confirmed against the
        real merged `docker compose config` output, not assumed.
  - [x] **A real secret-handling incident happened and was disclosed immediately, not
        buried**: an early, unredacted `docker compose config` dry-run (testing the
        override above) printed this *local dev* `.env`'s fully-resolved secrets into a
        tool-output transcript - a real Resend API key, WhatsApp gateway key/instance
        token, Cloudflare R2 credentials, and a GitHub PAT. Flagged to the owner
        immediately with a rotation recommendation for every value shown; owner took
        ownership of rotating them. Every `docker compose config` call after this point
        was piped through a Python/YAML filter that only ever prints the one service
        section actually being checked, never the full resolved output.
  - [x] **TLS**: no certbot needed - `3rdi.in`'s DNS is proxied through Cloudflare, which
        already terminates HTTPS at the edge (confirmed empirically: `curl https://app.
        3rdi.in` connects and gets a real response with no cert error, while the origin
        nginx has no `:443` listener for that host at all - Cloudflare is in "Flexible" or
        equivalent mode, edge HTTPS to a plain-HTTP origin). Origin `.env`/secrets are
        still freshly generated per B.2, never copied from dev.
  - [x] **Real bug found and fixed via an actual failed login, not by inspection**: the
        deployment guide's own documented `chmod 600` on `jwt_private.pem`/`master_v1.key`
        made them unreadable by the container's own unprivileged `csense` user (Compose's
        non-Swarm `secrets:` bind-mounts preserve the *host* file's uid/gid/mode exactly,
        not Swarm's `root:root 0444` normalization) - the first real admin login attempt
        failed with an actual `PermissionError` reading the key, not a hypothetical.
        Fixed (`chmod 644`) and the deployment guide itself corrected (B.2) so the next
        real deployment doesn't hit the same thing.
  - [x] **Migrations applied for real** against a genuinely fresh database (all 53+
        revisions, `bootstrap_roles.py` + `alembic upgrade head` via the real `migrate`
        one-shot service, re-run a second time to confirm idempotency - exit 0, no-op).
  - [x] **First platform admin bootstrapped for real** (`sara@reainmaker.ai`, per
        Operations Manual §A.2 - direct SQL insert, Argon2id-hashed, no self-service
        path). **Verified with a real login, not assumed**: real JWT issued, decoded and
        confirmed 19 real `platform_admin` permission claims in it, then used that real
        token against a real protected endpoint (`GET /api/v1/admin/organizations` -> `200
        []`, a genuinely empty fresh database, not an error). MFA enrollment for this
        account is the one B.6 step **not yet done** - named here rather than silently
        skipped.
  - [x] **Full stack verified healthy and serving real content**, not just "containers
        running": all core services `Up`/`healthy`; `whatsapp-gateway` deliberately
        stopped (crash-looping on missing `WHATSAPP_GATEWAY_API_KEY`/
        `WHATSAPP_INSTANCE_TOKEN` - real production vendor credentials, not something to
        generate, left for the owner same as `RESEND_API_KEY`). Verified real HTML/API
        responses at the origin (`curl -H "Host: app.3rdi.in" http://127.0.0.1/` -> the
        real customer-crm `index.html`; same confirmed for `console.`/`demo.`/apex) for
        all 4 routed hosts before ever trusting the public domain layer.
  - [ ] **Two real gaps found at the DNS/edge layer, outside what SSH access can fix**:
        (1) `console.3rdi.in`/`demo.3rdi.in`/the apex `3rdi.in`/`storage.3rdi.in` don't
        resolve publicly at all right now (checked directly against Cloudflare's own
        resolver, `1.1.1.1`) despite appearing as "current records" in a screenshot of the
        DNS panel - only `app.3rdi.in`/`api.3rdi.in` actually resolve. (2) `app.3rdi.in`'s
        root path (`/`) is intercepted and answered with a static JSON 404 *by Cloudflare
        itself*, before ever reaching this deployment - confirmed by the origin serving
        the real app correctly on the exact same path when reached directly, so this is
        not a build or routing bug on this end. Both need the owner's Cloudflare dashboard
        access (a stale Worker/Page Rule from an earlier attempt is the likely cause for
        the second one) - not something achievable from server SSH access alone.
  - [x] **2026-09-12: the real legacy model estate imported into this production
        instance for real, not just discovered.** The owner pointed at `csense.airivu.ai`/
        `demo.airivu.ai` as the two real sources to check - `demo.airivu.ai` turned out to
        resolve to a completely different, unrelated external host (`18.139.179.249`, no
        access), but `csense.airivu.ai` led to a **third real deployment on this same
        server**, distinct from the abandoned Docker one already replaced above: a raw
        Python process at `/var/www/csense/backend` (started well before this session,
        genuinely still serving real traffic on port 8005) - the actual original legacy
        system `legacy_model_manifest.py` was already built against. All 14 real model
        files there were re-verified byte-for-byte against that manifest's own recorded
        sha256 (all 14 matched exactly - nothing guessed, nothing re-derived) and staged
        for the real `import_legacy_models.py` operator script.
    - [x] **Found and fixed a real bug via the actual import run, not by inspection**: the
          production `.env`'s `MINIO_USE_TLS=true` (set for `storage.3rdi.in`'s real
          public HTTPS) also governed the *internal* `minio:9000` connection, which has no
          TLS at all - `ensure_buckets()`'s first real call failed with a genuine
          `SSL: WRONG_VERSION_NUMBER`. The code only ever had one shared flag for two
          connections with two different real TLS postures - fixed properly, not patched
          around: `minio_public_use_tls` (`csense_shared/config.py`) now governs
          `create_presign_client()` independently of `minio_use_tls` (defaults to the old
          shared behavior when unset, so no existing deployment needs a config change).
          Rebuilt every backend image (the shared lib change reaches all of them), full
          targeted test suite still green, no regressions.
    - [x] **Verified end to end after the fix, not just "import exited 0"**: all 14
          models registered (`SELECT ... FROM model_versions` - 9 `standard`/`validated`,
          5 InsightFace `biometric`/`revoked`, matching the manifest exactly), a real
          presigned URL generated for one of them confirmed `https://storage.3rdi.in/...`
          (not `http://` or the internal `minio:9000` name), and that exact URL fetched
          for real over the public internet -> `200`, real file bytes - the complete
          presign -> Cloudflare -> nginx -> Traefik -> MinIO chain, not assumed from the
          config alone.
    - [x] **A second real secret surfaced while investigating, disclosed the same way as
          the first**: `/var/www/csense/backend/.env.production` (the third deployment's
          own config) has a real MongoDB password in plain text
          (`MONGO_URL=mongodb://csense_admin:***@127.0.0.1:27017/...`) - only local
          (`127.0.0.1`, not reachable off-host) and belongs to the old legacy system, not
          this platform, but shown in a tool-output transcript regardless. Flagged to the
          owner; not rotated by this session (not this platform's credential to rotate).
  - [ ] **Not done yet, named rather than assumed complete**: MFA enrollment for the first
        admin (B.6); `backup.py`/`restore_exercise.py` run against this real production
        database (B.7, "confirm restores work before you ever need them for real"); a real
        smoke test via `e2e_vertical_slice.py` against the production URL; log shipping to
        an aggregator; the 2 real pilot customers (Abelamm/Autotek, Apti Services)
        migrated from local dev into this production instance - it is a genuinely fresh,
        empty database right now, by design (never copy dev data into production), not an
        oversight; `RESEND_API_KEY`/WhatsApp credentials (real vendor accounts, not
        something to generate); a named pilot tenant and cutover window remain, as always,
        the owner's own business decision (Phase 9's original scope, unchanged).

## Phase 10 — Stabilization and Handover

- [x] Admin manual, operator manual, API guide, incident runbooks
  - [x] `docs/07_OPERATIONS_MANUAL.md` — Part A (Admin): bootstrapping the first platform
        admin (there's no self-service path to platform scope, by design), organizations/
        tenants/reseller relationships, licensing (issuance/renewal/what `expired`
        actually restricts), support grants, the model/pipeline registry's own real
        promotion gate, audit log. Part B (Operator): topology table, starting/stopping
        the stack, `/healthz`/`/readyz` (and why they're deliberately not routed through
        Traefik), structured logs + `X-Correlation-ID` tracing, migrations, backup/
        restore (`scripts/backup.py`/`restore_exercise.py`, with the real numbers from
        this session's own runs), load/failure-injection testing, security scanning,
        scaling notes (`notification-worker`'s `FOR UPDATE SKIP LOCKED` design is
        genuinely multi-replica-safe), and common maintenance tasks (key rotation, stale
        Redis keys, the Docker-Desktop-crash recovery procedure this session hit
        repeatedly, including the `.wslconfig` memory-cap suggestion).
  - [x] `docs/08_API_GUIDE.md` — the two APIs and their audiences, human-session and
        API-key authentication (including the real "scopes capped at issuance" rule and
        rate-limit/usage-metering behavior), permissions/deny-wins, the real error shape,
        pagination, webhook signing/verification with a real code-shaped example,
        idempotency keys, real-time via WS tickets. Points at the real, live, auto-
        generated OpenAPI documents (`/api/v1/tenant/docs`, `/api/v1/admin/docs`) as the
        endpoint-by-endpoint reference rather than duplicating it by hand.
  - [x] `docs/09_INCIDENT_RUNBOOKS.md` — ten numbered runbooks (service down; Postgres
        unreachable; Redis unreachable, with the exact "what breaks vs. what keeps
        working" list this session's own failure-injection testing established; a
        tenant's camera fleet going offline/degraded, using the real `check_name`
        classification; license/quota state looking wrong; suspected credential
        compromise, ordered by blast radius from a single API key up to a compromised
        JWT signing key; a failed migration; MinIO/storage issues; notification delivery
        failures; and a real disaster-recovery restore, escalating the same drill
        `restore_exercise.py` already proves works). Each follows Detect/Diagnose/
        Mitigate/Resolve/Prevent with real commands, not placeholders.
  - [x] **Beyond the checklist's own four items**: `docs/10_PRODUCTION_DEPLOYMENT_GUIDE.md`
        plus `infra/docker-compose.prod.yml` and `infra/traefik/dynamic.prod.yml` — a
        real, validated (`docker compose config`, both the required-var fail-fast
        behavior and full successful resolution confirmed) production deployment
        mechanism: TLS via Traefik + Let's Encrypt HTTP-01, no host-published database/
        cache/storage ports, `restart` policies + resource limits + capped log rotation
        on every service, secrets-generation and domain-placeholder-replacement steps,
        and a real go-live checklist. Explicit, tabulated about what remains a genuine
        human/business decision this repo cannot make (which cloud host, real domain
        ownership, DNS, a container registry, vendor accounts, the pilot go/no-go itself
        - Phase 9's own `[NEEDS HUMAN/EXTERNAL INPUT]`) versus what the mechanism now
        handles for real once those are supplied.
  - [x] `docs/00_DOCUMENT_INDEX.md` updated to list all four as a real second half of the
        documentation pack.
- [!] Pilot defect resolution, false-positive tuning — needs real pilot data first

## Phase 11 — Mobile App (Expo/React Native)

Not part of the original six-document spec pack (`docs/00_DOCUMENT_INDEX.md`'s own
architecture baseline names only the Developer Console/Next.js and Customer CRM/Vite web
apps) - added as a direct, explicit request. **Pivoted mid-build**: the first pass was a
proprietary native Android app (Kotlin + Jetpack Compose - real, compiled, 12/12 unit
tests passing, preserved in git history under the commit "Native Android app: login,
dashboard, incidents, cameras, account"), but once it turned out real Expo tooling was
already available, the user explicitly redirected both platforms to one shared Expo/React
Native codebase instead - a deliberate, user-confirmed decision (asked and answered via
AskUserQuestion), not an assumption. `mobile/android/` and the empty `mobile/ios/`
scaffold were deleted; the real backend-contract reverse-engineering and lessons from that
first pass (cookie-based refresh, exact DTO shapes) carried forward into the rewrite.

- [x] **`mobile/app/`** (Expo SDK 57, React 19.2.3, React Native 0.86.3, TypeScript 6.0.3,
      Expo Router, bundle/package id `ai.airivu.csense` both platforms) - login, dashboard,
      incident list (cursor-paginated, status-filterable, `FlatList` infinite scroll) and
      detail (real acknowledge/investigate/resolve/dismiss actions with the same
      resolution-code vocabulary as the web CRM), camera list, account screen (license +
      quota usage, sign out). Auth-gated via Expo Router's `Stack.Protected`.
  - [x] **The real backend contract, reverse-engineered field-for-field**, not guessed:
        every DTO in `src/api/types.ts` mirrors a real Pydantic model from
        `backend/tenant_api/app/api/*.py` - the same contract the native Android build
        already established. Same two real contract details carried forward: the refresh
        token is never in a JSON body (httpOnly `csense_refresh` cookie, path-scoped to
        `/api/v1/auth`) - this app relies on the platform's own native cookie jar
        (NSURLSession/OkHttp) via `fetch()`, the same way the web CRM relies on the
        browser's; and logout needs a session id the JSON never provides, read from the
        `csense_session` cookie via `@preeternal/react-native-cookie-manager`
        (`src/auth/cookies.ts` - the maintained replacement for the now-deprecated
        `@react-native-cookies/cookies`, chosen after comparing real npm metadata against
        the other suggested alternative).
  - [x] Auth: `src/api/client.ts`'s `apiRequest()` reacts to a real `401` by calling
        `/api/v1/auth/refresh` and retrying once, with an in-flight-refresh singleton so
        concurrent 401s across screens share one refresh call rather than racing (unit-
        tested directly, see below) - the same guarantee `SessionAuthenticator` gave the
        native Android build, re-expressed for `fetch()`.
  - [x] Every real backend error (`csense_shared.errors.ProblemResponse`,
        `docs/08_API_GUIDE.md`'s own "Error shape") is parsed into a typed `ApiError`
        (`code`/`httpStatusCode`/`retryable` preserved), with a separate `NetworkError`
        for a request that never reached the server at all.
  - [x] Theme colours (`src/theme/colors.ts`) copied hex-for-hex from
        `frontend/customer-crm/src/styles.css`'s own light/dark palette, so severity and
        status mean the same colour on both clients; badges carry the label as visible
        text plus an `accessibilityLabel` prefix (`"Severity: "`/`"Status: "`), mirroring
        the web CRM's own colour-blind-safe design.
  - [x] **Real verification, boundary named honestly where it stops**: `npm run lint`
        (eslint-config-expo + react-hooks, zero errors), `npm run typecheck`
        (`tsc --noEmit`, zero errors), and **17 real Jest unit tests, all passing** -
        the `ApiError`/`NetworkError` mapping and 401-refresh-and-retry contract
        (including the concurrent-401 dedup case), login/logout state transitions in
        `AuthContext`, and cursor-pagination/status-filter-reset behavior in
        `useIncidents`. CI (`.github/workflows/ci.yml`'s `mobile-app` job) runs the same
        three commands on every push. No native build step exists in CI (no Xcode on a
        Linux runner), so this still isn't exercised on every push - but it **has** now
        been run for real on a native iOS Simulator build once, on 2026-09-14 (see the
        dated entry below): built from source (prebuild + CocoaPods + xcodebuild, 0
        errors), installed, and launched, with a real screenshot of the login screen
        working end to end. The native-cookie-jar reliance itself is still unexercised
        against a real backend on a real device - the Simulator run only proves the app
        launches and the theme renders, not a login round-trip (see that entry for why).
  - [x] One Windows/npm-specific real finding carried into the README: `expo-secure-store`
        and `@preeternal/react-native-cookie-manager` both needed
        `npm install --legacy-peer-deps` to resolve Expo SDK 57's own peer-dependency
        graph (transitive conflicts via `expo-router`'s `@expo/ui`/`react-native-worklets`
        dependencies) - `npx expo install` itself doesn't accept that flag, so each was
        installed via plain `npm install` and then corrected to the SDK-compatible
        version with `npx expo install --fix`.
- [x] iOS is no longer blocked - the whole point of the Expo pivot (over the earlier
      native-per-platform plan) is one codebase for both platforms, removing the earlier
      "needs a Mac for Xcode" blocker for the app's *code*. A real signed iOS binary via
      EAS Build (or a local Xcode archive) still needs the Mac the user has, and hasn't
      been done in this session - named in `mobile/app/README.md`, not silently claimed.

- [x] **2026-09-14: "Technical Atmosphere" theme applied to `frontend/customer-crm`**,
      implementing a previously-designed Claude Design canvas mockup
      (`design/csense-ui/System.dc.html`, `Main.dc.html`) into the real codebase for the
      first time, rather than leaving it as a design artifact:
  - [x] **Token swap, not a rewrite** - the design system's own stated philosophy.
        `src/styles.css`'s `:root` block gained the new primitive tokens (`--canvas
        #241C21`, `--well #1A1418`, `--raised #2A2026`, `--hair #362A31`, `--emerald
        #10B981`, `--rose #FF8ABB`, `--crimson #98134E`, `--ink`/`--dim`/`--faint`), with
        every existing semantic token (`--bg`, `--surface`, `--accent`, `--critical`, ...)
        remapped to reference them - so the ~20 existing pages needed no per-page edits.
        Removed the `@media (prefers-color-scheme: dark)` split entirely: this palette is
        the product's own identity, not a dark-mode variant, the same decision made for
        the mobile app below. Space Grotesk + JetBrains Mono loaded via Google Fonts.
        `button.primary` switched from `var(--accent)` to `var(--crimson)` fill - "crimson
        is a fill, never body text," the same rule the mobile app's primary buttons
        follow (see below).
  - [x] New `src/components/Icons.tsx` (22 icons ported verbatim from
        `System.dc.html`'s SVG paths, plus 3 extrapolated in the same style for pages the
        original 24-icon sheet didn't cover) and a rewritten `Layout.tsx`: the exact
        grouped-sidebar shell from `Main.dc.html` (Operations/Infrastructure/
        Intelligence/Alerting/Administration groups, 232px sidebar, LIVE-indicator top
        bar), replacing the old flat top-nav header.
  - [x] **Verified against a real authenticated session, not simulated**: the Vite dev
        server run directly (`./node_modules/.bin/vite`, bypassing an `npm`-on-PATH
        flakiness this session hit repeatedly), driven with a real Playwright script
        logging in as the real Abelamm pilot-tenant account and screenshotting the
        rendered login page and dashboard - not a static render or a design-tool preview.
  - [~] **Page-specific layouts not yet done**: the Incidents page's card treatment
        (evidence-thumbnail scan-sweep, severity chip, sparkline), the Dashboard
        telemetry rail, and the full Incident Detail page still use their pre-redesign
        layouts under the new tokens - named explicitly rather than silently left
        looking "redesigned" when only the shell and colours changed.

- [x] **2026-09-14: "Technical Atmosphere" theme applied to the mobile app** (same
      token-swap approach used for `frontend/customer-crm` directly above), plus the
      app's **first-ever real native build and Simulator run**,
      which surfaced two real, pre-existing bugs neither typecheck nor lint could catch:
  - [x] `src/theme/colors.ts` rewritten to the single dark palette (canvas `#241C21`,
        crimson `#98134E`, rose `#FF8ABB`, emerald `#10B981`) instead of the old light/dark
        split - `useColors()` kept as a hook so no call site needed to change. Primary
        action buttons (`login.tsx`'s submit, the incident detail screen's
        acknowledge/resolve/escalate actions) switched from the rose accent to crimson
        fill, matching the same "crimson is a fill, never body text" rule already applied
        to the web CRM's `button.primary`. New `src/components/Icons.tsx` ports the
        product's `react-native-svg` icon set for the tab bar and account screen.
  - [x] **Built and ran for real on a booted iOS 26 Simulator** (prebuild + CocoaPods +
        xcodebuild, 0 errors) - not Expo web (tried first and ruled out: this app's
        session handling relies on native cookie jars with no web equivalent, by design -
        `src/auth/cookies.ts`'s own comment), and not simulated by inspection. Found and
        fixed two real bugs this surfaced, both pre-existing (present before this
        session's theme changes, just never discovered because the app had never been
        run on a device):
    1. `expo-modules-core@57.0.14`'s native WorkletsAdapter is compiled against
       `react-native-worklets ^0.7-0.10`, but expo-router's own transitive
       `react-native-reanimated@4.6.0` pulls in worklets `0.12.x` - a real upstream
       version mismatch (`executeSync` doesn't exist in that worklets version), not
       anything this session's own dependency changes introduced. Neither package is used
       by this app's own code (pulled in only for expo-router's optional Drawer support);
       fixed via a `package.json` `overrides` pin to `reanimated@4.5.0`, whose own peer
       range (`worklets 0.10.x`) is what `expo-modules-core` actually supports, rather
       than forcing a mismatched pair directly.
    2. No route matched bare `/` - the root `_layout.tsx`'s `Stack` only names `(tabs)`
       and `login`, each gated by `Stack.Protected`, neither of which matches an empty
       path. Cold-launching the app rendered expo-router's own "Unmatched Route" screen
       instead of the app. Fixed with a new `app/index.tsx` that redirects to `/login` or
       `/(tabs)/dashboard` based on the same `isLoggedIn` state `_layout.tsx` already
       gates on - **an unconditional redirect to `/(tabs)/dashboard` was tried first and
       silently hung the app**: `Stack.Protected` removes the guarded-out branch from the
       navigator rather than falling back to the visible one, so redirecting to a route
       the guard hides loops forever instead of erroring. Worth knowing if this pattern
       (a root index redirecting into a `Stack.Protected`-gated tree) comes up again.
  - [x] Verified: `npm run typecheck` and `npm run lint` both clean, all 17 existing Jest
        tests still pass, and a real screenshot of the compiled app's login screen (sent
        to the user) shows the theme rendering correctly on device - dark canvas, crimson
        button, correct type.
  - [~] **Stops at the login screen.** No touch-input automation is available in this
        sandbox (no `idb`; `cliclick` installs but has no Accessibility permission to
        grant non-interactively, and the sandbox can't walk through the macOS permission
        prompt), so the post-login screens (dashboard/incidents/cameras/account) are
        verified by typecheck + code review only - the same standard as before this
        entry, not by a real authenticated screenshot the way the login screen and the
        web CRM were. A real device or a Mac with Accessibility permission granted to
        whatever drives it would close this gap.

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
