"""Redis-backed rotating refresh sessions (TRD §7.1), shared by every API that issues
tokens (Tenant API and Admin API both use this — the session record's `audience` field
is what keeps the two token families apart).

Only a keyed hash of the refresh token is ever stored. On every refresh call the token is
rotated: the old value is replaced, and if a client ever presents a refresh token that no
longer matches what's stored (because it was already rotated — i.e. replayed), the whole
session is revoked and the caller must re-authenticate.

## Grace window for concurrent legitimate use (added 2026-09-09, made multi-generation
## 2026-09-09 after an independent security review reproduced a real gap in the first cut)

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
rotation systems (Auth0 and others describe this exact pattern): a token that doesn't
match the *current* stored hash is still accepted if it matches a hash that was current at
some earlier generation, provided that generation's own short grace window hasn't yet
elapsed. A grace hit never rotates again or deletes the session — it hands the caller back
the SAME current token the winning caller already established, so every caller converges
on identical, currently-valid session state and nobody gets logged out.

### The first cut's real gap, found by an independent security review (2026-09-09)

The original implementation kept exactly **one** grace slot — the immediately-previous
generation's hash and the current raw token, in a single Redis hash (`grace_key`) that a
second rotation unconditionally overwrote. That is correct for exactly one rotation
happening while stragglers of the previous one are still in flight, but wrong the moment a
*second* legitimate rotation lands before every straggler of the *first* one has been
served: those first-generation stragglers now match neither the current hash nor the
(already clobbered) grace hash, so the `else`/reject branch fired — deleting the whole
session for every caller, including ones holding the brand-new, genuinely-current token.
This is realistic, not exotic: `frontend/customer-crm/src/api/client.ts`'s
`refreshAccessToken()` has no client-side mutex, so any number of independently-401ing
calls (or separate tabs) can each fire `/auth/refresh` with whatever cookie value they
individually captured, and ordinary network jitter — not adversarial timing — can deliver
a newer-generation call to Redis before an older-generation straggler that was actually
*dispatched* earlier. The review proved this live against real Redis with a real
`asyncio.gather` burst of 10 callers on generation 0 racing 10 callers on generation 1: 4
of 8 trials destroyed the session outright.

### The fix: a bounded multi-generation grace history, not one overwritten slot

`grace_key` is now a Redis **hash** holding:
- one field per recently-superseded generation, `field = that generation's token hash`,
  `value = the unix timestamp its OWN grace window expires at` — stamped once, at the
  moment that generation was superseded, and never touched again by a later rotation. A
  second (or third, or Nth) rotation only *adds* a new field; it never overwrites or
  shortens an already-live one. This is precisely what closes the gap above: generation
  0's entry survives generation 1's rotation, generation 2's rotation, etc., for as long as
  generation 0's own window says it should.
- one reserved field, `__current__`, holding the raw value of whatever the CURRENT token
  is right now. This is the one piece of raw-token state this module keeps outside of the
  access token itself, deliberately, and only for as long as some grace generation might
  still need to hand it back — updated on every real rotation, read (never written) on
  every grace hit. A grace hit for generation 0 does NOT hand back generation 1's raw
  token (which may itself be stale by the time generation 0's straggler is finally
  served) — it always hands back whatever is truly current *right now*, so every straggler
  from every still-live generation converges on the one truly currently-valid token, exactly
  like the single-slot design did for one generation, now for however many are live.

Matching a hash that is neither the current one nor found (unexpired) in this history —
forged, or a genuine replay past every live generation's window — still deletes the whole
session exactly as before. This path is not weakened at all; see "what this does NOT
widen" below.

**Bounding the history — `MAX_GRACE_GENERATIONS = 5`.** Each generation's entry already
self-expires via its own `expires_at` (checked lazily, on every call, before the
accept/reject decision) — so the history can't grow simply from the passage of time. The
cap exists as *defense in depth* against a caller (or an attacker who has legitimately
obtained a valid current token, e.g. a compromised-but-not-yet-detected session) rotating
far faster than `GRACE_WINDOW_SECONDS`, which would otherwise let the hash grow one field
per rotation for as long as that pace continued. 5 is generous relative to the realistic
concurrency this exists for (a handful of browser tabs / retries racing within a ~10s
window) while still being a hard, small ceiling — when exceeded, the *oldest-expiring*
entries are evicted first (their own TTL was going to reclaim them soonest anyway, so
evicting them early costs the least).

**What this does NOT widen.** The reject path's actual criterion is unchanged in kind: "is
there a still-live record that this exact hash was ever a legitimate current token,
superseded within its own grace window." Before this fix that record could only ever
contain one generation; now it can contain up to five, each with the *same*
`GRACE_WINDOW_SECONDS` (10s) TTL as before. A hash that was never a real generation of this
session (forged) still matches nothing, ever. A hash that WAS a real generation but has
been superseded for longer than 10 seconds — whether because only one rotation happened
and 10s passed, or because five rotations happened and its own 10-second clock ran out, or
because more than `MAX_GRACE_GENERATIONS` rotations happened since and it was evicted early
— still gets rejected and still destroys the session. What changes is *only* the specific
bug where a second rotation's mere occurrence, independent of any elapsed time, could
prematurely kill a first generation's still-live grace entry. An attacker racing a
legitimate client within the same 10-second window has exactly the tradeoff the original
single-generation design already accepted (see below) — multi-generation does not lengthen
that window, it just stops a later, *unrelated* legitimate rotation from artificially
shortening it.

One thing this framing doesn't say outright, worth stating precisely rather than leaving
implicit (found in the second round of security review): the *aggregate* number of
distinct stolen token values an attacker could successfully replay at once did grow, from
at most 1 (the single prior slot) to at most `MAX_GRACE_GENERATIONS` (5) — each still only
exploitable within its own unchanged 10-second window, and this is an unavoidable
consequence of the fix itself, not a separate flaw: a legitimate straggler and a malicious
replay are the same shape to the server, so protecting the former across more than one
generation necessarily protects the latter across the same generations too. Bounded, short-
lived, and accepted as the same order-of-magnitude tradeoff the single-generation design
already made — but the honest count is "up to 5 live windows," not "the window."

**Grace window: `GRACE_WINDOW_SECONDS = 10`** (unchanged). Chosen to comfortably absorb the
real scenarios this targets (two tabs racing a silent refresh, a retried request on a slow
network) while staying "a few seconds," not something that would meaningfully widen the
window for an actual stolen-token replay. A token stolen and replayed by an attacker
*within* 10 seconds of a legitimate rotation would now succeed instead of revoking the
session — this is the accepted tradeoff of the reuse-detection-interval design generally,
not a flaw specific to this implementation; 10 seconds is short enough that this requires
the attacker to already be racing the legitimate client in near-real-time, which is a much
narrower window than "any replay, ever" that the old code protected against regardless of
timing.

**Concurrency correctness (unchanged, still load-bearing)**: the compare-current /
check-every-live-grace-generation / rotate-or-reject decision has to be atomic, not "read,
then decide, then write" in separate round trips — two truly concurrent callers (real
`asyncio.gather`, not just sequential calls that happen to race in practice) can both
observe the same pre-rotation state if the decision isn't atomic, and both would then
believe themselves to be "the" legitimate rotator, corrupting the session into two
divergent current tokens where whichever caller's write lands second silently invalidates
the other's already-returned token. This module makes the whole read-decide-write
sequence, INCLUDING the lazy expiry of stale grace generations and the cap eviction, one
Redis `EVAL` (Lua runs to completion inside Redis without interleaving with any other
client, including another call to this same script) rather than separate
`HGETALL`/`HSET`/`HDEL` calls.

**TTL renewal on a grace hit**: yes, the main session key's TTL is re-extended on a grace
hit exactly like a normal rotation, since a grace hit represents real, legitimate activity
on the session and there's no reason to let it expire sooner just because it arrived a
moment after the "winning" call. Each grace generation's *own* expiry is deliberately never
extended by being read (a grace window is a short window, not "the previous token stays
valid as long as someone keeps asking") — matched-and-returned exactly, then left alone.
The `grace_key`'s outer Redis-level TTL (a hygiene backstop, not the thing enforcing any
individual generation's expiry — that's the per-field `expires_at` checked in Lua) is
refreshed to `GRACE_WINDOW_SECONDS` on every rotation, which is always long enough to cover
whatever the newest, longest-lived entry needs.

**Logging**: a grace-window hit is logged at INFO (`rotate_session_grace_window_hit`) —
it's expected, legitimate behavior, but operationally worth being able to see how often
real concurrent-tab races are happening. An actual rejection (forged token, or a real
replay past every live grace generation) is logged at WARNING
(`rotate_session_replay_rejected`), distinctly from a grace hit, since the latter is
evidence of exactly the attack this whole module exists to catch — or of the grace window
being too short/long, or the generation cap too small, for real traffic patterns, which is
itself useful to know.
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

# Defense in depth, independent of the per-generation TTL above: even though every grace
# generation self-expires on its own schedule, cap how many superseded generations a single
# session's grace history can hold at once, so a caller (or an attacker legitimately
# holding a valid current token) rotating far faster than GRACE_WINDOW_SECONDS can't grow
# it without bound. See module docstring for why 5 is generous-but-bounded for the real
# concurrency this exists to absorb.
MAX_GRACE_GENERATIONS = 5

# The reserved grace_key field holding the CURRENT token's raw value - the one piece of raw
# refresh-token state this module keeps outside the token itself, and only for as long as
# some grace generation might still need it. Not a real generation hash, so the lazy-expiry
# loop below must skip it explicitly.
_CURRENT_FIELD = "__current__"

# Atomic compare-current/check-every-live-grace-generation/rotate-or-reject. Runs to
# completion inside Redis with no interleaving from any other caller (including another
# concurrent call to this same script), which is what makes the fix here actually
# race-free rather than merely race-unlikely.
#
# KEYS[1] = session key, KEYS[2] = that session's grace key (a Redis HASH; see module
# docstring for its shape: one field per still-live superseded generation holding that
# generation's own expiry, plus one reserved `__current__` field holding the current raw
# token). ARGV = presented_hash, new_raw_token, new_token_hash, session_ttl_seconds,
# grace_ttl_seconds, max_grace_generations.
_ROTATE_SCRIPT = """
local session_key = KEYS[1]
local grace_key = KEYS[2]
local presented_hash = ARGV[1]
local new_raw_token = ARGV[2]
local new_token_hash = ARGV[3]
local session_ttl = tonumber(ARGV[4])
local grace_ttl = tonumber(ARGV[5])
local max_generations = tonumber(ARGV[6])
local CURRENT_FIELD = '__current__'

