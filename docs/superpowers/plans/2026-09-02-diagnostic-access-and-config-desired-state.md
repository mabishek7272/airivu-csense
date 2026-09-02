# Time-Limited Diagnostic Access + Config Desired-State — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close two related `CHECKLIST.md` items:
- "WireGuard/relay integration design + time-limited diagnostic access"
- "Redis config cache + invalidation, desired-state deployment to edge"

**Scope decision on the first item, made before any code — read before touching anything:**
WireGuard provisioning is already real (`CLAUDE.md`'s documented fixes: peer isolation,
fleet-wide address allocation, cross-tenant overlap guard). `cloud_relay`/`port_forward` are
connectivity *modes a device self-reports*, not services we operate — confirmed by grep:
`cloud_relay` appears only as an enum value and a pattern string, nowhere as backing
infrastructure, which is correct: a cloud relay is the customer's own setup, not ours to run.
So "integration design" for those three methods is already done by recording which mode a
device uses. **The only real gap was diagnostic access, and the user (2026-09-02) scoped it
explicitly: read-only — logs + health snapshot. No shell, no exec, nothing that mutates
device state.** This closes the whole checklist item without a new relay/session-broker
service, by riding on infrastructure already shipped this session:
- The heartbeat channel (agent → `tenant_api`) already exists and already carries a bounded
  `health` JSONB blob (this session's spool-telemetry work).
- Support-grant elevation (`current_tenant_context` accepting a platform token + an active
  `support_grants` row, permissions scoped to the grant's own approved codes) already exists
  and is exactly the time-boxing mechanism "time-limited" requires — no new expiry logic
  needed.
- So: the agent reports a small bounded log tail alongside its existing health snapshot: one
  new read endpoint, gated by a new permission scoped narrowly enough to appear safely in a
  support grant's `requested_scopes`; a platform developer reaches it the same way the
  incident-access work already proved out, end to end, this session.

**Scope decision on the second item:** the Redis 3-layer cache half stays deferred — the
checklist's own existing reasoning (Postgres-authoritative reads are enough at this scale,
same call already made for pipeline config and WS-realtime) still holds and this plan does
not revisit it. The desired-state-*deployment* half was blocked only on "no edge agent to
push to" — that blocker is gone. Scope is deliberately bounded to *device-level operational
config* (heartbeat interval, spool byte/row caps, backoff parameters) — NOT model/pipeline
weights, which is Phase 4 AI-runtime territory the edge agent explicitly stayed out of this
session (`docs/superpowers/plans/2026-09-02-edge-agent-offline-spool.md`'s own scope
boundary). Pushing inference config to a box that does no inference would be building ahead
of a consumer that doesn't exist — exactly what this project's own discipline avoids.

---

### Task 1: `diagnostic.read` permission + agent-side bounded log tail

**Files:** New migration `backend/migrations/versions/0053_diagnostic_read_permission.py`;
modify `backend/edge_agent/app/main.py` (or a new small `app/logbuf.py` — implementer's call,
justify it); test accordingly.

- [ ] **Step 1: Migration.** Add one customer-audience permission, mirroring migration 0044's
      exact shape (`_grant` helper, `ON CONFLICT DO NOTHING`):
      `("diagnostic.read", "diagnostic", "read", "elevated", "Read an edge device's recent
      logs and health snapshot for troubleshooting")`. Grant to `tenant_owner` and
      `tenant_member` (self-service diagnostics is a real, ordinary customer capability, not
      only a support-session one — this is deliberate: the same endpoint and permission serve
      both a tenant admin debugging their own device and a platform developer under an
      elevated grant). Do **not** add it to `DANGEROUS_SUPPORT_SCOPES`
      (`backend/admin_api/app/api/support.py`) — it's read-only and mints nothing persistent,
      the exact class of permission that denylist exists to distinguish from.

- [ ] **Step 2: Agent-side bounded log tail.** The agent must start keeping a small in-process
      ring buffer of its own recent log records (a `logging.Handler` subclass is the natural
      shape — read how `config.py`/`main.py` currently configure logging first). Cap it hard:
      pick a byte budget (e.g. 4 KiB) that, combined with the existing `spool` block, still
      sits comfortably inside `HEALTH_PAYLOAD_LIMIT` (16 KiB, `edge.py`) with real headroom —
      show the arithmetic in a comment, don't just pick a number. Redact anything that looks
      like a secret before it ever enters the buffer (the agent's own enrolment token/device
      credential must never be logged in the first place — grep the agent's existing log
      call sites to confirm none already do this before assuming the redaction is only
      theoretical). Include the tail in the `health` dict sent on heartbeat, under a `logs`
      key, structured (timestamp, level, message) not a raw string blob.
      TDD: buffer bounds correctly (oldest evicted, never exceeds its byte cap), heartbeat
      payload with a full log buffer plus a full spool block still fits `HEALTH_PAYLOAD_LIMIT`
      (a real regression test, not just "should fit" — this project already shipped one
      near-miss on this exact budget this session, don't repeat it), nothing resembling the
      device credential ever appears in a buffered line.

- [ ] **Step 3:** Full suite + `ruff check backend scripts`. Rebuild tenant-api and edge-agent
      images if either changed; confirm both start clean. Commit.

---

### Task 2: `GET /api/v1/tenant/edge/devices/{id}/diagnostics`

**Files:** Modify `backend/tenant_api/app/api/edge.py`.

- [ ] TDD a new route returning: the device's current `health` (including the new `logs`
      tail), `health_status`, `connectivity_method`/`connectivity_reason`, `last_seen_at`,
      and the last N (e.g. 10) `device_commands` rows for that device (type, status,
      result_code/result_summary, timestamps) — enough to answer "why is this device
      unreachable/degraded" without a database console. Gated by `require_permission(context,
      "diagnostic.read")` — nothing else; the route itself doesn't need to know or care
      whether `context` is an ordinary tenant user or a support-grant-elevated one, which is
      exactly the point of building it on top of `current_tenant_context` rather than a
      parallel admin-only path.
      Tests: an ordinary `tenant_member` with the permission can read their own device's
      diagnostics; a request for a device outside the tenant is refused (RLS); a request
      without `diagnostic.read` is refused; the response actually contains the log tail and
      command history, not just health.

- [ ] Full suite + ruff. Rebuild + confirm clean start. Commit.

---

### Task 3: Real e2e — elevated diagnostic access, end to end

**Files:** Create `scripts/e2e_diagnostic_access.py`. Read `scripts/e2e_support_grant_authorization.py`
first — this is the same elevation mechanism, a second real consumer of it.

- [ ] Must prove, against the live stack, reusing a real `edge-agent` container the way
      `scripts/e2e_edge_spool.py` already does:
      1. A real enrolled device is running and heartbeating with a real log tail.
      2. The tenant's own owner login reads `.../diagnostics` directly — 200, real log lines
         and health present.
      3. A platform developer with **no active grant** gets 401 on the same route.
      4. A support grant requesting `["diagnostic.read"]` is peer-approved; the elevated
         platform developer now reads the *same* device's diagnostics through the *same*
         route — 200, and the content matches what the tenant owner saw (proving the
         elevation reaches this new permission correctly, not just the incident-read path
         already proven).
      5. The grant is revoked; the identical elevated call now gets 401 immediately.
      6. The elevated developer's read is confirmed **not** to have granted anything beyond
         read — attempt an unrelated tenant-scoped write (e.g. issuing a device command) with
         the same elevated session and confirm it's refused (the grant's `requested_scopes`
         never included anything else, so `require_permission` on that other route fails) —
         this is the assertion that proves "read-only" is real, not just intended.
      7. Cleanup; PASS/FAIL summary; non-zero exit on failure.
- [ ] Run until it genuinely passes. `ruff check backend scripts`. Commit.

---

### Task 4: Device-level desired-state config push

**Files:** Modify `backend/tenant_api/app/api/edge.py` (a new command type + a config-push
route, or extend the existing command-issue route — implementer's call, justify it);
modify `backend/edge_agent/app/main.py`/`config.py` to apply an accepted config command;
test both sides.

- [ ] **Read `docs/03_APPLICATION_FLOWS.md` §15 FLOW-13 again for the conflict policy**:
      cloud desired state wins; expired commands are not executed; a device applies newer
      configuration only after artifact verification.
- [ ] Scope the config payload to what the agent's own `config.py` already exposes as
      runtime-tunable (heartbeat interval override, spool byte/row caps, backoff parameters)
      — do not invent new tunables, and do not include anything model/pipeline-related (out
      of scope per this plan's header).
- [ ] Server side: issuing a config-push command bumps `edge_devices.desired_state_version`
      (already exists, migration 0042, currently unused — this is the first real writer of
      it). The command payload is signed the same way every other `device_commands` row
      already is (`csense_shared.security.signed_commands` — reuse it, don't reinvent).
      An expired command must genuinely not be applied — test this against the real
      `expires_at` check already in the command-ack path, not a new one.
- [ ] Agent side: on receiving and validating a config command, apply the new values to its
      *running* config (not just on next restart — a device that has to be power-cycled to
      pick up a heartbeat-interval change isn't "deployed"), then report
      `observed_state_version` matching the command's target version on its next heartbeat
      (`edge_devices.observed_state_version`, also currently unused — this is its first real
      writer too). "Artifact verification" for this narrow payload means: reject a config
      command whose values fail the same bounds `config.py` already enforces at startup
      (don't accept a heartbeat interval of zero, a negative spool cap, etc.) — no signature
      scheme beyond the existing signed-command envelope is needed for a same-shape config
      payload.
- [ ] TDD both sides. Tests: a pushed config is genuinely observed running (not just
      acknowledged) within one heartbeat cycle; an expired config command is never applied
      and `observed_state_version` doesn't move; an out-of-bounds value is refused by the
      agent with a clear log line, not silently ignored or crash-applied; `desired_state`
      vs `observed_state` converge and the gap is visible via `GET /devices/{id}` (already
      exposes both per Task 2 of the offline-spool plan — confirm, don't assume).
- [ ] Full suite + ruff. Commit.

---

### Task 5: Real e2e — desired-state push, real container

**Files:** Create `scripts/e2e_edge_desired_state.py`.

- [ ] Against a real running `edge-agent` container: push a real config change (e.g. halve
      the heartbeat interval), confirm the agent's *actual* heartbeat cadence changes
      (measure real elapsed time between two heartbeats, not just that a value was echoed
      back), confirm `observed_state_version` catches up to `desired_state_version` via the
      real API. Push a second command already `expires_at`-expired and confirm it is never
      applied and the version numbers don't move. Cleanup, PASS/FAIL, non-zero exit on
      failure.
- [ ] Run until genuinely passing. `ruff check backend scripts`. Commit.

---

### Task 6: `CHECKLIST.md`

- [ ] Flip both items, in the established voice: what shipped for diagnostic access (the
      read-only scope decision and why, reuse of the elevation mechanism, the assertion that
      proves read-only is real), what shipped for desired-state (scoped to device-level
      config, the conflict-policy compliance, real cadence-change proof), and the honest
      remainder (Redis cache half stays deferred with its existing reasoning; no
      pipeline/model desired-state push yet, named as Phase 4 AI-runtime territory).
- [ ] Commit.
