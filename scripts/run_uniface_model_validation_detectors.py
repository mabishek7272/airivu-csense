"""Real gate 2-4 validation for the 3 uniface-zoo face detectors - blazeface, centerface,
retinaface - against the real golden set, through the REAL deployed HTTP route.

How this differs from its two predecessors, and why:

  * run_intern_model_validation.py calls the live HTTP API from the host, because the models
    it validates are reachable through a routed service.
  * run_uniface_model_validation_lowrisk.py / _crosscheck.py could NOT do that: at the time
    they ran, the ai-runtime image predated the code under test, so they imported engines.py
    directly (in-container throwaway path / a local venv) and said so plainly in every
    recorded run's own `method_note`.
  * This script runs the real `/internal/v1/validate-infer` HTTP endpoint, with the code
    under test actually deployed in the container serving it. ai-runtime has no Traefik
    route by design (see infra/docker-compose.yml - raw frames in, raw detections out, no
    tenant scoping of its own), so there is no host port to call and the HTTP client runs
    inside the container: scripts/_uniface_detector_validate_in_container.py.

    `--runtime-container` selects which container serves it. The default is the compose
    service; point it at a sidecar built from the same image with a candidate engines.py
    when validating code that is not yet in the deployed image, and the recorded run says
    which was used - never silently.

Grading comes from each model's own manifest under backend/tests/fixtures/golden/. Recall is
scored by IoU against the platform's own already-`production` SCRFD detector on the same
frame (the best real reference available in this repo - not a claim that SCRFD is truth);
false positives are scored on real Autotek NVR frames with no face in them, which is what
makes the FPR a measurement rather than an untested zero.

Usage:
    python scripts/run_uniface_model_validation_detectors.py [--runtime-container NAME]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import uuid

from minio import Minio

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_ROOT = os.path.join(REPO, "backend", "tests", "fixtures", "golden")
IN_CONTAINER_SCRIPT = os.path.join(REPO, "scripts", "_uniface_detector_validate_in_container.py")

SUITE_VERSION = "uniface-golden-v1"
RUNNER_VERSION = "run_uniface_model_validation_detectors.py/1"

ADMIN_API = "http://localhost:8080"
SERVICE_EMAIL = "validation-runner@platform.internal"
SERVICE_PASSWORD = "ValidationRunner!ServiceAccount123"  # pre-existing service account,
# created by run_intern_model_validation.py's own bootstrap_service_account() - reused
# as-is, same as run_uniface_model_validation_crosscheck.py does.

DETECTOR_MODELS = (
    "uniface-blazeface-detect",
    "uniface-centerface-detect",
    "uniface-retinaface-detect",
)
SCRFD_MODEL = "insightface-buffalo-l-detect"

CONFIDENCE = 0.5
CROP_FACTOR = 3.5
SWEEP_FACTORS = [2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0]

CONTAINER_WORKDIR = "/tmp/uniface_detector_validate"


def _docker_env(container: str, key: str) -> str:
    out = subprocess.run(
        ["docker", "exec", container, "printenv", key], capture_output=True, check=True,
    )
    return out.stdout.decode().strip()


def _minio_client() -> Minio:
    return Minio(
        "localhost:9000",
        access_key=_docker_env("csense-minio-1", "MINIO_ROOT_USER"),
        secret_key=_docker_env("csense-minio-1", "MINIO_ROOT_PASSWORD"),
        secure=False,
    )


def admin_api(path, payload=None, token=None, method="GET"):
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "application/json", "Host": "console.localhost"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{ADMIN_API}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        return exc.code, json.loads(body) if body else {}


def run_in_container(container: str, job: dict) -> dict:
    """Copies the harness + golden images into the container, runs it, reads the report."""
    subprocess.run(["docker", "exec", container, "mkdir", "-p", CONTAINER_WORKDIR], check=True)
    subprocess.run(
        ["docker", "cp", IN_CONTAINER_SCRIPT, f"{container}:{CONTAINER_WORKDIR}/harness.py"],
        check=True,
    )
    for local_path, container_path in job.pop("_files"):
        subprocess.run(["docker", "cp", local_path, f"{container}:{container_path}"], check=True)

    job_bytes = json.dumps(job, indent=2).encode()
    job_local = os.path.join("/tmp", f"detector_job_{uuid.uuid4().hex}.json")
    with open(job_local, "wb") as fh:
        fh.write(job_bytes)
    subprocess.run(
        ["docker", "cp", job_local, f"{container}:{CONTAINER_WORKDIR}/job.json"], check=True
    )
    os.unlink(job_local)

    result = subprocess.run(
        ["docker", "exec", container, "python",
         f"{CONTAINER_WORKDIR}/harness.py", f"{CONTAINER_WORKDIR}/job.json"],
        capture_output=True, check=False,
    )
    sys.stderr.write(result.stderr.decode())
    if result.returncode != 0:
        raise RuntimeError(f"in-container harness failed ({result.returncode})")

    out = subprocess.run(
        ["docker", "exec", container, "cat", job["report_path"]],
        capture_output=True, check=True,
    )
    return json.loads(out.stdout.decode())


def score(model_name: str, manifest: dict, result: dict) -> tuple[str, dict, dict]:
    """Applies the manifest's own tolerances. Returns (status, metrics, thresholds)."""
    tol = manifest["tolerance"]
    min_iou = tol["min_iou_vs_scrfd"]
    max_fpr = tol["max_false_positive_rate"]

    expected_faces = 0
    matched_faces = 0
    negative_frames = 0
    false_positive_frames = 0
    false_positive_boxes = 0
    ious: list[float] = []

    for image in result["images"]:
        if image["expected_face_count"] == 0:
            negative_frames += 1
            fps = image.get("false_positives", 0)
            false_positive_boxes += fps
            if fps:
                false_positive_frames += 1
            continue
        for face in image.get("per_face", []):
            expected_faces += 1
            ious.append(face["best_iou_vs_scrfd"])
            if face["best_iou_vs_scrfd"] >= min_iou:
                matched_faces += 1

    recall = (matched_faces / expected_faces) if expected_faces else None
    fpr = (false_positive_frames / negative_frames) if negative_frames else None

    metrics = {
        "graded_condition": manifest["images"][0].get("grade_on", "full_frame"),
        "expected_faces": expected_faces,
        "matched_faces": matched_faces,
        "recall_vs_scrfd": recall,
        "mean_iou_vs_scrfd": round(sum(ious) / len(ious), 4) if ious else None,
        "min_iou_vs_scrfd": round(min(ious), 4) if ious else None,
        "negative_frames": negative_frames,
        "false_positive_frames": false_positive_frames,
        "false_positive_boxes": false_positive_boxes,
        "false_positive_rate": fpr,
        "landmark_accuracy": None,
    }
    thresholds = {
        "min_iou_vs_scrfd": min_iou,
        "max_false_positive_rate": max_fpr,
        "confidence": CONFIDENCE,
    }

    passed = (
        recall == 1.0
        and (fpr is not None and fpr <= max_fpr)
    )
    return ("passed" if passed else "failed"), metrics, thresholds


