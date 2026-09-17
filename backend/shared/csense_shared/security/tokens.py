"""Asymmetric JWT access tokens + opaque rotating refresh tokens.

TRD §7.1:
- Access tokens: asymmetric JWT, 10-15 minute lifetime, issuer/audience/key ID, tenant
  membership reference, compact role/permission claims.
- Refresh sessions: opaque rotating token in secure HTTP-only SameSite cookie; only a
  keyed hash is stored server-side (Redis). Replay of an already-rotated refresh token
  revokes the whole session family and flags a security event.

Token audiences are `csense-customer` and `csense-platform` (TRD §6.1/6.2) and are never
interchangeable — the Tenant API rejects a platform-audience token and vice versa.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import jwt

from csense_shared.config import Settings

AUDIENCE_CUSTOMER = "csense-customer"
AUDIENCE_PLATFORM = "csense-platform"


class TokenError(Exception):
    """Raised for any invalid/expired/wrong-audience token. Callers must treat this as
    an authentication failure, never fall back to a default identity."""


@dataclass(frozen=True)
class AccessTokenClaims:
    subject_user_id: UUID
    audience: str
    tenant_id: UUID | None
    membership_id: UUID | None
    permissions: frozenset[str]
    site_scope_mode: str
    site_ids: frozenset[UUID]
    jti: str
    session_id: str


def issue_access_token(
    *,
    settings: Settings,
    user_id: UUID,
    audience: str,
    tenant_id: UUID | None,
    membership_id: UUID | None,
    permissions: frozenset[str],
    session_id: str,
    site_scope_mode: str = "none",
    site_ids: frozenset[UUID] = frozenset(),
    key_id: str = "local-dev-1",
) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": settings.jwt_issuer,
        "aud": audience,
        "sub": str(user_id),
        "iat": now,
        "nbf": now,
        "exp": now + settings.jwt_access_token_ttl_seconds,
        "jti": str(uuid4()),
        "sid": session_id,
        "perm": sorted(permissions),
    }
    if tenant_id is not None:
        payload["tenant_id"] = str(tenant_id)
    if membership_id is not None:
        payload["membership_id"] = str(membership_id)
    if tenant_id is not None:
        # Only meaningful alongside a real tenant membership - a platform-audience token
        # (no tenant_id) never carries these at all, matching how tenant_id/membership_id
        # are already conditionally included above.
        payload["ssm"] = site_scope_mode
        if site_ids:
            payload["sids"] = sorted(str(s) for s in site_ids)

    return jwt.encode(
        payload,
        settings.jwt_private_key,
        algorithm=settings.jwt_algorithm,
        headers={"kid": key_id},
    )


def decode_access_token(
    token: str, *, settings: Settings, expected_audience: str
) -> AccessTokenClaims:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_public_key,
            algorithms=[settings.jwt_algorithm],
            audience=expected_audience,
            issuer=settings.jwt_issuer,
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc

    tenant_id = payload.get("tenant_id")
    membership_id = payload.get("membership_id")
    return AccessTokenClaims(
        subject_user_id=UUID(payload["sub"]),
        audience=payload["aud"],
        tenant_id=UUID(tenant_id) if tenant_id else None,
        membership_id=UUID(membership_id) if membership_id else None,
        permissions=frozenset(payload.get("perm", [])),
        site_scope_mode=payload.get("ssm", "none"),
        site_ids=frozenset(UUID(s) for s in payload.get("sids", [])),
        jti=payload["jti"],
        session_id=payload["sid"],
    )


# --- Refresh tokens: opaque, rotating, only a keyed hash persisted server-side ---

def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    """One-way keyed digest for server-side storage/comparison. The raw token never
    touches a database row — only this hash does (TRD §7.1)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session_id() -> str:
    return str(uuid4())