if redis.call('EXISTS', session_key) == 0 then
  return cjson.encode({_status = 'none'})
end

local now = tonumber(redis.call('TIME')[1])

-- Lazily drop any grace generation whose OWN window has genuinely elapsed. Each
-- superseded generation carries its own expires_at, stamped once at the moment it was
-- superseded and never touched again - a later rotation adding a new generation never
-- shortens (or extends) an earlier one's remaining lifetime. This is exactly what the
-- single overwritten slot got wrong.
local raw_fields = redis.call('HGETALL', grace_key)
local live_generations = {}   -- hash -> expires_at, only entries not yet expired
local live_order = {}         -- {hash, expires_at} list, oldest-and-newest mixed, for cap eviction
for i = 1, #raw_fields, 2 do
  local field = raw_fields[i]
  if field ~= CURRENT_FIELD then
    local expires_at = tonumber(raw_fields[i + 1])
    if expires_at and expires_at > now then
      live_generations[field] = expires_at
      table.insert(live_order, {hash = field, expires_at = expires_at})
    else
      redis.call('HDEL', grace_key, field)
    end
  end
end

local current_hash = redis.call('HGET', session_key, 'token_hash')
local raw_token
local status

if current_hash == presented_hash then
  -- Legitimate rotation. The hash being superseded becomes a brand-new grace generation
  -- with its own fresh expiry; every generation still live from BEFORE this call keeps
  -- its own untouched expires_at untouched - a second, third, ... rotation no longer
  -- clobbers an earlier rotation's still-live grace entry.
  redis.call('HSET', session_key, 'token_hash', new_token_hash)
  redis.call('EXPIRE', session_key, session_ttl)

  redis.call('HSET', grace_key, CURRENT_FIELD, new_raw_token)
  redis.call('HSET', grace_key, current_hash, tostring(now + grace_ttl))
  table.insert(live_order, {hash = current_hash, expires_at = now + grace_ttl})

  -- Bound the history independent of TTL (defense in depth - see module docstring).
  -- Evict the soonest-to-expire entries first once the cap is exceeded; each one's own
  -- TTL was going to reclaim it soonest anyway, so evicting early costs the least.
  if #live_order > max_generations then
    table.sort(live_order, function(a, b) return a.expires_at < b.expires_at end)
    for i = 1, #live_order - max_generations do
      redis.call('HDEL', grace_key, live_order[i].hash)
    end
  end

  redis.call('EXPIRE', grace_key, grace_ttl)
  raw_token = new_raw_token
  status = 'rotated'
