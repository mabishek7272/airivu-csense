"""The single detection endpoint, at route level.

This endpoint has been the pipeline's only production caller since it was written, and
until now nothing in CI exercised it as an HTTP route: `test_pipeline_ingest.py` calls
`ingest_detection` directly, which skips authentication, the clock-skew guard, the camera
lookup and the response shape entirely, and the two `scripts/e2e_*.py` that do hit it need
a live stack and never run in CI.

That gap became load-bearing when the batch endpoint landed, because the two routes now
share their whole body (`_ingest_one`, `tenant_scoped_session`). An edit made for the
batch path can regress this one, and these tests are the only thing that would say so. So
they assert behaviour rather than status codes: which device id was recorded, whether an
incident was opened, whether a replay opened a second one.

Needs a migrated database; skipped otherwise.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest
import pytest_asyncio
from ingest_harness import (
    CAPTURED_AT,
    OUTSIDE_ZONE,
    count_detections,
    count_incidents,
    detection,
    foreign_tenant_camera,
    ingest_context,
    ingest_module,
    stored_capture_time,
    stored_device_id,
)

from csense_shared.security.tokens import AUDIENCE_CUSTOMER, issue_access_token

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN not set - skipping"
)

ENDPOINT = "/api/v1/tenant/ingest/detections"


@pytest_asyncio.fixture()
async def ctx():
    async with ingest_context() as value:
        yield value


async def post(ctx, body, *, headers=None):
    return await ctx["client"].post(
        ENDPOINT, json=body, headers=ctx["auth"] if headers is None else headers
    )


def customer_token(ctx, settings, permissions):
    return issue_access_token(
        settings=settings,
        user_id=uuid.uuid4(),
        audience=AUDIENCE_CUSTOMER,
        tenant_id=ctx["tenant_id"],
        membership_id=uuid.uuid4(),
        permissions=frozenset(permissions),
        session_id=uuid.uuid4().hex,
    )


# --- The happy path, asserted on what it actually did ----------------------------------

async def test_a_detection_inside_the_zone_opens_an_incident_and_schedules_the_alert(ctx):
    """202, not 201: the incident is opened synchronously but the alerts it schedules are
    delivered afterwards by the notification worker."""
    response = await post(ctx, detection(ctx, "happy"))

    assert response.status_code == 202
    body = response.json()
    assert body["incident_created"] is True
    assert body["incident_number"] == 1
    assert body["notifications_scheduled"] == 1
    assert body["rules_evaluated"] == 1
    assert body["duplicate"] is False
    assert await count_detections(ctx, f"{ctx['suffix']}-happy") == 1


async def test_the_response_carries_the_full_ingest_out_shape(ctx):
    """The device reads these fields; dropping one silently would be invisible to a test
    that only checked the status code."""
    body = (await post(ctx, detection(ctx, "shape"))).json()

    assert set(body) == {
        "detection_id", "duplicate", "rules_evaluated", "incident_id", "incident_number",
        "incident_created", "notifications_scheduled", "evidence_captured", "rejected_reasons",
    }


async def test_a_detection_outside_the_zone_is_recorded_and_says_why_it_did_not_alert(ctx):
    """"What did the camera see at 02:00" is a real question, so the detection is stored
    either way - and `rejected_reasons` is what makes "the camera sees people and I get no
    alerts" debuggable without reading server logs."""
    body = (await post(ctx, detection(ctx, "outside", bbox=OUTSIDE_ZONE))).json()

    assert body["incident_id"] is None
    assert body["notifications_scheduled"] == 0
    assert body["detection_id"]
    assert body["rejected_reasons"]
    assert await count_detections(ctx, f"{ctx['suffix']}-outside") == 1


async def test_the_original_capture_time_is_stored_not_the_arrival_time(ctx):
    """FLOW-13's conflict policy: an event replayed from a spool hours later keeps the
    moment it was captured, or every late incident lands in the wrong place in every list.
    """
    await post(ctx, detection(ctx, "captime"))

    assert await stored_capture_time(ctx, f"{ctx['suffix']}-captime") == CAPTURED_AT


async def test_a_naive_timestamp_is_read_as_utc_rather_than_rejected(ctx):
    """An edge device that omits an offset almost always means UTC, and refusing the
    detection over a missing "+00:00" would cost the alert."""
    body = detection(ctx, "naive")
    body["captured_at"] = CAPTURED_AT.replace(tzinfo=None).isoformat()

    assert (await post(ctx, body)).status_code == 202
    assert await stored_capture_time(ctx, f"{ctx['suffix']}-naive") == CAPTURED_AT


# --- The guards ------------------------------------------------------------------------

async def test_a_capture_time_past_the_skew_allowance_is_refused(ctx):
    """The same rejection the batch path surfaces per item. A timestamp days ahead would
    park an incident at the top of every list indefinitely."""
    future = dt.datetime.now(dt.UTC) + ingest_module.MAX_CLOCK_SKEW + dt.timedelta(minutes=5)
    response = await post(ctx, detection(ctx, "skew", captured_at=future))

    assert response.status_code == 422
    assert response.json()["code"] == "capture_time_in_future"
    # The one rejection time itself cures: the same bytes are accepted once the wall clock
    # catches up or NTP corrects the device, so the problem response says so. The batch
    # path's per-item `retryable` is this same flag, and reads it off the same exception.
    assert response.json()["retryable"] is True
    assert await count_detections(ctx, f"{ctx['suffix']}-skew") == 0


async def test_a_capture_time_inside_the_skew_allowance_is_accepted(ctx):
    """The boundary in the other direction - edge clocks drift, and a device a minute fast
    must not stop reporting intrusions."""
    near = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1)
    assert (await post(ctx, detection(ctx, "nearskew", captured_at=near))).status_code == 202


