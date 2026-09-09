"""Runs a real golden dataset through each of the 3 intern-trained models and records a
real `model_validation_runs` row per model - the same TRD §15.2 gates 2-4 pattern
`run_model_validation.py` established for `license-plate-detector`, generalised for two
things that script didn't need: (1) multi-class ground truth (per-image, per-class
present/absent/excluded, not one boolean), and (2) addressing the model by `version_id`
rather than by name, because these 3 models start in `uploaded` state - not yet
deployable - so the by-name `/internal/v1/infer` route (deployable-states only) cannot
reach them. `POST /internal/v1/validate-infer` (added alongside this script) is the
narrow, version_id-addressed path built for exactly this: real TRD gate 2-4 evidence has
to be producible *before* a version is promoted, not only re-producible after.

Golden set: `backend/tests/fixtures/golden/<model>/` - 8 real, hand-verified images shared
across all 3 manifests (7 pulled live from the real onboarded customer's Autotek NVR, 1 a
stock photo already reused as an evidence placeholder in this platform's own MinIO). See
each manifest's own `note` field for full provenance and for exactly which classes have no
real positive example and are therefore never scored for recall - only false-positive rate.
This is a genuinely thinner evidence base than license-plate-detector's (which had both
real positives and negatives); the thinness itself is recorded in every run's metrics
(`recall: null` wherever no positive example exists), not hidden by a substitute number.

Threshold: `max_false_positive_rate=0.0` for every class here (deliberately, not this
project's general 0.5 default) - chosen because there is zero recall evidence to weigh a
looser false-positive tolerance against for any of these 3 models, and two of the three
(child/adult, abuse) are exactly the kind of alarm class where a false trigger on
provably-calm footage is itself a real, disqualifying finding rather than noise to average
away. `recall` is never scored against a threshold - it stays `null` and is reported as
such, per class, whenever (as here, always) no positive ground truth exists.

    python scripts/run_intern_model_validation.py
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
GOLDEN_ROOT = os.path.join(REPO, "backend", "tests", "fixtures", "golden")
CONFIDENCE = "0.25"
SUITE_VERSION = "intern-golden-v1"
RUNNER_VERSION = "run_intern_model_validation.py/1"
MAX_FALSE_POSITIVE_RATE = 0.0

ADMIN_API = "http://localhost:8080"
SERVICE_EMAIL = "validation-runner@platform.internal"
SERVICE_PASSWORD = "ValidationRunner!ServiceAccount123"

MODELS = ("intern-child-adult-detection", "intern-abuse-detection", "intern-classroom-hazard-detection")

_INFER_SCRIPT = r"""
import sys, urllib.request, urllib.error

version_id, confidence = sys.argv[1], sys.argv[2]
data = sys.stdin.buffer.read()
boundary = "validationharnessboundary"

def field(name, value):
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
    ).encode()

body = (
    field("version_id", version_id)
    + field("confidence", confidence)
    + f'--{boundary}\r\nContent-Disposition: form-data; name="frame"; filename="f.jpg"\r\n'
      f"Content-Type: image/jpeg\r\n\r\n".encode()
    + data
    + f"\r\n--{boundary}--\r\n".encode()
)
req = urllib.request.Request(
    "http://localhost:8000/internal/v1/validate-infer",
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


def infer(image_bytes: bytes, version_id: str, confidence: str) -> dict:
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "ai-runtime",
         "python3", "-c", _INFER_SCRIPT, version_id, confidence],
        cwd=os.path.join(REPO, "infra"),
        input=image_bytes, capture_output=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"inference failed: {result.stderr.decode()[:500]}")
    return json.loads(result.stdout.decode())


def compute_class_metrics(per_image: list[dict], class_name: str, *, max_fpr: float):
    """Same `None`-not-0.0 discipline as `run_model_validation.py::compute_metrics` - a
    class with no positive (or no negative) ground truth reports `recall`/
    `false_positive_rate` as `None`, not a misleadingly-confident number. Images marked
    `null` for this class in the manifest (genuinely ambiguous ground truth) are excluded
    entirely, counted in neither positives nor negatives."""
    scored = [r for r in per_image if r["ground_truth"].get(class_name) is not None]
    positives = [r for r in scored if r["ground_truth"][class_name]]
    negatives = [r for r in scored if not r["ground_truth"][class_name]]
    recall = (
        sum(class_name in r["detected_classes"] for r in positives) / len(positives)
        if positives else None
    )
    fpr = (
        sum(class_name in r["detected_classes"] for r in negatives) / len(negatives)
        if negatives else None
    )
    passed = fpr is None or fpr <= max_fpr
    return {
        "recall": round(recall, 4) if recall is not None else None,
        "false_positive_rate": round(fpr, 4) if fpr is not None else None,
        "positive_count": len(positives),
        "negative_count": len(negatives),
        "excluded_count": len(per_image) - len(scored),
    }, passed


