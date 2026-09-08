"""Tests for `csense_shared.pipeline.runtime` (Task 2 of
docs/superpowers/plans/2026-09-08-pipeline-execution-runtime.md).

Two different kinds of test, matching the two kinds of code in the module:

  - `run_one_cycle` is pure logic with every I/O boundary injected (frame grab, the
    ai-runtime HTTP call, detection ingestion) - no real camera, no real network call, no
    real database. Task 1 (`test_frame_grab.py`) already proved the real frame-grab I/O
    boundary for real; this file does not re-prove it.
  - `active_cloud_assignments` is a real DB query and is tested against a real, migrated
    Postgres (`TEST_POSTGRES_DSN`), the same convention `test_pipeline_ingest.py` and
    `test_model_registry.py` already use for exactly this kind of DB-boundary code.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from csense_shared.pipeline.rules import DetectedObject
from csense_shared.pipeline.runtime import (
    Assignment,
    active_cloud_assignments,
    merge_resource_profile,
    run_one_cycle,
)

NOW = dt.datetime(2026, 9, 8, 12, 0, tzinfo=dt.UTC)
FRAME = np.zeros((2, 2, 3), dtype=np.uint8)


def _assignment(**overrides) -> Assignment:
    defaults = dict(
        assignment_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        site_id=uuid.uuid4(),
        camera_id=uuid.uuid4(),
        hostname="192.0.2.10",
        rtsp_port=554,
        main_stream_path="/main",
        username=None,
        connection_mode="direct",
        model_name="yolov8n-general",
        confidence=0.5,
        sample_fps=0.5,
        effective_from=NOW - dt.timedelta(days=1),
        effective_to=None,
    )
    defaults.update(overrides)
    return Assignment(**defaults)


class _Recorder:
    """A fake `ingest_fn` that records every call instead of touching a database."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, **kwargs) -> None:
        self.calls.append(kwargs)


def _grab_returns(frame):
    def _fn(_assignment):
        return frame

    return _fn


def _infer_returning(objects):
    async def _fn(model_name, frame, *, confidence):
        return objects

    return _fn


# --- run_one_cycle: pure logic, injected fakes, no real camera/network/DB ---------------


async def test_clearing_confidence_detection_calls_ingest_once_with_deterministic_id():
    ingest = _Recorder()
    assignment = _assignment(confidence=0.5)
    detected = [DetectedObject("person", 0.91, (0.1, 0.1, 0.2, 0.2))]

    status = await run_one_cycle(
        assignment,
        grab_frame_fn=_grab_returns(FRAME),
        http_infer_fn=_infer_returning(detected),
        ingest_fn=ingest,
        now=NOW,
    )

    assert status == "detected"
    assert len(ingest.calls) == 1
    assert ingest.calls[0]["source_event_id"] == f"pipeline-runtime:{assignment.camera_id}:{NOW.isoformat()}"
    assert ingest.calls[0]["tenant_id"] == assignment.tenant_id
    assert ingest.calls[0]["camera_id"] == assignment.camera_id


async def test_no_qualifying_detection_never_calls_ingest():
    ingest = _Recorder()
    assignment = _assignment(confidence=0.8)
    detected = [DetectedObject("person", 0.4, (0.1, 0.1, 0.2, 0.2))]

    status = await run_one_cycle(
        assignment,
        grab_frame_fn=_grab_returns(FRAME),
        http_infer_fn=_infer_returning(detected),
        ingest_fn=ingest,
        now=NOW,
    )

    assert status == "clean"
    assert ingest.calls == []


async def test_unreachable_camera_never_calls_infer_or_ingest():
    ingest = _Recorder()
    infer_calls: list[tuple] = []

    async def _infer(model_name, frame, *, confidence):
        infer_calls.append((model_name, frame, confidence))
        return []

    assignment = _assignment()
    status = await run_one_cycle(
        assignment,
        grab_frame_fn=_grab_returns(None),
        http_infer_fn=_infer,
        ingest_fn=ingest,
        now=NOW,
    )

    assert status == "unreachable"
    assert infer_calls == []
    assert ingest.calls == []


