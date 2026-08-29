"""Recipient groups and notification policies: the last "configure from the UI" gap
in alerting, closed - and proof that a recipient's own quiet hours actually change when
they are told, without ever silencing a high-severity incident.

Everything downstream of a published policy had been verified already
(e2e_ingest_to_alert.py). What was missing was a way to author a recipient group or a
policy without SQL, and quiet hours were wired but never plugged into the dispatcher. This
drives both through the real HTTP API and the real ingestion pipeline:

    create a group -> add a quiet-hours member and a normal one -> publish a policy ->
    post a medium-severity detection -> the quiet member's delivery is held, the normal
    member's is not -> post a high-severity detection -> quiet hours are ignored for both

    python scripts/e2e_notification_config.py
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import os
import urllib.error
import urllib.request
import uuid

import cv2
import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
API = "http://localhost:8080"
PASSWORD = "NotifCfgE2E!Password123"


def env(key: str) -> str:
    with open(os.path.join(REPO, ".env"), encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    raise KeyError(key)


def api(path, payload=None, token=None, method="POST", expect=(200, 201, 204)):
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
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        if exc.code in expect:
            return exc.code, (json.loads(body) if body else {})
        raise RuntimeError(f"{method} {path} -> {exc.code}: {body[:400]}") from exc


def step(n, text_):
    print(f"\n[{n}] {text_}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def frame() -> str:
    image = np.full((200, 200, 3), 60, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", image)
    return base64.b64encode(buf.tobytes()).decode() if ok else ""


def hhmm(moment: dt.datetime) -> str:
    return moment.strftime("%H:%M")


async def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []
    engine = create_async_engine(
        f"postgresql+asyncpg://csense_app:{env('POSTGRES_PASSWORD')}@localhost:5432/csense"
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    step(1, "Register a tenant, a site, a camera and a zone (all through their own APIs)")
    auth = api("/api/v1/auth/register", {
        "organization_name": f"Notif Cfg E2E {suffix}",
        "email": f"ops-{suffix}@northwind.example",
        "password": PASSWORD,
        "display_name": "Ops",
    })[1]
    tenant_id, token = auth["tenant_id"], auth["access_token"]

    site = api("/api/v1/tenant/sites", {
        "name": "Depot", "code": f"depot-{suffix}", "timezone": "Asia/Kolkata",
    }, token)[1]
    camera = api("/api/v1/tenant/cameras", {
        "site_id": site["id"], "name": "Dock camera", "code": f"dock-{suffix}",
    }, token)[1]
    zone = api("/api/v1/tenant/zones", {
        "site_id": site["id"], "name": "Restricted dock", "zone_type": "restricted",
        "polygon": [[0.35, 0.30], [1.0, 0.30], [1.0, 1.0], [0.35, 1.0]],
    }, token)[1]
    print(f"    site {site['id']}, camera {camera['id']}, zone {zone['id']}")

    step(2, "Create a recipient group with no members - it explains what that means")
    status, group = api("/api/v1/tenant/notifications/recipient-groups", {
        "name": "On call", "description": "Whoever is holding the pager",
    }, token)
    check(status == 201 and group["member_count"] == 0, "group created with zero members",
          failures)

    step(3, "A recipient with no address is refused")
    status, _ = api(
        f"/api/v1/tenant/notifications/recipient-groups/{group['id']}/members",
        {"channels": ["email"]}, token, expect=(422,),
    )
    check(status == 422, "a member with no email, phone or user id is rejected", failures)

    step(4, "Add a member whose quiet hours cover right now, and one that has none")
    now = dt.datetime.now(dt.UTC)
    quiet_start = hhmm(now - dt.timedelta(minutes=45))
    quiet_end = hhmm(now + dt.timedelta(minutes=45))
    status, quiet_member = api(
        f"/api/v1/tenant/notifications/recipient-groups/{group['id']}/members",
        {
            "display_name": "Night owl (quiet now)",
            "email": f"quiet-{suffix}@northwind.example",
            "channels": ["email"],
            "active_schedule": {"start": quiet_start, "end": quiet_end, "timezone": "UTC"},
        },
        token,
    )
    check(status == 201, "a member with quiet hours is accepted", failures)

    status, awake_member = api(
        f"/api/v1/tenant/notifications/recipient-groups/{group['id']}/members",
        {
            "display_name": "Always reachable",
            "email": f"awake-{suffix}@northwind.example",
            "channels": ["email"],
        },
        token,
    )
    check(status == 201, "a member with no schedule is accepted", failures)

    step(5, "An unrecognised timezone is refused, not silently ignored")
    status, _ = api(
        f"/api/v1/tenant/notifications/recipient-groups/{group['id']}/members",
        {
            "email": f"bad-tz-{suffix}@northwind.example", "channels": ["email"],
            "active_schedule": {"start": "22:00", "end": "06:00", "timezone": "Mars/Phobos"},
        },
        token, expect=(422,),
    )
    check(status == 422, "an unrecognised timezone is rejected", failures)

    step(6, "Publish a policy naming this group for medium and high severity")
    status, policy = api("/api/v1/tenant/notifications/policies", {
        "name": "Dock watch", "severities": ["medium", "high"],
    }, token)
    check(status == 201 and policy["active_version"] is None,
          "a fresh policy has no active version yet - it is a draft, invisible to dispatch",
          failures)

    status, _ = api(
        f"/api/v1/tenant/notifications/policies/{policy['id']}/versions",
        {"steps": [{"level": 0, "delay_seconds": 0, "channels": ["email"],
                    "recipient_group_ids": [str(uuid.uuid4())]}]},
        token, expect=(422,),
    )
    check(status == 422, "publishing a step naming a nonexistent group is refused",
          failures)

    status, published = api(
        f"/api/v1/tenant/notifications/policies/{policy['id']}/versions",
        {"steps": [{"level": 0, "delay_seconds": 0, "channels": ["email"],
                    "recipient_group_ids": [group["id"]]}]},
        token,
    )
    check(status == 201 and published["active_version"]["version_number"] == 1,
          "the real group publishes as version 1 and becomes active", failures)

    step(7, "Create a device identity and a medium-severity rule")
    device_email = f"device-{suffix}@northwind.example"
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        device_user_id = (await session.execute(
            text("INSERT INTO users (email_normalized, email_display, password_hash, "
                 "display_name, status, email_verified_at) "
                 "SELECT CAST(:e AS citext), CAST(:e AS text), u.password_hash, "
                 "'Dock camera device', 'active', now() FROM users u "
                 "WHERE u.email_normalized = CAST(:owner AS citext) RETURNING id"),
            {"e": device_email, "owner": f"ops-{suffix}@northwind.example"},
        )).scalar_one()
        await session.execute(
            text("INSERT INTO memberships (tenant_id, user_id, role_id, status, accepted_at) "
                 "SELECT :t, :u, r.id, 'active', now() FROM roles r "
                 "WHERE r.name = 'edge_device' AND r.tenant_id IS NULL"),
            {"t": uuid.UUID(tenant_id), "u": device_user_id},
        )
    device_token = api("/api/v1/auth/login", {
        "email": device_email, "password": PASSWORD,
    })[1]["access_token"]

    status, _rule = api("/api/v1/tenant/rules", {
        "site_id": site["id"], "camera_id": camera["id"], "zone_id": zone["id"],
        "name": "Medium-severity watch", "alertable_classes": ["person"],
        "min_confidence": 0.4, "severity": "medium",
    }, token)
    check(status == 201, "the rule that will trigger a medium-severity incident is created",
          failures)

    step(8, "Post a medium-severity detection - quiet hours should hold one recipient back")
    _, result = api("/api/v1/tenant/ingest/detections", {
        "camera_id": camera["id"],
        "source_event_id": f"e2e-{suffix}-medium",
        "captured_at": dt.datetime.now(dt.UTC).isoformat(),
        "objects": [{"class_name": "person", "confidence": 0.9,
                     "bbox": [0.44, 0.36, 0.55, 0.92]}],
        "frame_base64": frame(),
    }, device_token)
    check(result["incident_created"], "the incident opened", failures)

    async with factory() as session:
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        rows = (await session.execute(
            text(
                "SELECT d.recipient_ref, d.next_attempt_at, n.scheduled_at "
                "FROM notification_deliveries d JOIN notifications n "
                "ON n.id = d.notification_id "
                "WHERE n.tenant_id = :t AND n.incident_id = :i"
            ),
            {"t": uuid.UUID(tenant_id), "i": uuid.UUID(result["incident_id"])},
        )).all()
    by_recipient = {r[0]: (r[1], r[2]) for r in rows}
    quiet_next, quiet_scheduled = by_recipient.get(quiet_member["email"], (None, None))
    awake_next, awake_scheduled = by_recipient.get(awake_member["email"], (None, None))

    check(quiet_next is not None and quiet_next > quiet_scheduled + dt.timedelta(minutes=1),
          "the quiet-hours recipient's delivery is held past its scheduled time", failures)
    check(awake_next is not None and awake_next <= awake_scheduled + dt.timedelta(seconds=5),
          "the always-reachable recipient's delivery is not held at all", failures)
    if quiet_next:
        print(f"    quiet member  next_attempt_at={quiet_next.isoformat()}  "
              f"(scheduled {quiet_scheduled.isoformat()})")
    if awake_next:
        print(f"    awake member  next_attempt_at={awake_next.isoformat()}  "
              f"(scheduled {awake_scheduled.isoformat()})")

    step(9, "A high-severity detection ignores quiet hours for everyone")
    # A second camera, not the first: the pipeline folds a new detection into whatever
    # live incident already exists for that camera (that is correct - a continuing
    # situation should not spawn a second incident), which would make a same-camera
    # detection here prove nothing about severity. A different camera guarantees this is
    # genuinely a fresh incident.
    camera2 = api("/api/v1/tenant/cameras", {
        "site_id": site["id"], "name": "Second camera", "code": f"dock2-{suffix}",
    }, token)[1]
    status, _high_rule = api("/api/v1/tenant/rules", {
        "site_id": site["id"], "camera_id": camera2["id"], "zone_id": zone["id"],
        "name": "High-severity watch", "alertable_classes": ["person"],
        "min_confidence": 0.4, "severity": "high",
    }, token)
    _, high_result = api("/api/v1/tenant/ingest/detections", {
        "camera_id": camera2["id"],
        "source_event_id": f"e2e-{suffix}-high",
        "captured_at": dt.datetime.now(dt.UTC).isoformat(),
        "objects": [{"class_name": "person", "confidence": 0.9,
                     "bbox": [0.44, 0.36, 0.55, 0.92]}],
        "frame_base64": frame(),
    }, device_token)
    check(high_result["incident_created"], "the high-severity incident opened", failures)

    async with factory() as session:
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        rows = (await session.execute(
            text(
                "SELECT d.recipient_ref, d.next_attempt_at, n.scheduled_at "
                "FROM notification_deliveries d JOIN notifications n "
                "ON n.id = d.notification_id "
                "WHERE n.tenant_id = :t AND n.incident_id = :i"
            ),
            {"t": uuid.UUID(tenant_id), "i": uuid.UUID(high_result["incident_id"])},
        )).all()
    high_by_recipient = {r[0]: (r[1], r[2]) for r in rows}
    hq_next, hq_scheduled = high_by_recipient.get(quiet_member["email"], (None, None))
    check(
        hq_next is not None and hq_next <= hq_scheduled + dt.timedelta(seconds=5),
        "at high severity, even the quiet-hours recipient is not held back",
        failures,
    )

    step(10, "The group cannot be deleted while a published policy still names it")
    status, body = api(
        f"/api/v1/tenant/notifications/recipient-groups/{group['id']}", None, token,
        method="DELETE", expect=(409,),
    )
    check(status == 409, "deleting a referenced group is refused", failures)
    print(f"    -> {status} {body.get('message', '')[:90]}")

    step(11, "The policy cannot be deleted once it has produced notifications")
    status, body = api(
        f"/api/v1/tenant/notifications/policies/{policy['id']}", None, token,
        method="DELETE", expect=(409,),
    )
    check(status == 409, "deleting a policy with delivery history is refused", failures)
    print(f"    -> {status} {body.get('message', '')[:90]}")

    step(12, "Disabling the policy is how you actually stop it, and it sticks")
    api(f"/api/v1/tenant/notifications/policies/{policy['id']}", {"status": "disabled"}, token,
        method="PATCH")
    _, listing = api("/api/v1/tenant/notifications/policies", None, token, method="GET")
    disabled = next(p for p in listing if p["id"] == policy["id"])
    check(disabled["status"] == "disabled", "the policy reads back as disabled", failures)

    step(13, "Clean up")
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        await session.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": uuid.UUID(tenant_id)})
        await session.execute(
            text("DELETE FROM organizations WHERE display_name = :n"),
            {"n": f"Notif Cfg E2E {suffix}"},
        )
    await engine.dispose()
    print("    test tenant removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - recipient groups and policies are fully CRM-configurable, and quiet "
          "hours hold back a routine alert without ever muting a serious one")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
