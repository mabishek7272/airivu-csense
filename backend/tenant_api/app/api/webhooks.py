"""Webhook endpoints: create/list/update/rotate-secret/delete, plus a real signed
test-delivery (SCH §10.6/§10.7, CHECKLIST: "Webhook signing, verification, replay
protection"). Both the destination URL and the signing secret are write-only, the same
discipline `cameras.py`'s own credential handling already established - once set,
neither is ever returned by any endpoint again; `url_host_display` (just the hostname, no
path/query) is the only thing a list view shows back, enough to recognize which endpoint
is which without re-exposing whatever the full URL might embed.

**Delivery, scoped honestly**: this pass ships a real, on-demand test delivery
(`POST /{id}/test`) that signs a real payload and sends a real HTTP POST to the
configured URL, through the same SSRF guard camera probing already uses - not yet
automatic delivery driven by the outbox for every domain event (a systemic wiring effort
across every event producer, deserving its own pass). The signing/verification mechanism
itself (`csense_shared.security.webhooks`) is exactly what automatic delivery would use
once built; nothing here is a stub that would need replacing.
"""
from __future__ import annotations

import datetime as dt
import json
import secrets
import time
import uuid

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant, get_app_settings
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.config import Settings
from csense_shared.errors import ApiError, NotFoundError
from csense_shared.security.envelope import EnvelopeError, keyring_from_settings
from csense_shared.security.outbound import BlockedAddressError, resolve_public_endpoint
from csense_shared.security.permissions import require_permission
from csense_shared.security.secret_store import delete_secret, read_secret, write_secret
from csense_shared.security.tenant_context import TenantContext
from csense_shared.security.webhooks import SIGNING_SECRET_PURPOSE, URL_SECRET_PURPOSE, sign_payload

router = APIRouter(prefix="/api/v1/tenant/webhooks", tags=["webhooks"])

TEST_REQUEST_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class WebhookEndpointOut(BaseModel):
    id: str
    name: str
    url_host_display: str
    event_filters: list[str]
    status: str
    created_at: str


_SELECT = """
    SELECT id, name, url_host_display, event_filters, status::text, created_at
    FROM webhook_endpoints
"""


def _to_out(row) -> WebhookEndpointOut:
    return WebhookEndpointOut(
        id=str(row[0]), name=row[1], url_host_display=row[2],
        event_filters=list(row[3] or []), status=row[4], created_at=row[5].isoformat(),
    )


@router.get("", response_model=list[WebhookEndpointOut])
async def list_webhooks(
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[WebhookEndpointOut]:
    require_permission(context, "webhook.manage")
    rows = (await db.execute(text(f"{_SELECT} ORDER BY created_at DESC"))).all()
    return [_to_out(r) for r in rows]


class CreateWebhookIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)
    event_filters: list[str] = Field(default_factory=list)  # empty = all events


class CreateWebhookOut(WebhookEndpointOut):
    # Shown once, at creation - never again, the same discipline every other secret in
    # this codebase already follows.
    signing_secret: str


def _validate_and_split_url(url: str) -> tuple[str, int, str]:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ApiError(
            status_code=422, code="invalid_webhook_url",
            message="Webhook URLs must be https:// with a real hostname.",
        )
    port = parsed.port or 443
    return parsed.hostname, port, url


