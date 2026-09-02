"""Batch detection ingestion - the endpoint an offline edge device drains its spool into.

Three properties decide whether the spool design works at all, and each has a test here:

  **One poisoned row must not block a drain.** If a rejected detection failed the whole
  batch, a device would retry it forever, its spool would fill, and every event queued
  behind the bad one would be lost to eviction. A bad item gets its own error entry and
  everything else still lands.

  **A failure must not roll back items that already succeeded.** The single endpoint holds
  one transaction for its whole request; a batch that did the same would discard 39 good
  ingests because item 40 hit a database error - and would then be unable to continue
  anyway, since a failed statement poisons the surrounding transaction. Each item gets its
  own transaction instead.

  **Replaying a batch is a no-op.** A device that loses the response after the server
  committed will resend, so the second submission has to return `duplicate: true` per item
  and open no second incident. This is the property the whole at-least-once + server-dedup
  design rests on, so it is tested against a real database rather than a stand-in.

Needs a migrated database; skipped otherwise.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import importlib.util
import os
import pathlib
import sys
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from csense_shared.errors import ApiError, api_error_handler
from csense_shared.security.tokens import AUDIENCE_CUSTOMER, issue_access_token

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN not set - skipping"
)

# 02:00 UTC is 07:30 in Kolkata; the fixture's site is in Kolkata so a dropped timezone
# conversion would change rule scheduling rather than passing by luck (same choice, and
# the same reason, as test_pipeline_ingest.py).
CAPTURED_AT = dt.datetime(2026, 8, 27, 2, 0, tzinfo=dt.UTC)
INSIDE_ZONE = [0.44, 0.36, 0.55, 0.92]
ZONE_POLYGON = '{"polygon":[[0.35,0.30],[1.0,0.30],[1.0,1.0],[0.35,1.0]]}'


def _load_ingest_module():
    """Loads tenant_api's `app.api.ingest` by path.

    Every service under backend/ names its package `app`, so a plain import resolves to
    whichever service another test module happened to import first - `test_ai_runtime_*`
    is collected before this file and leaves ai_runtime's `app` cached. Same fix, and the
    same reasoning, as `test_worker_loop_isolation.py`: save and clear the cached `app*`
    modules, put this service's own directory on `sys.path` for the load (ingest.py really
    does import `app.deps_agent`, so that has to resolve), then put everything back.
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
        # unique to this test, so it collides with nothing.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(service_dir))
        for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
            del sys.modules[name]
        sys.modules.update(saved)


ingest_module = _load_ingest_module()


def _async_dsn() -> str:
    parts = dict(p.split("=", 1) for p in os.environ["TEST_POSTGRES_DSN"].split())
    return (
        f"postgresql+asyncpg://{parts['user']}:{parts['password']}"
        f"@{parts['host']}:{parts.get('port', '5432')}/{parts['dbname']}"
    )


