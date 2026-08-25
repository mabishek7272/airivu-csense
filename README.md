# AIRIVU CSense / 3RDi

Enterprise multi-tenant AI video analytics platform. This repo is being built from the
specification pack in [docs/](docs/) — start there for product/architecture context, and
see [CHECKLIST.md](CHECKLIST.md) for what's built vs. pending, and
[CLARIFICATIONS.md](CLARIFICATIONS.md) for open decisions.

## Repository layout

```
docs/            Specification pack (PRD, TRD, flows, UX brief, schema, delivery plan)
backend/
  shared/        Internal library: config, tenant-aware DB access, security, audit/outbox
  tenant_api/    FastAPI service — customer-facing, audience `csense-customer`
  admin_api/     FastAPI service — platform-facing, audience `csense-platform`
  migrations/    Alembic migrations (PostgreSQL schema, RLS policies, seed reference data)
  tests/         Unit + integration tests (pytest)
frontend/
  customer-crm/       React 18 + Vite — tenant operations UI
  developer-console/  Next.js 14 — platform operations UI
edge/            Edge agent (Phase 3+, not yet implemented)
infra/           Docker Compose stack, Traefik, MediaMTX config
```

## Prerequisites

- Docker Desktop (with Compose v2)
- OpenSSL (for local secret generation — Git Bash on Windows already has this)
- Node.js 20+ and Python 3.12+ only if you want to run frontend/backend outside Docker

## First run

```bash
# 1. Generate local dev secrets (.env + JWT keypair) — never commit these
bash scripts/generate_dev_secrets.sh

# 2. Bring up the stack
cd infra
docker compose --env-file ../.env up -d --build

# 3. Bootstrap the restricted DB roles and apply migrations (the migrate service does both)
docker compose --env-file ../.env run --rm migrate

# 4. (optional) Seed local smoke-test accounts — prints generated passwords once
docker compose --env-file ../.env run --rm migrate python seed_dev_data.py
```

Then:

- Customer CRM: http://localhost:8080 (routed via Traefik; `Host: app.localhost`) —
  visit `http://app.localhost:8080` directly, or use `http://localhost:5173` if running
  the Vite dev server outside Docker.
- Developer Console: `http://console.localhost:8080`, or `http://localhost:3000` in dev.
- Traefik dashboard (dev only): http://localhost:8081
- MinIO console: http://localhost:9001
- Tenant API docs: `http://localhost:8080/api/v1/tenant/docs`
- Admin API docs: `http://localhost:8080/api/v1/admin/docs`

## Running tests

```bash
cd backend
pip install ./shared -r tests/requirements.txt
pytest                                    # unit tests only (no infra needed)
```

The tenant-isolation tests additionally need a migrated Postgres and all three database
identities, because they verify what each role *cannot* do:

```bash
export TEST_POSTGRES_DSN="host=localhost port=5432 dbname=csense user=csense_app password=$POSTGRES_PASSWORD"
export TEST_POSTGRES_API_DSN="host=localhost port=5432 dbname=csense user=csense_api password=$POSTGRES_API_PASSWORD"
export TEST_POSTGRES_PLATFORM_DSN="host=localhost port=5432 dbname=csense user=csense_platform_api password=$POSTGRES_PLATFORM_API_PASSWORD"
pytest
```

## Database identities

Three separate Postgres roles, because PostgreSQL superusers bypass row-level security
entirely — connecting the APIs as the owner would make every tenant-isolation policy
silently inert:

| Role | Used by | Can bypass RLS? |
|---|---|---|
| `csense_app` (owner) | migrations / DDL only | yes (superuser) — never used by a running service |
| `csense_api` | Tenant API | **no** — not in the `csense_platform` group, so it cannot read across tenants even if it sets `app.is_platform` itself |
| `csense_platform_api` | Admin API | only when it explicitly opts in per transaction, *and* by virtue of group membership |

`backend/migrations/bootstrap_roles.py` creates these and refuses to proceed if any of
them could bypass RLS. The properties above are enforced by tests in
[backend/tests/test_tenant_isolation.py](backend/tests/test_tenant_isolation.py).

## Security notes for local development

- `.env` and `infra/secrets/` are git-ignored. Never commit them.
- The stack runs over plain HTTP on `localhost` for convenience. Production deployment
  requires TLS termination at Traefik, `Secure` cookies, and a managed secret store —
  none of that is wired up here (see CHECKLIST.md Phase 8/9).
- The demo accounts created by `seed_dev_data.py` are for local smoke testing only.

## Where this stands

This is a from-scratch build executed against the spec pack, phase by phase, tracked in
[CHECKLIST.md](CHECKLIST.md). Phases 0–1 (engineering foundation: tenant-isolated data
model, auth, both API skeletons, both frontend shells, CI) are implemented and covered by
tests. Later phases build on this foundation in order — see the checklist for exact scope
and for the handful of items that need a real human/business decision or external
resource (cloud account, contracted providers, real camera hardware, a pilot customer) to
go further.