@router.post("", response_model=CreateWebhookOut, status_code=201)
async def create_webhook(
    body: CreateWebhookIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
    settings: Settings = Depends(get_app_settings),
) -> CreateWebhookOut:
    require_permission(context, "webhook.manage")

    hostname, port, url = _validate_and_split_url(body.url)
    try:
        resolve_public_endpoint(hostname, port)
    except BlockedAddressError as exc:
        raise ApiError(status_code=422, code="address_not_permitted", message=str(exc)) from exc

    keyring = keyring_from_settings(settings)
    signing_secret = f"whsec_{secrets.token_urlsafe(32)}"
    try:
        url_secret_id = await write_secret(
            db, keyring, tenant_id=context.tenant_id, purpose=URL_SECRET_PURPOSE, plaintext=url, label=body.name,
        )
        signing_secret_id = await write_secret(
            db, keyring, tenant_id=context.tenant_id, purpose=SIGNING_SECRET_PURPOSE,
            plaintext=signing_secret, label=body.name,
        )
    except EnvelopeError as exc:
        raise ApiError(status_code=500, code="secret_store_failed", message=str(exc)) from exc

    new_id = (
        await db.execute(
            text(
                "INSERT INTO webhook_endpoints "
                "(tenant_id, name, url_secret_id, url_host_display, signing_secret_id, "
                " event_filters, created_by) "
                "VALUES (:tenant_id, :name, :url_secret_id, :host, :signing_secret_id, "
                " :event_filters, :created_by) "
                "RETURNING id"
            ),
            {
                "tenant_id": context.tenant_id, "name": body.name, "url_secret_id": url_secret_id,
                "host": hostname, "signing_secret_id": signing_secret_id,
                "event_filters": body.event_filters, "created_by": context.user_id,
            },
        )
    ).scalar_one()
    row = (await db.execute(text(f"{_SELECT} WHERE id = :id"), {"id": new_id})).first()

    await record_audit_and_outbox(
        db,
        tenant_id=context.tenant_id,
        actor_type="user",
        actor_id=str(context.user_id),
        support_grant_id=context.support_grant_id,
        action="webhook.create",
        outcome="success",
        target_type="webhook_endpoint",
        target_id=str(row[0]),
        reason=f"Created webhook '{body.name}' -> {hostname}",
        before_patch=None,
        after_patch={"name": body.name, "url_host": hostname, "event_filters": body.event_filters},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="webhook.created.v1",
        event_payload={"webhook_endpoint_id": str(row[0])},
        aggregate_type="webhook_endpoint",
        aggregate_id=str(row[0]),
    )

    out = _to_out(row)
    return CreateWebhookOut(**out.model_dump(), signing_secret=signing_secret)


class PatchWebhookIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    event_filters: list[str] | None = None
    status: str | None = Field(default=None, pattern="^(active|disabled)$")