@pytest_asyncio.fixture()
async def ctx():
    """A tenant with a site, a camera, an intrusion rule, one recipient, and a real
    enrolled device credential - so the tests authenticate the same way a device does,
    not through an overridden dependency."""
    engine = create_async_engine(_async_dsn())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:8]
    agent_token = f"bt{suffix}{uuid.uuid4().hex}"

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        org_id = (await session.execute(
            text("INSERT INTO organizations (organization_type, legal_name, display_name, "
                 "slug, status) VALUES ('direct_customer', :n, :n, :s, 'active') RETURNING id"),
            {"n": f"Batch Test {suffix}", "s": f"batch-{suffix}"},
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
    # No object store: evidence capture is exercised by the e2e script against MinIO.
    api.state.object_store = None

    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://ingest.test") as client:
        yield {
            "client": client, "factory": factory, "tenant_id": tenant_id,
            "site_id": site_id, "camera_id": camera_id, "device_id": device_id,
            "suffix": suffix, "auth": {"Authorization": f"Bearer {agent_token}"},
        }

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        await session.execute(text("DELETE FROM tenants WHERE organization_id = :o"), {"o": org_id})
        await session.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": org_id})
    await engine.dispose()


def detection(ctx, event_suffix: str, *, camera_id=None, captured_at=CAPTURED_AT, bbox=None):
    return {
        "camera_id": str(camera_id or ctx["camera_id"]),
        "source_event_id": f"batch-{ctx['suffix']}-{event_suffix}",
        "captured_at": captured_at.isoformat(),
        "objects": [{"class_name": "person", "confidence": 0.94, "bbox": bbox or INSIDE_ZONE}],
    }


async def post_batch(ctx, detections):
    return await ctx["client"].post(
        "/api/v1/tenant/ingest/detections/batch",
        json={"detections": detections},
        headers=ctx["auth"],
    )


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


# --- Shape and ordering ----------------------------------------------------------------

async def test_a_batch_returns_one_result_per_item_in_request_order(ctx):
    """A device deletes spool rows by `source_event_id`, so both the order and the echoed
    id have to be right - matching by list position alone would let a reordered response
    delete the wrong row."""
    submitted = [detection(ctx, str(i)) for i in range(5)]
    response = await post_batch(ctx, submitted)

    assert response.status_code == 202
    body = response.json()
    assert [r["source_event_id"] for r in body["results"]] == [
        d["source_event_id"] for d in submitted
    ]
    assert body["accepted"] == 5
    assert body["failed"] == 0


async def test_an_accepted_item_carries_the_same_shape_the_single_endpoint_returns(ctx):
    response = await post_batch(ctx, [detection(ctx, "shape")])

    result = response.json()["results"][0]["result"]
    assert set(result) == {
        "detection_id", "duplicate", "rules_evaluated", "incident_id", "incident_number",
        "incident_created", "notifications_scheduled", "evidence_captured", "rejected_reasons",
    }


async def test_the_batch_runs_the_same_pipeline_incidents_and_alerts_included(ctx):
    """Not a separate code path: a detection inside the zone opens an incident and
    schedules the alert, exactly as the single endpoint would."""
    response = await post_batch(ctx, [detection(ctx, "pipeline")])

    result = response.json()["results"][0]["result"]
    assert result["incident_created"] is True
    assert result["incident_number"] == 1
    assert result["notifications_scheduled"] == 1
    assert result["rules_evaluated"] == 1


# --- The cap ---------------------------------------------------------------------------

async def test_a_batch_over_the_cap_is_refused_and_ingests_nothing(ctx):
    """An uncapped batch is a memory-exhaustion vector on a shared API. Refusing the whole
    request is right *here* and nowhere else in this endpoint: the body is oversized before
    any item has been looked at, so there is nothing partial to preserve."""
    oversized = [detection(ctx, f"over-{i}") for i in range(ingest_module.MAX_BATCH + 1)]
    response = await post_batch(ctx, oversized)

    assert response.status_code == 422
    assert await count_detections(ctx, oversized[0]["source_event_id"]) == 0


async def test_a_batch_at_the_cap_is_accepted(ctx):
    """The boundary itself, so a future off-by-one is caught rather than quietly costing a
    device an item per batch."""
    response = await post_batch(ctx, [
        detection(ctx, f"cap-{i}") for i in range(ingest_module.MAX_BATCH)
    ])

    assert response.status_code == 202
    assert response.json()["accepted"] == ingest_module.MAX_BATCH


async def test_an_empty_batch_is_refused(ctx):
    """A device with nothing to send has no reason to call; an empty body is a bug in its
    drain loop, and saying so beats returning a cheerful empty 202."""
    assert (await post_batch(ctx, [])).status_code == 422


# --- One bad item must not fail the batch ----------------------------------------------

async def test_an_unknown_camera_fails_only_its_own_item(ctx):
    response = await post_batch(ctx, [
        detection(ctx, "good-1"),
        detection(ctx, "unknown-camera", camera_id=uuid.uuid4()),
        detection(ctx, "good-2"),
    ])

    assert response.status_code == 202
    body = response.json()
    assert [r["accepted"] for r in body["results"]] == [True, False, True]
    assert body["results"][1]["error_code"] == "not_found"
    assert body["results"][1]["result"] is None
    assert body["accepted"] == 2 and body["failed"] == 1


async def test_a_clock_days_out_fails_only_its_own_item(ctx):
    """The rejection the single endpoint returns as a 422 becomes one item's error code, so
    a device can tell this row will never be accepted and stop retrying it."""
    future = dt.datetime.now(dt.UTC) + dt.timedelta(days=2)
    response = await post_batch(ctx, [
        detection(ctx, "skew-good"),
        detection(ctx, "skew-bad", captured_at=future),
    ])

    body = response.json()
    assert body["results"][1]["error_code"] == "capture_time_in_future"
    assert body["results"][0]["accepted"] is True
    assert await count_detections(ctx, body["results"][0]["source_event_id"]) == 1


async def test_an_unanticipated_failure_does_not_roll_back_its_neighbours(ctx, monkeypatch):
    """The transaction-boundary property, asserted against the database rather than the
    response: item 2 raises after item 1 has been ingested, and item 1 must still be there.
    One transaction per item is what makes that true - a request-wide transaction would
    have discarded it, and could not have continued to item 3 either.
    """
    real_ingest = ingest_module.ingest_detection
    poisoned = f"batch-{ctx['suffix']}-poison"

    async def exploding(session, store, **kwargs):
        if kwargs["source_event_id"] == poisoned:
            raise RuntimeError("something unanticipated, mid-transaction")
        return await real_ingest(session, store, **kwargs)

    monkeypatch.setattr(ingest_module, "ingest_detection", exploding)

    response = await post_batch(ctx, [
        detection(ctx, "before"), detection(ctx, "poison"), detection(ctx, "after"),
    ])

    body = response.json()
    assert [r["accepted"] for r in body["results"]] == [True, False, True]
    assert body["results"][1]["error_code"] == "ingest_failed"
    assert await count_detections(ctx, f"batch-{ctx['suffix']}-before") == 1
    assert await count_detections(ctx, f"batch-{ctx['suffix']}-after") == 1
    assert await count_detections(ctx, poisoned) == 0


# --- Idempotency, against a real database ----------------------------------------------

async def test_resending_an_identical_batch_is_a_no_op(ctx):
    """The property the entire offline-spool design rests on: a device that loses the
    response after the server committed resends, and must not turn one intrusion into two
    incidents and two 3am calls."""
    batch = [detection(ctx, f"idem-{i}") for i in range(3)]

    first = (await post_batch(ctx, batch)).json()
    second = (await post_batch(ctx, batch)).json()

    assert all(r["result"]["duplicate"] is False for r in first["results"])
    assert all(r["result"]["duplicate"] is True for r in second["results"])
    assert all(r["result"]["incident_created"] is False for r in second["results"])
    assert all(r["result"]["notifications_scheduled"] == 0 for r in second["results"])
    # One incident for the whole thing, both times - the three frames correlate into it.
    assert await count_incidents(ctx) == 1
    for item in batch:
        assert await count_detections(ctx, item["source_event_id"]) == 1


async def test_a_repeat_inside_one_batch_is_deduplicated_too(ctx):
    """A spool that re-offered a row it had already sent - a crash between send and ack -
    produces exactly this."""
    same = detection(ctx, "twice")
    body = (await post_batch(ctx, [same, same])).json()

    assert body["results"][0]["result"]["duplicate"] is False
    assert body["results"][1]["result"]["duplicate"] is True
    assert await count_detections(ctx, same["source_event_id"]) == 1


# --- Auth ------------------------------------------------------------------------------

async def test_no_credential_is_refused(ctx):
    response = await ctx["client"].post(
        "/api/v1/tenant/ingest/detections/batch",
        json={"detections": [detection(ctx, "unauth")]},
    )
    assert response.status_code == 401
    assert await count_detections(ctx, f"batch-{ctx['suffix']}-unauth") == 0


async def test_an_unknown_credential_is_refused(ctx, monkeypatch, settings):
    """A credential that is neither a device token nor a valid customer token gets the one
    uniform 401 - the fallback path needs real signing keys to reach its own rejection, so
    it is given the throwaway keypair the token tests use."""
    monkeypatch.setattr(ingest_module, "get_settings", lambda: settings)
    response = await ctx["client"].post(
        "/api/v1/tenant/ingest/detections/batch",
        json={"detections": [detection(ctx, "badcred")]},
        headers={"Authorization": "Bearer " + "z" * 40},
    )
    assert response.status_code == 401


async def test_a_scoped_customer_token_may_also_ingest_a_batch(ctx, monkeypatch, settings):
    """The second auth path the single endpoint supports (a token carrying
    `detection.ingest`), which a forked batch handler could easily have dropped."""
    monkeypatch.setattr(ingest_module, "get_settings", lambda: settings)
    token = issue_access_token(
        settings=settings,
        user_id=uuid.uuid4(),
        audience=AUDIENCE_CUSTOMER,
        tenant_id=ctx["tenant_id"],
        membership_id=uuid.uuid4(),
        permissions=frozenset({"detection.ingest"}),
        session_id=uuid.uuid4().hex,
    )
    response = await ctx["client"].post(
        "/api/v1/tenant/ingest/detections/batch",
        json={"detections": [detection(ctx, "scoped")]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 202
    assert response.json()["results"][0]["accepted"] is True


async def test_a_customer_token_without_the_scope_is_refused(ctx, monkeypatch, settings):
    monkeypatch.setattr(ingest_module, "get_settings", lambda: settings)
    token = issue_access_token(
        settings=settings,
        user_id=uuid.uuid4(),
        audience=AUDIENCE_CUSTOMER,
        tenant_id=ctx["tenant_id"],
        membership_id=uuid.uuid4(),
        permissions=frozenset({"incident.read"}),
        session_id=uuid.uuid4().hex,
    )
    response = await ctx["client"].post(
        "/api/v1/tenant/ingest/detections/batch",
        json={"detections": [detection(ctx, "noscope")]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert await count_detections(ctx, f"batch-{ctx['suffix']}-noscope") == 0


async def test_the_device_identity_comes_from_the_credential_not_the_body(ctx):
    """A device must not be able to report on behalf of another device by saying so in the
    body - the same rule the single endpoint has, which a forked batch path could easily
    have lost."""
    item = detection(ctx, "spoof")
    item["edge_device_id"] = str(uuid.uuid4())
    await post_batch(ctx, [item])

    async with ctx["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        stored = (await session.execute(
            text("SELECT edge_device_id FROM detections WHERE tenant_id = :t AND source_event_id = :s"),
            {"t": ctx["tenant_id"], "s": item["source_event_id"]},
        )).scalar_one()

    assert stored == ctx["device_id"]


# --- The frame budget ------------------------------------------------------------------

def _frame(n: int) -> str:
    return base64.b64encode(b"\x00" * n).decode()


def test_frames_within_the_budget_are_all_kept():
    items = [
        ingest_module.DetectionIn(
            camera_id=uuid.uuid4(), source_event_id=str(i), captured_at=CAPTURED_AT,
            objects=[], frame_base64=_frame(100),
        )
        for i in range(3)
    ]
    assert ingest_module._apply_frame_budget(items, budget=10_000) == [
        i.frame_base64 for i in items
    ]


def test_frames_past_the_budget_are_dropped_but_the_detections_are_not():
    """The budget costs snapshots, never detections - `_apply_frame_budget` only ever
    returns fewer *frames*, and the caller still ingests every item."""
    items = [
        ingest_module.DetectionIn(
            camera_id=uuid.uuid4(), source_event_id=str(i), captured_at=CAPTURED_AT,
            objects=[], frame_base64=_frame(600),
        )
        for i in range(3)
    ]
    kept = ingest_module._apply_frame_budget(items, budget=1000)

    assert kept[0] == items[0].frame_base64
    assert kept[1] is None and kept[2] is None
    assert len(kept) == len(items)


def test_a_batch_without_frames_spends_no_budget():
    items = [
        ingest_module.DetectionIn(
            camera_id=uuid.uuid4(), source_event_id=str(i), captured_at=CAPTURED_AT, objects=[],
        )
        for i in range(3)
    ]
    assert ingest_module._apply_frame_budget(items, budget=0) == [None, None, None]


async def test_an_oversized_frame_costs_its_snapshot_not_its_detection(ctx, monkeypatch):
    """End to end: a frame past the budget is not decoded at all, and the detection it came
    with is still ingested."""
    monkeypatch.setattr(ingest_module, "MAX_BATCH_FRAME_BYTES", 64)
    decoded: list[str | None] = []
    monkeypatch.setattr(ingest_module, "_decode_frame", lambda f: decoded.append(f))

    item = detection(ctx, "bigframe")
    item["frame_base64"] = _frame(4096)
    body = (await post_batch(ctx, [item])).json()

    assert decoded == [None]  # never handed to the decoder
    assert body["results"][0]["accepted"] is True
    assert await count_detections(ctx, item["source_event_id"]) == 1