async def test_a_camera_outside_this_tenant_is_a_404_not_a_constraint_violation(ctx):
    """Row-level security would catch this too, but a clear 404 is better than an opaque
    database error - and it must not leak whether the camera exists elsewhere."""
    response = await post(ctx, detection(ctx, "nocam", camera_id=uuid.uuid4()))

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_a_camera_belonging_to_another_real_tenant_is_refused(ctx):
    """Stronger than the random-UUID case above: the camera genuinely exists, so this is
    the only version that could actually leak one tenant's footage into another's
    incidents. What refuses it is `_camera_site`'s explicit tenant predicate, in the code
    both endpoints now share - row-level security is a second layer that this suite cannot
    reach, because `TEST_POSTGRES_DSN` connects as `csense_app` (rolsuper, rolbypassrls)
    while the services run as `csense_api`.
    """
    async with foreign_tenant_camera(ctx) as other_camera:
        response = await post(ctx, detection(ctx, "crosstenant", camera_id=other_camera))

        assert response.status_code == 404
        assert await count_detections(ctx, f"{ctx['suffix']}-crosstenant") == 0


async def test_an_inverted_bounding_box_is_refused_by_validation(ctx):
    """A zero-area box can never overlap an ROI, so it could only ever be silently dropped
    deep in rule evaluation. Naming it at the device is the point."""
    body = detection(ctx, "badbox", bbox=[0.6, 0.6, 0.2, 0.2])

    assert (await post(ctx, body)).status_code == 422
    assert await count_detections(ctx, f"{ctx['suffix']}-badbox") == 0


# --- Replay --------------------------------------------------------------------------

async def test_a_replayed_source_event_id_is_a_duplicate_and_opens_no_second_incident(ctx):
    """The property the whole edge design rests on: a device that buffers and resends after
    a network drop must not turn one intrusion into a stream of 3am calls."""
    body = detection(ctx, "replay")

    first = (await post(ctx, body)).json()
    second = (await post(ctx, body)).json()

    assert first["duplicate"] is False
    assert first["incident_created"] is True
    assert second["duplicate"] is True
    assert second["incident_created"] is False
    assert second["notifications_scheduled"] == 0
    assert second["detection_id"] == first["detection_id"]
    assert await count_incidents(ctx) == 1
    assert await count_detections(ctx, body["source_event_id"]) == 1


# --- Authentication: both real paths, and both ways of failing -------------------------

async def test_no_credential_is_refused(ctx):
    response = await post(ctx, detection(ctx, "unauth"), headers={})

    assert response.status_code == 401
    assert await count_detections(ctx, f"{ctx['suffix']}-unauth") == 0


async def test_an_unknown_credential_is_refused(ctx, monkeypatch, settings):
    """Neither a device token nor a valid customer token. The fallback path needs real
    signing keys to reach its own rejection, so it gets the throwaway keypair."""
    monkeypatch.setattr(ingest_module, "get_settings", lambda: settings)
    response = await post(
        ctx, detection(ctx, "badcred"), headers={"Authorization": "Bearer " + "z" * 40}
    )

    assert response.status_code == 401
    assert await count_detections(ctx, f"{ctx['suffix']}-badcred") == 0


async def test_a_real_device_credential_is_accepted(ctx):
    """Resolved for real through `edge_agent_lookup`, not an overridden dependency."""
    assert (await post(ctx, detection(ctx, "devcred"))).status_code == 202


async def test_a_scoped_customer_token_is_accepted(ctx, monkeypatch, settings):
    """The second real auth path - anything authenticating as a tenant member rather than
    an enrolled device."""
    monkeypatch.setattr(ingest_module, "get_settings", lambda: settings)
    token = customer_token(ctx, settings, {"detection.ingest"})
    response = await post(
        ctx, detection(ctx, "scoped"), headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 202
    assert response.json()["incident_created"] is True


async def test_a_customer_token_without_detection_ingest_is_refused(ctx, monkeypatch, settings):
    """A valid, correctly-audienced tenant token is still not enough - the permission is
    what the `edge_device` role holds and nothing else does."""
    monkeypatch.setattr(ingest_module, "get_settings", lambda: settings)
    token = customer_token(ctx, settings, {"incident.read"})
    response = await post(
        ctx, detection(ctx, "noscope"), headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 401
    assert await count_detections(ctx, f"{ctx['suffix']}-noscope") == 0


async def test_the_device_identity_comes_from_the_credential_not_the_body(ctx):
    """A device must not be able to report on behalf of another device by saying so in the
    body. Asserted on the stored row, because the response would look identical either
    way - which is exactly how this could regress unnoticed."""
    body = detection(ctx, "spoof")
    body["edge_device_id"] = str(uuid.uuid4())
    await post(ctx, body)

    assert await stored_device_id(ctx, body["source_event_id"]) == ctx["device_id"]


async def test_the_scoped_token_path_still_trusts_the_bodys_device_id(ctx, monkeypatch, settings):
    """The deliberate asymmetry: a customer token has no device identity of its own to
    check the body against, so the self-reported field is all there is. Pinned here so the
    difference stays a documented decision rather than something a later reader "fixes"
    into the credential rule above - or loses entirely."""
    monkeypatch.setattr(ingest_module, "get_settings", lambda: settings)
    token = customer_token(ctx, settings, {"detection.ingest"})
    claimed = ctx["device_id"]
    body = detection(ctx, "selfreported")
    body["edge_device_id"] = str(claimed)

    await post(ctx, body, headers={"Authorization": f"Bearer {token}"})

    assert await stored_device_id(ctx, body["source_event_id"]) == claimed
