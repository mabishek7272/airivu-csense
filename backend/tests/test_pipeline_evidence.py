"""Evidence capture: masking, storage, digest verification, tenant-scoped access.

Privacy masking is on by default (PRD), so these tests check the masking actually changes
pixels rather than trusting that it was called. A masking function that silently no-ops
would pass any test that only asserts two objects exist.

Needs Postgres, MongoDB-free, and MinIO; skipped otherwise.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import uuid

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from csense_shared.pipeline.evidence import (
    capture_evidence,
    encode_jpeg,
    evidence_object_key,
    mask_polygon_region,
    mask_regions,
    presign_evidence,
)
from csense_shared.storage.objects import BUCKET_EVIDENCE, create_client, ensure_buckets

# No blanket asyncio mark: this module mixes sync (masking, key format) and async
# (storage) tests, and pytest.ini sets asyncio_mode = auto, so async tests are detected
# without one.
pytestmark = pytest.mark.skipif(
    not (os.environ.get("TEST_POSTGRES_DSN") and os.environ.get("TEST_MINIO_ENDPOINT")),
    reason="TEST_POSTGRES_DSN / TEST_MINIO_ENDPOINT not set - skipping",
)

CAPTURED_AT = dt.datetime(2026, 8, 26, 12, 0, tzinfo=dt.UTC)


def _async_dsn() -> str:
    parts = dict(p.split("=", 1) for p in os.environ["TEST_POSTGRES_DSN"].split())
    return (
        f"postgresql+asyncpg://{parts['user']}:{parts['password']}"
        f"@{parts['host']}:{parts.get('port', '5432')}/{parts['dbname']}"
    )


def _minio():
    class _Settings:
        minio_endpoint = os.environ["TEST_MINIO_ENDPOINT"]
        minio_root_user = os.environ["TEST_MINIO_USER"]
        minio_root_password = os.environ["TEST_MINIO_PASSWORD"]
        minio_use_tls = False

    client = create_client(_Settings())
    ensure_buckets(client)
    return client


def noisy_image(width: int = 320, height: int = 240) -> np.ndarray:
    """Random noise, so blurring provably changes it.

    A flat colour would blur to itself and the masking assertions would pass vacuously.
    """
    rng = np.random.default_rng(seed=42)
    return rng.integers(0, 255, size=(height, width, 3), dtype=np.uint8)


@pytest_asyncio.fixture()
async def tenant_ctx():
    engine = create_async_engine(_async_dsn())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:8]

    async with factory() as session:
        async with session.begin():
            await session.execute(text("SELECT set_config('app.is_platform', 'true', true)"))
            org_id = (
                await session.execute(
                    text(
                        "INSERT INTO organizations (organization_type, legal_name, display_name, slug, status) "
                        "VALUES ('direct_customer', :n, :n, :s, 'active') RETURNING id"
                    ),
                    {"n": f"Evidence Test {suffix}", "s": f"evidence-test-{suffix}"},
                )
            ).scalar_one()
            tenant_id = (
                await session.execute(
                    text("INSERT INTO tenants (organization_id, status) VALUES (:o, 'active') RETURNING id"),
                    {"o": org_id},
                )
            ).scalar_one()
            site_id = (
                await session.execute(
                    text("INSERT INTO sites (tenant_id, name, code) VALUES (:t, 'S', :c) RETURNING id"),
                    {"t": tenant_id, "c": f"site-{suffix}"},
                )
            ).scalar_one()
            camera_id = (
                await session.execute(
                    text(
                        "INSERT INTO cameras (tenant_id, site_id, name, code, status) "
                        "VALUES (:t, :s, 'C', :c, 'ready') RETURNING id"
                    ),
                    {"t": tenant_id, "s": site_id, "c": f"cam-{suffix}"},
                )
            ).scalar_one()

    async with factory() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
            )
            yield {"session": session, "tenant_id": tenant_id, "camera_id": camera_id, "minio": _minio()}
            await session.rollback()

    await engine.dispose()


# --- Masking -------------------------------------------------------------------------

def test_masking_actually_changes_the_region():
    """Guards against a no-op mask, which would pass any test that only counts objects."""
    image = noisy_image()
    box = (0.25, 0.25, 0.75, 0.75)
    masked = mask_regions(image, [box])

    height, width = image.shape[:2]
    region = (slice(int(0.25 * height), int(0.75 * height)), slice(int(0.25 * width), int(0.75 * width)))
    assert not np.array_equal(image[region], masked[region]), "masked region must differ"
    # Blurring noise reduces local variance; this is what "unrecoverable" looks like numerically.
    assert masked[region].var() < image[region].var()


def test_masking_leaves_the_rest_of_the_frame_untouched():
    """An operator still needs to see the scene; masking is targeted, not global."""
    image = noisy_image()
    masked = mask_regions(image, [(0.0, 0.0, 0.25, 0.25)])

    height, width = image.shape[:2]
    untouched = (slice(int(0.5 * height), height), slice(int(0.5 * width), width))
    assert np.array_equal(image[untouched], masked[untouched])


def test_masking_does_not_mutate_the_original():
    """The original must stay pristine - it is stored as its own evidence object."""
    image = noisy_image()
    before = image.copy()
    mask_regions(image, [(0.2, 0.2, 0.8, 0.8)])
    assert np.array_equal(image, before)


def test_out_of_range_boxes_are_clamped():
    """Detections at the frame edge can produce coordinates slightly outside 0..1; a
    negative slice index would silently mask the wrong region."""
    image = noisy_image()
    masked = mask_regions(image, [(-0.5, -0.5, 1.5, 1.5)])
    assert masked.shape == image.shape
    assert not np.array_equal(image, masked)


def test_empty_box_list_is_a_no_op_copy():
    image = noisy_image()
    masked = mask_regions(image, [])
    assert np.array_equal(image, masked)
    assert masked is not image


def test_degenerate_box_is_skipped():
    image = noisy_image()
    masked = mask_regions(image, [(0.5, 0.5, 0.5, 0.5)])
    assert np.array_equal(image, masked)


# --- Zone-shaped masking (CHECKLIST: "zone-privacy-level gating") --------------------

def test_polygon_masking_changes_pixels_inside_the_shape():
    """Same no-op-detection guard as test_masking_actually_changes_the_region above, for
    the polygon path - blurring a triangle rather than a rectangle."""
    image = noisy_image()
    triangle = ((0.5, 0.1), (0.9, 0.9), (0.1, 0.9))
    masked = mask_polygon_region(image, triangle)

    height, width = image.shape[:2]
    # A point well inside the triangle's centroid.
    cy, cx = int(0.6 * height), int(0.5 * width)
    region = (slice(cy - 5, cy + 5), slice(cx - 5, cx + 5))
    assert not np.array_equal(image[region], masked[region])
    assert masked[region].var() < image[region].var()


def test_polygon_masking_leaves_a_point_outside_the_shape_untouched():
    """A rectangle's own bounding box would over-mask a corner the real polygon
    excludes - this pins that the mask follows the actual polygon shape, not its
    bounding rectangle."""
    image = noisy_image()
    # A triangle occupying only the lower-left half of the frame.
    triangle = ((0.0, 1.0), (1.0, 1.0), (0.0, 0.0))
    masked = mask_polygon_region(image, triangle)

    height, width = image.shape[:2]
    # The top-right corner is inside the triangle's bounding box (0,0)-(1,1) but well
    # outside the triangle itself.
    corner = (slice(0, int(0.1 * height)), slice(int(0.9 * width), width))
    assert np.array_equal(image[corner], masked[corner])


def test_polygon_masking_does_not_mutate_the_original():
    image = noisy_image()
    before = image.copy()
    mask_polygon_region(image, ((0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)))
    assert np.array_equal(image, before)


def test_none_polygon_is_a_no_op_copy():
    image = noisy_image()
    masked = mask_polygon_region(image, None)
    assert np.array_equal(image, masked)
    assert masked is not image


def test_too_few_points_is_a_no_op_copy():
    """A polygon needs at least 3 points to enclose any area - the same MIN_POINTS
    validation zones.py's own ZoneIn already enforces at the API layer; this is the
    pipeline-side defensive counterpart for malformed/legacy geometry, matching
    _polygon_from_zone's own "degrade, don't raise" philosophy in ingest.py."""
    image = noisy_image()
    masked = mask_polygon_region(image, ((0.2, 0.2), (0.8, 0.8)))
    assert np.array_equal(image, masked)


