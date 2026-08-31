# 09 — Incident Runbooks

**Version:** 1.0
**Audience:** Whoever is on call for this platform's own infrastructure — these are
runbooks for operating CSense, not for the incident-management feature CSense itself
provides to its tenants.

Each runbook: **Detect** (how you'd notice this) → **Diagnose** (real commands, not
"investigate the issue") → **Mitigate** (stop the bleeding) → **Resolve** (fix root
cause) → **Prevent** (what to change so it doesn't recur). Every command references a
real script, endpoint, or table this codebase actually has.

---

## RB-1 — A backend service is down or crash-looping

**Detect**: `docker compose ps` shows a service not `Up`/`Healthy`; `/readyz` (B.2 in
the Operations Manual) returns non-200 or times out; Traefik starts returning 502s for
that service's routed paths.

**Diagnose**:
```bash
docker compose --env-file ../.env ps
docker compose --env-file ../.env logs --tail=200 <service>
```
Look for the last log line before it stopped — a clean shutdown logs
`{service}_stopped`; an unhandled crash won't. Check `unhandled_exception` entries
specifically (`csense_shared.errors.unhandled_exception_handler` logs these with a full
traceback server-side, even though the client only ever sees a generic 500).

**Mitigate**: `docker compose --env-file ../.env restart <service>`. For `tenant-api`/
`admin-api` (stateless), this is safe and immediate — no in-flight state to lose beyond
the requests actually in flight at restart time.

**Resolve**: root-cause from the traceback. Common categories already known in this
codebase: a bad migration state (RB-7), a dependency outage the service didn't handle
gracefully (RB-2/RB-3 — check whether `/readyz` reported `degraded` just before the
crash), or a real application bug (file a real fix, not a restart-and-hope).

**Prevent**: if it's a dependency-outage-triggered crash, verify the redis client
timeout fix (`csense_shared.db.redis.create_redis_client`) is actually deployed — a
Postgres or Redis outage should degrade `/readyz`, not crash the process. If it's a
resource-exhaustion crash, check `docker stats` and consider the resource limits in
`infra/docker-compose.prod.yml`.

---

## RB-2 — Postgres is unreachable or refusing connections

**Detect**: `/readyz` reports `"postgres": "unavailable"`; API calls return 500s with
`unhandled_exception` errors mentioning connection failures.

**Diagnose**:
```bash
docker compose --env-file ../.env exec -T postgres pg_isready -U csense_app -d csense
docker compose --env-file ../.env logs --tail=100 postgres
```
Check disk space on the host (a full disk is the single most common reason a database
container refuses new connections) and `docker stats postgres` for memory pressure.

**Mitigate**: if it's resource exhaustion, free the resource (disk/memory) and
`docker compose restart postgres`. If it's connection-limit exhaustion (many
long-running or leaked connections), check `SELECT count(*) FROM pg_stat_activity;` —
this codebase's own connection pooling (SQLAlchemy async engine, one pool per service)
should keep this bounded; a runaway count points at a real leak worth root-causing, not
just restarting past.

**Resolve**: once Postgres is back, confirm every dependent service recovers on its own
(`/readyz` returns to `ok`) — no manual reconnection step should be needed given the
engine's own pool reconnect behavior.

**Prevent**: this is exactly what `scripts/backup.py`/`restore_exercise.py` (Operations
Manual §B.5) exist for — if this outage involved actual data loss or corruption, that's
your recovery path, verified to work, not a first-time-under-pressure procedure.

---

## RB-3 — Redis is unreachable

**Detect**: `/readyz` reports `"redis": "unavailable"` with `"postgres": "ok"` — the
platform's own real failure-injection test (`scripts/load_test.py` phase 5) exercises
exactly this scenario and confirms the platform degrades gracefully rather than hanging.

**Diagnose**: `docker compose --env-file ../.env logs --tail=100 redis`;
`docker compose --env-file ../.env exec -T redis redis-cli ping`.

