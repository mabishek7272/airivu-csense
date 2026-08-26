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

async def test_capture_stores_both_variants(tenant_ctx):
    original, masked = await capture_evidence(
        tenant_ctx["session"],
        tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"],
        camera_id=tenant_ctx["camera_id"],
        image=noisy_image(),
        capture_time=CAPTURED_AT,
        mask_boxes=[(0.2, 0.2, 0.6, 0.6)],
    )

    assert original.privacy_variant == "original"
    assert masked.privacy_variant == "masked"
    # Different bytes, therefore different digests - proof the mask was applied before upload.
    assert original.sha256 != masked.sha256


async def test_stored_digest_matches_the_uploaded_bytes(tenant_ctx):
    """SCH §19: digest and size must match storage before evidence is available."""
    original, _ = await capture_evidence(
        tenant_ctx["session"],
        tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"],
        camera_id=tenant_ctx["camera_id"],
        image=noisy_image(),
        capture_time=CAPTURED_AT,
    )

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
    original, masked = await capture_evidence(
        tenant_ctx["session"],
        tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"],
        camera_id=tenant_ctx["camera_id"],
        image=noisy_image(),
        capture_time=CAPTURED_AT,
        mask_boxes=[(0.1, 0.1, 0.5, 0.5)],
    )

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
    original, _ = await capture_evidence(
        tenant_ctx["session"],
        tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"],
        camera_id=tenant_ctx["camera_id"],
        image=noisy_image(),
        capture_time=CAPTURED_AT,
    )
    classification = (
        await tenant_ctx["session"].execute(
            text("SELECT access_classification FROM evidence WHERE id = :id"),
            {"id": original.evidence_id},
        )
    ).scalar_one()
    assert classification == "restricted"


async def test_retention_expiry_is_recorded(tenant_ctx):
    _, masked = await capture_evidence(
        tenant_ctx["session"],
        tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"],
        camera_id=tenant_ctx["camera_id"],
        image=noisy_image(),
        capture_time=CAPTURED_AT,
        retention_days=30,
    )
    expires_at = (
        await tenant_ctx["session"].execute(
            text("SELECT expires_at FROM evidence WHERE id = :id"), {"id": masked.evidence_id}
        )
    ).scalar_one()
    assert expires_at.replace(tzinfo=dt.UTC) == CAPTURED_AT + dt.timedelta(days=30)


async def test_presign_returns_a_url_for_the_owning_tenant(tenant_ctx):
    _, masked = await capture_evidence(
        tenant_ctx["session"],
        tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"],
        camera_id=tenant_ctx["camera_id"],
        image=noisy_image(),
        capture_time=CAPTURED_AT,
    )
    url = await presign_evidence(
        tenant_ctx["session"], tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"], evidence_id=masked.evidence_id,
    )
    assert url and BUCKET_EVIDENCE in url


async def test_presign_refuses_another_tenants_evidence(tenant_ctx):
    """Object storage performs no authorisation of its own (SCH §2), so this check is the
    only thing standing between an id and someone else's snapshot."""
    _, masked = await capture_evidence(
        tenant_ctx["session"],
        tenant_ctx["minio"],
        tenant_id=tenant_ctx["tenant_id"],
        camera_id=tenant_ctx["camera_id"],
        image=noisy_image(),
        capture_time=CAPTURED_AT,
    )
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