async def test_same_camera_and_moment_produce_identical_source_event_id_both_times():
    """The idempotency property the whole design rests on: a crash-and-retry or an
    overlapping poll of the exact same (camera, moment) pair must synthesize the exact
    same id both times, so `ingest_detection`'s own (tenant_id, source_event_id) upsert
    absorbs the replay rather than creating a second detection."""
    ingest = _Recorder()
    assignment = _assignment()
    detected = [DetectedObject("person", 0.9, (0.1, 0.1, 0.2, 0.2))]

    for _ in range(2):
        await run_one_cycle(
            assignment,
            grab_frame_fn=_grab_returns(FRAME),
            http_infer_fn=_infer_returning(detected),
            ingest_fn=ingest,
            now=NOW,
        )

    assert len(ingest.calls) == 2
    assert ingest.calls[0]["source_event_id"] == ingest.calls[1]["source_event_id"]


async def test_tenant_override_confidence_wins_over_resource_profile():
    """`resource_profile.confidence` (0.4) would let a 0.6-confidence detection through;
    a `tenant_overrides` confidence of 0.9 must win and hold that same detection back -
    both at the merge level and end-to-end through `run_one_cycle`."""
    merged_without_override = merge_resource_profile({"confidence": 0.4}, {})
    merged_with_override = merge_resource_profile({"confidence": 0.4}, {"confidence": 0.9})

    assert merged_without_override["confidence"] == 0.4
    assert merged_with_override["confidence"] == 0.9

    detected = [DetectedObject("person", 0.6, (0.1, 0.1, 0.2, 0.2))]

    permissive = _assignment(confidence=merged_without_override["confidence"])
    strict = _assignment(confidence=merged_with_override["confidence"])

    ingest_permissive = _Recorder()
    status_permissive = await run_one_cycle(
        permissive,
        grab_frame_fn=_grab_returns(FRAME),
        http_infer_fn=_infer_returning(detected),
        ingest_fn=ingest_permissive,
        now=NOW,
    )
    ingest_strict = _Recorder()
    status_strict = await run_one_cycle(
        strict,
        grab_frame_fn=_grab_returns(FRAME),
        http_infer_fn=_infer_returning(detected),
        ingest_fn=ingest_strict,
        now=NOW,
    )

    assert status_permissive == "detected"
    assert len(ingest_permissive.calls) == 1
    assert status_strict == "clean"
    assert ingest_strict.calls == []


async def test_assignment_not_yet_effective_is_skipped_without_grabbing_a_frame():
    grabbed: list[Assignment] = []

    def _grab(a):
        grabbed.append(a)
        return FRAME

    assignment = _assignment(effective_from=NOW + dt.timedelta(days=1))
    status = await run_one_cycle(
        assignment,
        grab_frame_fn=_grab,
        http_infer_fn=_infer_returning([]),
        ingest_fn=_Recorder(),
        now=NOW,
    )

    assert status == "skipped_not_due"
    assert grabbed == []


# --- active_cloud_assignments: real DB query against a real, migrated Postgres ----------

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN not set - skipping"
)


def _async_dsn() -> str:
    parts = dict(p.split("=", 1) for p in os.environ["TEST_POSTGRES_DSN"].split())
    return (
        f"postgresql+asyncpg://{parts['user']}:{parts['password']}"
        f"@{parts['host']}:{parts.get('port', '5432')}/{parts['dbname']}"
    )


async def _insert_pipeline_version(
    session, *, pipeline_id, state, runtime_target, resource_profile=None,
    allowed_overrides_schema=None,
) -> uuid.UUID:
    import json as _json

    digest = uuid.uuid4().hex + uuid.uuid4().hex  # 64 hex chars - uniqueness is all that matters here
    next_number = (
        await session.execute(
            text(
                "SELECT COALESCE(MAX(version_number), 0) + 1 FROM pipeline_versions "
                "WHERE pipeline_id = :pipeline_id"
            ),
            {"pipeline_id": pipeline_id},
        )
    ).scalar_one()
    return (
        await session.execute(
            text(
                """
                INSERT INTO pipeline_versions
                    (pipeline_id, version_number, definition_json, definition_sha256,
                     allowed_overrides_schema, runtime_target, resource_profile, state)
                VALUES
                    (:pipeline_id, :version_number, CAST(:definition_json AS jsonb), :digest,
                     CAST(:allowed_overrides_schema AS jsonb), :runtime_target,
                     CAST(:resource_profile AS jsonb), :state)
                RETURNING id
                """
            ),
            {
                "pipeline_id": pipeline_id,
                "version_number": next_number,
                "definition_json": _json.dumps(
                    {"stages": [{"type": "infer", "model_name": "yolov8n-general", "min_model_state": "validated"}]}
                ),
                "digest": digest,
                "allowed_overrides_schema": _json.dumps(allowed_overrides_schema or {}),
                "runtime_target": runtime_target,
                "resource_profile": _json.dumps(resource_profile) if resource_profile is not None else None,
                "state": state,
            },
        )
    ).scalar_one()


