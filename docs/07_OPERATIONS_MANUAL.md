# 07 — Operations Manual (Admin + Operator)

**Version:** 1.0
**Status:** Living document, updated as the platform changes
**Audience:** Platform administrators (business/support operations) and infrastructure
operators (the people who run `docker compose`, watch logs, and get paged)

This document is split into two halves. The **Admin Manual** covers the platform-level
business operations a platform admin performs through the Developer Console / Admin API
— tenants, licenses, support access, the model and pipeline registries. The **Operator
Manual** covers running the infrastructure itself — starting/stopping the stack, backups,
migrations, scaling, and where to look when something is wrong.

Every procedure here is real: it names the actual endpoint, script, or command this
codebase already has, not a placeholder for one that doesn't exist yet.

---

## Part A — Admin Manual

### A.1 Roles and where they operate

| Role | Audience | Where | Typical work |
|---|---|---|---|
| `platform_admin` | `platform` | Developer Console (`console.localhost` / your admin domain) | Everything below |
| `tenant_owner` | `customer` | Customer CRM (`app.localhost` / your customer domain) | Runs their own tenant — team, cameras, sites, rules, license view, webhooks, API keys |
| `tenant_member` | `customer` | Customer CRM | Day-to-day incident work; narrower permission set than `tenant_owner` |

A platform admin never has implicit access to a tenant's own data. The one exception is
the **support grant** mechanism (A.5) — a time-boxed, audited, tenant-approved exception,
not a standing backdoor.

### A.2 Bootstrapping the first platform admin

There is no UI for creating the very first platform account — by design, since nothing
should be able to self-elevate to platform scope. Create it directly against Postgres,
the same way `scripts/e2e_licensing.py`'s own `bootstrap_platform_admin()` does for test
purposes:

```sql
-- Run via: docker compose exec -T postgres psql -U csense_app -d csense
INSERT INTO users (email_normalized, email_display, password_hash, status, display_name)
VALUES ('you@yourcompany.com', 'you@yourcompany.com', '<argon2id hash>', 'active', 'Your Name')
RETURNING id;

INSERT INTO platform_developers (user_id, status) VALUES ('<user id above>', 'active')
RETURNING id;

INSERT INTO platform_role_assignments (platform_developer_id, role_id, status)
SELECT '<developer id above>', id, 'active'
FROM roles WHERE tenant_id IS NULL AND name = 'platform_admin' AND audience = 'platform';
```

Generate the password hash with the same Argon2id parameters the app uses
(`csense_shared.security.passwords.hash_password`) — do not hand-roll a different scheme.
A one-line way, from the `backend/` directory with the venv active:

```
python -c "from csense_shared.security.passwords import hash_password; from csense_shared.config import get_settings; print(hash_password('a-real-strong-password', get_settings()))"
```

Every platform admin after this first one is created the normal way — invited or
provisioned through whatever internal process you set up around this same table
structure (`platform_developers` / `platform_role_assignments`). Immediately enroll MFA
for this account (`POST /api/v1/admin/auth/mfa/totp/enroll` then `/confirm`) — several
high-risk actions below require a recent step-up verification and will refuse without it.

### A.3 Organizations, tenants, and reseller relationships

- `GET/POST /api/v1/admin/organizations` — the commercial entity behind a tenant.
- Reseller/child-tenant relationships (`organization_relationships`) let a reseller
  organization see and act on its own children's tenants without platform-wide access —
  see `backend/admin_api/app/api/organizations.py` and `scripts/e2e_reseller.py` for the
  full shape.
- A tenant's own `GET /api/v1/tenant/*` surface is where the tenant manages itself; the
  admin side (`GET /api/v1/admin/*`) is for platform/reseller oversight, not day-to-day
  tenant operation.

### A.4 Licensing

Permission: `license.manage` (issuance/renewal), `license.read` (viewing).
**Issuance and renewal both require a recent MFA step-up** (TRD-SEC-010) — call
`POST /api/v1/admin/auth/mfa/verify` with a fresh TOTP code within a few minutes of the
action, or you'll get a `403 step_up_required`.