def test_polygon_entirely_outside_the_frame_is_a_no_op_copy():
    image = noisy_image()
    masked = mask_polygon_region(image, ((1.5, 1.5), (1.6, 1.5), (1.6, 1.6), (1.5, 1.6)))
    assert np.array_equal(image, masked)


def test_object_keys_are_tenant_prefixed():
    """SCH §14/§11.5: keys are opaque, tenant-prefixed, and built server-side."""
    tenant_id, incident_id, evidence_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    key = evidence_object_key(tenant_id, incident_id, evidence_id, "masked")
    assert key.startswith(f"{tenant_id}/incidents/{incident_id}/")
    assert key.endswith("/masked.jpg")


def test_unlinked_evidence_still_gets_a_wellformed_key():
    key = evidence_object_key(uuid.uuid4(), None, uuid.uuid4(), "original")
    assert "/incidents/unlinked/" in key


# --- Capture and storage -------------------------------------------------------------

async def test_capture_stores_all_three_variants(tenant_ctx):
    from csense_shared.pipeline.evidence import Annotation

    evidence = await capture_evidence(
        tenant_ctx["session"],
        tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"],
        camera_id=tenant_ctx["camera_id"],
        image=noisy_image(),
        capture_time=CAPTURED_AT,
        mask_boxes=[(0.2, 0.2, 0.6, 0.6)],
        annotations=[Annotation((0.2, 0.2, 0.6, 0.6), "person", 0.9)],
    )

    original, masked, annotated = evidence.original, evidence.masked, evidence.annotated
    assert original.privacy_variant == "original"
    assert masked.privacy_variant == "masked"
    assert annotated.privacy_variant == "annotated"
    # Three distinct digests: proof the mask and the boxes were applied before upload,
    # not promised and skipped.
    assert len({original.sha256, masked.sha256, annotated.sha256}) == 3


