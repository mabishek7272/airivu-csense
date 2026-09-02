"""Proves FLOW-13 (docs/03_APPLICATION_FLOWS.md §15) end-to-end against the live stack,
with the edge agent running as a real container - not imported as a Python module, not
mocked at any layer. This is the first time the whole chain (enrolment, the local
detection listener, the encrypted spool, the sync loop's backoff, batch resync, server
dedup) has ever run together.

A real tenant, site and camera are created through the real API. A real device row is
created and a real enrolment token is issued (`POST /devices`, then
`POST /devices/{id}/enrolment-token` - see `backend/tenant_api/app/api/edge.py`). A real
`csense-edge-agent` container is started (`docker run`, not `docker compose up`, so this
script controls its own isolated volume rather than sharing the dev stack's
`edgeagentstate` volume - see `_edge_agent_image_and_network` for why that separation
matters), pointed at the real token. It enrols for real and appears `online`.

Then the properties FLOW-13 exists for:

  - An event submitted to the agent's local HTTP listener while online is delivered
    directly and lands as a real detection row within seconds - never spooled.
  - The API is made genuinely unreachable with `docker network disconnect` (not a mocked
    flag, not an env var) and events submitted during the outage are spooled locally:
    `spool_depth` is 0 the whole time it's cut off (a heartbeat cannot carry a number it
    cannot deliver), and nothing reaches the server - both asserted directly.
  - Reconnecting drains the backlog automatically - nothing here manually triggers a
    drain, that would defeat the point - and every event lands with its *original*
    `captured_at` preserved (`capture_time` in the `detections` row), not the time it was
    actually delivered minutes later. This is FLOW-13's conflict policy ("edge original
    event identity/timestamps are preserved") and the single most important assertion
    below.
  - Exactly one detection row per `source_event_id` once the dust settles - proving the
    server's batch idempotency (Task 1) and the agent's ack-only-after-confirm spool
    (Task 4/5) compose correctly together, which no unit test on either side alone can
    show.

**Catching `spool_depth` actually rising is the fiddly part of this script**, and worth
explaining once: the server's `HEARTBEAT_INTERVAL_SECONDS` is a hardcoded 30s
(`edge.py`), and the agent adopts whatever the server returns on its very first
heartbeat - so no agent-side interval override changes its real cadence once the process
has been running for more than a few seconds. Heartbeats have *no* backoff (fixed 30s
tick, success or failure); the sync loop's drain attempts *do* back off exponentially.
So the reliable way to observe a mid-outage spool depth is to make sure the sync loop's
own next attempt is deterministically further away than 30s at the moment connectivity
returns - not to race two independently-scheduled loops and hope. This script drives the
agent's `CSENSE_BACKOFF_INITIAL_SECONDS` down to 10s and then *watches the container's
own log* for the fourth `drain_interrupted` line before reconnecting: at that point the
engine's `next_delay_seconds` computes a fresh sleep of `min(300, 10*2**3)` = 80s, jittered
to half-of-window..window = [40s, 80s]. 40 is a hard floor greater than the heartbeat's
30s hard ceiling, so the very next heartbeat is *guaranteed* to land before the sync loop
even attempts to drain - not merely likely to. See `_wait_for_deep_backoff`.

    python scripts/e2e_edge_spool.py
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid

API = "http://localhost:8080"
PASSWORD = "EdgeSpoolE2E!Password123"

# How many offline events to submit while the agent is cut off. Small enough that one
# drain batch clears it in a single request; large enough that "every one of them landed,
# with the right timestamp, with no duplicate" is actually exercising a batch, not a
# single-item coincidence.
OFFLINE_EVENT_COUNT = 3

# See the module docstring: the number of consecutive `drain_interrupted` lines to observe
# before reconnecting, and what it guarantees about the sync loop's next attempt.
BACKOFF_INITIAL_SECONDS = 10
FAILURES_BEFORE_RECONNECT = 4
DEEP_BACKOFF_TIMEOUT_SECONDS = 150

# Real bound (config.py clamps to [1.0, 300.0]); short so a cut connection is recognised
# in a few seconds rather than hanging out the default 30s timeout on every attempt, which
# would make the backoff-log-watching above take proportionally longer for no benefit.
REQUEST_TIMEOUT_SECONDS = 5

SPOOL_DEPTH_POLL_TIMEOUT_SECONDS = 45
FULL_DRAIN_TIMEOUT_SECONDS = 180


def api(path, payload=None, token=None, method="POST", expect=(200, 201, 204)):
    headers = {"Content-Type": "application/json", "Host": "app.localhost"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        if exc.code in expect:
            return exc.code, (json.loads(body) if body else {})
        raise RuntimeError(f"{method} {path} -> {exc.code}: {body[:500]}") from exc


def psql(sql: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def psql_rows(sql: str) -> list[str]:
    """Like `psql`, but returns every row rather than only the first."""
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    return [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]


def docker(*args: str, input_bytes: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["docker", *args], capture_output=True, input=input_bytes, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"docker {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.decode(errors='replace')[:800]}"
        )
    return result


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


# --- Container plumbing ------------------------------------------------------------------

def compose_project_and_network() -> tuple[str, str]:
    """Discovers the real compose project name and network, from the already-running
    `tenant-api` container, rather than hardcoding `csense`/`csense_csense` - this script
    has to attach a plain `docker run` container to the same network compose created, and
    guessing its name is exactly the kind of thing that quietly breaks on a differently
    named checkout."""
    container_id = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "ps", "-q", "tenant-api"],
        cwd="infra", capture_output=True, text=True, check=True,
    ).stdout.strip()
    if not container_id:
        raise RuntimeError("tenant-api is not running - start the stack first (docker compose up -d).")
    project = docker(
        "inspect", "-f", '{{ index .Config.Labels "com.docker.compose.project" }}', container_id
    ).stdout.decode().strip()
    networks = json.loads(
        docker("inspect", "-f", "{{json .NetworkSettings.Networks}}", container_id).stdout
    )
    network = next(iter(networks))
    return project, network


def build_edge_agent_image() -> None:
    """`docker compose build`, not `docker build` directly - keeps this script honouring
    whatever build args/context the real service definition uses, exactly as a real
    deployment's build would."""
    subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "build", "edge-agent"],
        cwd="infra", check=True,
    )