async def _insert_camera(session, *, tenant_id, site_id, code) -> uuid.UUID:
    return (
        await session.execute(
            text(
                """
                INSERT INTO cameras
                    (tenant_id, site_id, name, code, status, hostname, rtsp_port,
                     main_stream_path, connection_mode)
                VALUES
                    (:t, :s, :c, :c, 'ready', 'camera.example.invalid', 554, '/main', 'direct')
                RETURNING id
                """
            ),
            {"t": tenant_id, "s": site_id, "c": code},
        )
    ).scalar_one()


async def _insert_assignment(
    session, *, tenant_id, camera_id, pipeline_version_id, status, tenant_overrides=None,
    priority=100,
) -> uuid.UUID:
    import json as _json

    assignment_id = (
        await session.execute(
            text(
                """
                INSERT INTO pipeline_assignments
                    (tenant_id, camera_id, pipeline_version_id, status, tenant_overrides, priority)
                VALUES
                    (:t, :cam, :pv, :status, CAST(:overrides AS jsonb), :priority)
                RETURNING id
                """
            ),
            {
                "t": tenant_id, "cam": camera_id, "pv": pipeline_version_id,
                "status": status, "overrides": _json.dumps(tenant_overrides or {}),
                "priority": priority,
            },
        )
    ).scalar_one()
    if status != "active":
        # `status` is not directly settable to anything but 'active' by the INSERT's own
        # default-less column list above other than through this explicit value, but the
        # unique partial index only exists for 'active' rows - revoking here (rather than
        # inserting pre-revoked) exercises the same UPDATE path pipeline_assignments.py's
        # own revoke endpoint uses, so a revoked row looks exactly like a real one.
        await session.execute(
            text("UPDATE pipeline_assignments SET status = :status WHERE id = :id"),
            {"status": status, "id": assignment_id},
        )
    return assignment_id


