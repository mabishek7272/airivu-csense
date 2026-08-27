"""Reading and writing encrypted secrets, so callers never touch ciphertext directly.

Every path that stores a credential goes through here. That is the point: encryption
details in one place cannot drift, and no feature can accidentally invent its own weaker
scheme because the correct one was inconvenient to reach.

Two rules this module enforces on its callers by shape rather than by comment:

  `read_secret` returns bytes. Not a str, not a model, not something with a `__repr__`
  that will end up in a log line. The caller has to decide to decode it, which is the
  moment to think about where the value is going.

  There is no "list secrets with their values" function. Rotation works on ciphertext
  without decrypting it, and nothing else has a reason to read many credentials at once -
  a function that did would be the single most useful thing on the system to an attacker.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.security.envelope import (
    EnvelopeError,
    KeyRing,
    SealedSecret,
    open_secret,
    rewrap,
    seal,
)

logger = logging.getLogger(__name__)


async def write_secret(
    session: AsyncSession,
    keyring: KeyRing,
    *,
    tenant_id: uuid.UUID | None,
    purpose: str,
    plaintext: str,
    label: str | None = None,
    secret_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Stores a credential, replacing the value if `secret_id` already exists.

    The id is generated before sealing because it is part of what the ciphertext is bound
    to - the row cannot be sealed until it knows which row it is.
    """
    secret_id = secret_id or uuid.uuid4()
    sealed = seal(
        keyring, plaintext, tenant_id=tenant_id, secret_id=secret_id, purpose=purpose
    )
    row = sealed.as_row()

    await session.execute(
        text(
            """
            INSERT INTO encrypted_secrets
                (id, tenant_id, purpose, kek_id, wrapped_dek, dek_nonce,
                 ciphertext, ciphertext_nonce, label)
            VALUES (:id, :tenant_id, :purpose, :kek_id, :wrapped_dek, :dek_nonce,
                    :ciphertext, :ciphertext_nonce, :label)
            ON CONFLICT (id) DO UPDATE SET
                kek_id = EXCLUDED.kek_id,
                wrapped_dek = EXCLUDED.wrapped_dek,
                dek_nonce = EXCLUDED.dek_nonce,
                ciphertext = EXCLUDED.ciphertext,
                ciphertext_nonce = EXCLUDED.ciphertext_nonce,
                label = COALESCE(EXCLUDED.label, encrypted_secrets.label),
                rotated_at = now(),
                updated_at = now()
            """
        ),
        {
            "id": secret_id,
            "tenant_id": tenant_id,
            "purpose": purpose,
            "label": label,
            **row,
        },
    )
    # Deliberately no value, no length, no prefix. A length alone narrows a brute force.
    logger.info(
        "secret_written",
        extra={"secret_id": str(secret_id), "purpose": purpose,
               "tenant_id": str(tenant_id) if tenant_id else None},
    )
    return secret_id


async def read_secret(
    session: AsyncSession,
    keyring: KeyRing,
    *,
    secret_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    purpose: str,
    touch: bool = True,
) -> bytes:
    """Decrypts a stored credential.

    `touch` records that the secret was used. That timestamp is what answers "is this
    camera credential still in use, or can it be revoked", and it is worth the write:
    credentials nobody can account for are how a breach stays useful for years.

    Row-level security scopes the lookup, and the tenant is *also* checked
    cryptographically by the AAD - so a policy mistake alone is not enough to read another
    tenant's credential.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT kek_id, wrapped_dek, dek_nonce, ciphertext, ciphertext_nonce, purpose
                FROM encrypted_secrets
                WHERE id = :id
                """
            ),
            {"id": secret_id},
        )
    ).first()

    if row is None:
        raise EnvelopeError("No such secret.")
    if row[5] != purpose:
        # Caught by the AAD too; failing here gives the operator a clearer message than
        # an authentication failure would.
        raise EnvelopeError(
            f"Secret is stored for '{row[5]}', not '{purpose}'."
        )

    sealed = SealedSecret.from_row(
        {
            "kek_id": row[0], "wrapped_dek": row[1], "dek_nonce": row[2],
            "ciphertext": row[3], "ciphertext_nonce": row[4],
        }
    )
    plaintext = open_secret(
        keyring, sealed, tenant_id=tenant_id, secret_id=secret_id, purpose=purpose
    )

    if touch:
        await session.execute(
            text("UPDATE encrypted_secrets SET last_used_at = :now WHERE id = :id"),
            {"id": secret_id, "now": dt.datetime.now(dt.UTC)},
        )
    return plaintext


async def delete_secret(session: AsyncSession, *, secret_id: uuid.UUID) -> None:
    await session.execute(
        text("DELETE FROM encrypted_secrets WHERE id = :id"), {"id": secret_id}
    )
    logger.info("secret_deleted", extra={"secret_id": str(secret_id)})


async def rewrap_under_active_key(
    session: AsyncSession, keyring: KeyRing, *, batch: int = 200
) -> int:
    """Rewraps secrets still sealed under a retired key. Returns how many were changed.

    Run repeatedly until it returns 0. Batched and idempotent so an interrupted rotation
    can simply be re-run, and so a rotation on a large deployment does not hold one long
    transaction open.

    Needs a platform session: rotation legitimately spans every tenant.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT id, tenant_id, purpose, kek_id, wrapped_dek, dek_nonce,
                       ciphertext, ciphertext_nonce
                FROM encrypted_secrets
                WHERE kek_id <> :active
                ORDER BY created_at
                LIMIT :batch
                """
            ),
            {"active": keyring.active_id, "batch": batch},
        )
    ).all()

    changed = 0
    for row in rows:
        secret_id, tenant_id, purpose = row[0], row[1], row[2]
        sealed = SealedSecret.from_row(
            {
                "kek_id": row[3], "wrapped_dek": row[4], "dek_nonce": row[5],
                "ciphertext": row[6], "ciphertext_nonce": row[7],
            }
        )
        try:
            fresh = rewrap(
                keyring, sealed,
                tenant_id=tenant_id, secret_id=secret_id, purpose=purpose,
            )
        except EnvelopeError:
            # One unreadable row must not stop the rotation - most likely its old key was
            # removed too early, and that needs an operator, not a crashed job.
            logger.exception(
                "secret_rewrap_failed",
                extra={"secret_id": str(secret_id), "kek_id": row[3]},
            )
            continue

        await session.execute(
            text(
                """
                UPDATE encrypted_secrets
                SET kek_id = :kek_id, wrapped_dek = :wrapped_dek, dek_nonce = :dek_nonce,
                    rotated_at = now(), updated_at = now()
                WHERE id = :id
                """
            ),
            {"id": secret_id, **{k: v for k, v in fresh.as_row().items()
                                 if k in ("kek_id", "wrapped_dek", "dek_nonce")}},
        )
        changed += 1

    if changed:
        logger.info(
            "secrets_rewrapped", extra={"count": changed, "kek_id": keyring.active_id}
        )
    return changed
