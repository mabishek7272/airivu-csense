"""Proves Task 5 of `docs/superpowers/plans/2026-09-02-diagnostic-access-and-config-
desired-state.md`: `POST /api/v1/tenant/edge/devices/{id}/config` (Task 4, FLOW-13) does
not merely bump a counter that nothing downstream honours - a real, running `edge-agent`
container genuinely changes its heartbeat cadence when the cloud pushes a new one, an
already-expired config command is genuinely never applied, and an out-of-bounds value is
refused by the server before it ever reaches the device.

Runs against a real `edge-agent` container, reusing `scripts/e2e_edge_spool.py`'s
container-start/enrol/teardown pattern rather than re-deriving it.

Covers, in order:
  1. A real enrolled device starts at `desired_state_version == observed_state_version == 0`.
  2. Its *actual* heartbeat cadence is measured before any push, by polling the real
     `last_seen_at` timestamp `GET /devices/{id}` returns (not a log line, not an assumed
     constant) - proving the "before" baseline is real, not asserted from `config.py`'s
     default.

     **Why the "before" cadence is ~30s regardless of the agent's own boot-time
     `CSENSE_HEARTBEAT_INTERVAL_SECONDS`** (discovered by an earlier, wrong-expectation
     run of this exact script - not guessed): `heartbeat_loop`
     (`backend/edge_agent/app/main.py`) applies the server's `next_interval_seconds`
     advisory to `runtime_config.heartbeat_interval_seconds` *inside the same iteration*
     as the heartbeat that receives it, before that iteration's own `_wait` call - so even
     the very first inter-heartbeat gap already reflects the server's advisory, never the
     boot config. `POST /heartbeat` (`backend/tenant_api/app/api/edge.py`) always returns
     the same constant `HEARTBEAT_INTERVAL_SECONDS = 30`, and nothing has pinned the
     interval yet (`heartbeat_interval_pinned_by_config_push` only becomes true once a
     config-push has named the field) - so the real, measured "before" cadence is that
     constant, not whatever the agent booted with. This is documented, intended FLOW-13
     precedence ("before any config-push names this field, the advisory still governs"),
     not a bug - so this script measures against the real 30s, and pushes a genuinely
     *halved* 15s afterward, rather than asserting a boot value nothing can observe.
  3. A config push halves that real 30s cadence to 15s - both comfortably inside
     `HEARTBEAT_INTERVAL_BOUNDS = (5, 3600)` from `backend/edge_agent/app/config.py`, sized
     so 2-3 real cycles are observable in well under a minute. `desired_state_version`
     bumps to 1, confirmed via the real API.
  4. `observed_state_version` catches up to `desired_state_version` (also via a real
     `GET /devices/{id}` poll - Task 2 of the offline-spool plan's own claim that both
     counters are exposed there, re-confirmed rather than assumed).
  5. The cadence is measured again, the same way, *after* convergence - and is genuinely
     ~15s now, not ~30s. This is the assertion that proves "observed running": an echoed
     `observed_state_version` alone would not show this; a real second measurement of real
     elapsed wall-clock time between real heartbeats does.
  6. A second config push (to 5s) is issued for real through the real route, then its
     `device_commands` row is backdated directly (`expires_at = now() - interval '1
     minute'`) - the same technique `backend/tests/test_edge_command_config_push.py`'s own
     expired-command test uses, since the route's `ttl_seconds` has a 30s floor and cannot
     express "already expired" at issuance. `desired_state_version` bumps to 2 (bumped at
     issuance, per FLOW-13/the route's own docstring) but `observed_state_version` never
     moves past 1, and the heartbeat cadence measured across the wait stays ~15s, never
     dropping to ~5s - proving the expired push was never applied, not just unacknowledged.
  7. A config push naming an out-of-bounds `heartbeat_interval_seconds` (0, then -1) is
     refused with a real 422 by the server itself - `ConfigPushIn`'s own `ge=1` field
     constraint - before a command row is ever created or `desired_state_version` moves.
     This is the *server*-side bounds check; Task 4's agent-side `validate_config_push`
     bounds are already covered by unit tests, not by this script.
  8. Cleanup: container, volume, tenant/org/user/device.

    python scripts/e2e_edge_desired_state.py
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from itertools import pairwise

API = "http://localhost:8080"
PASSWORD = "EdgeDesiredStateE2E!Password123"

# `POST /heartbeat`'s own constant advisory (`HEARTBEAT_INTERVAL_SECONDS`,
# backend/tenant_api/app/api/edge.py) - what the REAL "before any config-push" cadence
# converges to regardless of the agent's own boot config, per the module docstring above.
# The container is booted at this same value (see step 3) purely for clarity - it does not
# actually control the measured "before" cadence, the server's advisory does.
SERVER_HEARTBEAT_ADVISORY_SECONDS = 30
# Half of the real "before" cadence - the plan's own suggested "clearly-measurable change".
# Comfortably inside `HEARTBEAT_INTERVAL_BOUNDS = (5, 3600)`, backend/edge_agent/app/config.py.
HEARTBEAT_AFTER_SECONDS = 15
# What the *second*, expired push asks for - distinct from both of the above, so if this
# value ever leaked into the running cadence it would be unambiguous, not a coincidence
# with either real value.
HEARTBEAT_EXPIRED_PUSH_SECONDS = 5

# The device-facing command poll (backend/edge_agent/app/main.py's COMMAND_POLL_SECONDS) is
# a fixed 30s, independent of any pushed heartbeat interval - so a push can take up to ~30s
# to even be noticed, plus up to one more heartbeat cycle before it's reported. Generous on
# purpose, the same discipline e2e_edge_spool.py's own backoff/drain timeouts use.
# The "before" measurement needs 2 full real gaps at the ~30s server-advisory cadence
# (see the module docstring) to land 2 samples, i.e. ~60s minimum - generous margin on top
# of that so normal container/network scheduling jitter can never turn a real pass into a
# false timeout.
CONVERGENCE_TIMEOUT_SECONDS = 90
CADENCE_POLL_TIMEOUT_SECONDS = 100
EXPIRED_WAIT_SECONDS = 50


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


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["docker", *args], capture_output=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"docker {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.decode(errors='replace')[:800]}"
        )
    return result


def compose_project_and_network() -> tuple[str, str]:
    """Mirrors `e2e_edge_spool.py`'s own helper - discovers the real compose project/
    network from the already-running `tenant-api` container rather than hardcoding it."""
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
    subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "build", "edge-agent"],
        cwd="infra", check=True,
    )


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def get_device(token: str, device_id: str) -> dict:
    _, device = api(f"/api/v1/tenant/edge/devices/{device_id}", token=token, method="GET")
    return device


def wait_for_device_online(token: str, device_id: str, *, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    device: dict = {}
    while time.monotonic() < deadline:
        device = get_device(token, device_id)
        if device.get("status") == "online" and device.get("online"):
            return device
        time.sleep(1.0)
    return device


def push_config(token: str, device_id: str, *, idempotency_key: str, expect=(201,), **fields) -> tuple[int, dict]:
    payload = {**fields, "idempotency_key": idempotency_key}
    return api(f"/api/v1/tenant/edge/devices/{device_id}/config", payload, token, expect=expect)


def wait_for_observed_version(token: str, device_id: str, *, target: int, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    device: dict = {}
    while time.monotonic() < deadline:
        device = get_device(token, device_id)
        if device.get("observed_state_version") == target:
            return device
        time.sleep(1.5)
    return device


def measure_heartbeat_intervals(token: str, device_id: str, *, count: int, timeout: float) -> list[float]:
    """Polls the real `last_seen_at` field on `GET /devices/{id}` (written by the real
    `/heartbeat` route on every real heartbeat the agent sends) at a fine grain (well under
    any cadence this script ever pushes) and returns the real elapsed seconds between
    `count - 1` pairs of *distinct* observed values - i.e. the agent's actual measured
    heartbeat cadence, not an echoed config value.
    """
    deadline = time.monotonic() + timeout
    timestamps: list[dt.datetime] = []
    last_raw: str | None = None
    while time.monotonic() < deadline and len(timestamps) < count:
        device = get_device(token, device_id)
        raw = device.get("last_seen_at")
        if raw and raw != last_raw:
            timestamps.append(dt.datetime.fromisoformat(raw.replace("Z", "+00:00")))
            last_raw = raw
        time.sleep(0.3)
    return [(b - a).total_seconds() for a, b in pairwise(timestamps)]


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []
    organization_name = f"Edge Desired State E2E {suffix}"
    email = f"owner-{suffix}@northwind.example"
    container_name = f"csense-edge-desired-state-e2e-{suffix}"
    volume_name = f"csense-edge-desired-state-e2e-{suffix}"

    tenant_id = None
    container_started = False
    volume_created = False
    project = network = image = None

    try:
        step(0, "Discover the real compose project/network and build the agent image")
        project, network = compose_project_and_network()
        image = f"{project}-edge-agent:latest"
        print(f"    project={project} network={network} image={image}")
        build_edge_agent_image()
        check(bool(docker("image", "inspect", image, check=False).returncode == 0), f"image '{image}' exists after build", failures)

        step(1, "Register a tenant and create a site through the real API")
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

        step(2, "Create a device record and issue a real, single-use enrolment token")
        status, device = api("/api/v1/tenant/edge/devices", {
            "name": "E2E Desired State Agent", "site_id": site_id,
            "device_type": "pc_linux", "role": "inference",
        }, token, expect=(201,))
        check(status == 201, "device record created", failures)
        device_id = device["id"]
        check(device["desired_state_version"] == 0, "a fresh device record starts at desired_state_version 0", failures)
        check(device["observed_state_version"] == 0, "a fresh device record starts at observed_state_version 0", failures)

        status, enrolment = api(
            f"/api/v1/tenant/edge/devices/{device_id}/enrolment-token", None, token, expect=(201,)
        )
        check(status == 201, "enrolment token issued", failures)
        enrolment_token = enrolment["token"]

        step(3, f"Start the real edge-agent container, heartbeat interval {SERVER_HEARTBEAT_ADVISORY_SECONDS}s")
        docker("volume", "create", volume_name)
        volume_created = True
        docker(
            "run", "-d", "--name", container_name, "--network", network,
            "-e", "CSENSE_API_BASE_URL=http://tenant-api:8000",
            "-e", "CSENSE_STATE_DIR=/var/lib/csense-agent",
            "-e", f"CSENSE_ENROLMENT_TOKEN={enrolment_token}",
            "-e", f"CSENSE_SERIAL_NUMBER=e2e-desired-state-{suffix}",
            "-e", f"CSENSE_HEARTBEAT_INTERVAL_SECONDS={SERVER_HEARTBEAT_ADVISORY_SECONDS}",
            "-v", f"{volume_name}:/var/lib/csense-agent",
            "--tmpfs", "/tmp",
            "--read-only",
            "--security-opt", "no-new-privileges:true",
            image,
        )
        container_started = True

        step(4, "The agent enrols for real and the device reaches 'online' at version (0, 0)")
        device_after_enrol = wait_for_device_online(token, device_id)
        check(device_after_enrol.get("status") == "online", "device status is 'online'", failures)
        check(device_after_enrol.get("online") is True, "device's derived 'online' flag is true", failures)
        check(device_after_enrol.get("desired_state_version") == 0, "desired_state_version is still 0 at enrolment", failures)
        check(device_after_enrol.get("observed_state_version") == 0, "observed_state_version is still 0 at enrolment", failures)

        step(5, f"Measure the REAL heartbeat cadence before any push (expect ~{SERVER_HEARTBEAT_ADVISORY_SECONDS}s - the server's own advisory, not necessarily the agent's boot config, see module docstring)")
        before_intervals = measure_heartbeat_intervals(
            token, device_id, count=3, timeout=CADENCE_POLL_TIMEOUT_SECONDS
        )
        check(
            len(before_intervals) == 2,
            f"observed 2 real inter-heartbeat gaps before the push (got {len(before_intervals)}: {before_intervals})",
            failures,
        )
        before_avg = sum(before_intervals) / len(before_intervals) if before_intervals else float("nan")
        print(f"    measured before-push intervals: {[round(x, 2) for x in before_intervals]}s (avg {before_avg:.2f}s)")
        check(
            all(SERVER_HEARTBEAT_ADVISORY_SECONDS - 5 <= x <= SERVER_HEARTBEAT_ADVISORY_SECONDS + 8 for x in before_intervals),
            f"each measured gap is close to the real pre-push cadence of {SERVER_HEARTBEAT_ADVISORY_SECONDS}s", failures,
        )

        step(6, f"Push a config change halving the heartbeat interval to {HEARTBEAT_AFTER_SECONDS}s")
        status, pushed = push_config(
            token, device_id, idempotency_key=f"e2e-desired-state-halve-{suffix}",
            heartbeat_interval_seconds=HEARTBEAT_AFTER_SECONDS,
        )
        check(status == 201, "config push accepted (201)", failures)
        check(
            pushed.get("device", {}).get("desired_state_version") == 1,
            f"desired_state_version bumped to 1 in the push response (got {pushed.get('device', {}).get('desired_state_version')})",
            failures,
        )
        first_command_id = pushed.get("command", {}).get("id")

        step(7, "desired_state_version == 1, confirmed via a fresh, real GET /devices/{id}")
        device_after_push = get_device(token, device_id)
        check(device_after_push.get("desired_state_version") == 1, "GET confirms desired_state_version is 1", failures)

        step(8, f"Wait (up to {CONVERGENCE_TIMEOUT_SECONDS}s) for observed_state_version to converge to 1")
        converged = wait_for_observed_version(token, device_id, target=1, timeout=CONVERGENCE_TIMEOUT_SECONDS)
        check(
            converged.get("observed_state_version") == 1,
            f"observed_state_version caught up to desired_state_version (got {converged.get('observed_state_version')})",
            failures,
        )

        step(9, f"Measure the REAL heartbeat cadence AFTER the push (expect ~{HEARTBEAT_AFTER_SECONDS}s, not {SERVER_HEARTBEAT_ADVISORY_SECONDS}s)")
        after_intervals = measure_heartbeat_intervals(
            token, device_id, count=3, timeout=CADENCE_POLL_TIMEOUT_SECONDS
        )
        check(
            len(after_intervals) == 2,
            f"observed 2 real inter-heartbeat gaps after the push (got {len(after_intervals)}: {after_intervals})",
            failures,
        )
        after_avg = sum(after_intervals) / len(after_intervals) if after_intervals else float("nan")
        print(f"    measured after-push intervals: {[round(x, 2) for x in after_intervals]}s (avg {after_avg:.2f}s)")
        check(
            all(HEARTBEAT_AFTER_SECONDS - 3 <= x <= HEARTBEAT_AFTER_SECONDS + 6 for x in after_intervals),
            f"each measured gap is close to the pushed {HEARTBEAT_AFTER_SECONDS}s cadence, not the old {SERVER_HEARTBEAT_ADVISORY_SECONDS}s", failures,
        )
        check(
            after_avg < before_avg * 0.75,
            f"the agent's ACTUAL measured cadence genuinely changed (before avg {before_avg:.2f}s -> after avg {after_avg:.2f}s), "
            "not just an echoed value", failures,
        )

        step(10, f"Push a second config change to {HEARTBEAT_EXPIRED_PUSH_SECONDS}s, then back-date it to already-expired")
        status, expired_pushed = push_config(
            token, device_id, idempotency_key=f"e2e-desired-state-expired-{suffix}",
            heartbeat_interval_seconds=HEARTBEAT_EXPIRED_PUSH_SECONDS,
        )
        check(status == 201, "second config push accepted (201)", failures)
        check(
            expired_pushed.get("device", {}).get("desired_state_version") == 2,
            f"desired_state_version bumped to 2 at ISSUANCE, per FLOW-13 (got {expired_pushed.get('device', {}).get('desired_state_version')})",
            failures,
        )
        second_command_id = expired_pushed.get("command", {}).get("id")
        check(
            second_command_id not in (None, first_command_id),
            "the second push minted a genuinely new command row, distinct from the first", failures,
        )

        # The route's own `ttl_seconds` has a 30s floor (DEFAULT_COMMAND_TTL_SECONDS /
        # ge=30 in edge.py's ConfigPushIn) - there is no way to ask the real route for an
        # already-expired command at issuance. So the command is issued for real, signed
        # for real, version-bumped for real, then its `expires_at` is rewound directly -
        # the exact technique backend/tests/test_edge_command_config_push.py's own expired-
        # command test uses, mirrored here at the database layer since neither `/config` nor
        # `/commands` exposes a "backdate this" API of its own. Targeted by this command's
        # own id, not "every command for this device", so the first (already-converged,
        # completed) command is left untouched.
        psql(
            "UPDATE device_commands SET expires_at = now() - interval '1 minute' "
            f"WHERE id = '{second_command_id}'"
        )
        # Postgres casts a boolean expression to `::text` as the words "true"/"false" (not
        # the "t"/"f" psql prints for a bare boolean *column* - confirmed against a live
        # container before relying on it, since the two really do differ).
        backdated = psql(
            f"SELECT (expires_at < now())::text FROM device_commands WHERE id = '{second_command_id}'"
        )
        check(backdated == "true", "the second command's expires_at is genuinely in the past now", failures)

        step(11, f"Wait {EXPIRED_WAIT_SECONDS}s (past a full command-poll cycle) and confirm the expired push was NEVER applied")
        time.sleep(EXPIRED_WAIT_SECONDS)
        device_after_expired_wait = get_device(token, device_id)
        check(
            device_after_expired_wait.get("desired_state_version") == 2,
            "desired_state_version stays at 2 (the expired command was issued, that part is real)", failures,
        )
        check(
            device_after_expired_wait.get("observed_state_version") == 1,
            f"observed_state_version did NOT move past 1 - the expired command was never applied "
            f"(got {device_after_expired_wait.get('observed_state_version')})",
            failures,
        )

        step(12, f"Confirm the agent's ACTUAL cadence still reflects the last real push (~{HEARTBEAT_AFTER_SECONDS}s), not the expired one ({HEARTBEAT_EXPIRED_PUSH_SECONDS}s)")
        post_expired_intervals = measure_heartbeat_intervals(
            token, device_id, count=3, timeout=CADENCE_POLL_TIMEOUT_SECONDS
        )
        check(
            len(post_expired_intervals) == 2,
            f"observed 2 more real inter-heartbeat gaps (got {len(post_expired_intervals)}: {post_expired_intervals})",
            failures,
        )
        post_expired_avg = (
            sum(post_expired_intervals) / len(post_expired_intervals) if post_expired_intervals else float("nan")
        )
        print(f"    measured post-expired-push intervals: {[round(x, 2) for x in post_expired_intervals]}s (avg {post_expired_avg:.2f}s)")
        check(
            all(HEARTBEAT_AFTER_SECONDS - 3 <= x <= HEARTBEAT_AFTER_SECONDS + 6 for x in post_expired_intervals),
            f"cadence is still ~{HEARTBEAT_AFTER_SECONDS}s, not the expired push's {HEARTBEAT_EXPIRED_PUSH_SECONDS}s", failures,
        )

        step(13, "A config push with heartbeat_interval_seconds=0 is refused by the SERVER itself (422), before it ever reaches the device")
        status, _rejected_zero = push_config(
            token, device_id, idempotency_key=f"e2e-desired-state-bounds-zero-{suffix}",
            heartbeat_interval_seconds=0, expect=(422,),
        )
        check(status == 422, f"heartbeat_interval_seconds=0 rejected with 422 (got {status})", failures)

        step(14, "A config push with heartbeat_interval_seconds=-1 is likewise refused (422)")
        status, _rejected_negative = push_config(
            token, device_id, idempotency_key=f"e2e-desired-state-bounds-negative-{suffix}",
            heartbeat_interval_seconds=-1, expect=(422,),
        )
        check(status == 422, f"heartbeat_interval_seconds=-1 rejected with 422 (got {status})", failures)

        step(15, "Neither rejected push created a command row or moved desired_state_version")
        device_after_bounds = get_device(token, device_id)
        check(
            device_after_bounds.get("desired_state_version") == 2,
            f"desired_state_version is still 2 - the two out-of-bounds pushes minted nothing "
            f"(got {device_after_bounds.get('desired_state_version')})",
            failures,
        )
        bounds_rows = psql(
            "SELECT count(*) FROM device_commands WHERE edge_device_id = "
            f"'{device_id}' AND idempotency_key IN "
            f"('e2e-desired-state-bounds-zero-{suffix}', 'e2e-desired-state-bounds-negative-{suffix}')"
        )
        check(bounds_rows == "0", "no device_commands row exists for either rejected push", failures)

    finally:
        step("cleanup", "Stop and remove the container, the volume, and every test row")
        if container_started:
            docker("rm", "-f", container_name, check=False)
            print(f"    removed container {container_name}")
        if volume_created:
            docker("volume", "rm", volume_name, check=False)
            print(f"    removed volume {volume_name}")
        if tenant_id:
            # Cascades cameras, sites, edge_devices, edge_enrolment_tokens,
            # edge_health_events and device_commands (all FK ... ON DELETE CASCADE from
            # tenants) - same discipline e2e_edge_spool.py and e2e_diagnostic_access.py
            # already document for their own cases.
            psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
            psql(f"DELETE FROM organizations WHERE display_name = '{organization_name}'")
            psql(f"DELETE FROM users WHERE email_normalized = '{email}'")
            leftover = psql(
                f"SELECT count(*) FROM edge_devices WHERE tenant_id = '{tenant_id}'"
            )
            check(leftover == "0", "no edge_devices rows survive the tenant's deletion", failures)
            print("    test tenant, organization, user and device removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        print(f"  (check `docker logs {container_name}` for the agent's own account of what happened, "
              "though the container is now removed - rerun with cleanup disabled to inspect it live)")
        return 1
    print(
        "PASS - a real enrolled edge-agent container's ACTUAL measured heartbeat cadence "
        "changed after a real config push (not just an echoed version number), "
        "observed_state_version converged to desired_state_version via the real API, an "
        "already-expired config push was genuinely never applied (version gap stayed "
        "visible, cadence never reflected it), and the server itself refused two "
        "out-of-bounds pushes with 422 before either ever reached the device"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
