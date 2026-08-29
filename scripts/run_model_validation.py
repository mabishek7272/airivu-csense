"""Runs a golden dataset through a real model and records a real `model_validation_runs`
row - the first one this platform has ever produced (TRD §15.2 gates 2-4: load/shape
compatibility, golden dataset functional tests, accuracy against declared thresholds -
gates 1 and 5-9, malware scanning through signed release manifests, are not this pass; see
CHECKLIST.md).

**Reference use case: `license-plate-detector`.** Ground truth (`backend/tests/fixtures/
golden/license-plate-detector/manifest.json`) is a small, hand-verified set - 10 real
frames, each actually looked at and labelled `has_plate: true/false` by inspection, not
inferred from which curated-review folder it came from (two came from unrelated folders
and turned out to show a plate anyway). This is presence ground truth, not bounding boxes:
producing pixel-accurate boxes by hand isn't something to claim confidence in, so the
metric is presence recall/false-positive-rate instead of IoU.

Runs at confidence 0.03 - this deployment's own established operating point for this model
(`scripts/build_demo_assets.py`'s `BLUR_CONFIDENCE`, chosen because a real plate at typical
camera distance scored as low as 0.06 and would have been missed at a conventional 0.4).
The same degenerate-giant-box filter that script uses (`MAX_PLATE_AREA_FRACTION`) is reused
here too, so a detection this deployment would discard as an export artifact doesn't get
counted as a true or false positive either.

Default thresholds (`--min-recall 0.8 --max-false-positive-rate 0.5`) reflect that this is
a deliberately recall-favouring operating point, not a balanced classifier - the demo site
work already established the reasoning: missing a real plate is a privacy failure, a false
trigger on a frame with no plate is not. Override either on the command line; nothing here
is hardcoded to make a result look better than it measured.

Inference reuses `build_demo_assets.py`'s own established mechanism (ai-runtime is
deliberately unreachable from the host - `docker compose exec`, frame bytes over stdin).
The report is uploaded to MinIO directly - published on `localhost:9000`, unlike ai-runtime
- then recorded through the real Admin API (`POST .../validation-runs`), authenticated as
a persistent bootstrapped service account (`validation-runner@platform.internal`, created
idempotently - not a throwaway e2e identity, since this produces real evidence a real
operator should be able to attribute later).

Exits 0 whenever the run completed and was recorded, whatever its `passed`/`failed`
status - that is a legitimate, informative outcome, not a script failure. Exits 1 only for
an infrastructure problem (can't reach ai-runtime/MinIO/the API at all).

    python scripts/run_model_validation.py [--min-recall 0.8] [--max-false-positive-rate 0.5]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import psycopg
from csense_shared.config import get_settings
from csense_shared.security.passwords import hash_password
from minio import Minio

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_DIR = os.path.join(REPO, "backend", "tests", "fixtures", "golden", "license-plate-detector")
MODEL_NAME = "license-plate-detector"
CONFIDENCE = "0.03"
MAX_PLATE_AREA_FRACTION = 0.12
SUITE_VERSION = "golden-v1"
RUNNER_VERSION = "run_model_validation.py/1"

ADMIN_API = "http://localhost:8080"
SERVICE_EMAIL = "validation-runner@platform.internal"
SERVICE_PASSWORD = "ValidationRunner!ServiceAccount123"

# Same inference-calling mechanism as build_demo_assets.py - not reinvented.
_INFER_SCRIPT = r"""
import sys, urllib.request, urllib.error

model_name, confidence = sys.argv[1], sys.argv[2]
data = sys.stdin.buffer.read()
boundary = "validationharnessboundary"

def field(name, value):
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
    ).encode()

body = (
    field("model_name", model_name)
    + field("confidence", confidence)
    + f'--{boundary}\r\nContent-Disposition: form-data; name="frame"; filename="f.jpg"\r\n'
      f"Content-Type: image/jpeg\r\n\r\n".encode()
    + data
    + f"\r\n--{boundary}--\r\n".encode()
)
req = urllib.request.Request(
    "http://localhost:8000/internal/v1/infer",
    data=body,
    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    method="POST",
)
try:
    resp = urllib.request.urlopen(req, timeout=30)
    sys.stdout.write(resp.read().decode())
