"""Runs the real golden set through the last 2 uniface-zoo models - `uniface-bisenet-
parsing` and `uniface-faceattribnet-attributes` - and records a real
`model_validation_runs` row per model via the real Admin API, the same TRD gate 2-4 pattern
`run_model_validation.py`, `run_intern_model_validation.py` and
`run_uniface_model_validation_crosscheck.py` established.

**Unlike `run_uniface_model_validation_crosscheck.py`, this one goes through the real
deployed HTTP endpoint.** That script had to import `engines.py` directly because the
`ai-runtime` container was running a pre-change image it was not allowed to rebuild. That
constraint no longer applies: `ai-runtime` was rebuilt from this worktree and recreated
before this script was run, so `POST /internal/v1/validate-infer-uniface` genuinely
reaches the new `BiSeNetEngine`/`FaceAttribNetEngine` code inside the real service. The
request is issued from inside the container (the service has no Traefik route by design),
the same `docker compose exec` + `urllib` shape `run_intern_model_validation.py` uses.

**What each model is actually graded on** - see each model's own manifest.json for the
full reasoning, and be clear that neither is an accuracy measurement:

  * `uniface-faceattribnet-attributes`: `sunglasses` and `mask` only, at threshold 0.5.
    Those are the only two of the five attributes a human can assert from this photograph.
    Eye-openness has no assertable ground truth here (both subjects' eyes are occluded by
    sunglasses) and `eyeglasses` has no documented meaning to grade against; both are
    recorded in full and scored on nothing. There is no bare-eyed face anywhere in this
    repository, so `sunglasses` has no negative control and its false-positive rate is
    unmeasured.

  * `uniface-bisenet-parsing`: structural plausibility only - the mask must not be
    degenerate, must contain `skin`, must contain an eye-region class, and must contain
    head context (`hair`/`cloth`/`neck`). Per-pixel accuracy and IoU are unmeasured: there
    is no per-pixel ground truth for these faces and none was fabricated.

    python3 scripts/run_uniface_model_validation_parsing_attrib.py
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import urllib.error
import urllib.request
import uuid

from minio import Minio

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_ROOT = os.path.join(REPO, "backend", "tests", "fixtures", "golden")

SUITE_VERSION = "uniface-golden-v1"
RUNNER_VERSION = "run_uniface_model_validation_parsing_attrib.py/1"
ENVIRONMENT = "local-docker-ai-runtime-http"

ADMIN_API = "http://localhost:8080"
SERVICE_EMAIL = "validation-runner@platform.internal"
SERVICE_PASSWORD = "ValidationRunner!ServiceAccount123"  # pre-existing service account,
# created by run_intern_model_validation.py's own bootstrap earlier in this project's
# history and reused as-is, exactly as run_uniface_model_validation_crosscheck.py does.

MODELS = ("uniface-faceattribnet-attributes", "uniface-bisenet-parsing")
DETECTOR_CONFIDENCE = "0.4"
MATCH_TOLERANCE_PX = 60

EYE_CLASSES = ("l_eye", "r_eye", "eye_g")
CONTEXT_CLASSES = ("hair", "cloth", "neck", "hat", "neck_l")

_INFER_SCRIPT = r"""
import json, sys, urllib.request, urllib.error

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
    "http://localhost:8000/internal/v1/validate-infer-uniface",
    data=body,
    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    method="POST",
)
try:
    resp = urllib.request.urlopen(req, timeout=300)
    sys.stdout.write(resp.read().decode())
except urllib.error.HTTPError as exc:
    sys.stderr.write(f"{exc.code} {exc.read().decode()}")
    sys.exit(1)
"""


AI_RUNTIME_CONTAINER = "csense-ai-runtime-1"


def infer(image_bytes: bytes, version_id: str) -> dict:
    # Plain `docker exec` on the container, not `docker compose exec`: this script runs
    # from an isolated git worktree, which has no `.env` of its own (git-ignored, not
    # copied into a worktree), and compose refuses to start without one. `docker exec`
    # needs no env file, and the container already carries its own environment - the same
    # read-only "talk to the live stack" access `_docker_env` below already uses.
    result = subprocess.run(
        ["docker", "exec", "-i", AI_RUNTIME_CONTAINER,
         "python3", "-c", _INFER_SCRIPT, version_id, DETECTOR_CONFIDENCE],
        input=image_bytes, capture_output=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"inference failed: {result.stderr.decode()[:800]}")
    return json.loads(result.stdout.decode())


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


def match_face(faces: list[dict], expected_center_px, width: int, height: int):
    """Nearest detected face-centre to the manifest's own expected centre - never an
    assumed output order. Same helper shape as the cross-check script's `match_face`."""
    best, best_dist = None, None
    for face in faces:
        x1, y1, x2, y2 = face["bbox"]
        cx, cy = (x1 + x2) / 2 * width, (y1 + y2) / 2 * height
        dist = ((cx - expected_center_px[0]) ** 2 + (cy - expected_center_px[1]) ** 2) ** 0.5
        if best_dist is None or dist < best_dist:
            best, best_dist = face, dist
    return best, best_dist