async def test_stored_digest_matches_the_uploaded_bytes(tenant_ctx):
    """SCH §19: digest and size must match storage before evidence is available."""
    evidence = await capture_evidence(
        tenant_ctx["session"],
        tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"],
        camera_id=tenant_ctx["camera_id"],
        image=noisy_image(),
        capture_time=CAPTURED_AT,
    )
    original = evidence.original

    response = tenant_ctx["minio"].get_object(BUCKET_EVIDENCE, original.object_key)
    try:
        payload = response.read()
    finally:
        response.close()
        response.release_conn()

    assert hashlib.sha256(payload).hexdigest() == original.sha256
    assert len(payload) == original.size_bytes


async def test_masked_variant_records_its_lineage(tenant_ctx):
    """A masked image must say what it was derived from, so the original is findable by
    someone with the right permission - and only by them."""
    evidence = await capture_evidence(
        tenant_ctx["session"],
        tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"],
        camera_id=tenant_ctx["camera_id"],
        image=noisy_image(),
        capture_time=CAPTURED_AT,
        mask_boxes=[(0.1, 0.1, 0.5, 0.5)],
    )
    original, masked = evidence.original, evidence.masked

    row = (
        await tenant_ctx["session"].execute(
            text(
                "SELECT privacy_variant::text, original_evidence_id, access_classification "
                "FROM evidence WHERE id = :id"
            ),
            {"id": masked.evidence_id},
        )
    ).first()
    assert row[0] == "masked"
    assert row[1] == original.evidence_id
    assert row[2] == "standard"


async def test_original_is_marked_restricted(tenant_ctx):
    """The unmasked image is the sensitive artifact; its classification says so."""
    evidence = await capture_evidence(
        tenant_ctx["session"],
        tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"],
        camera_id=tenant_ctx["camera_id"],
        image=noisy_image(),
        capture_time=CAPTURED_AT,
    )
    original = evidence.original
    classification = (
        await tenant_ctx["session"].execute(
            text("SELECT access_classification FROM evidence WHERE id = :id"),
            {"id": original.evidence_id},
        )
    ).scalar_one()
    assert classification == "restricted"


async def test_retention_expiry_is_recorded(tenant_ctx):
    evidence = await capture_evidence(
        tenant_ctx["session"],
        tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"],
        camera_id=tenant_ctx["camera_id"],
        image=noisy_image(),
        capture_time=CAPTURED_AT,
        retention_days=30,
    )
    masked = evidence.masked
    expires_at = (
        await tenant_ctx["session"].execute(
            text("SELECT expires_at FROM evidence WHERE id = :id"), {"id": masked.evidence_id}
        )
    ).scalar_one()
    assert expires_at.replace(tzinfo=dt.UTC) == CAPTURED_AT + dt.timedelta(days=30)


async def test_presign_returns_a_url_for_the_owning_tenant(tenant_ctx):
    evidence = await capture_evidence(
        tenant_ctx["session"],
        tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"],
        camera_id=tenant_ctx["camera_id"],
        image=noisy_image(),
        capture_time=CAPTURED_AT,
    )
    masked = evidence.masked
    url = await presign_evidence(
        tenant_ctx["session"], tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"], evidence_id=masked.evidence_id,
    )
    assert url and BUCKET_EVIDENCE in url


