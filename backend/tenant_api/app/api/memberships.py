"""Team membership management: invite, list, change role/status/scope.

The schema for this (`memberships`, `status: invited/active/suspended/revoked`,
`site_scope_mode: all/selected/none`) has existed since migration 0001; this is the first
API built against it. One permission, `membership.manage`, `tenant_owner`-only - this is
identity/access control, the thing every other permission is downstream of.

**All three site scopes are wired up and real.** `all`/`selected`/`none` are all accepted
by both `invite_member` and `update_membership`; `selected` writes real rows to
`membership_resource_scopes` (see `_replace_site_scope`). `selected` requires a non-empty
`site_ids` list - 422 `selected_scope_needs_sites` otherwise - and every id must be a real,
non-deleted site in this tenant - 422 `unknown_site` otherwise. Each write is a full
replace of that membership's site scope rows, not a diff, same as how `role_name`/`status`
are always full replacements in this same endpoint.

**What's still missing is the picker UI, not the API.** Nothing in the invite/edit dialog
yet lets an owner actually choose sites - that's a separate, later task in the same plan
(`docs/superpowers/plans/2026-09-17-licensing-rbac-reseller-features.md`, Feature A Task
6). Until that lands, a caller wanting `selected` scope has to pass `site_ids` directly
against the API.

**The zero-owners lockout guard**: nothing else in this schema stops a tenant revoking or
demoting its last active `tenant_owner`, which would lock the tenant out of its own
account permanently (nobody left with `membership.manage` to fix it). Checked before any
change that would cause it.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import current_tenant_context, db_session_for_tenant, get_app_settings
from app.repositories.identity import (
    count_active_owners,
    create_invited_membership,
    get_role_by_name,
    get_user_by_email,
)
from csense_shared.audit.outbox import record_audit_and_outbox
from csense_shared.config import Settings
from csense_shared.db.models import Membership
from csense_shared.errors import ApiError, ConflictError, NotFoundError
from csense_shared.notifications.bootstrap import build_registry
from csense_shared.notifications.providers import Message
from csense_shared.security.invitation_tickets import create_invitation_ticket
from csense_shared.security.permissions import require_permission
from csense_shared.security.tenant_context import TenantContext

router = APIRouter(prefix="/api/v1/tenant/memberships", tags=["memberships"])


class MembershipOut(BaseModel):
    id: str
    user_id: str
    email: str
    display_name: str
    role_name: str
    status: str
    site_scope_mode: str
    invited_at: str | None
    accepted_at: str | None


_SELECT = """
    SELECT m.id, m.user_id, u.email_display, u.display_name, r.name, m.status,
           m.site_scope_mode, m.invited_at, m.accepted_at
    FROM memberships m
    JOIN users u ON u.id = m.user_id
    JOIN roles r ON r.id = m.role_id