except urllib.error.HTTPError as exc:
    sys.stderr.write(f"{exc.code} {exc.read().decode()}")
    sys.exit(1)
"""


def infer(image_bytes: bytes, model_name: str, confidence: str) -> dict:
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "ai-runtime",
         "python3", "-c", _INFER_SCRIPT, model_name, confidence],
        cwd=os.path.join(REPO, "infra"),
        input=image_bytes, capture_output=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"inference failed: {result.stderr.decode()[:300]}")
    return json.loads(result.stdout.decode())


def _box_area_fraction(bbox) -> float:
    x1, y1, x2, y2 = bbox
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def compute_metrics(per_image: list[dict], *, min_recall: float, max_false_positive_rate: float):
    """Pure - no I/O, no side effects - so this is what `test_model_validation.py`
    actually pins, rather than anything needing a live server. `per_image` entries need
    only `has_plate`, `detected`, `inference_ms`.

    Returns `(metrics, thresholds, status)`. `recall`/`false_positive_rate` are `None`
    (not 0.0 or 1.0) when there are no positive/negative examples to measure against -
    silently defaulting either way would misrepresent a threshold nothing was actually
    checked against.
    """
    positives = [r for r in per_image if r["has_plate"]]
    negatives = [r for r in per_image if not r["has_plate"]]
    recall = sum(r["detected"] for r in positives) / len(positives) if positives else None
    fpr = sum(r["detected"] for r in negatives) / len(negatives) if negatives else None
    latencies = sorted(r["inference_ms"] for r in per_image)
    mean_ms = sum(latencies) / len(latencies)
    p95_ms = latencies[int(len(latencies) * 0.95)] if len(latencies) > 1 else latencies[0]

    passed = (
        (recall is None or recall >= min_recall)
        and (fpr is None or fpr <= max_false_positive_rate)
    )
    status = "passed" if passed else "failed"

    metrics = {
        "recall": round(recall, 4) if recall is not None else None,
        "false_positive_rate": round(fpr, 4) if fpr is not None else None,
        "mean_inference_ms": round(mean_ms, 2),
        "p95_inference_ms": round(p95_ms, 2),
        "positive_count": len(positives), "negative_count": len(negatives),
        "per_image": per_image,
    }
    thresholds = {"min_recall": min_recall, "max_false_positive_rate": max_false_positive_rate}
    return metrics, thresholds, status


def bootstrap_service_account(settings) -> None:
    """Idempotent - creates `validation-runner@platform.internal` once, mirroring
    `seed_dev_data.py`/`scripts/e2e_model_registry.py`'s own insert sequence (the only
    option: there is no self-service registration for platform accounts). Persistent, not
    deleted after the run - this is a real service identity, not a throwaway test one."""
    dsn = (
        f"host=localhost port=5432 dbname={settings.postgres_db} "
        f"user={settings.postgres_user} password={settings.postgres_password}"
    )
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")
        cur.execute("SELECT id FROM users WHERE email_normalized = %s", (SERVICE_EMAIL,))
        if cur.fetchone() is not None:
            return
        cur.execute(
            "INSERT INTO users (email_normalized, email_display, password_hash, status, display_name) "
            "VALUES (%s, %s, %s, 'active', 'Validation Runner (service account)') RETURNING id",
            (SERVICE_EMAIL, SERVICE_EMAIL, hash_password(SERVICE_PASSWORD, settings)),
        )
        user_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO platform_developers (user_id, status) VALUES (%s, 'active') RETURNING id",
            (user_id,),
        )
        developer_id = cur.fetchone()[0]
        cur.execute(
            "SELECT id FROM roles WHERE tenant_id IS NULL AND name = 'platform_admin' "
            "AND audience = 'platform'"
        )
        role_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO platform_role_assignments (platform_developer_id, role_id, status) "
            "VALUES (%s, %s, 'active')",
            (developer_id, role_id),
        )
        conn.commit()
        print(f"    created service account {SERVICE_EMAIL}")


def admin_api(path, payload=None, token=None, method="GET"):
    headers = {"Content-Type": "application/json", "Host": "console.localhost"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{ADMIN_API}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {path} -> {exc.code}: {exc.read().decode()[:400]}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-recall", type=float, default=0.8)
    parser.add_argument("--max-false-positive-rate", type=float, default=0.5)
    args = parser.parse_args()

    settings = get_settings()

    with open(os.path.join(GOLDEN_DIR, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)

    print(f"[1] Running {MODEL_NAME} at confidence {CONFIDENCE} against {len(manifest['images'])} golden frames")
    per_image = []
    for entry in manifest["images"]:
        path = os.path.join(GOLDEN_DIR, entry["file"])
        with open(path, "rb") as f:
            image_bytes = f.read()
        started = time.monotonic()
        result = infer(image_bytes, MODEL_NAME, CONFIDENCE)
        elapsed_ms = result.get("inference_ms", (time.monotonic() - started) * 1000)

        kept = [d for d in result["detections"] if _box_area_fraction(d["bbox"]) <= MAX_PLATE_AREA_FRACTION]
        detected = len(kept) > 0
        max_conf = max((d["confidence"] for d in kept), default=0.0)

        outcome = "TP" if entry["has_plate"] and detected else \
                  "FN" if entry["has_plate"] and not detected else \
                  "FP" if detected else "TN"
        print(f"    {entry['file'][:20]}...  has_plate={entry['has_plate']!s:5}  "
              f"detected={detected!s:5}  {outcome}  {elapsed_ms:.0f}ms")

        per_image.append({
            "file": entry["file"], "has_plate": entry["has_plate"], "detected": detected,
            "detection_count": len(kept), "max_confidence": round(max_conf, 4),
            "inference_ms": round(elapsed_ms, 2), "outcome": outcome,
        })

    metrics, thresholds, status = compute_metrics(
        per_image, min_recall=args.min_recall, max_false_positive_rate=args.max_false_positive_rate
    )

    print(f"\n[2] recall={metrics['recall']}  false_positive_rate={metrics['false_positive_rate']}  "
          f"mean={metrics['mean_inference_ms']:.0f}ms  p95={metrics['p95_inference_ms']:.0f}ms  -> {status}")

    print("\n[3] Uploading the full report to MinIO")
    report = {
        "model_name": MODEL_NAME, "suite_version": SUITE_VERSION, "confidence": CONFIDENCE,
        "status": status, "metrics": metrics, "thresholds": thresholds,
        "runner_version": RUNNER_VERSION,
    }
    report_bytes = json.dumps(report, indent=2).encode("utf-8")
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    run_id = uuid.uuid4()

    minio_client = Minio(
        "localhost:9000", access_key=settings.minio_root_user,
        secret_key=settings.minio_root_password, secure=False,
    )

    print("\n[4] Recording the run through the real Admin API")
    bootstrap_service_account(settings)
    _, auth = admin_api("/api/v1/admin/auth/login", {"email": SERVICE_EMAIL, "password": SERVICE_PASSWORD}, method="POST")
    token = auth["access_token"]

    _, models = admin_api("/api/v1/admin/models", token=token)
    matches = [m for m in models if m["model_name"] == MODEL_NAME]
    if not matches:
        raise RuntimeError(f"No registered version found for '{MODEL_NAME}'.")
    version_id = matches[0]["id"]

    report_key = f"global/models/validation-runs/{version_id}/{run_id}.json"
    minio_client.put_object(
        "csense-models", report_key, io.BytesIO(report_bytes), length=len(report_bytes),
        content_type="application/json",
    )
    print(f"    report uploaded to csense-models/{report_key}")

    _, run = admin_api(
        f"/api/v1/admin/model-versions/{version_id}/validation-runs",
        {
            "suite_version": SUITE_VERSION, "environment": "local-docker-compose",
            "status": status, "metrics": metrics, "thresholds": thresholds,
            "result_object_key": report_key, "result_sha256": report_sha256,
            "result_size_bytes": len(report_bytes), "runner_version": RUNNER_VERSION,
        },
        token=token, method="POST",
    )
    print(f"    recorded model_validation_runs row {run['id']} for {MODEL_NAME} ({version_id})")

    print(f"\n{'PASS' if status == 'passed' else 'RECORDED (did not meet thresholds)'} - "
          f"recall={metrics['recall']}, false_positive_rate={metrics['false_positive_rate']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