**Mitigate**: `docker compose --env-file ../.env restart redis`. **What breaks while
Redis is down** (know this before you need it under pressure): rate limiting on API
keys, WebSocket connection tickets, media session tokens, idempotency-key deduplication,
step-up-verification state, and the effective-config/entitlement cache. **What keeps
working**: everything that reads/writes Postgres directly — incidents, detections,
camera CRUD, licensing. A Redis outage degrades the platform; it does not take it down.

**Resolve**: confirm `/readyz` returns to `ok` within seconds of Redis coming back (the
real bound this session measured and fixed: previously an unbounded ~26s hang per
affected request, now a fast, clean fail-and-recover cycle — see `CHECKLIST.md`'s Phase
8 entry for the full story).

**Prevent**: if this recurs, check Redis's own persistence/eviction config and memory
limit — this platform treats Redis as a cache/coordination layer, not a system of
record, so data loss on a Redis restart is expected and tolerable, but a *crash-looping*
Redis is a resource or config problem worth root-causing.

---

## RB-4 — A tenant reports many cameras showing offline/degraded

**Detect**: a support ticket, or a spike in `camera_health_events` rows with
`status IN ('offline', 'degraded')` for one tenant over a short window.

**Diagnose**:
```sql
SELECT check_name, status, count(*), max(occurred_at)
FROM camera_health_events
WHERE tenant_id = '<tenant id>' AND occurred_at > now() - interval '1 hour'
GROUP BY check_name, status ORDER BY count(*) DESC;
```
`check_name = 'connectivity'` + `status = 'offline'` → the cameras genuinely aren't
answering (network/power/NVR issue on the customer's own site — not a platform problem).
`check_name = 'network'` → probes are answering but slowly (>3s round trip) — check
whether the tenant's own tunnel/VPN path is degraded (`edge_health_events` for the
relevant device tells the same story from the edge side).
`check_name = 'framerate'` → streams are up but reporting <5fps — an encoder/bandwidth
issue on the camera or NVR side, not connectivity.