@pytest_asyncio.fixture()
async def scenario():
    """One tenant, one site, five cameras - one per exclusion/inclusion scenario this
    function has to get right - and the pipeline/version/assignment rows that make each
    camera's status/state/runtime_target combination real."""
    engine = create_async_engine(_async_dsn())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:8]

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))

        org_id = (
            await session.execute(
                text(
                    "INSERT INTO organizations (organization_type, legal_name, display_name, slug, status) "
                    "VALUES ('direct_customer', :n, :n, :s, 'active') RETURNING id"
                ),
                {"n": f"Runtime Test {suffix}", "s": f"runtime-{suffix}"},
            )
        ).scalar_one()
        tenant_id = (
            await session.execute(
                text("INSERT INTO tenants (organization_id, status) VALUES (:o,'active') RETURNING id"),
                {"o": org_id},
            )
        ).scalar_one()
        site_id = (
            await session.execute(
                text(
                    "INSERT INTO sites (tenant_id, name, code, timezone) "
                    "VALUES (:t,'Depot',:c,'UTC') RETURNING id"
                ),
                {"t": tenant_id, "c": f"site-{suffix}"},
            )
        ).scalar_one()

        pipeline_id = (
            await session.execute(
                text(
                    "INSERT INTO pipelines (code, name, use_case) "
                    "VALUES (:c, 'Runtime test pipeline', 'test') RETURNING id"
                ),
                {"c": f"runtime-test-{suffix}"},
            )
        ).scalar_one()

        version_published_cloud = await _insert_pipeline_version(
            session, pipeline_id=pipeline_id, state="published", runtime_target="cloud",
            resource_profile={"confidence": 0.6, "sample_fps": 1.0},
            allowed_overrides_schema={"confidence": "number"},
        )
        version_draft_cloud = await _insert_pipeline_version(
            session, pipeline_id=pipeline_id, state="draft", runtime_target="cloud",
        )
        version_deprecated_cloud = await _insert_pipeline_version(
            session, pipeline_id=pipeline_id, state="deprecated", runtime_target="cloud",
        )
        version_published_edge = await _insert_pipeline_version(
            session, pipeline_id=pipeline_id, state="published", runtime_target="edge",
        )

        cam_good = await _insert_camera(session, tenant_id=tenant_id, site_id=site_id, code=f"good-{suffix}")
        cam_draft = await _insert_camera(session, tenant_id=tenant_id, site_id=site_id, code=f"draft-{suffix}")
        cam_deprecated = await _insert_camera(session, tenant_id=tenant_id, site_id=site_id, code=f"dep-{suffix}")
        cam_edge = await _insert_camera(session, tenant_id=tenant_id, site_id=site_id, code=f"edge-{suffix}")
        cam_revoked = await _insert_camera(session, tenant_id=tenant_id, site_id=site_id, code=f"rev-{suffix}")

        await _insert_assignment(
            session, tenant_id=tenant_id, camera_id=cam_good, pipeline_version_id=version_published_cloud,
            status="active", tenant_overrides={"confidence": 0.9},
        )
        await _insert_assignment(
            session, tenant_id=tenant_id, camera_id=cam_draft, pipeline_version_id=version_draft_cloud,
            status="active",
        )
        await _insert_assignment(
            session, tenant_id=tenant_id, camera_id=cam_deprecated, pipeline_version_id=version_deprecated_cloud,
            status="active",
        )
        await _insert_assignment(
            session, tenant_id=tenant_id, camera_id=cam_edge, pipeline_version_id=version_published_edge,
            status="active",
        )
        await _insert_assignment(
            session, tenant_id=tenant_id, camera_id=cam_revoked, pipeline_version_id=version_published_cloud,
            status="revoked",
        )

    yield {
        "factory": factory,
        "tenant_id": tenant_id,
        "cam_good": cam_good,
        "cam_draft": cam_draft,
        "cam_deprecated": cam_deprecated,
        "cam_edge": cam_edge,
        "cam_revoked": cam_revoked,
    }

    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        await session.execute(text("DELETE FROM tenants WHERE organization_id = :o"), {"o": org_id})
        await session.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": org_id})
        # pipelines/pipeline_versions are platform-global (migration 0031's own docstring:
        # same reasoning models/model_versions got in migration 0006) - the tenant cascade
        # above never reaches them, and pipeline_versions.pipeline_id is ondelete=RESTRICT,
        # so leaving this out would leak 1 pipeline + 4 versions on every test run.
        # test_model_registry.py's own teardown does the equivalent cleanup for
        # models/model_versions; mirrored here. Versions first, to satisfy RESTRICT.
        await session.execute(text("DELETE FROM pipeline_versions WHERE pipeline_id = :p"), {"p": pipeline_id})
        await session.execute(text("DELETE FROM pipelines WHERE id = :p"), {"p": pipeline_id})
    await engine.dispose()


def _fixed_camera_ids(actual):
    return {a.camera_id for a in actual}


async def test_excludes_draft_pipeline_version(scenario):
    async with scenario["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        actual = await active_cloud_assignments(session)
    assert scenario["cam_draft"] not in _fixed_camera_ids(actual)


async def test_excludes_deprecated_pipeline_version(scenario):
    async with scenario["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        actual = await active_cloud_assignments(session)
    assert scenario["cam_deprecated"] not in _fixed_camera_ids(actual)


async def test_excludes_edge_runtime_target(scenario):
    async with scenario["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        actual = await active_cloud_assignments(session)
    assert scenario["cam_edge"] not in _fixed_camera_ids(actual)


async def test_excludes_revoked_assignment(scenario):
    async with scenario["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        actual = await active_cloud_assignments(session)
    assert scenario["cam_revoked"] not in _fixed_camera_ids(actual)


async def test_includes_active_published_cloud_assignment_with_merged_config(scenario):
    async with scenario["factory"]() as session, session.begin():
        await session.execute(text("SELECT set_config('app.is_platform','true',true)"))
        actual = await active_cloud_assignments(session)

    matches = [a for a in actual if a.camera_id == scenario["cam_good"]]
    assert len(matches) == 1
    found = matches[0]
    assert found.tenant_id == scenario["tenant_id"]
    assert found.model_name == "yolov8n-general"
    # resource_profile said 0.6; tenant_overrides said 0.9 - the override must win.
    assert found.confidence == 0.9
    # sample_fps has no override on this assignment, so resource_profile's own value holds.
    assert found.sample_fps == 1.0