def score_faceattribnet(manifest, response, width, height):
    per_image, correct, total = [], 0, 0
    faces = response.get("face_state") or []
    threshold = manifest["threshold"]
    for entry in manifest["images"]:
        for gt in entry["faces"]:
            matched, dist = match_face(faces, gt["expected_center_px"], width, height)
            if matched is None or dist > MATCH_TOLERANCE_PX:
                per_image.append({
                    "file": entry["file"], "expected_center_px": gt["expected_center_px"],
                    "matched": False, "note": "no real detected face matched within 60px",
                })
                total += len(manifest["classes"])
                continue
            record = {
                "file": entry["file"], "expected_center_px": gt["expected_center_px"],
                "match_distance_px": round(dist, 1), "matched": True,
                "predicted": {k: matched[k] for k in manifest["attribute_order"]},
                # Recorded to make the "independent binary heads, not a softmax" claim
                # checkable from the report itself rather than taken on trust.
                "predicted_sum": round(sum(matched[k] for k in manifest["attribute_order"]), 4),
                "ungraded": {
                    "left_eye_open": matched["left_eye_open"],
                    "right_eye_open": matched["right_eye_open"],
                    "eyeglasses": matched["eyeglasses"],
                },
            }
            for class_name in manifest["classes"]:
                expected = gt[class_name]
                predicted = matched[class_name] >= threshold
                ok = predicted == expected
                record[f"{class_name}_expected"] = expected
                record[f"{class_name}_predicted"] = predicted
                record[f"{class_name}_correct"] = ok
                total += 1
                correct += int(ok)
            per_image.append(record)
    return per_image, correct, total


def score_bisenet(manifest, response, width, height):
    per_image, correct, total = [], 0, 0
    faces = response.get("parsing") or []
    th = manifest["thresholds"]
    for entry in manifest["images"]:
        for gt in entry["faces"]:
            matched, dist = match_face(faces, gt["expected_center_px"], width, height)
            if matched is None or dist > MATCH_TOLERANCE_PX:
                per_image.append({
                    "file": entry["file"], "expected_center_px": gt["expected_center_px"],
                    "matched": False, "note": "no real detected face matched within 60px",
                })
                total += len(manifest["classes"])
                continue
            fractions = matched["class_fractions"]
            checks = {
                "mask_not_degenerate": (
                    matched["distinct_classes"] >= th["min_distinct_classes"]
                    and max(fractions.values()) <= th["max_single_class_fraction"]
                ),
                "skin_present": fractions.get("skin", 0.0) >= th["min_skin_fraction"],
                "eye_class_present": any(c in fractions for c in EYE_CLASSES),
                "hair_or_cloth_context_present": any(c in fractions for c in CONTEXT_CLASSES),
            }
            record = {
                "file": entry["file"], "expected_center_px": gt["expected_center_px"],
                "match_distance_px": round(dist, 1), "matched": True,
                "crop_size": matched["crop_size"],
                "distinct_classes": matched["distinct_classes"],
                "class_fractions": fractions,
                "checks": checks,
            }
            for name in manifest["classes"]:
                total += 1
                correct += int(checks[name])
            per_image.append(record)
    return per_image, correct, total


