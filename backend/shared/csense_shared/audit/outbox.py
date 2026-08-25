"""Write audit event + outbox event together, in the caller's existing transaction.

Architecture principle 8 ("Outbox before dual writes"): state change and event intent
are committed together. This helper does not commit/begin a transaction itself — the
caller's `tenant_session()`/`platform_session()` block owns the transaction boundary, so
a failure anywhere in the request rolls back the business mutation, the audit row, and
the outbox row atomically.

Redaction: TRD-SEC-005 requires secrets/tokens/passwords/camera URLs/provider secrets and
regulated identifiers never appear in audit payloads. `before_patch`/`after_patch` must be
pre-redacted by the caller before being passed in here — this module does not attempt to
guess what's sensitive in an arbitrary dict.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.db.models import AuditEvent, OutboxEvent


async def record_audit_and_outbox(
    session: AsyncSession,
    *,
    tenant_id: UUID | None,
    actor_type: str,
    actor_id: str | None,
    action: str,
    outcome: str,
    target_type: str | None = None,
    target_id: str | None = None,
    reason: str | None = None,
    before_patch: dict[str, Any] | None = None,
    after_patch: dict[str, Any] | None = None,
    correlation_id: UUID | None = None,
    session_id: UUID | None = None,
    represented_actor_id: UUID | None = None,
    support_grant_id: UUID | None = None,
    event_type: str | None = None,
    event_payload: dict[str, Any] | None = None,
    aggregate_type: str | None = None,
    aggregate_id: str | None = None,
) -> None:
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_type=actor_type,
            actor_id=actor_id,
            represented_actor_id=represented_actor_id,
            support_grant_id=support_grant_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            outcome=outcome,
            reason=reason,
            before_patch=before_patch,
            after_patch=after_patch,
            session_id=session_id,
            correlation_id=correlation_id,
        )
    )

    if event_type is not None:
        session.add(
            OutboxEvent(
                tenant_id=tenant_id,
                aggregate_type=aggregate_type or (target_type or "unknown"),
                aggregate_id=aggregate_id or (target_id or "unknown"),
                event_type=event_type,
                payload=event_payload or {},
                correlation_id=correlation_id,
            )
        )
