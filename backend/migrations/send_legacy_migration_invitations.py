"""Sends the real invitation - ticket + email - for every user `import_legacy_users.py`
created (CLARIFICATIONS.md #30). Deliberately a separate, manually-triggered script: the
import creates `invited` accounts with no password; this is the step that actually puts a
usable link in front of a real person, and per the owner's own decision that is not
something to fire automatically as a side effect of the import.

    python send_legacy_migration_invitations.py [--confirm-send]

**Without `--confirm-send` (the default): dry run only.** Prints exactly who would be
emailed and the real invitation-link *origin* this deployment is configured with (the
same value `app/api/memberships.py::_crm_origin` would produce). The token portion of the
printed link is a labeled placeholder, not a working link - producing a real, redeemable
token means calling `create_invitation_ticket`, which writes it to Redis. That write is
itself a side effect, and this mode promises zero: no ticket is created, no email is sent.

**With `--confirm-send`**: creates a real invitation ticket (`create_invitation_ticket` -
the same Redis-backed, single-use, 7-day ticket `POST /api/v1/tenant/memberships` issues)
and sends a real email through the same provider registry and `Message` shape
`app/api/memberships.py::_send_invitation_email` uses. That function is mirrored here
rather than imported: importing tenant_api's `app` package would pull in FastAPI and its
own dependency chain, which this migrations image deliberately does not carry (see
`requirements.txt` - alembic/sqlalchemy/psycopg/pydantic/minio only, matching
`import_legacy_models.py`'s already-established footprint). The actual send is not
reimplemented by this mirroring - constructing a `Message` and handing it to whatever
`build_registry` resolves for the `email` channel is the same call, unchanged; only the
five-line wrapper around it is duplicated, and this docstring is the tripwire if the two
ever drift.

**Selection is deliberately narrow.** A candidate must be an `invited` membership with
`invited_by IS NULL` *and* have a matching `user.legacy_import` audit_events row written
by `import_legacy_users.py` for that exact user. `invited_by IS NULL` alone is not a safe
filter on its own - `provision_organization_with_invited_owner`
(`csense_shared/tenancy/provisioning.py`, used by both the Admin API's organization
provisioning and a reseller's child-tenant provisioning) also leaves `invited_by` NULL for
an entirely unrelated real flow. The audit-event join is load-bearing here, not decorative
- it is what keeps this script from ever re-inviting (or, worse, first-inviting under
migration cover) someone a normal team-invite or reseller-provisioning flow already
handled.
"""
from __future__ import annotations

import argparse
import asyncio
import os

import psycopg

from csense_shared.config import Settings, get_settings
from csense_shared.db.redis import create_redis_client
from csense_shared.notifications.bootstrap import build_registry
from csense_shared.notifications.providers import Message
from csense_shared.security.invitation_tickets import create_invitation_ticket

AUDIT_IMPORT_ACTION = "user.legacy_import"
AUDIT_IMPORT_ACTOR_ID = "import_legacy_users.py"
AUDIT_SEND_ACTION = "user.legacy_invitation_sent"
AUDIT_SEND_ACTOR_ID = "send_legacy_migration_invitations.py"

_CANDIDATES_SQL = """
    SELECT m.id, m.tenant_id, m.user_id, u.email_display, u.display_name
    FROM memberships m
    JOIN users u ON u.id = m.user_id
    WHERE m.status = 'invited'
      AND m.invited_by IS NULL
      AND u.status = 'invited'
      AND u.password_hash IS NULL
      AND EXISTS (
        SELECT 1 FROM audit_events ae
        WHERE ae.action = %(import_action)s
          AND ae.actor_id = %(import_actor)s
          AND ae.target_type = 'user'
          AND ae.target_id = m.user_id::text
      )
    ORDER BY m.created_at
"""


def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "csense")
    return f"host={host} port={port} dbname={db} user={user} password={password}"


def _crm_origin(settings: Settings) -> str:
    # Mirrors app/api/memberships.py::_crm_origin exactly - see module docstring for why
    # this is duplicated rather than imported.
    origins = settings.customer_crm_origins
    return origins[0] if origins else "http://app.localhost:8080"


async def _send_invitation_email(settings: Settings, *, email: str, link: str) -> bool:
    # Mirrors app/api/memberships.py::_send_invitation_email exactly (same subject, same
    # body, same registry/provider call) - see module docstring for why.
    registry = build_registry(settings)
    provider = registry.get("email")
    if provider is None:
        return False
    result = await provider.send(
        Message(
            recipient=email,
            subject="You've been invited to AIRIVU CSense",
            body=(
                "You've been invited to join a CSense tenant.\n\n"
                f"Accept the invitation here: {link}\n\n"
                "This link is valid for 7 days and can only be used once."
            ),
        )
    )
    return result.accepted


async def _run(confirm_send: bool) -> None:
    settings = get_settings()

    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            _CANDIDATES_SQL,
            {"import_action": AUDIT_IMPORT_ACTION, "import_actor": AUDIT_IMPORT_ACTOR_ID},
        )
        candidates = cur.fetchall()

        if not candidates:
            print("No pending legacy-migration invitations found.")
            return

        print(f"{len(candidates)} legacy-migrated account(s) pending invitation:\n")

        if not confirm_send:
            origin = _crm_origin(settings)
            for membership_id, tenant_id, _user_id, email, display_name in candidates:
                print(
                    f"  WOULD SEND -> {email} ({display_name})  "
                    f"membership={membership_id} tenant={tenant_id}"
                )
                print(
                    f"    link (preview only, no ticket issued): "
                    f"{origin}/accept-invitation?token=<issued-only-with---confirm-send>"
                )
            print(
                "\nDry run; --confirm-send was not passed. No invitation ticket was created "
                "and no email was sent."
            )
            return

        # memberships/audit_events are RLS-forced; this script connects as the owner (a
        # superuser), which bypasses RLS anyway - set for consistency with the other
        # operator scripts that write to these tables the same way.
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")

        redis_client = create_redis_client(settings)
        sent = failed = 0
        try:
            for membership_id, tenant_id, user_id, email, display_name in candidates:
                token = await create_invitation_ticket(
                    redis_client, settings,
                    membership_id=membership_id, tenant_id=tenant_id, user_id=user_id, email=email,
                )
                link = f"{_crm_origin(settings)}/accept-invitation?token={token}"
                ok = await _send_invitation_email(settings, email=email, link=link)

                cur.execute(
                    """
                    INSERT INTO audit_events
                        (tenant_id, actor_type, actor_id, action, target_type, target_id, outcome, reason)
                    VALUES (%s, 'system', %s, %s, 'membership', %s, %s, %s)
                    """,
                    (
                        tenant_id,
                        AUDIT_SEND_ACTOR_ID,
                        AUDIT_SEND_ACTION,
                        str(membership_id),
                        "success" if ok else "failed",
                        f"Legacy migration invitation {'accepted by provider' if ok else 'NOT accepted by provider'} "
                        f"for {display_name}",
                    ),
                )

                if ok:
                    print(f"  SENT -> {email} ({display_name})")
                    sent += 1
                else:
                    print(f"  FAILED -> {email} ({display_name}) - provider did not accept the message")
                    failed += 1
            conn.commit()
        finally:
            try:
                await redis_client.aclose()
            except AttributeError:
                await redis_client.close()

        print(f"\nDone: {sent} sent, {failed} failed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-send",
        action="store_true",
        help="Actually create invitation tickets and send emails. Without this flag: dry run only.",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.confirm_send))


if __name__ == "__main__":
    main()
