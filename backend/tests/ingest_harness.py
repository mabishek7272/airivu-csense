"""Shared setup for the two ingest endpoint test modules.

`test_ingest_single.py` and `test_ingest_batch.py` exercise two routes that now share
their whole body (`_ingest_one`, `tenant_scoped_session`), so they need the identical
fixture: a real tenant with a camera and an intrusion rule, a real device credential, and
the router mounted on a real ASGI app. Duplicating that in both files would let the two
drift apart, which is exactly the failure the single-endpoint tests exist to catch.

Not a `conftest.py` addition: `ctx` is far too generic a fixture name to put in the
namespace of every test in the suite (`test_pipeline_ingest.py` already has its own,
differently shaped one), and the module-loading below should not run for test sessions
that never touch ingestion. Each module declares its own one-line fixture over
`ingest_context()` instead.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import os
import pathlib
import sys
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from csense_shared.errors import ApiError, api_error_handler

# 02:00 UTC is 07:30 in Kolkata; the fixture's site is in Kolkata so a dropped timezone
# conversion would change rule scheduling rather than passing by luck (same choice, and
# the same reason, as test_pipeline_ingest.py).
CAPTURED_AT = dt.datetime(2026, 8, 27, 2, 0, tzinfo=dt.UTC)
INSIDE_ZONE = [0.44, 0.36, 0.55, 0.92]
OUTSIDE_ZONE = [0.02, 0.36, 0.12, 0.92]
ZONE_POLYGON = '{"polygon":[[0.35,0.30],[1.0,0.30],[1.0,1.0],[0.35,1.0]]}'


def _load_ingest_module():
    """Loads tenant_api's `app.api.ingest` by path.

    Every service under backend/ names its package `app`, so a plain import resolves to
    whichever service another test module happened to import first - `test_ai_runtime_*`
    is collected before either ingest module and leaves ai_runtime's `app` cached. Same
    fix, and the same reasoning, as `test_worker_loop_isolation.py`: save and clear the
    cached `app*` modules, put this service's own directory on `sys.path` for the load
    (ingest.py really does import `app.deps_agent`, so that has to resolve), then put
    everything back.
    """
    service_dir = pathlib.Path(__file__).resolve().parents[1] / "tenant_api"
    saved = {n: m for n, m in sys.modules.items() if n == "app" or n.startswith("app.")}
    for name in list(saved):
        del sys.modules[name]
    sys.path.insert(0, str(service_dir))
    try:
        path = service_dir / "app" / "api" / "ingest.py"
        spec = importlib.util.spec_from_file_location("csense_tenant_ingest_under_test", path)
        module = importlib.util.module_from_spec(spec)
        # Registered before execution, and left registered: `@dataclasses.dataclass` looks
        # its own class's module up in `sys.modules` while the class body is being
        # processed, so a module executing outside it cannot define one. The name is
        # unique to these tests, so it collides with nothing.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(service_dir))
        for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
            del sys.modules[name]
        sys.modules.update(saved)


ingest_module = _load_ingest_module()


def async_dsn() -> str:
    parts = dict(p.split("=", 1) for p in os.environ["TEST_POSTGRES_DSN"].split())
    return (
        f"postgresql+asyncpg://{parts['user']}:{parts['password']}"
        f"@{parts['host']}:{parts.get('port', '5432')}/{parts['dbname']}"
    )


@asynccontextmanager
async def ingest_context():
    """A tenant with a site, a camera, an intrusion rule, one recipient, and a real
    enrolled device credential - so the tests authenticate the way a device actually does,
    through `edge_agent_lookup`, rather than through an overridden dependency that would
    prove nothing about the auth wiring."""
    engine = create_async_engine(async_dsn())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:8]
    agent_token = f"bt{suffix}{uuid.uuid4().hex}"

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        org_id = (await session.execute(
            text("INSERT INTO organizations (organization_type, legal_name, display_name, "
                 "slug, status) VALUES ('direct_customer', :n, :n, :s, 'active') RETURNING id"),
            {"n": f"Ingest Route Test {suffix}", "s": f"ingest-route-{suffix}"},
        )).scalar_one()
        tenant_id = (await session.execute(
            text("INSERT INTO tenants (organization_id, status) VALUES (:o,'active') RETURNING id"),
            {"o": org_id},
        )).scalar_one()
        site_id = (await session.execute(
            text("INSERT INTO sites (tenant_id, name, code, timezone) "
                 "VALUES (:t,'Depot',:c,'Asia/Kolkata') RETURNING id"),
            {"t": tenant_id, "c": f"site-{suffix}"},
        )).scalar_one()
        camera_id = (await session.execute(
            text("INSERT INTO cameras (tenant_id, site_id, name, code, status) "
                 "VALUES (:t,:s,'Bay 2',:c,'ready') RETURNING id"),
            {"t": tenant_id, "s": site_id, "c": f"cam-{suffix}"},
        )).scalar_one()
        zone_id = (await session.execute(
            text("INSERT INTO zones (tenant_id, site_id, name, zone_type, geometry_json) "
                 "VALUES (:t,:s,'Dock','restricted', CAST(:g AS jsonb)) RETURNING id"),
            {"t": tenant_id, "s": site_id, "g": ZONE_POLYGON},
        )).scalar_one()
        group_id = (await session.execute(
            text("INSERT INTO recipient_groups (tenant_id, name) VALUES (:t,'On call') RETURNING id"),
            {"t": tenant_id},
        )).scalar_one()
        await session.execute(
            text("INSERT INTO recipient_group_members (tenant_id, recipient_group_id, "
                 "display_name, email, channels) "
                 "VALUES (:t,:g,'Guard','guard@example.com', CAST('[\"email\"]' AS jsonb))"),
            {"t": tenant_id, "g": group_id},
        )
        await session.execute(
            text("INSERT INTO detection_rules (tenant_id, site_id, camera_id, zone_id, name, "
                 "type_code, alertable_classes, min_confidence, severity, min_roi_overlap) "
                 "VALUES (:t,:s,:c,:z,'No entry','zone.intrusion', CAST('[\"person\"]' AS jsonb), "
                 "0.4,'high',0.3)"),
            {"t": tenant_id, "s": site_id, "c": camera_id, "z": zone_id},
        )
        device_id = (await session.execute(
            text("INSERT INTO edge_devices (tenant_id, site_id, name, status, "
                 "agent_token_prefix, agent_token_hash, agent_token_issued_at) "
                 "VALUES (:t,:s,'Gateway','enrolled',:p,:h, now()) RETURNING id"),
            {"t": tenant_id, "s": site_id, "p": agent_token[:8],
             "h": hashlib.sha256(agent_token.encode()).hexdigest()},
        )).scalar_one()

    api = FastAPI()
    api.include_router(ingest_module.router)
    # The real services register this handler too; without it an ApiError would surface as
    # an unhandled exception rather than the 401/404 a device actually sees.
    api.add_exception_handler(ApiError, api_error_handler)
    api.state.session_factory = factory
    # No object store: evidence capture is exercised by the e2e scripts against MinIO.
    api.state.object_store = None

    transport = httpx.ASGITransport(app=api)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://ingest.test") as client:
            yield {
                "client": client, "factory": factory, "tenant_id": tenant_id,
                "site_id": site_id, "camera_id": camera_id, "device_id": device_id,
                "suffix": suffix, "auth": {"Authorization": f"Bearer {agent_token}"},
            }
    finally:
        async with factory() as session, session.begin():
            await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
            await session.execute(
                text("DELETE FROM tenants WHERE organization_id = :o"), {"o": org_id}
            )
            await session.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": org_id})
        await engine.dispose()


@asynccontextmanager
async def foreign_tenant_camera(ctx):
    """A camera belonging to a *different*, real tenant.

    Stronger than a random UUID: it proves the lookup refuses a camera that genuinely
    exists, which is the only version of this that could ever leak. Note what does the
    refusing here - `_camera_site`'s explicit `c.tenant_id = :tenant_id` predicate, not
    row-level security. RLS is a second layer underneath, and this suite cannot exercise
    it at all: `TEST_POSTGRES_DSN` connects as `csense_app`, which is `rolsuper` and
    `rolbypassrls`, while the services run as `csense_api`. See this module's callers.
    """
    factory = ctx["factory"]
    suffix = uuid.uuid4().hex[:8]
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        org_id = (await session.execute(
            text("INSERT INTO organizations (organization_type, legal_name, display_name, "
                 "slug, status) VALUES ('direct_customer', :n, :n, :s, 'active') RETURNING id"),
            {"n": f"Other Tenant {suffix}", "s": f"other-{suffix}"},
        )).scalar_one()
        other_tenant = (await session.execute(
            text("INSERT INTO tenants (organization_id, status) VALUES (:o,'active') RETURNING id"),
            {"o": org_id},
        )).scalar_one()
        other_site = (await session.execute(
            text("INSERT INTO sites (tenant_id, name, code, timezone) "
                 "VALUES (:t,'Their depot',:c,'Asia/Kolkata') RETURNING id"),
            {"t": other_tenant, "c": f"othersite-{suffix}"},
        )).scalar_one()
        camera_id = (await session.execute(
            text("INSERT INTO cameras (tenant_id, site_id, name, code, status) "
                 "VALUES (:t,:s,'Their bay',:c,'ready') RETURNING id"),
            {"t": other_tenant, "s": other_site, "c": f"othercam-{suffix}"},
        )).scalar_one()
    try:
        yield camera_id
    finally:
        async with factory() as session, session.begin():
            await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
            await session.execute(
                text("DELETE FROM tenants WHERE organization_id = :o"), {"o": org_id}
            )
            await session.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": org_id})


def detection(ctx, event_suffix: str, *, camera_id=None, captured_at=CAPTURED_AT, bbox=None):
    """One request body, with a `source_event_id` unique to this fixture's tenant."""
    return {
        "camera_id": str(camera_id or ctx["camera_id"]),
        "source_event_id": f"{ctx['suffix']}-{event_suffix}",
        "captured_at": captured_at.isoformat() if hasattr(captured_at, "isoformat") else captured_at,
        "objects": [{"class_name": "person", "confidence": 0.94, "bbox": bbox or INSIDE_ZONE}],
    }


async def count_detections(ctx, source_event_id: str) -> int:
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        return (await session.execute(
            text("SELECT count(*) FROM detections WHERE tenant_id = :t AND source_event_id = :s"),
            {"t": ctx["tenant_id"], "s": source_event_id},
        )).scalar_one()


async def count_incidents(ctx) -> int:
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        return (await session.execute(
            text("SELECT count(*) FROM incidents WHERE tenant_id = :t"), {"t": ctx["tenant_id"]},
        )).scalar_one()


async def stored_device_id(ctx, source_event_id: str):
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        return (await session.execute(
            text("SELECT edge_device_id FROM detections "
                 "WHERE tenant_id = :t AND source_event_id = :s"),
            {"t": ctx["tenant_id"], "s": source_event_id},
        )).scalar_one()


async def stored_capture_time(ctx, source_event_id: str):
    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        return (await session.execute(
            text("SELECT capture_time FROM detections "
                 "WHERE tenant_id = :t AND source_event_id = :s"),
            {"t": ctx["tenant_id"], "s": source_event_id},
        )).scalar_one()
