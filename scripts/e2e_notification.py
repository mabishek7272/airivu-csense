"""End-to-end proof that an incident actually reaches a person.

Creates a throwaway tenant with one recipient, opens an incident, schedules the escalation
ladder, and then waits for the running notification-worker container to pick it up and
send. Nothing here sends anything itself - that is the point. If a message arrives, it
arrived through the real worker, the real policy resolution and the real provider.

    python scripts/e2e_notification.py your.address@example.com
    python scripts/e2e_notification.py your.address@example.com --keep

The tenant is deleted afterwards unless --keep is passed.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import uuid

from csense_shared.notifications.escalation import schedule_incident_notifications
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def env(key: str) -> str:
    with open(os.path.join(REPO, ".env"), encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    raise KeyError(key)


def dsn() -> str:
    return (
        f"postgresql+asyncpg://csense_app:{env('POSTGRES_PASSWORD')}"
        "@localhost:5432/csense"
    )


async def main(recipient: str, keep: bool) -> int:
    engine = create_async_engine(dsn())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:8]
    now = dt.datetime.now(dt.UTC)

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))

        org_id = (await session.execute(
            text("INSERT INTO organizations (organization_type, legal_name, display_name, "
                 "slug, status) VALUES ('direct_customer', :n, :n, :s, 'active') RETURNING id"),
            {"n": f"E2E Notify {suffix}", "s": f"e2e-notify-{suffix}"},
        )).scalar_one()
        tenant_id = (await session.execute(
            text("INSERT INTO tenants (organization_id, status) VALUES (:o,'active') RETURNING id"),
            {"o": org_id},
        )).scalar_one()
        site_id = (await session.execute(
            text("INSERT INTO sites (tenant_id, name, code, timezone, address_json) "
                 "VALUES (:t, 'Chennai Depot', :c, 'Asia/Kolkata', "
                 "CAST(:addr AS jsonb)) RETURNING id"),
            {
                "t": tenant_id,
                "c": f"site-{suffix}",
                "addr": '{"line1":"12 Anna Salai","city":"Chennai","country":"India"}',
            },
        )).scalar_one()
        camera_id = (await session.execute(
            text("INSERT INTO cameras (tenant_id, site_id, name, code, status) "
                 "VALUES (:t,:s,'Loading Bay 2',:c,'ready') RETURNING id"),
            {"t": tenant_id, "s": site_id, "c": f"cam-{suffix}"},
        )).scalar_one()
        incident_id = (await session.execute(
            text("INSERT INTO incidents (tenant_id, site_id, camera_id, incident_number, "
                 "type_code, severity, title, first_detected_at, last_detected_at) "
                 "VALUES (:t,:s,:c,1,'zone.intrusion','high','Intrusion',:n,:n) RETURNING id"),
            {"t": tenant_id, "s": site_id, "c": camera_id, "n": now},
        )).scalar_one()
        group_id = (await session.execute(
            text("INSERT INTO recipient_groups (tenant_id, name) VALUES (:t,'On call') "
                 "RETURNING id"),
            {"t": tenant_id},
        )).scalar_one()
        await session.execute(
            text("INSERT INTO recipient_group_members (tenant_id, recipient_group_id, "
                 "display_name, email, channels) "
                 "VALUES (:t,:g,'On-call responder',:e, CAST('[\"email\"]' AS jsonb))"),
            {"t": tenant_id, "g": group_id, "e": recipient},
        )

        # No policy is configured for this tenant, so this exercises the default-policy
        # fallback: a tenant who has set nothing up still gets told.
        notification_ids = await schedule_incident_notifications(
            session, incident_id=incident_id, now=now
        )

    print(f"tenant     : {tenant_id}")
    print(f"incident   : {incident_id}")
    print(f"recipient  : {recipient}")
    print(f"scheduled  : {len(notification_ids)} notification(s)")
    if not notification_ids:
        print("Nothing was scheduled - check recipient groups.")
        return 1

    print("\nWaiting for the notification-worker container to send...")
    deadline = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=90)
    final: list[tuple] = []
    while dt.datetime.now(dt.UTC) < deadline:
        await asyncio.sleep(3)
        async with factory() as session:
            await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
            final = (await session.execute(
                text("SELECT d.status::text, d.provider_code, d.provider_message_id, "
                     "d.failure_code, d.failure_summary_redacted "
                     "FROM notification_deliveries d "
                     "JOIN notifications n ON n.id = d.notification_id "
                     "WHERE n.tenant_id = :t"),
                {"t": tenant_id},
            )).all()
        if final and all(row[0] not in ("queued", "sending") for row in final):
            break

    print()
    for status, provider, message_id, failure_code, failure in final:
        print(f"  {status:<10} provider={provider} id={message_id}")
        if failure_code:
            print(f"             failure={failure_code}: {failure}")

    accepted = [r for r in final if r[0] == "accepted"]
    print()
    if accepted:
        print(f"SENT. Check {recipient} - subject begins 'Urgent: Intrusion at Chennai Depot'.")
    else:
        print("NOT SENT. See the failure above and `docker compose logs notification-worker`.")

    if not keep:
        async with factory() as session, session.begin():
            await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
            await session.execute(
                text("DELETE FROM tenants WHERE organization_id = :o"), {"o": org_id}
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id = :o"), {"o": org_id}
            )
        print("Test tenant removed.")

    await engine.dispose()
    return 0 if accepted else 1


if __name__ == "__main__":
    if len(sys.argv) < 2 or "@" not in sys.argv[1]:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1], "--keep" in sys.argv)))