def record_run(minio_client, token, model_name, version_id, status, metrics, thresholds,
               per_image, method_note):
    report = {
        "model_name": model_name, "version_id": version_id, "suite_version": SUITE_VERSION,
        "status": status, "metrics": metrics, "thresholds": thresholds,
        "per_image": per_image, "runner_version": RUNNER_VERSION,
        "method_note": method_note,
    }
    report_bytes = json.dumps(report, indent=2, default=str).encode("utf-8")
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    run_id = uuid.uuid4()
    report_key = f"global/models/validation-runs/{version_id}/{run_id}.json"
    minio_client.put_object(
        "csense-models", report_key, io.BytesIO(report_bytes), length=len(report_bytes),
        content_type="application/json",
    )
    metrics_with_note = dict(metrics)
    metrics_with_note["method_note"] = method_note
    status_code, run = admin_api(
        f"/api/v1/admin/model-versions/{version_id}/validation-runs",
        {
            "suite_version": SUITE_VERSION, "environment": "local-docker-http",
            "status": status, "metrics": metrics_with_note, "thresholds": thresholds,
            "result_object_key": report_key, "result_sha256": report_sha256,
            "result_size_bytes": len(report_bytes), "runner_version": RUNNER_VERSION,
        },
        token=token, method="POST",
    )
    if status_code != 201:
        raise RuntimeError(f"validation-runs POST failed: {status_code} {run}")
    print(f"    recorded model_validation_runs row {run['id']} ({report_key})")
    return run