async def test_presign_refuses_another_tenants_evidence(tenant_ctx):
    """Object storage performs no authorisation of its own (SCH §2), so this check is the
    only thing standing between an id and someone else's snapshot."""
    evidence = await capture_evidence(
        tenant_ctx["session"],
        tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"],
        camera_id=tenant_ctx["camera_id"],
        image=noisy_image(),
        capture_time=CAPTURED_AT,
    )
    masked = evidence.masked
    url = await presign_evidence(
        tenant_ctx["session"], tenant_ctx["minio"],
        tenant_id=uuid.uuid4(), evidence_id=masked.evidence_id,
    )
    assert url is None


async def test_malformed_digest_is_rejected_by_the_schema(tenant_ctx):
    """The digest format check is a constraint, not a convention. Asserting the specific
    IntegrityError rather than a blind Exception means a typo in the SQL cannot make this
    pass for the wrong reason."""
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await tenant_ctx["session"].execute(
            text(
                "INSERT INTO evidence (tenant_id, camera_id, object_id, evidence_type, "
                "capture_time, sha256) VALUES (:t, :c, :o, 'snapshot', now(), 'not-a-digest')"
            ),
            {"t": tenant_ctx["tenant_id"], "c": tenant_ctx["camera_id"], "o": uuid.uuid4()},
        )


def test_jpeg_encoding_roundtrips():
    import cv2

    image = noisy_image(64, 48)
    payload = encode_jpeg(image)
    decoded = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape == image.shape


async def test_no_annotations_reuses_the_masked_variant(tenant_ctx):
    """Without boxes, an annotated image would be byte-identical to the masked one.
    Storing it would mean a duplicate object and row for every capture, so the masked
    record is reused - and callers still never have to branch on whether one exists."""
    evidence = await capture_evidence(
        tenant_ctx["session"],
        tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"],
        camera_id=tenant_ctx["camera_id"],
        image=noisy_image(),
        capture_time=CAPTURED_AT,
    )
    assert evidence.annotated.evidence_id == evidence.masked.evidence_id

    stored = (
        await tenant_ctx["session"].execute(
            text("SELECT count(*) FROM evidence WHERE camera_id = :c"),
            {"c": tenant_ctx["camera_id"]},
        )
    ).scalar_one()
    assert stored == 2, "only original and masked should be written"


# --- Zone-privacy-level gating: capture_evidence's own zone_privacy_level/zone_polygon --

ZONE_POLYGON = ((0.1, 0.1), (0.6, 0.1), (0.6, 0.6), (0.1, 0.6))


async def test_standard_zone_original_stays_genuinely_unmasked(tenant_ctx):
    """Regression guard: a `standard` (the default) or missing privacy level must not
    pick up any zone-wide masking - only `sensitive`/`high` do."""
    raw = noisy_image()
    evidence = await capture_evidence(
        tenant_ctx["session"],
        tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"],
        camera_id=tenant_ctx["camera_id"],
        image=raw,
        capture_time=CAPTURED_AT,
        zone_privacy_level="standard",
        zone_polygon=ZONE_POLYGON,
    )
    original = evidence.original
    response = tenant_ctx["minio"].get_object(BUCKET_EVIDENCE, original.object_key)
    try:
        payload = response.read()
    finally:
        response.close()
        response.release_conn()
    assert hashlib.sha256(payload).hexdigest() == hashlib.sha256(encode_jpeg(raw)).hexdigest()


async def test_sensitive_zone_masks_the_whole_zone_area_but_keeps_a_real_original(tenant_ctx):
    """`sensitive`: masked/annotated get zone-wide masking, but the true original is still
    stored - evidence.download still has something real to return."""
    raw = noisy_image()
    evidence = await capture_evidence(
        tenant_ctx["session"],
        tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"],
        camera_id=tenant_ctx["camera_id"],
        image=raw,
        capture_time=CAPTURED_AT,
        zone_privacy_level="sensitive",
        zone_polygon=ZONE_POLYGON,
    )

    # The original is the real, unmasked frame.
    original_resp = tenant_ctx["minio"].get_object(BUCKET_EVIDENCE, evidence.original.object_key)
    try:
        original_payload = original_resp.read()
    finally:
        original_resp.close()
        original_resp.release_conn()
    assert hashlib.sha256(original_payload).hexdigest() == hashlib.sha256(encode_jpeg(raw)).hexdigest()

    # The masked variant differs from the raw frame inside the zone's own area (not just
    # wherever mask_boxes happened to point, since none were passed here at all).
    masked_resp = tenant_ctx["minio"].get_object(BUCKET_EVIDENCE, evidence.masked.object_key)
    try:
        masked_payload = masked_resp.read()
    finally:
        masked_resp.close()
        masked_resp.release_conn()
    assert masked_payload != encode_jpeg(raw)
    assert evidence.original.evidence_id != evidence.masked.evidence_id
    assert evidence.original.object_key != evidence.masked.object_key


