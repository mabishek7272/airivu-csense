"""Tenant-scoped pipeline catalogue browsing: `GET /api/v1/tenant/pipelines/assignable`.

Added 2026-09-08 alongside the Customer CRM pipeline-assignment page - see
`backend/tenant_api/app/api/pipeline_assignments.py`'s own module docstring for why a
tenant now gets a narrow read of `pipeline_versions` where the docstring used to say
"never". This file proves, against a live database rather than by inspection, the two
things that decision actually rests on: only `published` versions are ever returned (a
`draft` or `deprecated` sibling of the very same pipeline must never leak through), and
the permission gate is real - an unauthenticated call and a wrongly-permissioned one are
both rejected by the real route, not merely documented as required.

Same two-DSN discipline as `test_edge_diagnostics.py`'s own module docstring:
`TEST_POSTGRES_DSN` (`csense_app`, BYPASSRLS) sets up/tears down fixture rows only; the
ASGI app under test is mounted on `TEST_POSTGRES_API_DSN` (`csense_api`), the real role a
production tenant-api container connects as.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import sys
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from csense_shared.errors import ApiError, api_error_handler
from csense_shared.security.tokens import AUDIENCE_CUSTOMER, issue_access_token

REQUIRED_DSNS = ("TEST_POSTGRES_DSN", "TEST_POSTGRES_API_DSN")

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(name) for name in REQUIRED_DSNS),
    reason=f"integration DSNs not set ({', '.join(REQUIRED_DSNS)}) - skipping",
)


def _load_pipeline_assignments_module():
    """Loads tenant_api's `app.api.pipeline_assignments` by path - same fix, same
    reasoning, as `test_edge_diagnostics.py`'s own loader: every service under backend/
    names its package `app`, so a plain import would resolve to whichever service another
    test module happened to import first."""
    service_dir = pathlib.Path(__file__).resolve().parents[1] / "tenant_api"
    saved = {n: m for n, m in sys.modules.items() if n == "app" or n.startswith("app.")}
    for name in list(saved):
        del sys.modules[name]
    sys.path.insert(0, str(service_dir))
    try:
        spec = importlib.util.spec_from_file_location(
            "csense_tenant_pipeline_assignments_under_test",
            service_dir / "app" / "api" / "pipeline_assignments.py",
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(service_dir))
        for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
            del sys.modules[name]
        sys.modules.update(saved)


pipeline_assignments = _load_pipeline_assignments_module()
# Same trick test_edge_diagnostics.py's own module docstring explains: grab the exact
# `get_app_settings` callable `current_tenant_context` resolves its `Settings` through,
# off that dependency's own `__globals__`, so `dependency_overrides` can target it by
# identity after `_load_pipeline_assignments_module` has already dropped `app.deps` from
# `sys.modules`.
_get_app_settings = pipeline_assignments.current_tenant_context.__globals__["get_app_settings"]


def _async_dsn(env_var: str) -> str:
    parts = dict(p.split("=", 1) for p in os.environ[env_var].split())
    return (
        f"postgresql+asyncpg://{parts['user']}:{parts['password']}"
        f"@{parts['host']}:{parts.get('port', '5432')}/{parts['dbname']}"
    )


async def _make_version(session, *, state: str, suffix: str) -> dict:
    """A throwaway pipeline + version, created as `draft` (the only state creation
    supports) then moved to `state` with a plain UPDATE if it isn't - the same two-step
    path `test_pipeline_registry.py`'s own `tenant_with_camera` fixture uses, since the
    immutability trigger (migration 0031) only ever guards `definition_json`/its digest,
    never the state column."""
    definition = {"stages": [{"type": "infer", "model_name": f"test-model-{suffix}", "min_model_state": "validated"}]}
    digest = hashlib.sha256(json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    pipeline_id = (
        await session.execute(
            text(
                "INSERT INTO pipelines (code, name, use_case, description) "
                "VALUES (:c, :n, 'test', :d) RETURNING id"
            ),
            {"c": f"assignable-test-{suffix}", "n": f"Assignable Test {suffix}", "d": f"desc-{suffix}"},
        )
    ).scalar_one()
    version_id = (
        await session.execute(
            text(
                "INSERT INTO pipeline_versions "
                "(pipeline_id, version_number, definition_json, definition_sha256, allowed_overrides_schema) "
                "VALUES (:p, 1, :def, :digest, :schema) RETURNING id"
            ),
            {
                "p": pipeline_id, "def": json.dumps(definition), "digest": digest,
                "schema": json.dumps({"min_confidence": "number"}),
            },
        )
    ).scalar_one()
    if state != "draft":
        await session.execute(
            text("UPDATE pipeline_versions SET state = :state WHERE id = :id"),
            {"state": state, "id": version_id},
        )
    return {"pipeline_id": pipeline_id, "version_id": version_id, "code": f"assignable-test-{suffix}"}


@asynccontextmanager
async def _assignable_context(settings):
    owner_engine = create_async_engine(_async_dsn("TEST_POSTGRES_DSN"))
    owner_factory = async_sessionmaker(owner_engine, expire_on_commit=False)
    api_engine = create_async_engine(_async_dsn("TEST_POSTGRES_API_DSN"))
    api_factory = async_sessionmaker(api_engine, expire_on_commit=False)

    suffix = uuid.uuid4().hex[:8]
    async with owner_factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        org_id = (
            await session.execute(
                text(
                    "INSERT INTO organizations (organization_type, legal_name, display_name, slug, status) "
                    "VALUES ('direct_customer', :n, :n, :s, 'active') RETURNING id"
                ),
                {"n": f"Assignable Test {suffix}", "s": f"assignable-test-{suffix}"},
            )
        ).scalar_one()
        tenant_id = (
            await session.execute(
                text("INSERT INTO tenants (organization_id, status) VALUES (:o, 'active') RETURNING id"),
                {"o": org_id},
            )
        ).scalar_one()

        published = await _make_version(session, state="published", suffix=f"pub-{suffix}")
        draft = await _make_version(session, state="draft", suffix=f"draft-{suffix}")
        deprecated = await _make_version(session, state="deprecated", suffix=f"dep-{suffix}")

    api = FastAPI()
    api.include_router(pipeline_assignments.router)
    api.add_exception_handler(ApiError, api_error_handler)
    api.state.session_factory = api_factory
    api.dependency_overrides[_get_app_settings] = lambda: settings

    transport = httpx.ASGITransport(app=api)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://tenant.test") as client:
            yield {
                "client": client, "tenant_id": tenant_id,
                "published": published, "draft": draft, "deprecated": deprecated,
            }
    finally:
        async with owner_factory() as session, session.begin():
            await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
            for version in (published, draft, deprecated):
                await session.execute(
                    text("DELETE FROM pipeline_versions WHERE id = :id"), {"id": version["version_id"]}
                )
                await session.execute(
                    text("DELETE FROM pipelines WHERE id = :id"), {"id": version["pipeline_id"]}
                )
            await session.execute(text("DELETE FROM tenants WHERE organization_id = :o"), {"o": org_id})
            await session.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": org_id})
        await owner_engine.dispose()
        await api_engine.dispose()


@pytest_asyncio.fixture()
async def ctx(settings):
    async with _assignable_context(settings) as value:
        yield value


def _headers(settings, *, tenant_id, permissions) -> dict:
    token = issue_access_token(
        settings=settings, user_id=uuid.uuid4(), audience=AUDIENCE_CUSTOMER,
        tenant_id=tenant_id, membership_id=uuid.uuid4(),
        permissions=frozenset(permissions), session_id=uuid.uuid4().hex,
    )
    return {"Authorization": f"Bearer {token}"}


async def _get_assignable(ctx, headers):
    return await ctx["client"].get("/api/v1/tenant/pipelines/assignable", headers=headers)


# --- The one property this endpoint exists to guarantee ---------------------------------

async def test_only_the_published_version_is_returned(ctx, settings):
    response = await _get_assignable(
        ctx, _headers(settings, tenant_id=ctx["tenant_id"], permissions={"pipeline.assign"})
    )

    assert response.status_code == 200, response.text
    ids = {row["pipeline_version_id"] for row in response.json()}
    assert str(ctx["published"]["version_id"]) in ids
    assert str(ctx["draft"]["version_id"]) not in ids, "a draft version must never be assignable"
    assert str(ctx["deprecated"]["version_id"]) not in ids, "a deprecated version must never be assignable"


async def test_the_shape_is_the_narrow_tenant_facing_one(ctx, settings):
    """Fewer fields than the admin catalogue's `PipelineVersionOut` on purpose - see the
    module docstring. This pins the shape, not just the filter."""
    response = await _get_assignable(
        ctx, _headers(settings, tenant_id=ctx["tenant_id"], permissions={"pipeline.assign"})
    )

    row = next(r for r in response.json() if r["pipeline_version_id"] == str(ctx["published"]["version_id"]))
    assert row["pipeline_code"] == ctx["published"]["code"]
    assert row["use_case"] == "test"
    assert row["allowed_overrides_schema"] == {"min_confidence": "number"}
    # Fields the admin-only shape carries that a tenant has no business seeing here.
    assert "owner_team" not in row
    assert "pipeline_status" not in row
    assert "definition_json" not in row


# --- The permission gate, proven against the real route, not documented and trusted -----

async def test_an_unauthenticated_call_is_rejected(ctx):
    response = await _get_assignable(ctx, headers={})
    assert response.status_code == 401


async def test_a_token_without_pipeline_assign_is_rejected(ctx, settings):
    response = await _get_assignable(
        ctx, _headers(settings, tenant_id=ctx["tenant_id"], permissions={"camera.read"})
    )
    assert response.status_code == 403