def bootstrap_service_account(settings) -> None:
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


def run_one_model(model_name: str, version_id: str, token: str, settings) -> dict:
    golden_dir = os.path.join(GOLDEN_ROOT, model_name)
    with open(os.path.join(golden_dir, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)

    print(f"\n[{model_name}] running {len(manifest['images'])} golden frames at confidence {CONFIDENCE}")
    per_image = []
    for entry in manifest["images"]:
        path = os.path.join(golden_dir, entry["file"])
        with open(path, "rb") as f:
            image_bytes = f.read()
        started = time.monotonic()
        result = infer(image_bytes, version_id, CONFIDENCE)
        elapsed_ms = result.get("inference_ms", (time.monotonic() - started) * 1000)

        detected_classes = sorted({d["class_name"] for d in result["detections"]})
        print(f"    {entry['file'][:35]:35s} truth={entry['present']}  detected={detected_classes}  {elapsed_ms:.0f}ms")

        per_image.append({
            "file": entry["file"], "ground_truth": entry["present"],
            "detected_classes": detected_classes, "inference_ms": round(elapsed_ms, 2),
        })

    per_class = {}
    overall_passed = True
    for class_name in manifest["classes"]:
        metrics, passed = compute_class_metrics(per_image, class_name, max_fpr=MAX_FALSE_POSITIVE_RATE)
        per_class[class_name] = metrics
        overall_passed = overall_passed and passed

    status = "passed" if overall_passed else "failed"
    latencies = sorted(r["inference_ms"] for r in per_image)
    mean_ms = sum(latencies) / len(latencies)

    print(f"  -> {status}: " + ", ".join(
        f"{c}(recall={m['recall']}, fpr={m['false_positive_rate']})" for c, m in per_class.items()
    ))

    report = {
        "model_name": model_name, "version_id": version_id, "suite_version": SUITE_VERSION,
        "confidence": CONFIDENCE, "status": status, "per_class": per_class,
        "thresholds": {"max_false_positive_rate": MAX_FALSE_POSITIVE_RATE, "min_recall": None},
        "mean_inference_ms": round(mean_ms, 2), "per_image": per_image,
        "runner_version": RUNNER_VERSION,
    }
    report_bytes = json.dumps(report, indent=2).encode("utf-8")
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    run_id = uuid.uuid4()

    minio_client = Minio(
        "localhost:9000", access_key=settings.minio_root_user,
        secret_key=settings.minio_root_password, secure=False,
    )
    report_key = f"global/models/validation-runs/{version_id}/{run_id}.json"
    minio_client.put_object(
        "csense-models", report_key, io.BytesIO(report_bytes), length=len(report_bytes),
        content_type="application/json",
    )

    _, run = admin_api(
        f"/api/v1/admin/model-versions/{version_id}/validation-runs",
        {
            "suite_version": SUITE_VERSION, "environment": "local-docker-compose",
            "status": status, "metrics": {"per_class": per_class, "mean_inference_ms": report["mean_inference_ms"]},
            "thresholds": report["thresholds"],
            "result_object_key": report_key, "result_sha256": report_sha256,
            "result_size_bytes": len(report_bytes), "runner_version": RUNNER_VERSION,
        },
        token=token, method="POST",
    )
    print(f"    recorded model_validation_runs row {run['id']} ({report_key})")
    return report


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    settings = get_settings()

    bootstrap_service_account(settings)
    _, auth = admin_api("/api/v1/admin/auth/login", {"email": SERVICE_EMAIL, "password": SERVICE_PASSWORD}, method="POST")
    token = auth["access_token"]

    _, models = admin_api("/api/v1/admin/models", token=token)
    reports = []
    for model_name in MODELS:
        matches = [m for m in models if m["model_name"] == model_name]
        if not matches:
            raise RuntimeError(f"No registered version found for '{model_name}'.")
        reports.append(run_one_model(model_name, matches[0]["id"], token, settings))

    print("\n=== summary ===")
    for r in reports:
        print(f"{r['model_name']}: {r['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
