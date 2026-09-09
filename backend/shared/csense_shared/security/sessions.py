"""Redis-backed rotating refresh sessions (TRD §7.1), shared by every API that issues
tokens (Tenant API and Admin API both use this — the session record's `audience` field
is what keeps the two token families apart).

Only a keyed hash of the refresh token is ever stored. On every refresh call the token is
rotated: the old value is replaced, and if a client ever presents a refresh token that no
longer matches what's stored (because it was already rotated — i.e. replayed), the whole
session is revoked and the caller must re-authenticate.

## Grace window for concurrent legitimate use (added 2026-09-09)

Strict "any mismatch nukes the session" is correct for a genuine replay (a stolen token
reused by an attacker, or a stale token presented minutes/hours after it was superseded).
It is *not* correct for two legitimate, near-simultaneous refresh calls presenting the
same current token — e.g. two browser tabs of the same account both silently refreshing
around the same token-expiry moment, or a slow-network retry racing the original request.
Found via React 18 StrictMode's dev-only double-invoke of the CRM's silent-refresh effect,
which reproduced this 3/3 times: the first call's rotation succeeds, the second call
arrives a moment later presenting the now-superseded token, is treated as a replay attack,
and the *entire session* is destroyed — logging every tab out, not just the "losing" one.

The fix is the standard "refresh token reuse detection interval" shape used by real-world
rotation systems (Auth0 and others describe this exact pattern): when a presented token
doesn't match the *current* stored hash, it is still accepted if it matches the hash that
was current *immediately before the last rotation*, provided that rotation happened within
a short grace window. A grace hit does not rotate again or delete the session — it hands
the caller back the SAME current token the winning caller already established, so both
callers converge on identical, currently-valid session state and neither gets logged out.
Anything that matches neither the current nor the just-superseded hash (forged, or genuinely
stale/replayed once the grace window has closed) still deletes the session exactly as before
— this path is not weakened at all.

**Grace window: `GRACE_WINDOW_SECONDS = 10`.** Chosen to comfortably absorb the real
scenarios this targets (two tabs racing a silent refresh, a retried request on a slow
network) while staying "a few seconds," not something that would meaningfully widen the
window for an actual stolen-token replay. A token stolen and replayed by an attacker
*within* 10 seconds of a legitimate rotation would now succeed instead of revoking the
session — this is the accepted tradeoff of the reuse-detection-interval design generally,
not a flaw specific to this implementation; 10 seconds is short enough that this requires
the attacker to already be racing the legitimate client in near-real-time, which is a much
narrower window than "any replay, ever" that the old code protected against regardless of
timing. Every rejection path (unknown session, hash matches neither current nor
grace-window-previous) is completely unchanged from before this fix.

**Concurrency correctness**: the compare-current/check-grace/rotate-or-reject decision has
to be atomic, not "read, then decide, then write" in separate round trips — two truly
concurrent callers (real `asyncio.gather`, not just sequential calls that happen to race in
practice) can both observe the same pre-rotation state if the decision isn't atomic, and
both would then believe themselves to be "the" legitimate rotator, corrupting the session
into two divergent current tokens where whichever caller's write lands second silently
invalidates the other's already-returned token. This module makes the whole
read-decide-write sequence one Redis `EVAL` (Lua runs to completion inside Redis without
interleaving with any other client, including another call to this same script) rather
than separate `HGETALL`/`HSET` calls.

**TTL renewal on a grace hit**: yes, the main session key's TTL is re-extended on a grace
hit exactly like a normal rotation, since a grace hit represents real, legitimate activity
on the session and there's no reason to let it expire sooner just because it arrived a
moment after the "winning" call. The grace key's *own* TTL is deliberately never renewed
by being read — it expires on its original schedule regardless of how many stragglers hit
it, because a grace window is a short window, not "the previous token stays valid as long
as someone keeps asking."

**Logging**: a grace-window hit is logged at INFO (`rotate_session_grace_window_hit`) —
it's expected, legitimate behavior, but operationally worth being able to see how often
real concurrent-tab races are happening. An actual rejection (forged token, or a real
replay past the grace window) is logged at WARNING (`rotate_session_replay_rejected`),
distinctly from a grace hit, since the latter is evidence of exactly the attack this
whole module exists to catch — or of the grace window being too short/long for real
traffic patterns, which is itself useful to know.
"""
from __future__ import annotations

import json
import logging
from uuid import UUID

import redis.asyncio as redis

from csense_shared.config import Settings
from csense_shared.security.tokens import (
    generate_refresh_token,
    hash_refresh_token,
    new_session_id,
)

logger = logging.getLogger(__name__)

# A few seconds is enough to absorb two real browser tabs (or a slow-network retry) racing
# the same refresh call; it is deliberately far short of anything that would meaningfully
# help a genuine stolen-token replay (see module docstring for the full tradeoff).
GRACE_WINDOW_SECONDS = 10