async def test_high_zone_never_stores_an_unmasked_frame(tenant_ctx):
    """`high`: the real privacy guarantee - no unmasked bytes exist anywhere in storage
    for this capture, not merely a permission check standing between them and a reader."""
    raw = noisy_image()
    evidence = await capture_evidence(
        tenant_ctx["session"],
        tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"],
        camera_id=tenant_ctx["camera_id"],
        image=raw,
        capture_time=CAPTURED_AT,
        zone_privacy_level="high",
        zone_polygon=ZONE_POLYGON,
    )

    # "original" is a distinct evidence row (its own id, its own restricted
    # classification - the read-path contract callers rely on is unchanged)...
    assert evidence.original.evidence_id != evidence.masked.evidence_id
    # ...but points at the SAME object as masked: no separate unmasked upload happened.
    assert evidence.original.object_key == evidence.masked.object_key
    assert evidence.original.sha256 == evidence.masked.sha256

    # What's actually in storage under that key is provably not the raw frame.
    response = tenant_ctx["minio"].get_object(BUCKET_EVIDENCE, evidence.original.object_key)
    try:
        payload = response.read()
    finally:
        response.close()
        response.release_conn()
    assert hashlib.sha256(payload).hexdigest() != hashlib.sha256(encode_jpeg(raw)).hexdigest()

    # Real row-count check: still exactly 2 evidence rows (original + masked; no
    # annotations were passed so annotated reuses masked, same as the no-zone case) -
    # confirming this reuses precedent rather than a hidden third upload.
    stored = (
        await tenant_ctx["session"].execute(
            text("SELECT count(*) FROM evidence WHERE camera_id = :c AND capture_time = :ct"),
            {"c": tenant_ctx["camera_id"], "ct": CAPTURED_AT},
        )
    ).scalar_one()
    assert stored == 2

    # And genuinely only 2 distinct objects were uploaded to MinIO for this capture - not
    # 3 duplicate copies of the same masked bytes under different keys.
    original_row = (
        await tenant_ctx["session"].execute(
            text("SELECT object_id FROM evidence WHERE id = :id"), {"id": evidence.original.evidence_id}
        )
    ).scalar_one()
    masked_row = (
        await tenant_ctx["session"].execute(
            text("SELECT object_id FROM evidence WHERE id = :id"), {"id": evidence.masked.evidence_id}
        )
    ).scalar_one()
    assert original_row == masked_row


async def test_high_zones_original_still_reports_restricted_classification(tenant_ctx):
    """The evidence.download read-path contract doesn't change shape for a `high` zone -
    it just cannot return unmasked bytes any more, because none exist."""
    evidence = await capture_evidence(
        tenant_ctx["session"],
        tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"],
        camera_id=tenant_ctx["camera_id"],
        image=noisy_image(),
        capture_time=CAPTURED_AT,
        zone_privacy_level="high",
        zone_polygon=ZONE_POLYGON,
    )
    classification = (
        await tenant_ctx["session"].execute(
            text("SELECT access_classification FROM evidence WHERE id = :id"),
            {"id": evidence.original.evidence_id},
        )
    ).scalar_one()
    assert classification == "restricted"


async def test_a_zone_with_no_polygon_gets_no_extra_masking(tenant_ctx):
    """A `sensitive`/`high` zone_privacy_level with no polygon (e.g. malformed geometry
    that _polygon_from_zone in ingest.py already degrades to None) must not crash or
    silently mask the whole frame - it falls back to whatever mask_boxes alone would
    have produced, the same as before this feature existed."""
    raw = noisy_image()
    evidence = await capture_evidence(
        tenant_ctx["session"],
        tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"],
        camera_id=tenant_ctx["camera_id"],
        image=raw,
        capture_time=CAPTURED_AT,
        zone_privacy_level="high",
        zone_polygon=None,
    )
    # No polygon means zone_masking_applies is False even for `high` - the real original
    # is still stored, exactly like the standard/no-zone case.
    original_resp = tenant_ctx["minio"].get_object(BUCKET_EVIDENCE, evidence.original.object_key)
    try:
        original_payload = original_resp.read()
    finally:
        original_resp.close()
        original_resp.release_conn()
    assert hashlib.sha256(original_payload).hexdigest() == hashlib.sha256(encode_jpeg(raw)).hexdigest()
