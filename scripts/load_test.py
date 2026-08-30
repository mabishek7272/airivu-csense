"""Real load/spike/endurance/failure-injection test suite (CHECKLIST: "Load/spike/
endurance/failure-injection test suite (automatable, local-scale)") - against the real
running stack (real Postgres, real Redis, real HTTP through Traefik), not simulated.

**Deliberately scaled to this local, single-host deployment.** Concurrency and duration
here name real numbers this machine can sustain without itself becoming the bottleneck
being measured - not what a load test against dedicated production infrastructure would
use. A production-scale run (hundreds of concurrent users, hours of soak) needs its own
dedicated environment and its own pass; this proves the mechanism and the four kinds of
test it should run, for real, against the real API.

Four phases, one throwaway tenant registered once and reused for all of them:

1. **Baseline load**: `BASELINE_CONCURRENCY` workers hitting a mix of real,
   authenticated, DB-backed read endpoints for `BASELINE_SECONDS` - p50/p95/p99 latency
   and error rate.
2. **Spike**: a sudden burst to `SPIKE_CONCURRENCY` workers (3x baseline) for a short
   window - does error rate/latency blow up disproportionately, or degrade gracefully.
3. **Endurance**: baseline concurrency sustained for `ENDURANCE_SECONDS` (longer, but
   still a local-scale minutes-not-hours exercise), split into time buckets - compares
   the first bucket's p95 to the last bucket's to catch a slow leak/degradation a short
   burst would never surface.
4. **Failure injection**: stops the real redis container mid-run, confirms
   `GET /readyz` reports `degraded` (not a hang or crash) with redis specifically marked
   `unavailable`, restarts it, confirms recovery to `ok` within a bounded time. Wrapped
   in `try/finally` so redis is guaranteed to come back up even if an assertion fails
   mid-test.

    python scripts/load_test.py
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import httpx

API = "http://localhost:8080"
PASSWORD = "LoadTestE2E!Password123"

BASELINE_CONCURRENCY = 10
BASELINE_SECONDS = 15
SPIKE_CONCURRENCY = 30
SPIKE_SECONDS = 8
ENDURANCE_CONCURRENCY = 10
ENDURANCE_SECONDS = 45
ENDURANCE_BUCKET_SECONDS = 15


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
        raise RuntimeError(f"{method} {path} -> {exc.code}: {body[:400]}") from exc


def psql(sql: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def readyz_check() -> dict:
    """`GET /readyz` deliberately has no Traefik route (`infra/traefik/dynamic.yml` only
    routes `/api/v1/*` and media paths to the backends) - a health/readiness endpoint is
    for orchestration, not the public edge, and that boundary is a real design choice
    worth keeping rather than working around by exposing it. Reached instead the way
    Docker's own container healthcheck would reach it: from inside the network, via
    `docker compose exec`, using only the stdlib already installed in the tenant-api
    image (no curl in a `python:slim` base, no new dependency for this)."""
    inline_script = (
        "import urllib.request as u; import sys; "
        "sys.stdout.write(u.urlopen('http://localhost:8000/readyz', timeout=10).read().decode())"
    )
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "tenant-api", "python", "-c", inline_script],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout.strip())


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


class Sample:
    __slots__ = ("elapsed_at", "latency_ms", "status")

    def __init__(self, elapsed_at: float, latency_ms: float, status: int):
        self.elapsed_at = elapsed_at
        self.latency_ms = latency_ms
        self.status = status


async def run_phase(client: httpx.AsyncClient, token: str, urls: list[str], *, concurrency: int, duration_seconds: float) -> list[Sample]:
    """`concurrency` workers loop making requests (round-robin over `urls`) until
    `duration_seconds` elapses, each recording its own latency and status - a plain
    worker-pool pattern, no external load-testing tool pulled in for this."""
    samples: list[Sample] = []
    lock = asyncio.Lock()
    start = time.monotonic()
    deadline = start + duration_seconds

    async def worker(worker_id: int):
        i = worker_id
        while time.monotonic() < deadline:
            url = urls[i % len(urls)]
            i += 1
            request_start = time.monotonic()
            try:
                response = await client.get(url, headers={"Authorization": f"Bearer {token}", "Host": "app.localhost"})
                status = response.status_code
            except httpx.HTTPError:
                status = 0  # a real connection-level failure, not an HTTP status
            latency_ms = (time.monotonic() - request_start) * 1000
            async with lock:
                samples.append(Sample(request_start - start, latency_ms, status))

    await asyncio.gather(*(worker(w) for w in range(concurrency)))
    return samples


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(len(ordered) * pct), len(ordered) - 1)
    return ordered[index]


def summarize(name: str, samples: list[Sample], failures: list[str], *, max_error_rate: float = 0.05) -> None:
    if not samples:
        check(False, f"{name}: zero samples collected", failures)
        return
    latencies = [s.latency_ms for s in samples]
    errors = [s for s in samples if s.status == 0 or s.status >= 500]
    error_rate = len(errors) / len(samples)
    p50, p95, p99 = percentile(latencies, 0.50), percentile(latencies, 0.95), percentile(latencies, 0.99)
    print(
        f"    {name}: {len(samples)} requests, "
        f"p50={p50:.0f}ms p95={p95:.0f}ms p99={p99:.0f}ms error_rate={error_rate:.1%}"
    )
    check(error_rate <= max_error_rate, f"{name}: error rate {error_rate:.1%} <= {max_error_rate:.0%}", failures)


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    step(1, "Register a throwaway tenant with real data to read")
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Load Test {suffix}",
        "email": f"owner-{suffix}@northwind.example",
        "password": PASSWORD, "display_name": "Owner",
    })
    token, tenant_id = auth["access_token"], auth["tenant_id"]
    _, site = api("/api/v1/tenant/sites", {"name": f"Site {suffix}", "code": f"site-{suffix}"}, token, expect=(201,))
    for i in range(5):
        api("/api/v1/tenant/cameras", {
            "site_id": site["id"], "name": f"Camera {i}", "code": f"cam-{suffix}-{i}",
        }, token, expect=(201,))

    urls = [
        f"{API}/api/v1/tenant/cameras",
        f"{API}/api/v1/tenant/license",
        f"{API}/api/v1/tenant/sites",
        f"{API}/api/v1/tenant/audit-events",
    ]

    async def run_load_phases() -> None:
        async with httpx.AsyncClient(timeout=30.0, limits=httpx.Limits(max_connections=SPIKE_CONCURRENCY + 5)) as client:
            step(2, f"Baseline load: {BASELINE_CONCURRENCY} concurrent workers for {BASELINE_SECONDS}s")
            samples = await run_phase(client, token, urls, concurrency=BASELINE_CONCURRENCY, duration_seconds=BASELINE_SECONDS)
            summarize("baseline", samples, failures)

            step(3, f"Spike: a sudden burst to {SPIKE_CONCURRENCY} concurrent workers for {SPIKE_SECONDS}s")
            spike_samples = await run_phase(client, token, urls, concurrency=SPIKE_CONCURRENCY, duration_seconds=SPIKE_SECONDS)
            # A spike is allowed to be slower and to shed a few more requests than
            # baseline - it is not allowed to fall over. A higher, but still bounded,
            # error-rate ceiling names that distinction explicitly.
            summarize("spike", spike_samples, failures, max_error_rate=0.10)

            step(4, f"Endurance: {ENDURANCE_CONCURRENCY} concurrent workers sustained for {ENDURANCE_SECONDS}s")
            endurance_samples = await run_phase(
                client, token, urls, concurrency=ENDURANCE_CONCURRENCY, duration_seconds=ENDURANCE_SECONDS,
            )
            summarize("endurance (overall)", endurance_samples, failures)

            first_bucket = [s.latency_ms for s in endurance_samples if s.elapsed_at < ENDURANCE_BUCKET_SECONDS]
            last_bucket = [s.latency_ms for s in endurance_samples if s.elapsed_at >= ENDURANCE_SECONDS - ENDURANCE_BUCKET_SECONDS]
            first_p95, last_p95 = percentile(first_bucket, 0.95), percentile(last_bucket, 0.95)
            print(f"    endurance drift: first-bucket p95={first_p95:.0f}ms  last-bucket p95={last_p95:.0f}ms")
            # A real, if generous, ceiling on latency creeping up under sustained load -
            # not "must be identical" (real variance exists), but "must not be visibly
            # getting worse over time", which is what a leak or unbounded queue looks like.
            check(
                last_p95 <= first_p95 * 2.5 + 50,
                f"no runaway latency drift under sustained load (last p95 {last_p95:.0f}ms "
                f"vs first p95 {first_p95:.0f}ms)", failures,
            )

    asyncio.run(run_load_phases())

    step(5, "Failure injection: stop the real redis container mid-run, confirm graceful degradation")
    try:
        subprocess.run(
            ["docker", "compose", "--env-file", "../.env", "stop", "redis"],
            cwd="infra", capture_output=True, check=True,
        )
        print("    redis stopped")
        time.sleep(2)  # let the running service's own connection actually notice

        ready = readyz_check()
        check(ready.get("status") == "degraded", f"readyz reports degraded while redis is down (got {ready})", failures)
        check(
            ready.get("dependencies", {}).get("redis") == "unavailable",
            "redis is specifically named as the unavailable dependency", failures,
        )
        check(ready.get("dependencies", {}).get("postgres") == "ok", "postgres is unaffected and still ok", failures)
    finally:
        subprocess.run(
            ["docker", "compose", "--env-file", "../.env", "start", "redis"],
            cwd="infra", capture_output=True, check=True,
        )
        print("    redis restarted")

    step(6, "Confirm real recovery within a bounded time, not just that the container is running again")
    recovered = False
    for _ in range(15):
        try:
            if readyz_check().get("status") == "ok":
                recovered = True
                break
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            pass
        time.sleep(2)
    check(recovered, "readyz returns to status=ok within 30s of redis coming back", failures)

    step(7, "Clean up")
    psql(f"DELETE FROM tenants WHERE id = '{tenant_id}'")
    psql(f"DELETE FROM organizations WHERE display_name = 'Load Test {suffix}'")
    psql(f"DELETE FROM users WHERE email_normalized = 'owner-{suffix}@northwind.example'")
    print("    test tenant and user removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - load, spike, endurance, and failure-injection all behaved correctly against the real stack")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