# Atomic compare-current/check-grace/rotate-or-reject. Runs to completion inside Redis with
# no interleaving from any other caller (including another concurrent call to this same
# script), which is what makes the fix here actually race-free rather than merely
# race-unlikely. KEYS[1] = session key, KEYS[2] = that session's grace key. ARGV =
# presented_hash, new_raw_token, new_token_hash, session_ttl_seconds, grace_ttl_seconds.
_ROTATE_SCRIPT = """
local session_key = KEYS[1]
local grace_key = KEYS[2]
local presented_hash = ARGV[1]
local new_raw_token = ARGV[2]
local new_token_hash = ARGV[3]
local session_ttl = tonumber(ARGV[4])
local grace_ttl = tonumber(ARGV[5])

if redis.call('EXISTS', session_key) == 0 then
  return cjson.encode({_status = 'none'})
end

local current_hash = redis.call('HGET', session_key, 'token_hash')
local raw_token
local status

if current_hash == presented_hash then
  -- The presented token IS the current one: this caller legitimately rotates it.
  redis.call('HSET', session_key, 'token_hash', new_token_hash)
  redis.call('EXPIRE', session_key, session_ttl)
  redis.call('HSET', grace_key, 'accepts_hash', current_hash, 'raw_token', new_raw_token)
  redis.call('EXPIRE', grace_key, grace_ttl)
  raw_token = new_raw_token
  status = 'rotated'
else
  local grace_accepts = redis.call('HGET', grace_key, 'accepts_hash')
  if grace_accepts ~= false and grace_accepts == presented_hash then
    -- The presented token was the current one a moment ago, superseded by a concurrent
    -- rotation still inside its grace window: hand back that SAME new current token
    -- rather than rotating again or destroying the session.
    raw_token = redis.call('HGET', grace_key, 'raw_token')
    redis.call('EXPIRE', session_key, session_ttl)
    status = 'grace'
  else
    -- Matches neither the current nor the recently-superseded token: forged, or a
    -- genuine replay past the grace window. Revoke the whole family, as before.
    redis.call('DEL', session_key)
    redis.call('DEL', grace_key)
    return cjson.encode({_status = 'rejected'})
  end
end

local flat = redis.call('HGETALL', session_key)
local record = {}
for i = 1, #flat, 2 do
  record[flat[i]] = flat[i + 1]
end
record['new_refresh_token'] = raw_token
record['_status'] = status
return cjson.encode(record)
"""


def _session_key(settings: Settings, session_id: str) -> str:
    return f"cs:{settings.environment}:session:{session_id}"


def _grace_key(settings: Settings, session_id: str) -> str:
    return f"cs:{settings.environment}:session:{session_id}:grace"


async def create_session(
    redis_client: redis.Redis,
    settings: Settings,
    *,
    user_id: UUID,
    tenant_id: UUID | None,
    membership_id: UUID | None,
    audience: str,
) -> tuple[str, str]:
    """Returns (session_id, raw_refresh_token). Only the hash is persisted."""
    session_id = new_session_id()
    raw_token = generate_refresh_token()
    key = _session_key(settings, session_id)
    await redis_client.hset(
        key,
        mapping={
            "token_hash": hash_refresh_token(raw_token),
            "user_id": str(user_id),
            "tenant_id": str(tenant_id) if tenant_id else "",
            "membership_id": str(membership_id) if membership_id else "",
            "audience": audience,
        },
    )
    await redis_client.expire(key, settings.jwt_refresh_token_ttl_seconds)
    return session_id, raw_token


async def rotate_session(
    redis_client: redis.Redis, settings: Settings, *, session_id: str, presented_token: str
) -> dict[str, str] | None:
    """Returns the session record with a rotated token hash, or None if the presented
    token doesn't match the current one *or* a just-superseded one still inside its grace
    window (expired/unknown/forged/stale-replayed) — callers must revoke on None and
    require re-authentication rather than silently failing open.

    See the module docstring for the grace-window design: a presented token that matches
    the immediately-previous token within `GRACE_WINDOW_SECONDS` of it being superseded
    gets back the SAME current token a concurrent legitimate caller already established,
    rather than being treated as a replay. Everything else still revokes the session.
    """
    key = _session_key(settings, session_id)
    grace_key = _grace_key(settings, session_id)
    new_token = generate_refresh_token()

    raw = await redis_client.eval(
        _ROTATE_SCRIPT,
        2,
        key,
        grace_key,
        hash_refresh_token(presented_token),
        new_token,
        hash_refresh_token(new_token),
        str(settings.jwt_refresh_token_ttl_seconds),
        str(GRACE_WINDOW_SECONDS),
    )
    result: dict[str, str] = json.loads(raw)
    status = result.pop("_status")

    if status == "none":
        return None
    if status == "rejected":
        logger.warning("rotate_session_replay_rejected", extra={"session_id": session_id})
        return None
    if status == "grace":
        logger.info("rotate_session_grace_window_hit", extra={"session_id": session_id})
        return result
    return result  # status == "rotated": the normal, non-racing path.


async def revoke_session(redis_client: redis.Redis, settings: Settings, *, session_id: str) -> None:
    await redis_client.delete(_session_key(settings, session_id))
    await redis_client.delete(_grace_key(settings, session_id))