def record_run(minio_client, token, model_name, version_id, status, metrics, thresholds, per_image):
    report = {
        "model_name": model_name, "version_id": version_id, "suite_version": SUITE_VERSION,
        "status": status, "metrics": metrics, "thresholds": thresholds,
        "per_image": per_image, "runner_version": RUNNER_VERSION,
        "method_note": (
            "Run through the real, deployed POST /internal/v1/validate-infer-uniface on a "
            "freshly rebuilt ai-runtime container - not the docker-cp/local-import fallback "
            "the earlier uniface validation scripts had to use."
        ),
    }
    report_bytes = json.dumps(report, indent=2, default=str).encode("utf-8")
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    run_id = uuid.uuid4()
    report_key = f"global/models/validation-runs/{version_id}/{run_id}.json"
    minio_client.put_object(
        "csense-models", report_key, io.BytesIO(report_bytes), length=len(report_bytes),
        content_type="application/json",
    )
    status_code, run = admin_api(
        f"/api/v1/admin/model-versions/{version_id}/validation-runs",
        {
            "suite_version": SUITE_VERSION, "environment": ENVIRONMENT,
            "status": status, "metrics": metrics, "thresholds": thresholds,
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
    minio_client = _minio_client()
    _, auth = admin_api(
        "/api/v1/admin/auth/login", {"email": SERVICE_EMAIL, "password": SERVICE_PASSWORD},
        method="POST",
    )
    token = auth["access_token"]
    _, registered = admin_api("/api/v1/admin/models", token=token)

    results = {}
    for model_name in MODELS:
        matches = [m for m in registered if m["model_name"] == model_name]
        if not matches:
            raise RuntimeError(f"No registered version found for '{model_name}'.")
        version_id = matches[0]["id"]

        golden_dir = os.path.join(GOLDEN_ROOT, model_name)
        with open(os.path.join(golden_dir, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)

        print(f"\n[{model_name}] version {version_id}")

        image_path = os.path.join(golden_dir, manifest["images"][0]["file"])
        with open(image_path, "rb") as fh:
            image_bytes = fh.read()
        response = infer(image_bytes, version_id)
        width, height = response["frame_size"]
        print(f"  detector found {response.get('face_count')} faces "
              f"({response['inference_ms']} ms, frame {width}x{height})")

        if model_name == "uniface-faceattribnet-attributes":
            per_image, correct, total = score_faceattribnet(manifest, response, width, height)
            thresholds = {
                "graded_classes": manifest["classes"],
                "threshold": manifest["threshold"],
                "require_all_correct": True,
                "ungraded": ["left_eye_open", "right_eye_open", "eyeglasses"],
            }
        else:
            per_image, correct, total = score_bisenet(manifest, response, width, height)
            thresholds = dict(manifest["thresholds"])
            thresholds["graded_checks"] = manifest["classes"]
            thresholds["require_all_correct"] = True
            thresholds["crop_margin"] = manifest["crop_margin"]

        status = "passed" if total > 0 and correct == total else "failed"
        metrics = {
            "correct": correct, "total": total,
            "pass_rate": round(correct / total, 4) if total else 0.0,
            # Never a number where there is no evidence - the same None-not-0.0 discipline
            # run_intern_model_validation.py established for the missing-child-photo gap.
            "accuracy": None,
            "accuracy_note": (
                "Per-pixel segmentation accuracy/IoU is unmeasured - no per-pixel ground "
                "truth exists for these faces and none was fabricated."
                if model_name == "uniface-bisenet-parsing"
                else "sunglasses false-positive rate is unmeasured - no bare-eyed face "
                     "exists anywhere in this repository to act as a negative control. "
                     "Eye-openness and eyeglasses accuracy are unmeasured (no assertable "
                     "ground truth on this image)."
            ),
        }
        print(f"  -> {status}: {correct}/{total} checks passed")
        for record in per_image:
            print(f"    {json.dumps(record, default=str)}")

        record_run(minio_client, token, model_name, version_id, status, metrics, thresholds, per_image)
        results[model_name] = {"version_id": version_id, "status": status, "metrics": metrics}

        code, body = promote(token, version_id, "validating", f"{SUITE_VERSION} run recorded: {status}")
        print(f"    promote -> validating: {code} {body.get('state', body)}")
        results[model_name]["promoted_to"] = "validating"

        if status == "passed":
            code, body = promote(token, version_id, "validated", f"{SUITE_VERSION} passed all checks")
            print(f"    promote -> validated: {code} {body.get('state', body) if isinstance(body, dict) else body}")
            results[model_name]["promote_validated_response"] = {"status_code": code, "body": body}
            if code == 200:
                results[model_name]["promoted_to"] = body.get("state", "validated")

    print("\n=== summary ===")
    for name, r in results.items():
        print(f"{name}: {r['status']} ({r['metrics']['correct']}/{r['metrics']['total']}) "
              f"-> {r.get('promoted_to')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
