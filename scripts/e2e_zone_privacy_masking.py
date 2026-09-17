"""End-to-end verification of zone-privacy-level gating - the real thing this pass wires
up, previously CRUD-only (`zones.privacy_level` set via the API, read by nothing).

Proves against the real running stack, not a unit test double:

  1. Register a tenant through the real public API.
  2. Create a site, then two zones through the real `POST /api/v1/tenant/zones` -
     one `standard` (the default), one `high` - and a rule watching each, through the
     real `POST /api/v1/tenant/rules`. Exercises the real Pydantic validation and the
     real INSERT, not a direct psql seed.
  3. Feed a real, synthetic frame with KNOWN pixel content through `ingest_detection()`
     for each zone (the same function `pipeline-runtime` calls in production) - real
     per-pixel noise around a distinct mean colour fills the zone's own polygon area, a
     different noise mean fills everywhere else. A flat colour would blur to itself
     (nothing for a weighted local average to change - see `test_pipeline_evidence.py`'s
     own `noisy_image()` fixture), so "was this region blurred" is checked via variance
     reduction on real noise, not a colour match - a real, checkable pixel fact
     afterward, not a guess.
  4. Fetch the real stored evidence bytes back from the real running MinIO and confirm:
     - the `standard` zone's `original` evidence is genuinely still the raw, unblurred
       frame (the zone's own noise variance is essentially unchanged) - regression
       check, this feature must not touch zones that don't opt into it.
     - the `high` zone's `original` evidence has NO unmasked copy anywhere - its stored
       bytes are the same as `masked`'s, and the zone's own noise is genuinely
       blurred (variance measurably reduced), not left untouched.
  5. Confirm the real presigned-download read path (`GET /api/v1/tenant/detections`)
     still returns a real URL for every variant - the read-path contract is unchanged,
     only what bytes exist behind it for a `high` zone.

Run from the repo root with the stack up:
    python scripts/e2e_zone_privacy_masking.py
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("CSENSE_BASE", "http://localhost:8080")
COMPOSE = ["docker", "compose", "--env-file", "../.env"]
INFRA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "infra")

PASSWORD = "E2EZonePrivacy!Password123"

# Both zones cover the left half of the frame, in normalised 0..1 polygon points, matching
# the ZoneIn/create_zone contract exactly (zones.py MIN_POINTS requires >= 3).
ZONE_POLYGON = [[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]]

# BGR (OpenCV convention, matching evidence.py's own encode_jpeg). NOISE around a mean,
# not a flat colour - test_pipeline_evidence.py's own noisy_image() fixture already
# documents why: "A flat colour would blur to itself and the masking assertions would
# pass vacuously" (Gaussian blur of a spatially-uniform region IS that same region -
# there's nothing for a weighted local average to change). Two distinct means (zone vs.
# outside) still let a region be identified by its mean colour; "was this region
# blurred" is checked by variance reduction instead, the same real signal the unit
# tests use.
ZONE_MEAN = (10, 10, 220)  # a saturated red, BGR
OUTSIDE_MEAN = (220, 180, 10)  # a distinct saturated blue, BGR
NOISE_SEED = 20260917
FRAME_SIZE = (480, 640)  # (height, width)


def _post(path: str, payload: dict, token: str | None = None, expect: tuple[int, ...] = (200, 201)) -> tuple[int, dict]:
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read()
        code = exc.code
        if code in expect:
            return code, (json.loads(body) if body else {})
        raise RuntimeError(f"POST {path} -> {code}: {body[:500]}") from exc


def _get(path: str, token: str) -> dict:
    request = urllib.request.Request(
        BASE + path, headers={"Authorization": f"Bearer {token}"}, method="GET"
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def _in_postgres(sql: str) -> str:
    result = subprocess.run(
        [*COMPOSE, "exec", "-T", "postgres", "psql", "-qtA", "-U", "csense_app", "-d", "csense", "-c", sql],
        cwd=INFRA_DIR, capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"psql failed:\n{result.stderr[-1000:]}")
    return result.stdout.strip()


def _read_env(key: str) -> str:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    with open(env_path) as handle:
        for line in handle:
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    raise KeyError(f"{key} not found in .env")


def step(number: int, title: str) -> None:
    print(f"\n[{number}] {title}")


def check(condition: bool, description: str, failures: list[str]) -> None:
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    email = f"zoneprivacy-{suffix}@example.com"
    failures: list[str] = []

    step(1, "Register a tenant through the real public API")
    _, auth = _post("/api/v1/auth/register", {
        "organization_name": f"Zone Privacy E2E {suffix}",
        "email": email, "password": PASSWORD, "display_name": "Owner",
    })
    token, tenant_id = auth["access_token"], auth["tenant_id"]
    print(f"    tenant {tenant_id}")

    step(2, "Create a site, two real zones (standard, high), a camera, and two rules")
    _, site = _post("/api/v1/tenant/sites", {
        "name": "Depot", "code": f"depot-{suffix}", "timezone": "UTC",
    }, token, expect=(201,))
    site_id = site["id"]

    _, standard_zone = _post("/api/v1/tenant/zones", {
        "site_id": site_id, "name": "Standard Zone", "zone_type": "general",
        "privacy_level": "standard", "polygon": ZONE_POLYGON,
    }, token, expect=(201,))
    check(standard_zone["privacy_level"] == "standard", "standard zone really stored privacy_level=standard", failures)

    _, high_zone = _post("/api/v1/tenant/zones", {
        "site_id": site_id, "name": "High Privacy Zone", "zone_type": "general",
        "privacy_level": "high", "polygon": ZONE_POLYGON,
    }, token, expect=(201,))
    check(high_zone["privacy_level"] == "high", "high zone really stored privacy_level=high", failures)

    _, standard_camera = _post("/api/v1/tenant/cameras", {
        "site_id": site_id, "name": "Standard Cam", "code": f"std-cam-{suffix}",
    }, token, expect=(201,))
    _, high_camera = _post("/api/v1/tenant/cameras", {
        "site_id": site_id, "name": "High Privacy Cam", "code": f"high-cam-{suffix}",
    }, token, expect=(201,))

    _, standard_rule = _post("/api/v1/tenant/rules", {
        "site_id": site_id, "camera_id": standard_camera["id"], "zone_id": standard_zone["id"],
        "name": "Standard watch", "type_code": "zone.intrusion", "alertable_classes": ["person"],
        "min_confidence": 0.1, "min_roi_overlap": 0.1,
    }, token, expect=(201,))
    _, high_rule = _post("/api/v1/tenant/rules", {
        "site_id": site_id, "camera_id": high_camera["id"], "zone_id": high_zone["id"],
        "name": "High privacy watch", "type_code": "zone.intrusion", "alertable_classes": ["person"],
        "min_confidence": 0.1, "min_roi_overlap": 0.1,
    }, token, expect=(201,))
    print(f"    site {site_id[:8]}  standard zone {standard_zone['id'][:8]}  high zone {high_zone['id'][:8]}")

    step(3, "Feed a real synthetic frame (known per-pixel noise means) through the real ingest_detection() for each zone")
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend", "shared"))
    import asyncio

    import numpy as np
    from csense_shared.pipeline.ingest import ingest_detection
    from csense_shared.pipeline.rules import DetectedObject
    from csense_shared.storage.objects import create_client
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    height, width = FRAME_SIZE
    rng = np.random.default_rng(NOISE_SEED)

    def _noisy_fill(shape: tuple[int, int], mean: tuple[int, int, int]) -> np.ndarray:
        # Uniform noise around `mean`, clipped to a valid byte range - never a flat
        # fill. A region's *mean* colour still identifies it (zone vs. outside); whether
        # it was blurred afterward is a variance question, checked separately below.
        noise = rng.integers(-40, 41, size=(*shape, 3))
        return np.clip(np.array(mean) + noise, 0, 255).astype(np.uint8)

    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :] = _noisy_fill((height, width), OUTSIDE_MEAN)
    frame[:, : width // 2] = _noisy_fill((height, width // 2), ZONE_MEAN)  # left half = the zone's own polygon area

    # A person detection fully inside the zone (well within its own bbox), matching
    # min_roi_overlap - real coordinates, not the whole frame, so the rule genuinely has
    # to check overlap rather than firing on anything at all.
    person = DetectedObject("person", 0.9, (0.05, 0.2, 0.35, 0.9))

    pg_password = _read_env("POSTGRES_PASSWORD")

    class _MinioSettings:
        minio_endpoint = "localhost:9000"
        minio_root_user = _read_env("MINIO_ROOT_USER")
        minio_root_password = _read_env("MINIO_ROOT_PASSWORD")
        minio_use_tls = False

    async def run_ingest(camera_id: str, site_id: str) -> object:
        engine = create_async_engine(f"postgresql+asyncpg://csense_app:{pg_password}@localhost:5432/csense")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        minio = create_client(_MinioSettings())

        async with factory() as session, session.begin():
            await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            result = await ingest_detection(
                session,
                minio,
                tenant_id=uuid.UUID(tenant_id),
                site_id=uuid.UUID(site_id),
                camera_id=uuid.UUID(camera_id),
                source_event_id=f"zone-privacy-{suffix}-{camera_id[:8]}",
                captured_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
                objects=[person],
                frame=frame,
            )
        await engine.dispose()
        return result

    standard_result = asyncio.run(run_ingest(standard_camera["id"], site_id))
    high_result = asyncio.run(run_ingest(high_camera["id"], site_id))
    check(standard_result.incident_created and standard_result.evidence_captured, "standard-zone incident opened with evidence", failures)
    check(high_result.incident_created and high_result.evidence_captured, "high-zone incident opened with evidence", failures)

    step(4, "Fetch the real stored evidence bytes back from MinIO and check the actual pixels")
    import cv2

    minio = create_client(_MinioSettings())

    def _fetch_evidence_image(detection_id: str, variant: str) -> np.ndarray:
        row = _in_postgres(
            f"SET app.is_platform = true; "
            f"SELECT so.bucket, so.object_key FROM evidence e "
            f"JOIN stored_objects so ON so.id = e.object_id "
            f"WHERE e.detection_id = '{detection_id}' AND e.privacy_variant = '{variant}' "
            f"LIMIT 1;"
        )
        bucket, key = row.strip().split("|")
        response = minio.get_object(bucket, key)
        try:
            payload = response.read()
        finally:
            response.close()
            response.release_conn()
        return cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)

    # The zone patch as it was actually generated, before any capture/storage/JPEG
    # round-trip - the real reference for "how noisy was this region originally."
    def _zone_patch(image: np.ndarray) -> np.ndarray:
        # Sample a patch well inside the zone (not at its edge, where JPEG compression
        # alone can introduce a few units of drift even on an untouched region).
        return image[height // 2 - 20 : height // 2 + 20, width // 4 - 20 : width // 4 + 20]

    def _patch_spatial_variance(patch: np.ndarray) -> float:
        # Variance PER CHANNEL over spatial position, then averaged - not var() over the
        # flattened B/G/R array. ZONE_MEAN's channels (10, 10, 220) differ hugely from
        # each other, so a flattened variance is dominated by that fixed between-channel
        # gap (unaffected by blur) and buries the actual spatial-noise signal blurring
        # changes.
        return float(patch.reshape(-1, 3).astype(float).var(axis=0).mean())

    reference_variance = _patch_spatial_variance(
        frame[height // 2 - 20 : height // 2 + 20, width // 4 - 20 : width // 4 + 20]
    )

    def _zone_region_was_blurred(image: np.ndarray) -> bool:
        # A flat colour blurs to itself (nothing for a weighted local average to
        # change) - the real, checkable signal for "was this actually blurred" is
        # variance reduction, not a colour match. Gaussian blur of real per-pixel
        # noise measurably lowers variance; an untouched noisy region does not.
        #
        # The threshold has to clear a real, measured floor: JPEG's own lossy
        # round-trip alone (no blur at all) reduces this patch's variance to ~45% of
        # the source noise's (quantisation smooths high-frequency noise even
        # untouched) - confirmed empirically against encode_jpeg() directly. A real
        # Gaussian blur at this codebase's kernel size crushes it under 1%. 20% sits
        # well clear of both real signals.
        patch_variance = _patch_spatial_variance(_zone_patch(image))
        return patch_variance < reference_variance * 0.2

    standard_original = _fetch_evidence_image(standard_result.detection_id, "original")
    check(
        not _zone_region_was_blurred(standard_original),
        "standard zone: original evidence is genuinely unmasked (zone noise variance unchanged)", failures,
    )

    high_original = _fetch_evidence_image(high_result.detection_id, "original")
    high_masked = _fetch_evidence_image(high_result.detection_id, "masked")
    check(
        _zone_region_was_blurred(high_original),
        "high zone: 'original' evidence is NOT the raw noisy frame - it was never stored", failures,
    )
    check(
        np.array_equal(high_original, high_masked),
        "high zone: 'original' evidence is pixel-identical to 'masked' (same object, not a coincidence)", failures,
    )

    # Real, direct DB check: original and masked point at the same stored object.
    object_ids = _in_postgres(
        f"SET app.is_platform = true; "
        f"SELECT string_agg(DISTINCT object_id::text, ',') FROM evidence "
        f"WHERE detection_id = '{high_result.detection_id}' AND privacy_variant IN ('original','masked');"
    )
    check(
        "," not in object_ids and object_ids.strip() != "",
        f"high zone: original and masked evidence rows share one object_id ({object_ids.strip()[:8]}...)", failures,
    )

    step(5, "The real presigned-download read path still returns a URL for every variant")
    listing = _get(f"/api/v1/tenant/detections?limit=5&camera_id={high_camera['id']}&with_evidence_only=true", token)
    check(bool(listing["items"]), "high-zone detection is listed with evidence", failures)
    if listing["items"]:
        variants_with_urls = {e["variant"]: bool(e["url"]) for e in listing["items"][0]["evidence"]}
        check(
            variants_with_urls.get("original") is True,
            f"a real URL is still returned for the 'original' variant even though it has no unmasked bytes behind it ({variants_with_urls})",
            failures,
        )

    step(6, "Clean up")
    tid = _in_postgres(f"SET app.is_platform = true; SELECT tenant_id FROM memberships m JOIN users u ON u.id = m.user_id WHERE u.email_normalized = '{email}' LIMIT 1;")
    if tid:
        _in_postgres(f"SET app.is_platform = true; DELETE FROM tenants WHERE id = '{tid}';")
    _in_postgres(f"SET app.is_platform = true; DELETE FROM organizations WHERE display_name = 'Zone Privacy E2E {suffix}';")
    _in_postgres(f"SET app.is_platform = true; DELETE FROM users WHERE email_normalized = '{email}';")
    print("    test tenant and user removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - zone-privacy-level gating verified against real pixels in real storage:")
    print("       standard zones stay genuinely unmasked; high zones never store an unmasked frame at all.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
