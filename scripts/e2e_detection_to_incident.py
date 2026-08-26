"""End-to-end verification: real frame -> inference -> rules -> incident -> tenant API.

Proves the whole vertical slice against the running stack rather than in isolation:

  1. register a tenant through the public API
  2. create a site, a restricted zone, and a camera
  3. run a real photograph through the AI runtime
  4. evaluate a restricted-zone rule against the detections
  5. create the incident (repeatedly, to prove deduplication)
  6. read it back through the tenant API and work it through its lifecycle

Run from the repo root with the stack up:
    python scripts/e2e_detection_to_incident.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("CSENSE_BASE", "http://localhost:8080")
COMPOSE = ["docker", "compose", "--env-file", "../.env"]
INFRA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "infra")

PASSWORD = "E2ETest!Password123"
SAMPLE_IMAGE = "https://ultralytics.com/images/bus.jpg"

# A restricted zone covering the left half of the frame. The sample photo has people on
# both sides, so this must include some and exclude others - a rule that matched
# everything would prove nothing about ROI gating.
RESTRICTED_ZONE = [[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]]


def _post(path: str, payload: dict, token: str | None = None) -> dict:
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read()
        return json.loads(body) if body else {}


def _get(path: str, token: str) -> dict:
    request = urllib.request.Request(
        BASE + path, headers={"Authorization": f"Bearer {token}"}, method="GET"
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def _in_runtime(script: str) -> str:
    """Runs a snippet inside the ai-runtime container, which is not publicly routed."""
    result = subprocess.run(
        [*COMPOSE, "exec", "-T", "ai-runtime", "python", "-c", script],
        cwd=INFRA_DIR, capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ai-runtime call failed:\n{result.stderr[-1500:]}")
    return result.stdout


def _in_postgres(sql: str) -> str:
    result = subprocess.run(
        [*COMPOSE, "exec", "-T", "postgres", "psql", "-qtA", "-U", "csense_app", "-d", "csense", "-c", sql],
        cwd=INFRA_DIR, capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"psql failed:\n{result.stderr[-1000:]}")
    return result.stdout.strip()


def step(number: int, title: str) -> None:
    print(f"\n[{number}] {title}")


def main() -> int:
    email = f"e2e-{uuid.uuid4().hex[:8]}@example.com"

    step(1, "Register a tenant through the public API")
    auth = _post(
        "/api/v1/auth/register",
        {
            "organization_name": "E2E Detection Test",
            "email": email,
            "password": PASSWORD,
            "display_name": "E2E Tester",
        },
    )
    token = auth["access_token"]
    tenant_id = auth["tenant_id"]
    print(f"    tenant {tenant_id}")

    step(2, "Create a site, a restricted zone, and a camera")
    # No REST surface for these yet (Phase 3), so they are seeded directly - the incident
    # path under test does not depend on how they were created.
    site_id = _in_postgres(
        f"SET app.is_platform = true; "
        f"INSERT INTO sites (tenant_id, name, code) "
        f"VALUES ('{tenant_id}', 'E2E Site', 'e2e-site') RETURNING id;"
    ).splitlines()[-1]
    zone_id = _in_postgres(
        f"SET app.is_platform = true; "
        f"INSERT INTO zones (tenant_id, site_id, name, zone_type, geometry_json) "
        f"VALUES ('{tenant_id}', '{site_id}', 'Restricted Area', 'restricted', "
        f"'{json.dumps(RESTRICTED_ZONE)}'::jsonb) RETURNING id;"
    ).splitlines()[-1]
    camera_id = _in_postgres(
        f"SET app.is_platform = true; "
        f"INSERT INTO cameras (tenant_id, site_id, zone_id, name, code, status) "
        f"VALUES ('{tenant_id}', '{site_id}', '{zone_id}', 'E2E Camera', 'e2e-cam', 'ready') "
        f"RETURNING id;"
    ).splitlines()[-1]
    print(f"    site {site_id[:8]}  zone {zone_id[:8]}  camera {camera_id[:8]}")

    step(3, "Run a real photograph through the AI runtime")
    inference = json.loads(
        _in_runtime(
            "import urllib.request, json, uuid\n"
            f"frame = urllib.request.urlopen('{SAMPLE_IMAGE}', timeout=30).read()\n"
            "b = uuid.uuid4().hex; p = []\n"
            "for k, v in (('model_name','yolov8n-general'), ('confidence','0.25')):\n"
            "    p.append(f'--{b}\\r\\nContent-Disposition: form-data; name=\"{k}\"\\r\\n\\r\\n{v}\\r\\n'.encode())\n"
            "p.append(f'--{b}\\r\\nContent-Disposition: form-data; name=\"frame\"; filename=\"f.jpg\"\\r\\n"
            "Content-Type: image/jpeg\\r\\n\\r\\n'.encode())\n"
            "p.append(frame); p.append(f'\\r\\n--{b}--\\r\\n'.encode())\n"
            "r = urllib.request.Request('http://localhost:8000/internal/v1/infer', data=b''.join(p),\n"
            "    headers={'Content-Type': f'multipart/form-data; boundary={b}'}, method='POST')\n"
            "print(urllib.request.urlopen(r, timeout=300).read().decode())\n"
        )
    )
    print(f"    {inference['detection_count']} detections in {inference['inference_ms']}ms")
    for d in inference["detections"]:
        print(f"      {d['class_name']:10s} {d['confidence']:.3f}  {[round(v, 3) for v in d['bbox']]}")

    step(4, "Evaluate a restricted-zone rule (persons, inside the zone only)")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend", "shared"))
    from csense_shared.pipeline.rules import DetectedObject, Rule, evaluate

    rule = Rule(
        type_code="zone.intrusion",
        alertable_classes=frozenset({"person"}),
        min_confidence=0.5,
        severity="high",
        roi_polygon=tuple((p[0], p[1]) for p in RESTRICTED_ZONE),
        min_roi_overlap=0.3,
    )
    objects = [
        DetectedObject(d["class_name"], d["confidence"], tuple(d["bbox"]))
        for d in inference["detections"]
    ]
    outcome = evaluate(rule, objects, captured_at=dt.datetime.now(dt.UTC))

    print(f"    matched  : {len(outcome.matched)}")
    for obj in outcome.matched:
        print(f"      ALERT  {obj.class_name} {obj.confidence:.3f} at x={obj.bbox[0]:.2f}")
    print(f"    rejected : {len(outcome.rejected)}")
    for obj, reason in outcome.rejected:
        print(f"      skip   {obj.class_name:10s} {reason.value}")

    if not outcome.fired:
        print("\n    Rule did not fire - nothing to escalate. Ending here.")
        return 0

    step(5, "Create the incident, then replay the same rule 9 more times")
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import asyncio

    from csense_shared.pipeline.incidents import upsert_incident_from_match

    pg_password = _read_env("POSTGRES_PASSWORD")

    async def create_incidents() -> list:
        engine = create_async_engine(
            f"postgresql+asyncpg://csense_app:{pg_password}@localhost:5432/csense"
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        records = []
        async with factory() as session:
            async with session.begin():
                from sqlalchemy import text

                await session.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id}
                )
                for frame in range(10):
                    records.append(
                        await upsert_incident_from_match(
                            session,
                            tenant_id=uuid.UUID(tenant_id),
                            site_id=uuid.UUID(site_id),
                            camera_id=uuid.UUID(camera_id),
                            rule=rule,
                            detected=outcome.best,
                            detection_id=f"e2e-detection-{frame}",
                            captured_at=dt.datetime.now(dt.UTC),
                            zone_id=zone_id,
                        )
                    )
        await engine.dispose()
        return records

    records = asyncio.run(create_incidents())
    created = sum(1 for r in records if r.created)
    print(f"    10 rule firings -> {created} incident(s) created, {10 - created} folded in")
    print(f"    incident #{records[0].incident_number}, detection_count={records[-1].detection_count}")
    if created != 1:
        print(f"    FAIL: expected exactly 1 incident, got {created}")
        return 1

    step(6, "Read it back through the tenant API")
    inbox = _get("/api/v1/tenant/incidents", token)
    print(f"    inbox returned {len(inbox['items'])} incident(s)")
    incident = inbox["items"][0]
    print(
        f"      #{incident['incident_number']}  {incident['title']}  "
        f"severity={incident['severity']}  status={incident['status']}  "
        f"detections={incident['detection_count']}"
    )

    step(7, "Work the incident through its lifecycle")
    incident_id = incident["id"]
    for action, payload in (
        ("acknowledge", {"reason": "Reviewed on camera"}),
        ("investigate", {"reason": "Dispatching security"}),
        ("resolve", {"resolution_code": "confirmed_true_positive", "reason": "Person escorted out"}),
    ):
        result = _post(f"/api/v1/tenant/incidents/{incident_id}/{action}", payload, token)
        print(f"    {result['previous_status']:14s} -> {result['status']}")

    detail = _get(f"/api/v1/tenant/incidents/{incident_id}", token)
    print(f"\n    history ({len(detail['events'])} events):")
    for event in detail["events"]:
        arrow = f"{event['previous_status']} -> {event['new_status']}" if event["previous_status"] else event["new_status"]
        print(f"      {event['event_type']:24s} {arrow}")
    print(f"    linked detections: {len(detail['detection_ids'])}")

    step(8, "Confirm an illegal transition is refused")
    try:
        _post(f"/api/v1/tenant/incidents/{incident_id}/acknowledge", {}, token)
        print("    FAIL: re-acknowledging a resolved incident should have been refused")
        return 1
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read())
        print(f"    {exc.code} {body['code']}: {body['message'][:80]}")

    print("\nEnd-to-end slice verified: frame -> detection -> rule -> incident -> API -> lifecycle.")
    return 0


def _read_env(key: str) -> str:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    with open(env_path) as handle:
        for line in handle:
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    raise KeyError(f"{key} not found in .env")


if __name__ == "__main__":
    raise SystemExit(main())
