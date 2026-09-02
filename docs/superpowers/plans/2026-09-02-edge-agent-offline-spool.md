# Edge Agent + Encrypted Offline Spool — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close `CHECKLIST.md`'s "Edge encrypted offline spool, reconnect cursor, batch resync, dedup" — which requires first creating the edge agent it lives in, since `backend/edge/agent/` has never existed.

**Spec:** `docs/03_APPLICATION_FLOWS.md` §15 "FLOW-13: Edge Offline and Resynchronization" is the
authoritative behaviour (9 steps + a conflict policy). Read it before starting any task.

---

## Decisions already made (do not silently revisit; raise it if you think one is wrong)

**Runtime: Python, shipped as a container, built for arm64.** Chosen by the user, targeting
constrained/ARM edge hardware (Pi/Jetson class).

**The agent does NOT install `csense_shared`.** That package declares `sqlalchemy`, `asyncpg`,
`redis`, `minio`, `argon2-cffi`, `pyjwt[crypto]` — several needing compilation on ARM, none of
which an agent that only talks HTTP to the API needs. The agent's entire dependency set is
`httpx` + `cryptography` + stdlib `sqlite3`; both third-party packages publish prebuilt
`aarch64` wheels, so the image builds on ARM without a toolchain.

**The spool has its own device-local key and does NOT use `csense_shared.security.envelope`.**
Two independent reasons, both load-bearing:
1. **Security.** `envelope.py` seals against the *platform* KEK (`master_v1.key`). Shipping that
   key to a box sitting in a customer's building would put the key that decrypts *every tenant's*
   camera credentials, TOTP secrets and webhook secrets on hardware outside our control. The
   device gets its own key, generated on the device at first run, and that key protects only that
   device's own spooled events.
