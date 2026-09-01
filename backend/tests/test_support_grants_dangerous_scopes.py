"""A support grant's requested_scopes must never include a permission code that lets an
elevated, revocable session mint access that outlives the grant itself - a new member
(possibly an owner), a new API key, a new webhook endpoint, a new child tenant, or the
ability to revoke a different developer's own grant. Pure function, no DB needed.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from csense_shared.errors import ApiError


def _load_support_module():
    """Loads admin_api's `app/api/support.py` by path under a synthetic module name - the
    same technique `test_nvr_adapter.py`'s `_load_nvr_adapter()` and
    `test_notification_worker.py`'s `_load_worker()` use for the identical problem: every
    service in this repo names its package `app` (tenant_api, admin_api, ai_runtime,
    notification_worker), so a plain `from app.X import Y` risks silently resolving to
    whichever service's `app` a different test module already cached in `sys.modules`,
    depending on collection order.

    Unlike those two target modules, `support.py` itself does a real `from app.deps
    import ...` - an absolute import that needs an actual `app` package on `sys.path` to
    resolve while this exec runs, not just a loadable single file. So this saves whatever
    `app`/`app.*` entries already exist in `sys.modules` (e.g. ai_runtime's own `app`, if
    its tests happened to collect first), lets the real import machinery build admin_api's
    `app`/`app.deps` fresh for the duration of this load, then tears those back down and
    restores exactly what was there before - leaving `sys.modules` exactly as it found it
    (aside from this module's own synthetic name, same as the two examples above), so the
    result is independent of collection order in either direction.
    """
    admin_api_root = Path(__file__).resolve().parents[1] / "admin_api"

    saved = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if name == "app" or name.startswith("app.")
    }
    path_inserted = str(admin_api_root) not in sys.path
    if path_inserted:
        sys.path.insert(0, str(admin_api_root))

    try:
        spec = importlib.util.spec_from_file_location(
            "csense_admin_support", admin_api_root / "app" / "api" / "support.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        if path_inserted:
            sys.path.remove(str(admin_api_root))
        sys.modules.update(saved)


_support = _load_support_module()
DANGEROUS_SUPPORT_SCOPES = _support.DANGEROUS_SUPPORT_SCOPES
reject_dangerous_scopes = _support.reject_dangerous_scopes


def test_an_ordinary_scope_is_accepted():
    reject_dangerous_scopes(["incident.read", "camera.read"])  # does not raise


def test_membership_manage_is_refused():
    with pytest.raises(ApiError) as exc:
        reject_dangerous_scopes(["incident.read", "membership.manage"])
    assert exc.value.status_code == 422
    assert "membership.manage" in exc.value.message


def test_api_client_manage_is_refused():
    with pytest.raises(ApiError):
        reject_dangerous_scopes(["api_client.manage"])


def test_webhook_manage_is_refused():
    with pytest.raises(ApiError):
        reject_dangerous_scopes(["webhook.manage"])


def test_reseller_manage_children_is_refused():
    with pytest.raises(ApiError):
        reject_dangerous_scopes(["reseller.manage_children"])


def test_support_revoke_is_refused():
    # A support session must never be able to touch grant governance itself - including
    # ending a different platform developer's own active session on the same tenant.
    with pytest.raises(ApiError):
        reject_dangerous_scopes(["support.revoke"])


def test_the_dangerous_set_is_exactly_these_five_codes():
    # Names the set explicitly so a future addition to it is a deliberate code review
    # decision, not a silent accident of whatever happened to be checked in this test.
    assert DANGEROUS_SUPPORT_SCOPES == {
        "membership.manage", "api_client.manage", "webhook.manage",
        "reseller.manage_children", "support.revoke",
    }