def submit_event(container: str, payload: dict) -> dict:
    """POSTs one detection to the agent's local listener, from inside the container's own
    network namespace (127.0.0.1:8099 - see `backend/edge_agent/app/source.py`). The
    payload travels over the exec'd process's stdin rather than being interpolated into a
    shell command, so nothing here has to worry about quoting a JSON body safely."""
    script = (
        "import sys, urllib.request\n"
        "body = sys.stdin.buffer.read()\n"
        "req = urllib.request.Request('http://127.0.0.1:8099/detections', data=body, "
        "headers={'Content-Type': 'application/json'}, method='POST')\n"
        "with urllib.request.urlopen(req, timeout=20) as r:\n"
        "    sys.stdout.write(r.read().decode())\n"
    )
    result = docker(
        "exec", "-i", container, "python3", "-c", script,
        input_bytes=json.dumps(payload).encode(),
    )
    return json.loads(result.stdout.decode())


def container_logs(container: str) -> str:
    result = docker("logs", container, check=False)
    return result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")


def wait_for_deep_backoff(container: str, *, failures: int, timeout: float) -> bool:
    """Polls the container's own logs until `failures` consecutive `drain_interrupted`
    lines have been logged - see the module docstring for exactly what that guarantees
    about the sync loop's next attempt, and why that is what makes the spool_depth
    assertion below deterministic rather than a coin flip between two independent loops.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if container_logs(container).count("drain_interrupted") >= failures:
            return True
        time.sleep(1.0)
    return False


def wait_for_device_online(token: str, device_id: str, *, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    device: dict = {}
    while time.monotonic() < deadline:
        _, device = api(f"/api/v1/tenant/edge/devices/{device_id}", token=token, method="GET")
        if device.get("status") == "online" and device.get("online"):
            return device
        time.sleep(1.0)
    return device


def poll_max_spool_depth(token: str, device_id: str, *, timeout: float) -> int:
    """The highest `health.spool.depth` observed across the poll window, not merely the
    last one - see the module docstring for why this can be a narrow, real window rather
    than a steady-state value."""
    deadline = time.monotonic() + timeout
    highest = 0
    while time.monotonic() < deadline:
        _, device = api(f"/api/v1/tenant/edge/devices/{device_id}", token=token, method="GET")
        depth = ((device.get("health") or {}).get("spool") or {}).get("depth")
        if isinstance(depth, int) and depth > highest:
            highest = depth
        time.sleep(0.4)
    return highest


def wait_for_detections(tenant_id: str, source_event_ids: list[str], *, timeout: float) -> dict[str, str]:
    """Polls the DB directly until every id in `source_event_ids` has landed, returning
    `{source_event_id: capture_time_epoch_seconds_as_text}`."""
    remaining = set(source_event_ids)
    found: dict[str, str] = {}
    deadline = time.monotonic() + timeout
    ids_sql = ", ".join(f"'{sid}'" for sid in source_event_ids)
    while time.monotonic() < deadline and remaining:
        rows = psql_rows(
            "SELECT source_event_id || '|' || extract(epoch from capture_time) "
            f"FROM detections WHERE tenant_id = '{tenant_id}' AND source_event_id IN ({ids_sql})"
        )
        for row in rows:
            sid, _, epoch = row.partition("|")
            found[sid] = epoch
            remaining.discard(sid)
        if remaining:
            time.sleep(1.5)
    return found


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []
    organization_name = f"Edge Spool E2E {suffix}"
    email = f"owner-{suffix}@northwind.example"
    container_name = f"csense-edge-spool-e2e-{suffix}"
    volume_name = f"csense-edge-spool-e2e-{suffix}"

    tenant_id = None
    container_started = False
    volume_created = False
    disconnected = False
    project = network = image = None

    try:
        step(0, "Discover the real compose project/network and build the agent image")
        project, network = compose_project_and_network()
        image = f"{project}-edge-agent:latest"
        print(f"    project={project} network={network} image={image}")
        build_edge_agent_image()
        check(bool(docker("image", "inspect", image, check=False).returncode == 0), f"image '{image}' exists after build", failures)

        step(1, "Register a tenant and create a site + camera through the real API")
        _, auth = api("/api/v1/auth/register", {
            "organization_name": organization_name,
            "email": email,
            "password": PASSWORD, "display_name": "Owner",
        })
        token, tenant_id = auth["access_token"], auth["tenant_id"]

        _, site = api("/api/v1/tenant/sites", {
            "name": "Depot", "code": f"depot-{suffix}", "timezone": "UTC",
        }, token, expect=(201,))
        site_id = site["id"]

        _, camera = api("/api/v1/tenant/cameras", {
            "site_id": site_id, "name": "Gate Camera", "code": f"gate-{suffix}",
        }, token, expect=(201,))
        camera_id = camera["id"]

        step(2, "Create a device record and issue a real, single-use enrolment token")
        status, device = api("/api/v1/tenant/edge/devices", {
            "name": "E2E Spool Agent", "site_id": site_id,
            "device_type": "pc_linux", "role": "inference",
        }, token, expect=(201,))
        check(status == 201, "device record created", failures)
        device_id = device["id"]
        check(device["status"] == "pending", "a fresh device starts 'pending', unenrolled", failures)

        status, enrolment = api(
            f"/api/v1/tenant/edge/devices/{device_id}/enrolment-token", None, token, expect=(201,)
        )
        check(status == 201, "enrolment token issued", failures)
        enrolment_token = enrolment["token"]

        step(3, "Start the real edge-agent container, pointed at the real token")
        # A dedicated, uniquely-named volume - not the dev stack's shared `edgeagentstate`
        # (which `docker compose up edge-agent` would reuse across every invocation of
        # this script, silently loading a *previous* run's credential and enrolling
        # nothing). `--read-only` + `--tmpfs /tmp` mirror the compose service's own
        # hardening; the named volume is the one writable path, exactly as
        # `backend/edge_agent/Dockerfile` documents.
        docker("volume", "create", volume_name)
        volume_created = True
        docker(
            "run", "-d", "--name", container_name, "--network", network,
            "-e", "CSENSE_API_BASE_URL=http://tenant-api:8000",
            "-e", "CSENSE_STATE_DIR=/var/lib/csense-agent",
            "-e", f"CSENSE_ENROLMENT_TOKEN={enrolment_token}",
            "-e", f"CSENSE_SERIAL_NUMBER=e2e-spool-{suffix}",
            "-e", f"CSENSE_BACKOFF_INITIAL_SECONDS={BACKOFF_INITIAL_SECONDS}",
            "-e", f"CSENSE_REQUEST_TIMEOUT_SECONDS={REQUEST_TIMEOUT_SECONDS}",
            "-v", f"{volume_name}:/var/lib/csense-agent",
            "--tmpfs", "/tmp",
            "--read-only",
            "--security-opt", "no-new-privileges:true",
            image,
        )
        container_started = True

        step(4, "The agent enrols for real and the device reaches 'online'")
        device_after_enrol = wait_for_device_online(token, device_id)
        check(device_after_enrol.get("status") == "online", "device status is 'online'", failures)
        check(device_after_enrol.get("online") is True, "device's derived 'online' flag is true", failures)
        check(
            device_after_enrol.get("agent_version") == "0.1.0",
            "the device record carries the real agent's reported version", failures,
        )
        spool = (device_after_enrol.get("health") or {}).get("spool") or {}
        check(spool.get("depth") == 0, "spool depth starts at 0", failures)

        step(5, "An event submitted while online is delivered directly, not spooled")
        online_source_event_id = f"e2e-online-{suffix}"
        online_captured_at = dt.datetime.now(dt.UTC)
        response = submit_event(container_name, {
            "camera_id": camera_id,
            "source_event_id": online_source_event_id,
            "captured_at": online_captured_at.isoformat(),
            "objects": [{"class_name": "person", "confidence": 0.91, "bbox": [0.1, 0.1, 0.5, 0.6]}],
        })
        result = response.get("results", [{}])[0]
        check(result.get("accepted") is True, "the agent accepted the online event", failures)
        check(result.get("outcome") == "delivered", f"outcome was 'delivered', not spooled (got {result.get('outcome')!r})", failures)

        landed = wait_for_detections(tenant_id, [online_source_event_id], timeout=10.0)
        check(online_source_event_id in landed, "the online event landed as a real detection row within 10s", failures)

        step(6, f"Cut the agent off the network for real (docker network disconnect {network})")
        docker("network", "disconnect", network, container_name)
        disconnected = True

        offline_events = []
        for i in range(OFFLINE_EVENT_COUNT):
            source_event_id = f"e2e-offline-{i}-{suffix}"
            captured_at = dt.datetime.now(dt.UTC)
            offline_events.append((source_event_id, captured_at))

        step(7, f"Submit {OFFLINE_EVENT_COUNT} events to the agent's local listener while it's cut off")
        for source_event_id, captured_at in offline_events:
            response = submit_event(container_name, {
                "camera_id": camera_id,
                "source_event_id": source_event_id,
                "captured_at": captured_at.isoformat(),
                "objects": [{"class_name": "person", "confidence": 0.85, "bbox": [0.2, 0.2, 0.4, 0.4]}],
            })
            result = response.get("results", [{}])[0]
            check(
                result.get("outcome") == "spooled",
                f"{source_event_id}: agent reports 'spooled' (got {result.get('outcome')!r})", failures,
            )

        offline_ids = [sid for sid, _ in offline_events]
        ids_sql = ", ".join(f"'{sid}'" for sid in offline_ids)
        present_while_offline = psql(
            f"SELECT count(*) FROM detections WHERE tenant_id = '{tenant_id}' "
            f"AND source_event_id IN ({ids_sql})"
        )
        check(present_while_offline == "0", "nothing reached the server while genuinely cut off (queried directly)", failures)

        # A device that cannot reach the API cannot report a spool_depth via heartbeat
        # either - so the honest place to look for "did nothing arrive" is the DB above,
        # not the device's own (necessarily stale) health snapshot. Confirmed separately
        # here for completeness.
        _, device_while_offline = api(f"/api/v1/tenant/edge/devices/{device_id}", token=token, method="GET")
        offline_spool = (device_while_offline.get("health") or {}).get("spool") or {}
        check(
            offline_spool.get("depth") == 0,
            "the last-known spool depth is still 0 while cut off - a heartbeat can't carry "
            "a number it can't deliver", failures,
        )

        step(8, "Wait until the sync loop's own backoff guarantees it won't out-race the next heartbeat")
        deep_backoff = wait_for_deep_backoff(
            container_name, failures=FAILURES_BEFORE_RECONNECT, timeout=DEEP_BACKOFF_TIMEOUT_SECONDS
        )
        check(deep_backoff, f"observed {FAILURES_BEFORE_RECONNECT} consecutive drain failures within {DEEP_BACKOFF_TIMEOUT_SECONDS}s", failures)

        step(9, f"Restore connectivity (docker network connect {network}) - nothing here triggers a drain")
        docker("network", "connect", network, container_name)
        disconnected = False

        step(10, "spool_depth, observed via the real heartbeat/health API, rises before the drain empties it")
        max_depth = poll_max_spool_depth(token, device_id, timeout=SPOOL_DEPTH_POLL_TIMEOUT_SECONDS)
        check(max_depth > 0, f"a nonzero spool_depth was observed via the API (max seen: {max_depth})", failures)
        check(
            max_depth == OFFLINE_EVENT_COUNT,
            f"the observed depth matched the real backlog size (expected {OFFLINE_EVENT_COUNT}, saw {max_depth})",
            failures,
        )

        step(11, f"Poll for the backlog to drain automatically (up to {FULL_DRAIN_TIMEOUT_SECONDS}s, no manual trigger)")
        landed = wait_for_detections(tenant_id, offline_ids, timeout=FULL_DRAIN_TIMEOUT_SECONDS)
        check(
            set(landed) == set(offline_ids),
            f"every spooled event now exists server-side ({len(landed)}/{len(offline_ids)})", failures,
        )

        step(12, "The original captured_at survived the outage - not the delivery time (FLOW-13's conflict policy)")
        for source_event_id, captured_at in offline_events:
            stored_epoch = landed.get(source_event_id)
            if stored_epoch is None:
                check(False, f"{source_event_id}: no row to compare (did not land)", failures)
                continue
            drift = abs(float(stored_epoch) - captured_at.timestamp())
            check(
                drift < 1.0,
                f"{source_event_id}: stored capture_time matches the original captured_at "
                f"(drift {drift:.3f}s), not the delivery time (minutes later)", failures,
            )

        step(13, "No duplicates: exactly one detection row per source_event_id")
        all_ids = [online_source_event_id, *offline_ids]
        all_ids_sql = ", ".join(f"'{sid}'" for sid in all_ids)
        dup_rows = psql_rows(
            "SELECT source_event_id || '|' || count(*) FROM detections "
            f"WHERE tenant_id = '{tenant_id}' AND source_event_id IN ({all_ids_sql}) "
            "GROUP BY source_event_id"
        )
        counts = {}
        for row in dup_rows:
            sid, _, n = row.partition("|")
            counts[sid] = int(n)
        for sid in all_ids:
            check(counts.get(sid) == 1, f"{sid}: exactly one detection row (got {counts.get(sid, 0)})", failures)

    finally:
        step("cleanup", "Stop and remove the container, the volume, and every test row")
        if disconnected and network and container_started:
            docker("network", "connect", network, container_name, check=False)
        if container_started:
            docker("rm", "-f", container_name, check=False)
            print(f"    removed container {container_name}")
        if volume_created:
            docker("volume", "rm", volume_name, check=False)
            print(f"    removed volume {volume_name}")
        if tenant_id:
            # Cascades cameras, sites, edge_devices, edge_enrolment_tokens,
            # edge_health_events and detections (all FK ... ON DELETE CASCADE from
            # tenants) - verified against the live schema, the same discipline
            # e2e_webhook_dispatch.py documents for outbox_events' own, different case.
            psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
            psql(f"DELETE FROM organizations WHERE display_name = '{organization_name}'")
            psql(f"DELETE FROM users WHERE email_normalized = '{email}'")
            leftover = psql(
                f"SELECT count(*) FROM detections WHERE tenant_id = '{tenant_id}'"
            )
            check(leftover == "0", "no detection rows survive the tenant's deletion", failures)
            print("    test tenant, organization, user and all its detections removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        print(f"  (check `docker logs {container_name}` for the agent's own account of what happened, "
              "though the container is now removed - rerun with cleanup disabled to inspect it live)")
        return 1
    print(
        "PASS - a real enrolled edge-agent container delivered a live event, was cut off "
        "for real, spooled events locally while genuinely unreachable, and drained them "
        "automatically on reconnect with original captured_at preserved and no duplicates"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