def promote(token, version_id, target_state, reason):
    return admin_api(
        f"/api/v1/admin/model-versions/{version_id}/promote",
        {"target_state": target_state, "reason": reason},
        token=token, method="POST",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--runtime-container", default="csense-ai-runtime-1",
        help="Container serving /internal/v1/validate-infer. Point at a sidecar built from "
             "the same image when the deployed image predates the code under test.",
    )
    args = parser.parse_args()
    container = args.runtime_container

    method_note = (
        f"Real HTTP /internal/v1/validate-infer, served by container '{container}'. The "
        "HTTP client runs inside that container because ai-runtime has no Traefik route by "
        "design (infra/docker-compose.yml, TRD S16), so there is no host port to call. "
        "Recall is scored by IoU against the platform's own production SCRFD detector on "
        "the same frame; false positives are scored on real Autotek NVR frames containing "
        "no face. Landmark accuracy is NOT scored - no hand-labelled ground truth exists "
        "in this repo."
    )

    _, auth = admin_api(
        "/api/v1/admin/auth/login", {"email": SERVICE_EMAIL, "password": SERVICE_PASSWORD},
        method="POST",
    )
    token = auth.get("access_token")
    if not token:
        raise RuntimeError(f"admin login failed: {auth}")

    _, versions = admin_api("/api/v1/admin/models", token=token)
    by_name = {}
    for row in versions:
        by_name.setdefault(row["model_name"], row)

    scrfd_version = by_name[SCRFD_MODEL]["id"]
    minio_client = _minio_client()

    job = {
        "scrfd_version_id": scrfd_version,
        "confidence": CONFIDENCE,
        "crop_factor": CROP_FACTOR,
        "report_path": f"{CONTAINER_WORKDIR}/report.json",
        "models": {},
        "_files": [],
    }
    manifests = {}
    for model_name in DETECTOR_MODELS:
        golden_dir = os.path.join(GOLDEN_ROOT, model_name)
        with open(os.path.join(golden_dir, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        manifests[model_name] = manifest
        images = []
        for item in manifest["images"]:
            local = os.path.join(golden_dir, item["file"])
            remote = f"{CONTAINER_WORKDIR}/{model_name}__{item['file']}"
            job["_files"].append((local, remote))
            images.append({
                "path": remote,
                "expected_face_count": item["expected_face_count"],
                "grade_on": item.get("grade_on", "full_frame"),
            })
        job["models"][model_name] = {
            "version_id": by_name[model_name]["id"], "images": images,
        }

    # The sweep reuses an image already staged into the container by the loop above, so it
    # needs no extra `_files` entry - just the container-side path.
    job["sweep"] = {
        "path": f"{CONTAINER_WORKDIR}/uniface-retinaface-detect__stock_streetscene_3adults.jpg",
        "factors": SWEEP_FACTORS,
    }

    print(f"Running the real validate-infer route inside '{container}'...")
    report = run_in_container(container, job)

    exit_code = 0
    for model_name in DETECTOR_MODELS:
        result = report["models"][model_name]
        version_id = result["version_id"]
        status, metrics, thresholds = score(model_name, manifests[model_name], result)
        print(f"\n[{model_name}] version {version_id}: {status.upper()}")
        print(f"    recall_vs_scrfd={metrics['recall_vs_scrfd']} "
              f"mean_iou={metrics['mean_iou_vs_scrfd']} "
              f"fpr={metrics['false_positive_rate']} "
              f"({metrics['false_positive_boxes']} boxes on "
              f"{metrics['negative_frames']} face-free frames)")

        record_run(
            minio_client, token, model_name, version_id, status, metrics, thresholds,
            result["images"], method_note,
        )

        code, body = promote(
            token, version_id, "validating", f"{SUITE_VERSION} run recorded: {status}"
        )
        if code == 409 and "to 'validating'" in str(body.get("message", "")):
            # Already there from an earlier run of this suite. Re-running the same
            # validation must not read as a failure - the state is what we wanted.
            print("    promote -> validating: already validating (no-op)")
        else:
            print(f"    promote -> validating: {code} {body.get('state', body)}")
        if status == "passed":
            code, body = promote(
                token, version_id, "validated", f"{SUITE_VERSION} passed all graded checks"
            )
            print(f"    promote -> validated: {code} "
                  f"{body.get('state', body.get('code', body))}")
            if code != 200:
                # Expected for these 3: all are access_classification=biometric, and
                # `validated` is a DEPLOYABLE_STATE, so the pre-existing biometric-
                # acknowledgement gate refuses it without the owner's explicit sign-off.
                # That is the gate working, not a failure of this run.
                print("      (biometric acknowledgement gate - owner's call, not forced here)")
        else:
            exit_code = 1

    sweep = report.get("detection_range_sweep")
    if sweep:
        print("\nDetection-range sweep (does each model still find the same real face as it "
              "gets smaller in frame?):")
        for row in sweep:
            flags = " ".join(
                f"{name.split('-')[1][:10]}={'Y' if v['found_real_face'] else 'n'}"
                f"({v['confidence']})"
                for name, v in row["models"].items()
            )
            print(f"    face={row['face_fraction_of_crop_width']:.4f} of width: {flags}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
