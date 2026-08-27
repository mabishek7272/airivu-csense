"""End-to-end proof of the vertical slice: a posted detection reaches a person.

Everything downstream of ingestion had been verified in pieces. This exercises the whole
path in one run, through the real HTTP API and the real worker container:

    POST /ingest/detections  ->  rule evaluation  ->  detection row  ->  incident
                             ->  masked + annotated evidence in MinIO
                             ->  escalation ladder scheduled
                             ->  notification-worker sends the email

Nothing here calls the pipeline directly. If an alert arrives, it arrived the way a real
edge device would have caused it to.

It also checks the two behaviours that are easy to get wrong and expensive to get wrong:
replaying the same `source_event_id` must not produce a second alert, and acknowledging
the incident must stop the ladder.

    python scripts/e2e_ingest_to_alert.py your.address@example.com

Requires the local stack to be running.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
API = "http://localhost:8080"
PASSWORD = "IngestE2E!Password123"


def env(key: str) -> str:
    with open(os.path.join(REPO, ".env"), encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    raise KeyError(key)


def api(path: str, payload: dict | None = None, token: str | None = None, method="POST"):
    headers = {"Content-Type": "application/json", "Host": "app.localhost"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{path} -> {exc.code}: {exc.read().decode()[:400]}") from exc


def make_frame():
    """A synthetic frame with a person-shaped rectangle, so evidence has something to mask."""
    import cv2
    import numpy as np

    image = np.full((720, 1280, 3), 60, dtype=np.uint8)
    cv2.rectangle(image, (0, 520), (1280, 720), (90, 90, 95), -1)   # ground
    cv2.rectangle(image, (560, 300), (700, 660), (150, 140, 130), -1)  # body
    cv2.circle(image, (630, 260), 46, (170, 160, 150), -1)             # head
    ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise RuntimeError("failed to encode the synthetic frame")
    return base64.b64encode(buffer.tobytes()).decode()


def step(n: int, text_: str) -> None:
    print(f"\n[{n}] {text_}")


async def main(recipient: str) -> int:
    suffix = uuid.uuid4().hex[:8]
    engine = create_async_engine(
        f"postgresql+asyncpg://csense_app:{env('POSTGRES_PASSWORD')}@localhost:5432/csense"
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    step(1, "Register a tenant through the API")
    email = f"ops-{suffix}@northwind.example"
    device_email = f"device-{suffix}@northwind.example"
    auth = api("/api/v1/auth/register", {
        "organization_name": f"Ingest E2E {suffix}",
        "email": email,
        "password": PASSWORD,
        "display_name": "Edge Operator",
    })
    tenant_id = uuid.UUID(auth["tenant_id"])
    owner_token = auth["access_token"]
    print(f"    tenant {tenant_id}")

    step(2, "Create a site, camera, zone, rule and recipient (direct SQL - no CRUD API yet)")
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))

        site_id = (await session.execute(
            text("INSERT INTO sites (tenant_id, name, code, timezone, address_json) "
                 "VALUES (:t,'Chennai Depot',:c,'Asia/Kolkata', CAST(:a AS jsonb)) RETURNING id"),
            {"t": tenant_id, "c": f"site-{suffix}",
             "a": '{"line1":"12 Anna Salai","city":"Chennai","country":"India"}'},
        )).scalar_one()

        camera_id = (await session.execute(
            text("INSERT INTO cameras (tenant_id, site_id, name, code, status) "
                 "VALUES (:t,:s,'Loading Bay 2',:c,'ready') RETURNING id"),
            {"t": tenant_id, "s": site_id, "c": f"cam-{suffix}"},
        )).scalar_one()

        # The restricted zone: the right-hand half of the frame, in normalised coordinates.
        zone_id = (await session.execute(
            text("INSERT INTO zones (tenant_id, site_id, name, zone_type, geometry_json) "
                 "VALUES (:t,:s,'Restricted dock','restricted', CAST(:g AS jsonb)) RETURNING id"),
            {"t": tenant_id, "s": site_id,
             "g": '{"polygon":[[0.35,0.30],[1.0,0.30],[1.0,1.0],[0.35,1.0]]}'},
        )).scalar_one()

        await session.execute(
            text("INSERT INTO detection_rules (tenant_id, site_id, camera_id, zone_id, name, "
                 "type_code, alertable_classes, min_confidence, severity, min_roi_overlap) "
                 "VALUES (:t,:s,:c,:z,'No entry to the dock','zone.intrusion', "
                 "CAST('[\"person\"]' AS jsonb), 0.4, 'high', 0.3)"),
            {"t": tenant_id, "s": site_id, "c": camera_id, "z": zone_id},
        )

        group_id = (await session.execute(
            text("INSERT INTO recipient_groups (tenant_id, name) VALUES (:t,'On call') RETURNING id"),
            {"t": tenant_id},
        )).scalar_one()
        await session.execute(
            text("INSERT INTO recipient_group_members (tenant_id, recipient_group_id, "
                 "display_name, email, channels) "
                 "VALUES (:t,:g,'On-call responder',:e, CAST('[\"email\"]' AS jsonb))"),
            {"t": tenant_id, "g": group_id, "e": recipient},
        )

        # The device gets its own identity, not a share of the operator's. A token carries
        # exactly one membership and therefore one role, so widening tenant_owner would be
        # the only alternative - and an operator's browser session must not be able to
        # forge detections. This user holds edge_device, whose sole permission is
        # detection.ingest; Phase 3 replaces it with an enrolled device certificate.
        device_user_id = (await session.execute(
            # email_normalized is citext and email_display is text, so one parameter
            # cannot serve both without an explicit cast - Postgres refuses to deduce it.
            text("INSERT INTO users (email_normalized, email_display, password_hash, "
                 "display_name, status, email_verified_at) "
                 "SELECT CAST(:e AS citext), CAST(:e AS text), u.password_hash, "
                 "'Edge device (Loading Bay 2)', 'active', now() "
                 "FROM users u WHERE u.email_normalized = CAST(:owner AS citext) RETURNING id"),
            {"e": device_email, "owner": email},
        )).scalar_one()
        await session.execute(
            text("INSERT INTO memberships (tenant_id, user_id, role_id, status, accepted_at) "
                 "SELECT :t, :u, r.id, 'active', now() FROM roles r "
                 "WHERE r.name = 'edge_device' AND r.tenant_id IS NULL"),
            {"t": tenant_id, "u": device_user_id},
        )
    print(f"    camera {camera_id}, zone covering the right-hand dock")

    # The device's own token, carrying detection.ingest and nothing else.
    device_token = api(
        "/api/v1/auth/login", {"email": device_email, "password": PASSWORD}
    )["access_token"]

    step(3, "POST a detection: a person inside the restricted zone")
    source_event_id = f"e2e-{suffix}-0001"
    body = {
        "camera_id": str(camera_id),
        "source_event_id": source_event_id,
        "captured_at": dt.datetime.now(dt.UTC).isoformat(),
        "objects": [
            # Inside the zone - this one should fire.
            {"class_name": "person", "confidence": 0.94, "bbox": [0.44, 0.36, 0.55, 0.92]},
            # Outside it - present in the frame, masked in evidence, but not alerting.
            {"class_name": "person", "confidence": 0.88, "bbox": [0.05, 0.40, 0.15, 0.90]},
        ],
        "frame_base64": make_frame(),
    }
    result = api("/api/v1/tenant/ingest/detections", body, device_token)
    print(f"    incident #{result['incident_number']}  created={result['incident_created']}")
    print(f"    rules evaluated={result['rules_evaluated']}  "
          f"evidence={result['evidence_captured']}  "
          f"notifications={result['notifications_scheduled']}")

    if not result["incident_created"]:
        print("    FAILED: no incident was created.")
        return 1
    incident_id = result["incident_id"]

    step(4, "Replay the same source_event_id - must not alert twice")
    replay = api("/api/v1/tenant/ingest/detections", body, device_token)
    print(f"    duplicate={replay['duplicate']}  "
          f"notifications={replay['notifications_scheduled']}")
    if not replay["duplicate"] or replay["notifications_scheduled"]:
        print("    FAILED: a replayed event produced new work.")
        return 1

    step(5, "Wait for the notification-worker to send")
    deadline = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=90)
    rows: list = []
    while dt.datetime.now(dt.UTC) < deadline:
        await asyncio.sleep(3)
        async with factory() as session:
            await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
            rows = (await session.execute(
                text("SELECT d.status::text, d.provider_code, d.failure_summary_redacted "
                     "FROM notification_deliveries d JOIN notifications n "
                     "ON n.id = d.notification_id WHERE n.tenant_id = :t"),
                {"t": tenant_id},
            )).all()
        if rows and all(r[0] not in ("queued", "sending") for r in rows):
            break
    for status, provider, failure in rows:
        print(f"    {status:<10} provider={provider}" + (f"  {failure}" if failure else ""))

    step(6, "Check the evidence variants landed")
    async with factory() as session:
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        variants = (await session.execute(
            text("SELECT privacy_variant::text, count(*) FROM evidence "
                 "WHERE incident_id = :i GROUP BY 1 ORDER BY 1"),
            {"i": uuid.UUID(incident_id)},
        )).all()
    print("    " + ", ".join(f"{v}={c}" for v, c in variants) or "    none")

    step(7, "Acknowledge the incident - the escalation ladder must stop")
    api(f"/api/v1/tenant/incidents/{incident_id}/acknowledge", {}, owner_token)
    async with factory() as session:
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        remaining = (await session.execute(
            text("SELECT count(*) FROM notifications WHERE tenant_id = :t "
                 "AND status IN ('pending','scheduled')"),
            {"t": tenant_id},
        )).scalar_one()
    print(f"    notifications still pending: {remaining}")

    accepted = [r for r in rows if r[0] == "accepted"]
    print()
    if accepted and remaining == 0:
        print(f"PASS. Check {recipient} - subject begins 'Urgent: Intrusion at Chennai Depot'.")
        outcome = 0
    else:
        print("FAIL. See the delivery rows above.")
        outcome = 1

    if "--keep" not in sys.argv:
        async with factory() as session, session.begin():
            await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
            org_id = (await session.execute(
                text("SELECT organization_id FROM tenants WHERE id = :t"), {"t": tenant_id}
            )).scalar_one()
            await session.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
            await session.execute(
                text("DELETE FROM organizations WHERE id = :o"), {"o": org_id}
            )
        print("Test tenant removed.")

    await engine.dispose()
    return outcome


if __name__ == "__main__":
    if len(sys.argv) < 2 or "@" not in sys.argv[1]:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1])))

