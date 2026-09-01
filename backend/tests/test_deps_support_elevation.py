"""current_tenant_context()'s platform-token elevation path - the wiring on top of
elevate_from_grant() (test_support_grant_elevation.py already covers that function
directly). This file only exercises the header/token dispatch logic itself, with a fake
DB-call stand-in, so it does not need a real database.
"""
from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from csense_shared.errors import AuthenticationError
from csense_shared.security.support_elevation import ElevatedGrant
from csense_shared.security.tokens import AUDIENCE_CUSTOMER, AUDIENCE_PLATFORM, issue_access_token


def _load_deps():
    """Loads app/deps.py by path, not `from app.deps import ...`.

    Every service under backend/ names its FastAPI app package `app` (tenant_api,
    ai_runtime, notification_worker, ...) - a plain import resolves to whichever service's
    `app` happened to be cached first by another test module collected earlier in the same
    run, silently exercising the wrong code. Same fix test_nvr_adapter.py /
    test_notification_worker.py already use for the identical problem. deps.py itself only
    imports from fastapi/sqlalchemy/csense_shared (no sibling `app.*` modules), so loading
    it standalone this way is safe.
    """
    path = Path(__file__).resolve().parents[1] / "tenant_api" / "app" / "deps.py"
    spec = importlib.util.spec_from_file_location("csense_tenant_deps", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


deps = _load_deps()
current_tenant_context = deps.current_tenant_context


class _FakeRequest:
    def __init__(self, session_factory=None):
        self.state = type("State", (), {"correlation_id": "11111111-1111-1111-1111-111111111111"})()
        self.app = type("App", (), {"state": type("AppState", (), {"session_factory": session_factory})()})()


class _FakeAsyncSession:
    """Just enough shape for bootstrap_session() (real code, not mocked) to open and
    close a transaction around the call to the mocked elevate_from_grant below - the
    mock never actually touches this object, so it doesn't need to look like a real
    AsyncSession beyond supporting `async with ...` and `.begin()`."""

    def begin(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeSessionFactory:
    """A plain callable instance, not a bare function - `_FakeRequest` stores this as a
    class attribute on a `type(...)`-built stand-in, and a plain function assigned that
    way binds as a method (an implicit `self` argument) when read off the instance."""

    def __call__(self) -> _FakeAsyncSession:
        return _FakeAsyncSession()


_fake_session_factory = _FakeSessionFactory()


@pytest.mark.asyncio
async def test_platform_token_without_header_is_rejected(settings):
    token = issue_access_token(
        settings=settings, user_id=uuid.uuid4(), audience=AUDIENCE_PLATFORM,
        tenant_id=None, membership_id=None, permissions=frozenset(), session_id="s1",
    )
    with pytest.raises(AuthenticationError):
        await current_tenant_context(
            request=_FakeRequest(), authorization=f"Bearer {token}",
            x_support_tenant_id=None, settings=settings,
        )


@pytest.mark.asyncio
async def test_platform_token_with_header_but_no_active_grant_is_rejected(settings):
    developer_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    token = issue_access_token(
        settings=settings, user_id=developer_id, audience=AUDIENCE_PLATFORM,
        tenant_id=None, membership_id=None, permissions=frozenset(), session_id="s1",
    )
    with patch.object(deps, "elevate_from_grant", new=AsyncMock(return_value=None)):
        with pytest.raises(AuthenticationError):
            await current_tenant_context(
                request=_FakeRequest(session_factory=_fake_session_factory),
                authorization=f"Bearer {token}",
                x_support_tenant_id=str(tenant_id),
                settings=settings,
            )


@pytest.mark.asyncio
async def test_platform_token_with_active_grant_builds_elevated_context(settings):
    developer_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    grant_id = uuid.uuid4()
    token = issue_access_token(
        settings=settings, user_id=developer_id, audience=AUDIENCE_PLATFORM,
        tenant_id=None, membership_id=None, permissions=frozenset({"support.request"}), session_id="s1",
    )
    fake_grant = ElevatedGrant(grant_id=grant_id, permissions=frozenset({"incident.read"}))
    with patch.object(deps, "elevate_from_grant", new=AsyncMock(return_value=fake_grant)):
        context = await current_tenant_context(
            request=_FakeRequest(session_factory=_fake_session_factory),
            authorization=f"Bearer {token}",
            x_support_tenant_id=str(tenant_id),
            settings=settings,
        )
    assert context.tenant_id == tenant_id
    assert context.user_id == developer_id
    assert context.membership_id is None
    assert context.support_grant_id == grant_id
    # Elevated permissions come from the grant, not the developer's own platform token.
    assert context.permissions == frozenset({"incident.read"})
    assert "support.request" not in context.permissions


@pytest.mark.asyncio
async def test_ordinary_customer_token_path_is_unaffected(settings):
    tenant_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    token = issue_access_token(
        settings=settings, user_id=uuid.uuid4(), audience=AUDIENCE_CUSTOMER,
        tenant_id=tenant_id, membership_id=membership_id,
        permissions=frozenset({"incident.read"}), session_id="s1",
    )
    context = await current_tenant_context(
        request=_FakeRequest(), authorization=f"Bearer {token}",
        x_support_tenant_id=None, settings=settings,
    )
    assert context.tenant_id == tenant_id
    assert context.membership_id == membership_id
    assert context.support_grant_id is None
