"""Authenticating an edge device, as opposed to a person.

A device is not a user. It has no session, no refresh token and no membership; it holds one
long-lived credential issued at enrolment. This module resolves that credential into an
identity the rest of the API can scope by.

**The device never says which tenant it belongs to.** The credential establishes that, and
only the credential. A field in the request body would be an invitation to claim someone
else's tenant, and would have to be checked against the credential anyway - so it does not
exist.

**Every failure looks the same.** Unknown prefix, wrong digest, disabled device, retired
device: all `401` with one message. Distinguishing them would let someone with a list of
guesses learn which prefixes are real.

The lookup goes through a `SECURITY DEFINER` function for the same reason enrolment does:
row-level security scopes queries by tenant, and resolving the credential is what
*discovers* the tenant. The Tenant API's own role deliberately cannot bypass RLS, so the
exception is one narrow function rather than a broader grant.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import logging

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.errors import ApiError

logger = logging.getLogger(__name__)

TOKEN_PREFIX_LENGTH = 8

# Statuses a device may hold and still be allowed to report. `pending` is absent on
# purpose: a device that has not enrolled has no credential to present.
ACTIVE_STATUSES = ("enrolled", "online", "offline")


@dataclasses.dataclass(frozen=True)
class AgentContext:
    """Who is calling, when the caller is a device."""

    device_id: str
    tenant_id: str
    name: str
    role: str


def _rejection() -> ApiError:
    return ApiError(
        status_code=401,
        code="device_unauthenticated",
        message="That device credential is not valid.",
    )


def _presented_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise _rejection()
    return value.strip()


async def current_agent(request: Request) -> AgentContext:
    """Resolves the presented device credential, or refuses.

    Opens its own session rather than depending on the tenant-scoped one: that dependency
    needs a tenant, and here the tenant is not yet known.
    """
    presented = _presented_token(request)
    prefix = presented[:TOKEN_PREFIX_LENGTH]
    digest = hashlib.sha256(presented.encode()).hexdigest()

    factory = request.app.state.session_factory
    async with factory() as db:
        row = (
            await db.execute(
                text("SELECT * FROM edge_agent_lookup(:prefix)"), {"prefix": prefix}
            )
        ).first()

    if row is None:
        logger.warning("agent_auth_unknown_prefix", extra={"token_prefix": prefix})
        raise _rejection()

    # Constant-time, and on the digest rather than the token.
    if not hmac.compare_digest(row[2] or "", digest):
        logger.warning("agent_auth_bad_credential", extra={"token_prefix": prefix})
        raise _rejection()

    if row[3] not in ACTIVE_STATUSES:
        # A disabled or retired device must stop being able to report the moment it is
        # marked so, without waiting for a credential to expire.
        logger.warning(
            "agent_auth_inactive_device",
            extra={"device_id": str(row[0]), "status": row[3]},
        )
        raise _rejection()

    return AgentContext(
        device_id=str(row[0]), tenant_id=str(row[1]), name=row[4], role=row[5]
    )


async def agent_db_session(
    request: Request, agent: AgentContext = Depends(current_agent)
) -> AsyncSession:
    """A session scoped to the calling device's tenant.

    The scope comes from the credential, never from the request, so a device physically
    cannot read or write another tenant's rows even if it asks to. `set_config(..., true)`
    keeps the setting inside the transaction so it cannot leak to the next checkout of a
    pooled connection.
    """
    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": agent.tenant_id},
        )
        yield session