2. **Fit.** `envelope.py`'s AAD binds `tenant_id`/`secret_id`/`purpose` — database-row concepts.
   A spool row is bound to a different set of facts (the spool file and the row's own id).

The scheme is nonetheless the *same primitive* (AES-256-GCM, random 12-byte nonce per row, AAD
binding the row so a ciphertext cannot be moved to another row), so the reasoning in
`envelope.py`'s docstring still applies and should be referenced rather than restated.

**Scope boundary — the agent does not do inference in this pass.** Its job here is enrolment,
heartbeat, and the event pipeline: accept detections, deliver them online, spool them when
offline, resync on reconnect. The detection *source* is an explicit, narrow interface with one
real implementation (a local HTTP endpoint the agent listens on, which is how a co-located
inference process — the existing `ai-runtime`, or anything else — hands it events). Building
ARM inference is separate, larger, and belongs with the Phase 4 AI work. **Say this plainly in
the agent's own docstring; do not build a fake "detector" and imply the agent sees.**

**At-least-once + server dedup = effectively-once.** A spooled row is deleted only after the
server acknowledges it. The server already enforces a unique `(tenant_id, source_event_id)`
(migration 0012), so a replayed batch is a no-op there. This is the property that makes the whole
design safe; every task must preserve it.

---

### Task 1: Batch ingestion endpoint

Only single-detection `POST /api/v1/tenant/ingest/detections` exists. A device draining a
day-long spool one HTTP round trip at a time is the thing FLOW-13 step 6 ("uploads batches")
exists to avoid.

**Files:** Modify `backend/tenant_api/app/api/ingest.py`; test `backend/tests/` (new file).

- [ ] **Step 1: Read the existing endpoint first.** `ingest.py`'s current `POST /detections`,
      its `DetectionIn`/`ObjectIn`/`IngestOut` models, its clock-skew guard (`MAX_CLOCK_SKEW`,
      5 minutes), its auth (`current_agent` device credential *or* a scoped customer token with
      `detection.ingest`), and `csense_shared/pipeline/ingest.py`'s `ingest_detection`.

- [ ] **Step 2: Write failing tests, then implement `POST /api/v1/tenant/ingest/detections/batch`.**
      Requirements, all of which need a test:
      - Body: `{"detections": [DetectionIn, ...]}`, capped (`MAX_BATCH = 100`; a cap is required,
        an uncapped batch is a memory-exhaustion vector on a shared API).
      - Returns `202` with a per-item result list, **in request order**, each carrying the same
        shape the single endpoint returns plus the `source_event_id` it corresponds to, so a
        device can tell exactly which items landed.
      - **One item failing must not fail the batch.** A malformed or rejected detection returns
        its own error entry; the rest still process. A device draining a spool cannot be blocked
        forever by one poisoned row — that is the whole point.
      - Each item goes through the *same* `ingest_detection` path as the single endpoint (do not
        fork the pipeline logic), so dedup, rule evaluation, incidents, evidence and notifications
        behave identically.
      - Idempotency holds across the batch: sending the same batch twice yields
        `duplicate: true` for every item the second time and creates no second incident.
      - Same auth as the single endpoint.
      - `frame_base64` is allowed but the batch's *total* decoded frame bytes are capped
        (reuse/extend `MAX_FRAME_BYTES` thinking); state the limit in the docstring.
      Test the duplicate-batch case against a real DB — it is the property the spool depends on.

- [ ] **Step 3:** Full backend suite + `ruff check backend scripts`. Commit.

---

### Task 2: Heartbeat carries spool depth

`docs/05_BACKEND_SCHEMA.md:567` and `docs/04_UI_UX_DESIGN_BRIEF.md:224` already name "spool
depth" as telemetry the UI shows. `HeartbeatIn` has no such field.

**Files:** Modify `backend/tenant_api/app/api/edge.py` (`HeartbeatIn`, and the heartbeat handler).

- [ ] **Step 1:** Add optional `spool_depth: int | None` and `spool_dropped: int | None` to
      `HeartbeatIn`, persisted into the existing health snapshot (`health` dict / the columns the
      handler already writes — read the handler first; do NOT add a migration if the existing
      JSON snapshot can carry it, and say which you chose and why).
- [ ] **Step 2:** A device reporting a non-zero `spool_dropped` is a real degradation — the box
      is losing events. Decide and implement whether that alone should mark the device
      `degraded`; whichever you choose, justify it in a comment. Test it.
- [ ] **Step 3:** Full suite + ruff. Commit.

---

### Task 3: Agent skeleton + device-local spool crypto

**Files:** Create `backend/edge_agent/` — `app/__init__.py`, `app/config.py`, `app/crypto.py`,
`requirements.txt`; test `backend/tests/test_edge_spool_crypto.py`.

- [ ] **Step 1: Config.** Plain environment-driven settings (do NOT import
      `csense_shared.config.Settings` — it pulls the whole server dependency tree). Needs at
      minimum: API base URL, enrolment token / credential path, spool path, spool bounds, poll and
      backoff intervals, local listen address for the detection source, device key path.

- [ ] **Step 2: TDD the crypto.** `app/crypto.py`:
      - `generate_device_key()` → 32 random bytes.
      - `load_or_create_device_key(path)` → creates with `0600` on first run, refuses to use a
        key file that is group/world **writable** (mirror `envelope.py`'s permission reasoning and
        cite it; note it deliberately does not refuse merely *readable*, matching that module).
      - `seal_row(key, row_id, plaintext) -> bytes` / `open_row(key, row_id, blob) -> bytes` using
        AES-256-GCM with a fresh 12-byte nonce per call, AAD binding `row_id` so a ciphertext
        cannot be relocated to another spool row.
      Tests must include: round trip; a tampered ciphertext is rejected; a ciphertext moved to a
      different `row_id` is rejected; a different key cannot open it; the permission refusal.
      (`backend/tests/test_envelope.py` is the model for this file's style and rigour.)

- [ ] **Step 3:** Suite + ruff. Commit.

---

### Task 4: The encrypted spool

**Files:** Create `backend/edge_agent/app/spool.py`; test `backend/tests/test_edge_spool.py`.

SQLite via stdlib `sqlite3` (no dependency, already on the device, survives restarts and power
loss with WAL). One table; payload column holds the sealed blob from Task 3, never plaintext.

- [ ] **Step 1: TDD it.** Required behaviour, each needing a test:
      - `append(event)` persists durably (WAL + synchronous=FULL — justify the durability choice
        in a comment; a spool that loses events on power loss defeats its purpose).
      - `drain(limit)` returns oldest-first by `(captured_at, id)` — chronological order is part of
        FLOW-13's "preserve original event identity/timestamps".
      - `ack(ids)` deletes only acknowledged rows. **Nothing is ever deleted before the server
        acknowledges it** — assert this explicitly.
      - `depth()` / `dropped_count()` for the Task 2 telemetry.
      - **Bounded size.** When the cap is hit, drop *oldest* and increment a persistent dropped
        counter. Document the tradeoff honestly in the module docstring: oldest-drop preserves the
        ability to deliver a contiguous recent window and matches ring-buffer convention, but it
        does mean the earliest events of a long outage are the ones lost. The counter existing —
        and reaching the server via heartbeat — is what stops that being silent.
      - Reopening the spool after a simulated crash (close mid-write / reopen) loses nothing
        acknowledged and re-offers anything unacknowledged.
      - Plaintext never appears in the file: write a known string, then assert those bytes are
        absent from the raw file on disk. This is the test that proves "encrypted" is true.

- [ ] **Step 2:** Suite + ruff. Commit.

---

### Task 5: The sync loop

**Files:** Create `backend/edge_agent/app/sync.py`, `app/source.py`, `app/main.py`;
test `backend/tests/test_edge_sync.py`.

- [ ] **Step 1: `source.py`** — the narrow detection-source interface plus one real
      implementation: a small local HTTP listener accepting the same detection shape the API
      takes, so a co-located inference process hands events to the agent. Its docstring must state
      plainly that the agent does not itself do inference in this pass, and why (see Decisions).

- [ ] **Step 2: TDD `sync.py`.** Behaviour, each needing a test (use a fake HTTP client — no
      network in unit tests, same discipline `test_webhook_dispatcher.py` uses with its injected
      `send_fn`):
      - Online: an event delivers immediately and is never spooled.
      - Offline (the API is unreachable): the event is spooled, and the agent does not lose it,
        crash, or spin hot.
      - Reconnect: spooled events drain in batches, oldest first, and are acked/deleted only after
        the server confirms. Interrupting the drain mid-way re-offers the undelivered remainder.
      - **Dedup**: a batch that the server reports as `duplicate: true` is still acked and removed
        locally — a duplicate is a successful outcome, not a retry loop.
      - Backoff on repeated failure (bounded, with jitter — justify the ceiling).
      - The agent keeps accepting new events while draining a backlog; a large backlog must not
        block current events indefinitely. State the chosen policy (e.g. interleave, or bounded
        drain per cycle) and test it.

- [ ] **Step 3: `main.py`** — wire enrolment (`POST /api/v1/tenant/edge/enrol`), the heartbeat
      loop (reporting `spool_depth`/`spool_dropped` from Task 2 and honouring the server's
      returned `next_interval_seconds`), the command channel (`GET /commands/pending`,
      `POST /commands/{id}/ack` — respecting FLOW-13's "expired commands are not executed"), and
      the sync loop, as concurrent tasks. **Read `backend/notification_worker/app/main.py` first**
      — it is the established shape for multi-loop services in this repo, including the failure
      isolation between loops, and this should follow it.

- [ ] **Step 4:** Suite + ruff. Commit.

---

### Task 6: Container, buildable for arm64

**Files:** Create `backend/edge_agent/Dockerfile`; modify `infra/docker-compose.yml`.

- [ ] **Step 1:** Dockerfile modelled on `backend/notification_worker/Dockerfile` (read it), but
      **it must not `pip install ./shared`** — see Decisions. Non-root user, `read_only` where the
      compose service allows, no published ports except the local detection listener.
- [ ] **Step 2: Prove the arm64 claim rather than asserting it.** Build for arm64 explicitly
      (`docker buildx build --platform linux/arm64`) and report the real outcome and image size.
      If buildx/qemu is unavailable on this machine, say so plainly and instead verify that every
      pinned dependency publishes an `aarch64` wheel (check the actual wheel list on PyPI) — do
      not claim a build you did not run.
- [ ] **Step 3:** Add the service to `infra/docker-compose.yml` (dev only — it is customer-premises
      software, so think about whether it belongs in `docker-compose.prod.yml` at all and say what
      you decided). Confirm it starts clean.
- [ ] **Step 4:** Commit.

---

### Task 7: Real e2e against the live stack

**Files:** Create `scripts/e2e_edge_spool.py`. Read `scripts/e2e_webhook_dispatch.py` and
`scripts/e2e_support_grant_authorization.py` for the established conventions.

- [ ] Must prove, against the running stack, with the agent running as a real container:
      1. The agent enrols for real and appears as an `online` device.
      2. An event submitted while online reaches the API and creates a real detection.
      3. **The API is then made genuinely unreachable to the agent** (stop the container, or cut it
         off the compose network — not a mocked flag), events are submitted, and they are spooled:
         assert `spool_depth` rises and that nothing reached the server.
      4. Restore connectivity. The backlog drains automatically. Assert every spooled event now
         exists server-side, with its **original `captured_at` preserved**, not the delivery time
         (FLOW-13's conflict policy).
      5. **No duplicates**: exactly one detection row per `source_event_id`, even though the drain
         may have retried.
      6. Full cleanup, PASS/FAIL per assertion, non-zero exit on failure.

- [ ] Run it until it genuinely passes. `ruff check backend scripts`. Commit.

---

### Task 8: `CHECKLIST.md`

- [ ] Flip `- [ ] Edge encrypted offline spool, reconnect cursor, batch resync, dedup` to `[x]`
      with sub-bullets in this file's established voice: what shipped, the device-local-key
      decision and *why* (the platform KEK must not leave our infrastructure), the oldest-drop
      eviction tradeoff, the honest scope boundary (the agent does not do inference yet), and the
      real-stack proof. Add an honest `- [ ]` for what remains — at minimum: no inference on the
      agent, and no Customer CRM UI showing spool depth even though the field now arrives.
- [ ] Commit.
