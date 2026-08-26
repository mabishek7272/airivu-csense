"""Retry scheduling for notification deliveries (TRD §18).

Exponential backoff with jitter, and per-channel limits, because the channels do not fail
alike:

  Email tolerates a long tail. A message delivered 20 minutes late is still useful, and
  Resend rate limits clear quickly.

  WhatsApp does not. An intrusion alert that arrives 20 minutes late is worse than
  useless - the operator has either already dealt with it or the moment has passed - so
  it gives up sooner and escalates to another channel instead of retrying into the void.

Jitter is not decoration. Every camera on a site tends to fire at once, so their alerts
retry at the same instant, hammer the provider together, and get rate limited together -
a thundering herd that turns one transient failure into a sustained outage. Randomising
the delay spreads them.
"""
from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass

from csense_shared.notifications.providers import Channel


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float
    # Fraction of the computed delay to randomise by, +/-.
    jitter_ratio: float = 0.25

    def next_delay(self, attempt: int, *, rng: random.Random | None = None) -> dt.timedelta:
        """Delay before attempt number `attempt` (1-based: attempt 1 already happened)."""
        if attempt < 1:
            attempt = 1
        raw = min(self.base_delay_seconds * (2 ** (attempt - 1)), self.max_delay_seconds)
        spread = raw * self.jitter_ratio
        chooser = rng or random
        jittered = raw + chooser.uniform(-spread, spread)
        # Never negative, and never zero - a zero delay is a busy loop.
        return dt.timedelta(seconds=max(1.0, jittered))

    def exhausted(self, attempt_count: int) -> bool:
        return attempt_count >= self.max_attempts


# Per-channel policies. The totals matter more than the individual numbers:
#   email    ~4 attempts over roughly 15 minutes
#   whatsapp ~3 attempts over roughly 2 minutes, then escalate
#   webhook  ~6 attempts over roughly an hour (a customer's endpoint may be redeploying)
POLICIES: dict[Channel, RetryPolicy] = {
    Channel.EMAIL: RetryPolicy(max_attempts=4, base_delay_seconds=30, max_delay_seconds=600),
    Channel.WHATSAPP: RetryPolicy(max_attempts=3, base_delay_seconds=15, max_delay_seconds=120),
    Channel.SMS: RetryPolicy(max_attempts=3, base_delay_seconds=20, max_delay_seconds=180),
    Channel.WEB_PUSH: RetryPolicy(max_attempts=3, base_delay_seconds=10, max_delay_seconds=120),
    Channel.IN_APP: RetryPolicy(max_attempts=2, base_delay_seconds=5, max_delay_seconds=30),
    # Webhooks are the most forgiving: the receiver is someone else's system, and a
    # deploy window should not lose an event.
    Channel.WEBHOOK: RetryPolicy(max_attempts=6, base_delay_seconds=30, max_delay_seconds=1800),
}

DEFAULT_POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=30, max_delay_seconds=600)


def policy_for(channel: Channel) -> RetryPolicy:
    return POLICIES.get(channel, DEFAULT_POLICY)


def schedule_next_attempt(
    channel: Channel,
    attempt_count: int,
    *,
    now: dt.datetime | None = None,
    rng: random.Random | None = None,
) -> dt.datetime | None:
    """When to try again, or None when the channel has given up.

    Returning None is a decision, not an error: the delivery is marked `abandoned` and the
    escalation ladder moves to the next step, which is a different person or a different
    channel. Retrying one dead channel forever is how an alert reaches nobody.
    """
    policy = policy_for(channel)
    if policy.exhausted(attempt_count):
        return None
    moment = now or dt.datetime.now(dt.UTC)
    return moment + policy.next_delay(attempt_count + 1, rng=rng)