else
  if live_generations[presented_hash] ~= nil then
    -- The presented token was current at some earlier generation, and THAT generation's
    -- own grace window has not elapsed - regardless of how many rotations have happened
    -- since. Always hand back the actual CURRENT token (mirrored into CURRENT_FIELD on
    -- every real rotation), never a stale intermediate one and never a fresh mint - a
    -- grace hit must never itself trigger a new rotation.
    raw_token = redis.call('HGET', grace_key, CURRENT_FIELD)
    redis.call('EXPIRE', session_key, session_ttl)
    status = 'grace'
  else
    -- Matches no live generation: forged, or a genuine replay past every live grace
    -- window (or evicted by the generation cap). Revoke the whole family, as before.
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
    token doesn't match the current one *or* any still-live superseded generation in its
    grace history (expired/unknown/forged/stale-replayed) — callers must revoke on None and
    require re-authentication rather than silently failing open.

    See the module docstring for the multi-generation grace design: a presented token that
    matches ANY recent generation still inside that generation's own
    `GRACE_WINDOW_SECONDS` (up to `MAX_GRACE_GENERATIONS` tracked at once) gets back the
    SAME current token a concurrent legitimate caller already established, rather than
    being treated as a replay - regardless of how many OTHER legitimate rotations have
    happened in the meantime. Everything else still revokes the session.
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
        str(MAX_GRACE_GENERATIONS),
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