"""


def _to_out(row) -> MembershipOut:
    return MembershipOut(
        id=str(row[0]), user_id=str(row[1]), email=row[2], display_name=row[3],
        role_name=row[4], status=row[5], site_scope_mode=row[6],
        invited_at=row[7].isoformat() if row[7] else None,
        accepted_at=row[8].isoformat() if row[8] else None,
    )


@router.get("", response_model=list[MembershipOut])
async def list_memberships(
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[MembershipOut]:
    require_permission(context, "membership.manage")
    rows = (await db.execute(text(f"{_SELECT} ORDER BY m.created_at"))).all()
    return [_to_out(row) for row in rows]


class InviteIn(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=200)
    role_name: str = Field(pattern="^(tenant_owner|tenant_operator|tenant_member|tenant_viewer)$")
    site_scope_mode: str = Field(default="none", pattern="^(all|selected|none)$")
    site_ids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("site_ids")
    @classmethod
    def _selected_needs_ids_elsewhere(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        # Real cross-field validation (site_scope_mode == "selected" implies len > 0)
        # happens in invite_member itself, not here - Pydantic v2 field_validators don't
        # see sibling fields without a model_validator, and the real error needs to name
        # which sites don't belong to this tenant anyway (a DB check), which can't happen
        # in a pure field validator.
        return value


class InviteOut(MembershipOut):
    # Only ever set when the email could not actually be sent - see invite_member's own
    # reasoning. None (the common case, once Resend is configured) means: it already went
    # out, and this process is not holding onto the token any more.
    invitation_link: str | None = None


async def _replace_site_scope(
    db: AsyncSession, *, tenant_id: uuid.UUID, membership_id: uuid.UUID,
    site_scope_mode: str, site_ids: list[uuid.UUID],
) -> None:
    if site_scope_mode == "selected" and not site_ids:
        raise ApiError(
            status_code=422, code="selected_scope_needs_sites",
            message="Pick at least one site, or choose a different site-access option.",
        )
    if site_ids:
        found = (
            await db.execute(
                text("SELECT count(*) FROM sites WHERE id = ANY(:ids) AND deleted_at IS NULL"),
                {"ids": site_ids},
            )
        ).scalar_one()
        if found != len(set(site_ids)):
            raise ApiError(
                status_code=422, code="unknown_site",
                message="One or more selected sites don't exist in this tenant.",
            )

    # Full replace, not a diff - the picker UI always submits the complete desired set,
    # same as how role_name/status are always full replacements in this same endpoint.
    await db.execute(
        text(
            "DELETE FROM membership_resource_scopes "
            "WHERE membership_id = :mid AND resource_type = 'site'"
        ),
        {"mid": membership_id},
    )
    for site_id in set(site_ids):
        await db.execute(
            text(
                "INSERT INTO membership_resource_scopes "
                "(tenant_id, membership_id, resource_type, resource_id, effect) "
                "VALUES (:tid, :mid, 'site', :sid, 'allow')"
            ),
            {"tid": tenant_id, "mid": membership_id, "sid": site_id},
        )


@router.post("", response_model=InviteOut, status_code=201)
async def invite_member(
    body: InviteIn,
    request: Request,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
    settings: Settings = Depends(get_app_settings),
) -> InviteOut:
    require_permission(context, "membership.manage")

    existing_user = await get_user_by_email(db, body.email)
    if existing_user is not None:
        already = (
            await db.execute(
                text(
                    "SELECT 1 FROM memberships WHERE tenant_id = :t AND user_id = :u "
                    "AND status != 'revoked'"
                ),
                {"t": context.tenant_id, "u": existing_user.id},
            )
        ).first()
        if already is not None:
            raise ConflictError("This email already has a membership in this tenant.")

    role = await get_role_by_name(db, body.role_name, "customer")

    user, membership = await create_invited_membership(
        db, tenant_id=context.tenant_id, email=body.email, display_name=body.display_name,
        role=role, site_scope_mode=body.site_scope_mode, invited_by=context.user_id,
    )

    if body.site_scope_mode == "selected":
        await _replace_site_scope(
            db, tenant_id=context.tenant_id, membership_id=membership.id,
            site_scope_mode=body.site_scope_mode, site_ids=body.site_ids,
        )

    await record_audit_and_outbox(
        db,
        tenant_id=context.tenant_id,
        actor_type="user",
        actor_id=str(context.user_id),
        support_grant_id=context.support_grant_id,
        action="membership.invite",
        outcome="success",
        target_type="membership",
        target_id=str(membership.id),
        reason=f"Invited {body.email} as {body.role_name}",
        before_patch=None,
        after_patch={"email": body.email, "role_name": body.role_name, "status": "invited"},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="membership.invited.v1",
        event_payload={"membership_id": str(membership.id), "email": body.email, "role_name": body.role_name},
        aggregate_type="membership",
        aggregate_id=str(membership.id),
    )

    token = await create_invitation_ticket(
        request.app.state.redis, settings,
        membership_id=membership.id, tenant_id=context.tenant_id, user_id=user.id, email=body.email,
    )
    invitation_link = f"{_crm_origin(settings)}/accept-invitation?token={token}"

    sent = await _send_invitation_email(settings, email=body.email, link=invitation_link)

    row = (
        await db.execute(text(f"{_SELECT} WHERE m.id = :id"), {"id": membership.id})
    ).first()
    # invitation_link stays None once the email actually went out - only surfaced here
    # because there is no other way to reach the invited person otherwise, and the token
    # must not linger in this process (or a response body) past the point it has one.
    return InviteOut(**_to_out(row).model_dump(), invitation_link=None if sent else invitation_link)


def _crm_origin(settings: Settings) -> str:
    # First configured origin is the one a real person's browser actually loads (through
    # Traefik) - the second (localhost:5173) only ever matters for `npm run dev`.
    origins = settings.customer_crm_origins
    return origins[0] if origins else "http://app.localhost:8080"


async def _send_invitation_email(settings: Settings, *, email: str, link: str) -> bool:
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


class MembershipPatchIn(BaseModel):
    role_name: str | None = Field(
        default=None, pattern="^(tenant_owner|tenant_operator|tenant_member|tenant_viewer)$"
    )
    status: str | None = Field(default=None, pattern="^(active|suspended|revoked)$")
    site_scope_mode: str | None = Field(default=None, pattern="^(all|selected|none)$")
    site_ids: list[uuid.UUID] | None = None


@router.patch("/{membership_id}", response_model=MembershipOut)
async def update_membership(
    membership_id: uuid.UUID,
    body: MembershipPatchIn,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> MembershipOut:
    require_permission(context, "membership.manage")

    membership = await db.get(Membership, membership_id)
    if membership is None or membership.tenant_id != context.tenant_id:
        raise NotFoundError("No such membership.")

    owner_role = await get_role_by_name(db, "tenant_owner", "customer")
    is_currently_active_owner = membership.role_id == owner_role.id and membership.status == "active"
    would_demote = body.role_name is not None and body.role_name != "tenant_owner"
    would_deactivate = body.status is not None and body.status != "active"

    if is_currently_active_owner and (would_demote or would_deactivate):
        remaining = await count_active_owners(db, context.tenant_id)
        if remaining <= 1:
            raise ApiError(
                status_code=422,
                code="last_owner_cannot_be_removed",
                message=(
                    "This is the tenant's only active owner. Promote another member to "
                    "owner before changing or removing this one."
                ),
            )

    before = {"role_id": str(membership.role_id), "status": membership.status,
              "site_scope_mode": membership.site_scope_mode}

    if body.role_name is not None:
        role = await get_role_by_name(db, body.role_name, "customer")
        membership.role_id = role.id
    if body.status is not None:
        membership.status = body.status
    if body.site_scope_mode is not None:
        membership.site_scope_mode = body.site_scope_mode
        await _replace_site_scope(
            db, tenant_id=context.tenant_id, membership_id=membership.id,
            site_scope_mode=body.site_scope_mode, site_ids=body.site_ids or [],
        )
    await db.flush()

    await record_audit_and_outbox(
        db,
        tenant_id=context.tenant_id,
        actor_type="user",
        actor_id=str(context.user_id),
        support_grant_id=context.support_grant_id,
        action="membership.update",
        outcome="success",
        target_type="membership",
        target_id=str(membership_id),
        reason=f"role={body.role_name} status={body.status} scope={body.site_scope_mode}",
        before_patch=before,
        after_patch={"role_id": str(membership.role_id), "status": membership.status,
                     "site_scope_mode": membership.site_scope_mode},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="membership.updated.v1",
        event_payload={"membership_id": str(membership_id)},
        aggregate_type="membership",
        aggregate_id=str(membership_id),
    )

    row = (await db.execute(text(f"{_SELECT} WHERE m.id = :id"), {"id": membership_id})).first()
    return _to_out(row)