**Mitigate**: nothing to mitigate platform-side if the cameras themselves are the
problem — this is a customer-facing "here's what we can see from here" conversation,
not a platform incident, *unless* the platform's own probe mechanism itself is failing
(check whether `POST /api/v1/tenant/cameras/{id}/probe` calls are erroring rather than
just reporting `offline` — a 5xx from the probe endpoint itself, as opposed to a
successful probe that discovered the camera is unreachable, is the platform's own bug).

**Resolve/Prevent**: obstruction and glare/night-vision detection are deliberately not
built yet (would need real frame decode — see `CHECKLIST.md`'s own reasoning) — if a
pattern of "camera reports online but the footage is unusable" recurs often enough to
matter, that's the signal to prioritize building it, backed by real customer evidence
rather than a guess at need.

---

## RB-5 — A tenant's license/quota state looks wrong

**Detect**: a tenant reports being unexpectedly blocked from creating a resource (a real
`402 license_restricted` or `402 quota_exceeded`), or reports their license status looks
stale/wrong.

**Diagnose**:
```
GET /api/v1/admin/licenses?tenant_id=<id>     # the real, freshly-synced status
```
Status is computed lazily on read (`csense_shared.licensing.lifecycle.
sync_license_status`) — if the tenant's own `GET /api/v1/tenant/license` was called
recently, the status shown is already current; there is no separate background process
to check for staleness.
```sql
SELECT quota_code, limit_value, reserved_value, consumed_value, period_end
FROM quota_ledgers WHERE tenant_id = '<tenant id>';
```
If `consumed_value` looks wrong relative to what the tenant actually has, that's a real
bug worth root-causing (every consuming write goes through the row-locked
`reserve_quota()` in the same transaction as the resource create — a mismatch means
something bypassed it, or a delete path isn't decrementing).

**Mitigate**: `POST /api/v1/admin/licenses/{id}/renew` restores `active` status and
extends the term (requires MFA step-up) if the real issue is simply "this license
lapsed and needs renewing." If it's a genuine quota-accounting bug, that needs a code
fix, not a manual data patch as the first resort — a hand-edited `quota_ledgers` row is
a last resort, logged and reasoned about explicitly if you ever have to do it.

**Resolve/Prevent**: see `CHECKLIST.md`'s licensing entries for the full design
reasoning (why grace exists, why renewal updates the same row instead of issuing a new
one, why the quota ledger's own `period_end` has to be extended alongside the license's).

---

## RB-6 — Suspected credential compromise / security incident

**Detect**: unexpected audit log entries (`audit.read`), an alert from the DAST/SAST
scanning pipeline surfacing something new, a report from a tenant or a security
researcher, or unusual API-key usage patterns (`GET /api/v1/tenant/api-clients/{id}/usage`
showing a spike from an unexpected time/pattern).

**Diagnose**: query `audit_events` for the affected principal (`actor_id`) across the
window in question — every mutation this platform makes is recorded here, so this is a
real, complete trail, not a partial one. For a suspected compromised API key or device
credential, check `last_used_at` on the specific key/device row.

**Mitigate — immediately, in order of blast radius**:
1. **A single API key**: `POST /api/v1/tenant/api-clients/{id}/keys/{key_id}/revoke` —
   takes effect on the *next* request (no propagation delay; the credential lookup reads
   live state every time, not a cache).
2. **A single edge device credential**: mark the device's status such that
   `edge_agent_lookup`'s own `ACTIVE_STATUSES` check excludes it (retire/disable it via
   the edge device management endpoint) — same immediate-effect property.
3. **A compromised human account**: force a password reset (there's no "kill all
   sessions" endpoint yet — the shortest real path today is a password change, which
   invalidates future logins immediately; existing access tokens remain valid until
   their own short TTL expires, and a refresh-token revoke path is the thing to check
   for/add if this hasn't already been built by the time you need it).
4. **A compromised JWT signing key**: this is the serious one — rotate the keypair
   (Operations Manual §B.9) and accept that every existing session gets invalidated at
   once. Treat this as a "everyone re-logs-in" event, communicated proactively, not
   discovered by every user hitting 401s simultaneously.

**Resolve**: once contained, do the actual forensics from the audit trail — what did the
compromised credential actually touch, during what window, and does anything downstream
(evidence, incident data, other tenants via a support grant that was active at the time)
need its own review.

**Prevent**: this is exactly the class of finding `scripts/dast_baseline.py` and the CI
SAST/SCA scans exist to catch before it becomes an incident — treat a new finding from
either as worth triaging promptly, not as noise to defer indefinitely.

---

## RB-7 — A migration failed partway, or `alembic_version` looks wrong

**Detect**: `docker compose run --rm migrate` exits non-zero, or the app fails to start
with a schema-mismatch-shaped error (a query referencing a column/table that doesn't
exist).

**Diagnose**:
```bash
docker compose --env-file ../.env exec -T postgres psql -U csense_app -d csense -tAc \
  "SELECT version_num FROM alembic_version;"
```
Compare against `backend/migrations/versions/` to see exactly which migration is
"current" versus which ones exist. Alembic migrations in this repo run inside a
transaction per migration (Alembic's default) — a failed migration should have rolled
back cleanly, leaving `alembic_version` at the last *successful* one, not a half-applied
state. Confirm that's actually true before assuming otherwise.

**Mitigate**: fix whatever caused the failure (a real syntax/logic error in the
migration, or an environment issue like insufficient privileges) and re-run
`docker compose run --rm migrate` — it's idempotent per-migration (Alembic tracks what's
applied) and safe to re-run.

**Resolve**: if a migration genuinely left things in a bad state (rare, given the
transactional-per-migration default, but possible for anything using
`ALTER TYPE ... ADD VALUE`, which some Postgres versions can't run inside the same
transaction as other DDL) — this is exactly the scenario `restore_exercise.py`
(Operations Manual §B.5) exists to be your escape hatch for, restoring the last known-
good backup rather than hand-patching schema state under pressure.

**Prevent**: never run a new migration against production without having run it against
a restored copy of production data first (`restore_exercise.py`'s own throwaway-database
mechanism is exactly this pattern, extendable to "apply the new migration too, then
check" before touching the real database).

---

## RB-8 — MinIO / object storage issues (evidence, models, exports not accessible)

**Detect**: incident evidence/snapshots fail to load; model uploads fail;
`scripts/backup.py`'s own MinIO inventory step errors out.

**Diagnose**: `docker compose --env-file ../.env logs --tail=100 minio`; confirm the
bucket set is intact (`csense-evidence`, `csense-clips`, `csense-models`,
`csense-exports`, `csense-diagnostics`, `csense-audit-archive`, `csense-backups` —
`csense_shared.storage.objects.ALL_BUCKETS`, auto-created by `ensure_buckets()` if
missing, so a missing bucket self-heals on the next call that needs it rather than
requiring manual intervention).

**Mitigate**: `docker compose --env-file ../.env restart minio`. If disk space is the
issue (evidence/clips/models are real bytes, not metadata — this is the most likely
long-run capacity concern in this stack), that's a genuine capacity-planning problem to
size for ahead of time, not something to firefight repeatedly.

**Resolve/Prevent**: this is why `scripts/backup.py`'s own MinIO inventory manifest
exists — it gives you a real, point-in-time accounting of what's in each bucket
(object count, total bytes) to compare against after any incident, confirming nothing
was silently lost.

---

## RB-9 — Notifications (email/WhatsApp) aren't being delivered

**Detect**: a tenant reports not receiving alerts; `notification_deliveries` rows stuck
in `pending`/`sending` past their `next_attempt_at`, or accumulating `failed` status.

**Diagnose**:
```sql
SELECT channel, status, failure_code, count(*)
FROM notification_deliveries
WHERE created_at > now() - interval '1 hour'
GROUP BY channel, status, failure_code ORDER BY count(*) DESC;
```
Check `docker compose logs notification-worker` for the actual provider error
(`csense_shared.notifications.providers` — Resend for email, the WhatsApp gateway
service for WhatsApp). A `failure_code` naming a real provider-side rejection (bad
recipient, provider outage, rate limit) points at the provider, not this platform's own
queueing — the outbox/worker mechanism itself (claim via `FOR UPDATE SKIP LOCKED`, a
stalled-delivery sweep that requeues anything stuck in `sending` too long) is
independently tested and is rarely the actual fault here.

**Mitigate**: if it's a provider-side outage, nothing to do but wait — deliveries will
retry automatically up to their configured attempt limit. If it's a config issue
(`RESEND_API_KEY`, `WHATSAPP_GATEWAY_URL`/`WHATSAPP_GATEWAY_API_KEY` in `.env`), fix the
config and restart `notification-worker`.

**Resolve/Prevent**: SMS and web-push channels are explicitly not built yet (no provider
contracted — `CHECKLIST.md`'s own `[NEEDS HUMAN INPUT]` note) — don't spend time
debugging an SMS delivery "failure" that's actually just an unimplemented channel.

---

## RB-10 — You need to restore from backup for real (not a drill)

**Detect**: data corruption, an unrecoverable migration failure (RB-7), or a genuine
disaster-recovery need.

**Procedure**: this is exactly what `scripts/restore_exercise.py` already proves works,
run for real instead of into a throwaway database:

1. Stop write traffic to the affected service (take `tenant-api`/`admin-api` out of
   Traefik's rotation, or `docker compose stop` them — decide based on blast radius).
2. Identify the backup to restore: list objects under `csense-backups/{environment}/
   postgres/` (same lookup `restore_exercise.py` does, but you choose which timestamp
   instead of always taking the latest).
3. Download it and `pg_restore` it — **into the real database this time**, not
   `csense_restore_verify`. Take a fresh `pg_dump` of the current (bad) state first if
   there's any chance you'll need to compare or partially recover from it.
4. Run the same row-count sanity checks `restore_exercise.py` does (organizations,
   tenants, users, cameras, incidents) as your first correctness signal, then whatever
   deeper validation the specific incident calls for.
5. Restart services, confirm `/readyz` is `ok`, and run a real smoke test
   (`scripts/e2e_vertical_slice.py` exercises a broad real path through the system) before
   declaring the incident resolved.

**This is not the moment to improvise the mechanism** — that's exactly what
`restore_exercise.py` exists to have already proven, in a low-stakes drill, before a
high-stakes real one.