@router.patch("/{webhook_id}", response_model=WebhookEndpointOut)
async def update_webhook(
    webhook_id: uuid.UUID,
    body: PatchWebhookIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> WebhookEndpointOut:
    """Name/event filters/status only - never the URL or secret. Changing the
    destination is a new endpoint, not an edit of this one; rotating the secret has its
    own dedicated endpoint below, since it needs to hand back the new value once."""
    require_permission(context, "webhook.manage")

    changes = body.model_dump(exclude_unset=True)
    if not changes:
        existing = (await db.execute(text(f"{_SELECT} WHERE id = :id"), {"id": webhook_id})).first()
        if existing is None:
            raise NotFoundError("No such webhook.")
        return _to_out(existing)

    assignments = ", ".join(f"{field} = :{field}" for field in changes)
    result = (
        await db.execute(
            text(f"UPDATE webhook_endpoints SET {assignments}, updated_at = now() WHERE id = :id RETURNING id"),
            {**changes, "id": webhook_id},
        )
    ).first()
    if result is None:
        raise NotFoundError("No such webhook.")

    row = (await db.execute(text(f"{_SELECT} WHERE id = :id"), {"id": webhook_id})).first()
    return _to_out(row)


@router.delete("/{webhook_id}", status_code=204)
async def delete_webhook(
    webhook_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> None:
    require_permission(context, "webhook.manage")

    row = (
        await db.execute(
            text("SELECT url_secret_id, signing_secret_id FROM webhook_endpoints WHERE id = :id"),
            {"id": webhook_id},
        )
    ).first()
    if row is None:
        raise NotFoundError("No such webhook.")

    await db.execute(text("DELETE FROM webhook_endpoints WHERE id = :id"), {"id": webhook_id})
    await delete_secret(db, secret_id=row[0])
    await delete_secret(db, secret_id=row[1])


class RotateSecretOut(BaseModel):
    signing_secret: str


@router.post("/{webhook_id}/rotate-secret", response_model=RotateSecretOut)
async def rotate_webhook_secret(
    webhook_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
    settings: Settings = Depends(get_app_settings),
) -> RotateSecretOut:
    require_permission(context, "webhook.manage")

    row = (
        await db.execute(
            text("SELECT name, signing_secret_id FROM webhook_endpoints WHERE id = :id"), {"id": webhook_id}
        )
    ).first()
    if row is None:
        raise NotFoundError("No such webhook.")
    name, old_secret_id = row

    keyring = keyring_from_settings(settings)
    new_secret = f"whsec_{secrets.token_urlsafe(32)}"
    new_secret_id = await write_secret(
        db, keyring, tenant_id=context.tenant_id, purpose=SIGNING_SECRET_PURPOSE, plaintext=new_secret, label=name,
    )
    await db.execute(
        text("UPDATE webhook_endpoints SET signing_secret_id = :id, updated_at = now() WHERE id = :webhook_id"),
        {"id": new_secret_id, "webhook_id": webhook_id},
    )
    await delete_secret(db, secret_id=old_secret_id)

    await record_audit_and_outbox(
        db,
        tenant_id=context.tenant_id,
        actor_type="user",
        actor_id=str(context.user_id),
        support_grant_id=context.support_grant_id,
        action="webhook.rotate_secret",
        outcome="success",
        target_type="webhook_endpoint",
        target_id=str(webhook_id),
        reason="Signing secret rotated",
        before_patch=None,
        after_patch=None,
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="webhook.secret_rotated.v1",
        event_payload={"webhook_endpoint_id": str(webhook_id)},
        aggregate_type="webhook_endpoint",
        aggregate_id=str(webhook_id),
    )

    return RotateSecretOut(signing_secret=new_secret)


class TestDeliveryOut(BaseModel):
    delivered: bool
    response_status: int | None
    response_time_ms: int | None
    error: str | None


@router.post("/{webhook_id}/test", response_model=TestDeliveryOut)
async def test_webhook(
    webhook_id: uuid.UUID,
    request: Request,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
    settings: Settings = Depends(get_app_settings),
) -> TestDeliveryOut:
    """A real signed HTTP POST to the configured URL - not a dry run. The SSRF check
    runs fresh here too, not just at creation time: a public DNS record can be
    repointed at a private address after an endpoint was created, and this is the
    moment a real outbound connection is actually about to happen."""
    require_permission(context, "webhook.manage")

    row = (
        await db.execute(
            text("SELECT url_secret_id, signing_secret_id FROM webhook_endpoints WHERE id = :id"),
            {"id": webhook_id},
        )
    ).first()
    if row is None:
        raise NotFoundError("No such webhook.")
    url_secret_id, signing_secret_id = row

    keyring = keyring_from_settings(settings)
    url = (
        await read_secret(db, keyring, secret_id=url_secret_id, tenant_id=context.tenant_id, purpose=URL_SECRET_PURPOSE)
    ).decode()
    signing_secret = (
        await read_secret(
            db, keyring, secret_id=signing_secret_id, tenant_id=context.tenant_id, purpose=SIGNING_SECRET_PURPOSE,
        )
    ).decode()

    hostname, port, _ = _validate_and_split_url(url)
    try:
        # Confirms the destination is still a public address right before connecting -
        # see this endpoint's own docstring for why a fresh check matters here.
        resolve_public_endpoint(hostname, port)
    except BlockedAddressError as exc:
        raise ApiError(status_code=422, code="address_not_permitted", message=str(exc)) from exc

    payload = {
        "event": "webhook.test",
        "webhook_endpoint_id": str(webhook_id),
        "tenant_id": str(context.tenant_id),
        "sent_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    body_bytes = json.dumps(payload).encode()
    signature = sign_payload(signing_secret, body_bytes)

    delivery_id = uuid.uuid4()
    delivered = False
    response_status: int | None = None
    response_time_ms: int | None = None
    error: str | None = None
    started = time.monotonic()

    try:
        async with httpx.AsyncClient(timeout=TEST_REQUEST_TIMEOUT) as client:
            response = await client.post(
                url,
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-CSense-Signature": signature,
                    "X-CSense-Delivery-Id": str(delivery_id),
                    "X-CSense-Event": "webhook.test",
                },
            )
        response_time_ms = int((time.monotonic() - started) * 1000)
        response_status = response.status_code
        delivered = 200 <= response.status_code < 300
    except httpx.HTTPError as exc:
        response_time_ms = int((time.monotonic() - started) * 1000)
        error = str(exc)[:500]

    await db.execute(
        text(
            "INSERT INTO webhook_deliveries "
            "(id, tenant_id, webhook_endpoint_id, event_type, payload, status, sent_at, "
            " response_status, response_time_ms, failure_summary_redacted) "
            "VALUES (:id, :tenant_id, :webhook_id, 'webhook.test', CAST(:payload AS jsonb), "
            " :status, now(), :response_status, :response_time_ms, :error)"
        ),
        {
            "id": delivery_id, "tenant_id": context.tenant_id, "webhook_id": webhook_id,
            "payload": json.dumps(payload),
            "status": "succeeded" if delivered else "failed",
            "response_status": response_status, "response_time_ms": response_time_ms, "error": error,
        },
    )

    return TestDeliveryOut(
        delivered=delivered, response_status=response_status, response_time_ms=response_time_ms, error=error,
    )
