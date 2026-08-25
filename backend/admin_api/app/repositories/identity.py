"""Platform identity repository — Admin API's own privileged-session-only queries.

Never reachable from Tenant API code paths (TRD §7.2) because it lives in a separate
service/deployment with its own dependency wiring; there is no shared import path from
tenant_api into admin_api or vice versa.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from csense_shared.db.models import (
    Permission,
    PlatformDeveloper,
    PlatformRoleAssignment,
    RolePermission,
    User,
)


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email_normalized == email.lower()))
    return result.scalar_one_or_none()


async def get_platform_developer(session: AsyncSession, user_id: UUID) -> PlatformDeveloper | None:
    result = await session.execute(
        select(PlatformDeveloper).where(PlatformDeveloper.user_id == user_id, PlatformDeveloper.status == "active")
    )
    return result.scalar_one_or_none()


async def get_platform_permissions(session: AsyncSession, platform_developer_id: UUID) -> frozenset[str]:
    result = await session.execute(
        select(Permission.code, RolePermission.effect)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(
            PlatformRoleAssignment,
            (PlatformRoleAssignment.role_id == RolePermission.role_id)
            & (PlatformRoleAssignment.status == "active"),
        )
        .where(PlatformRoleAssignment.platform_developer_id == platform_developer_id)
    )
    rows = result.all()
    allowed = {code for code, effect in rows if effect == "allow"}
    denied = {code for code, effect in rows if effect == "deny"}
    return frozenset(allowed - denied)