1. **Create a plan**: `POST /api/v1/admin/license-plans` — `code`, `name`,
   `license_type`, `billing_period`, and `default_entitlements` (a map of entitlement
   code → `{value_type, limit_numeric | enabled_boolean | value_json}`; the common shape
   is `{"camera.count": {"value_type": "limit_numeric", "limit_numeric": N}}`).
2. **Issue a license**: `POST /api/v1/admin/licenses` — `tenant_id`, `plan_code`,
   optional `expires_at` and `grace_days` (default 14 — the reduced-friction warning
   window before a lapsed license becomes a hard stop; see `[[license lifecycle
   design]]` in `CHECKLIST.md`'s own entry for the full reasoning). Refused (409) if the
   tenant already has an active-or-grace license — supersede via renewal, not a second
   issuance.
3. **Renew a license**: `POST /api/v1/admin/licenses/{id}/renew` — extends the *same*
   license row (so quota history is preserved) and restores `active` status from
   `grace`/`expired`/`suspended`. A `revoked` license cannot be renewed (409) — issue a
   new one instead; revocation is a deliberate terminal state.
4. **A lapsed license is visible, not hidden**: both the platform's own
   `GET /api/v1/admin/licenses` and the tenant's `GET /api/v1/tenant/license` show the
   real, freshly-computed status (`active`/`grace`/`expired`/`suspended`/`revoked`) —
   status transitions happen lazily on the next read of a given license, not via a
   background job (see `csense_shared.licensing.lifecycle`).
5. **What "expired" actually restricts**: currently wired into camera creation only
   (`cameras.py`'s `create_camera`, the one place quota enforcement already lives) — a
   `402 license_restricted`, distinct from `402 quota_exceeded`. `grace` does not
   restrict anything; only `expired`/`suspended`/`revoked` do.

### A.5 Support grants (temporary tenant access)

Permission: `support.request`/`support.approve`/`support.revoke` (platform side),
`support.access` (tenant side approves/denies).

1. A platform developer requests access: `POST /api/v1/admin/support-grants` with
   `tenant_id` and a `reason`.
2. The tenant owner sees it as a banner in the Customer CRM and approves or denies it.
3. Once approved, it's active until `expires_at` (or explicit revoke by either side) —
   surfaced via `GET .../support-grants` on both sides, and every state change is
   audited (`record_audit_and_outbox`).
4. **What this does not yet do**: actually elevate a platform developer's own access
   token to reach tenant data — the governance/audit lifecycle is real and complete; the
   *authorization* half (a live grant actually widening what a token can do) is a
   separate, larger change to the core auth layer, not built this pass. See
   `CHECKLIST.md`'s own note on this.

### A.6 Model and pipeline registry

Permission: `model.upload`/`model.promote`/`model.read`, `pipeline.manage`/
`pipeline.publish`/`pipeline.assign`/`pipeline.read`.

- Models move through `uploaded → validating → validated → production` (or `failed`,
  `retired`). **`validating → validated` now requires a passing `model_validation_runs`
  row on record** — there is no way to promote an unvalidated model through the API.
  `scripts/run_model_validation.py` is the reference harness that produces one for real,
  against a real golden dataset.
- Pipeline versions are immutable once created (SCH §8.4/§8.8) — a published version's
  `definition_json` cannot be repointed at different content; a new version is required
  for any change. `pipeline_assignments` binds a published version to a camera, with a
  real overlap constraint (one active assignment per camera/priority slot at a time).

### A.7 Audit log

Permission: `audit.read` (both audiences, in different scopes — a tenant sees its own,
a platform admin sees theirs plus, with `audit.export`, cross-tenant export).
`GET /api/v1/tenant/audit-events` (tenant scope) / the admin equivalent — every mutation
this platform makes goes through `record_audit_and_outbox`, so this is a complete,
append-only record, not a best-effort log. Filter by `action`, `target_type`, `outcome`,
`since`/`until`.

---

## Part B — Operator Manual

### B.1 Topology

| Service | Role | Reachable |
|---|---|---|
| `traefik` | Ingress, TLS termination, routing (`infra/traefik/dynamic.yml`) | `:80`/`:443` in prod, `:8080` locally |
| `tenant-api` | Customer-facing API (`/api/v1/tenant`, `/api/v1/auth`, `/ws/v1/tenant`) | Behind Traefik only |
| `admin-api` | Platform-facing API (`/api/v1/admin`) | Behind Traefik only |
| `notification-worker` | Drains the notification outbox (SKIP LOCKED, scale-safe) | Internal only |
| `ai-runtime` | Model inference | Internal only |
| `whatsapp-gateway` | WhatsApp Business API bridge | Internal only |
| `postgres` / `redis` / `minio` | System of record, cache/coordination, object storage | Internal (published to host in dev only) |
| `mediamtx` | RTSP/WebRTC/HLS media relay | `:8888`/`:8889` (WebRTC), `:8189/udp` |
| `customer-crm` / `developer-console` / `demo-site` | Frontends | Behind Traefik only |

Every backend service reads config from `.env` (via `csense_shared.config.Settings`,
`pydantic-settings`, `env_file=".env"`) and Docker secrets for the JWT keypair.

### B.2 Starting, stopping, and checking the stack

```bash
cd infra
docker compose --env-file ../.env up -d          # start everything
docker compose --env-file ../.env ps              # what's running
docker compose --env-file ../.env logs -f tenant-api   # follow one service's logs
docker compose --env-file ../.env down             # stop everything (data volumes persist)
```

Every backend service exposes `GET /healthz` (liveness — always `200` if the process is
up) and `GET /readyz` (readiness — checks real Postgres and Redis connectivity, reports
`{"status": "ok"|"degraded", "dependencies": {...}}`). Neither is routed through Traefik
(`infra/traefik/dynamic.yml` only routes `/api/v1/*` and media paths) — that's
deliberate, a health/readiness endpoint is for orchestration, not the public edge. Reach
it from inside the network:

```bash
docker compose --env-file ../.env exec -T tenant-api python -c \
  "import urllib.request as u; print(u.urlopen('http://localhost:8000/readyz', timeout=5).read().decode())"
```

Or configure your orchestrator's own healthcheck to call it directly by container DNS
name (`http://tenant-api:8000/healthz`), the way Docker Compose's own `depends_on:
condition: service_healthy` already does for Postgres/Redis/MinIO.

### B.3 Logs

All services log structured JSON (`csense_shared.logging.configure_logging`) —
`timestamp`, `severity`, `name`, `message`, plus request-scoped fields
(`correlation_id`, `path`, `code` for API errors). `docker compose logs <service>` is
the default access path locally; ship these to a real log aggregator in production (see
`10_PRODUCTION_DEPLOYMENT_GUIDE.md` — this is one of the externally-dependent choices,
not something this repo can pre-wire without knowing your target).

Every response carries `X-Correlation-ID` (generated if the caller didn't send one,
`csense_shared.middleware.CorrelationIdMiddleware`) — grep logs for it to trace one
request across services.

### B.4 Migrations

```bash
cd infra
docker compose --env-file ../.env build migrate     # after any migration file changes
docker compose --env-file ../.env run --rm migrate   # apply pending migrations
```

Check the current head:

```bash
docker compose --env-file ../.env exec -T postgres psql -U csense_app -d csense -tAc \
  "SELECT version_num FROM alembic_version;"
```

There are 49 migrations as of this writing (`backend/migrations/versions/`), all
forward-only in normal operation — a `downgrade()` exists on each for development
rollback, not as a production procedure (several are irreversible by nature, e.g. an
added enum value).

### B.5 Backup and restore

```bash
cd scripts
python backup.py             # real pg_dump + real MinIO object inventory manifest, both
                              # uploaded to the csense-backups bucket
python restore_exercise.py   # finds the LATEST backup, actually restores it into a
                              # throwaway database, and compares row counts against the
                              # real one - proves the backup works, not just that it exists
```

**Run `backup.py` on a real schedule** (cron / your orchestrator's own scheduled job
mechanism — nothing in this repo schedules it automatically, by design: a backup cadence
is an operational decision, not a code default). **Run `restore_exercise.py`
periodically too** (weekly is a reasonable starting cadence) — a backup nobody has ever
restored is a belief, not a backup. See `CHECKLIST.md`'s own entry for the real numbers
from the last local run (1.4MB dump, 573MB of model artifacts inventoried, full restore
match on 5 representative tables).

MinIO backup is an **inventory manifest**, not a full replica-into-itself (copying
within the same MinIO instance protects against nothing a real outage would take out) —
real off-site replication (`mc mirror` to a genuinely separate target, or your cloud
provider's own bucket replication) is a production-environment decision, covered in
`10_PRODUCTION_DEPLOYMENT_GUIDE.md`.

### B.6 Load, spike, endurance, and failure-injection testing

```bash
cd scripts && python load_test.py
```

Four phases against the real running stack: baseline load, a sudden concurrency spike,
sustained endurance (checks for latency drift over time), and a real failure injection
(stops the redis container, confirms `/readyz` degrades gracefully and recovers). Safe
to run against a staging environment; **do not run this against a live production
tenant's traffic** — it creates and deletes a real throwaway tenant and generates real
load, and the failure-injection phase deliberately stops a real dependency.

### B.7 Security scanning

- **CI** (`.github/workflows/ci.yml`): SAST (`bandit`), SCA (`pip-audit` + `npm audit`
  on both frontends), secret scanning (`gitleaks`, full history), and container/IaC
  scanning (Trivy) run on every push — currently non-blocking (findings are reported,
  not gated on; see Phase 8 in `CHECKLIST.md` for the exact baseline each one carries and
  why).
- **Local DAST**: `python scripts/dast_baseline.py` — OWASP ZAP against the real running
  `tenant-api`/`admin-api`, driven from their own live OpenAPI documents. Needs the
  `zaproxy/zap-stable` image (`docker pull zaproxy/zap-stable` once) and the stack
  running. Reports land in `reports/dast/` (gitignored).
- **What's out of scope for automation**: a real independent penetration test — flagged
  in `CHECKLIST.md` as needing a contracted third party.

### B.8 Scaling notes

- `tenant-api`/`admin-api` are stateless per request (auth is a bearer JWT, not a
  server-side session) — horizontally scalable by adding replicas behind Traefik with no
  code changes.
- `notification-worker` is already designed for multiple replicas — claiming uses
  `FOR UPDATE SKIP LOCKED`, so several instances draining the same queue is safe by
  construction, not something that needs a leader-election scheme bolted on.
- `postgres`/`redis`/`minio` are single-instance in this compose file — a production
  deployment should point these at managed/clustered equivalents rather than running
  them as single containers (see `10_PRODUCTION_DEPLOYMENT_GUIDE.md`).

### B.9 Common maintenance tasks

| Task | How |
|---|---|
| Rotate a JWT signing keypair | Generate a new RSA keypair, update the mounted secret files, restart `tenant-api`/`admin-api`. Existing tokens signed with the old key stop verifying at restart — plan a maintenance window or a dual-key rollover if zero-downtime rotation matters to you. |
| Rotate a Postgres/Redis/MinIO password | Update `.env`, recreate the affected containers. `POSTGRES_API_PASSWORD`/`POSTGRES_PLATFORM_API_PASSWORD` are the two application roles' own credentials — the top-level `POSTGRES_PASSWORD` is the bootstrap superuser (`csense_app`), used by migrations and admin scripts, not by the running API services. |
| Clear a stale rate limit / WS ticket / idempotency key | These are all plain Redis keys under `cs:{environment}:...` prefixes (SCH §13) — `redis-cli` against the running container, or let them expire naturally (everything in that keyspace carries a TTL). |
| Recover from a Docker Desktop / low-memory crash (local dev) | Restart Docker Desktop, then `docker compose --env-file ../.env up -d`. If this recurs often on a memory-constrained host, cap the WSL2 VM's memory via `.wslconfig` (`%UserProfile%\.wslconfig`, `[wsl2]\nmemory=6GB\nswap=4GB`) so it can't crowd out the rest of the system - a step this session recommended but left for the operator to apply, since it changes how much memory is available to everything else on the machine. |
