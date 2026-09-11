"""Runs a real golden dataset through the 4 uniface-zoo models whose decode needed a
public-repo cross-check before writing it (`uniface-fairface-attributes`,
`uniface-minifasnet-antispoofing`, `uniface-mobilegaze-estimation`,
`uniface-pipnet-landmark`), and records a real `model_validation_runs` row per model via
the real Admin API - the same TRD gate 2-4 pattern `run_model_validation.py` and
`run_intern_model_validation.py` established.

**Why this doesn't call `/internal/v1/validate-infer` the way those two scripts do.** The
new decode code these 4 models need lives in this worktree's `backend/ai_runtime/app/
engines.py` - it is not yet inside the running `ai-runtime` container's image, and a real
client demo is running on that exact container as of this session, so it must not be
rebuilt or restarted here (see CHECKLIST.md / the session's own operating constraint).
Confirmed empirically, not assumed: calling the real, currently-running
`/internal/v1/validate-infer` against `uniface-fairface-attributes` returns
`detection_count: 0` with **no error at all** - not the loud `output_contract_unknown`
501 you'd expect. The old, still-running `OnnxEngine._decode` misreads `race_output`'s
`(1, 7)` shape as a 7-column end2end YOLO box row (the same column count as
`batch_idx,x1,y1,x2,y2,class,score`) purely by coincidence, and silently produces zero
boxes instead of erroring - a live example of exactly the silent-wrong-decode failure
class this project has already been bitten by twice (the plate detector's class/score
swap; the backwards child/adult label map). That is precisely why these 4 needed a
dedicated engine/dispatch fix, not just a decode: `build_engine()`'s new model_name-keyed
dispatch (this session's change) routes these 4 away from `OnnxEngine` entirely, and each
new engine's own generic `infer()` now raises loudly instead.

Since the real decode can't be reached through the live HTTP API without a restart, this
script instead imports `app.engines` directly (the module is self-contained - only stdlib
+ numpy at import time, onnxruntime/opencv/insightface are lazy inside methods) and runs
real inference locally, against the real artifact bytes pulled from the real `csense-
models` MinIO bucket and a real face crop from the real, already-`production` SCRFD face
detector (`insightface-buffalo-l-detect`). This is the exact fallback path the task
instructions anticipated for this situation - and it is the *same* decode code that will
run inside the container once it's rebuilt, not a reimplementation. Once the owner
rebuilds `ai-runtime`, `scripts/run_intern_model_validation.py`'s own docker-exec-to-HTTP
pattern becomes reachable for these 4 too and can be used to reproduce these exact numbers
end-to-end through the real service.

Credentials (MinIO, Postgres) are read live from the running containers' own environment
(`docker exec <container> printenv <KEY>`) rather than a `.env` file, since this worktree
does not carry one (git-ignored, not copied into an isolated worktree) - the same
read-only "probe the live stack" access this session's constraint explicitly allows.

    python3.12 scripts/run_uniface_model_validation_crosscheck.py   # needs onnxruntime, opencv-python,
                                                          # numpy, insightface, minio, psycopg
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import cv2
import numpy as np
from minio import Minio

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_ROOT = os.path.join(REPO, "backend", "tests", "fixtures", "golden")
ENGINES_PATH = os.path.join(REPO, "backend", "ai_runtime", "app", "engines.py")

SUITE_VERSION = "uniface-golden-v1"
RUNNER_VERSION = "run_uniface_model_validation_crosscheck.py/1"

ADMIN_API = "http://localhost:8080"
SERVICE_EMAIL = "validation-runner@platform.internal"
SERVICE_PASSWORD = "ValidationRunner!ServiceAccount123"  # pre-existing service account,
# created by run_intern_model_validation.py's own bootstrap_service_account() earlier
# this session - reused as-is here rather than re-run, since it already exists (confirmed
# via a real, read-only `SELECT ... FROM users` before writing this script).

MODEL_ARTIFACT_KEYS = {
    "uniface-fairface-attributes": (
        "global/models/uniface-fairface-attributes/uniface-2026-09/"
        "9c8c47d437cd310538d233f2465f9ed0524cb7fb51882a37f74e8bc22437fdbf.onnx"
    ),
    "uniface-minifasnet-antispoofing": (
        "global/models/uniface-minifasnet-antispoofing/uniface-2026-09/"
        "b32929adc2d9c34b9486f8c4c7bc97c1b69bc0ea9befefc380e4faae4e463907.onnx"
    ),
    "uniface-mobilegaze-estimation": (
        "global/models/uniface-mobilegaze-estimation/uniface-2026-09/"
        "404fec1efd07ff49f981e47f461c20c2627119e465ec441bbd1c067d3f16e657.onnx"
    ),
    "uniface-pipnet-landmark": (
        "global/models/uniface-pipnet-landmark/uniface-2026-09/"
        "9862838dc6144bc772b6485f6f6d31295c0b1c1ab7293e6ddeb0a439cb10218d.onnx"
    ),
}
SCRFD_ARTIFACT_KEY = (
    "global/models/insightface-buffalo-l-detect/legacy-2026-08/"
    "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91.onnx"
)


def _docker_env(container: str, key: str) -> str:
    out = subprocess.run(
        ["docker", "exec", container, "printenv", key], capture_output=True, check=True,
    )
    return out.stdout.decode().strip()


def _load_engines_module():
    spec = importlib.util.spec_from_file_location("uniface_engines", ENGINES_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["uniface_engines"] = module
    spec.loader.exec_module(module)
    return module


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
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        return exc.code, json.loads(body) if body else {}


def detect_faces(engines, detector, image: np.ndarray) -> list:
    height, width = image.shape[:2]
    detector.det_thresh = 0.4
    bboxes, kpss = detector.detect(image)
    detections = []
    for i in range(bboxes.shape[0]):
        x1, y1, x2, y2, score = (float(v) for v in bboxes[i])
        keypoints = [(float(kx) / width, float(ky) / height, score) for kx, ky in kpss[i]]
        detections.append(
            engines.Detection(
                class_id=0, class_name="face", confidence=score,
                bbox=(x1 / width, y1 / height, x2 / width, y2 / height),
                keypoints=keypoints,
            )
        )
    return detections


def match_face(detections: list, expected_center_px: list[float], width: int, height: int):
    """Matches a manifest ground-truth entry to the nearest real detected face by centre
    distance - not by assuming a fixed detector output order."""
    best, best_dist = None, None
    for d in detections:
        x1, y1, x2, y2 = d.bbox
        cx, cy = (x1 + x2) / 2 * width, (y1 + y2) / 2 * height
        dist = ((cx - expected_center_px[0]) ** 2 + (cy - expected_center_px[1]) ** 2) ** 0.5
        if best_dist is None or dist < best_dist:
            best, best_dist = d, dist
    return best, best_dist


def record_run(minio_client, token, version_id, status, metrics, thresholds, per_image):
    report = {
        "model_name": None, "version_id": version_id, "suite_version": SUITE_VERSION,
        "status": status, "metrics": metrics, "thresholds": thresholds,
        "per_image": per_image, "runner_version": RUNNER_VERSION,
    }
    report_bytes = json.dumps(report, indent=2, default=str).encode("utf-8")
    import hashlib
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
            "suite_version": SUITE_VERSION, "environment": "local-out-of-container-venv",
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
    status_code, body = admin_api(
        f"/api/v1/admin/model-versions/{version_id}/promote",
        {"target_state": target_state, "reason": reason},
        token=token, method="POST",
    )
    return status_code, body


def main() -> int:
    engines = _load_engines_module()

    tmp_dir = "/tmp/uniface_validation_artifacts"
    os.makedirs(tmp_dir, exist_ok=True)
    minio_client = _minio_client()

    scrfd_path = os.path.join(tmp_dir, "scrfd_detector.onnx")
    if not os.path.exists(scrfd_path):
        minio_client.fget_object("csense-models", SCRFD_ARTIFACT_KEY, scrfd_path)

    from insightface import model_zoo

    detector = model_zoo.get_model(scrfd_path, providers=["CPUExecutionProvider"])
    detector.prepare(ctx_id=-1)

    _, auth = admin_api(
        "/api/v1/admin/auth/login", {"email": SERVICE_EMAIL, "password": SERVICE_PASSWORD},
        method="POST",
    )
    token = auth["access_token"]

    _, models = admin_api("/api/v1/admin/models", token=token)
    results = {}

    for model_name, object_key in MODEL_ARTIFACT_KEYS.items():
        matches = [m for m in models if m["model_name"] == model_name]
        if not matches:
            raise RuntimeError(f"No registered version found for '{model_name}'.")
        version_id = matches[0]["id"]

        artifact_path = os.path.join(tmp_dir, f"{model_name}.onnx")
        if not os.path.exists(artifact_path):
            minio_client.fget_object("csense-models", object_key, artifact_path)

        golden_dir = os.path.join(GOLDEN_ROOT, model_name)
        with open(os.path.join(golden_dir, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)

        print(f"\n[{model_name}] version {version_id}")

        if model_name == "uniface-fairface-attributes":
            engine = engines.FairFaceEngine(artifact_path)
        elif model_name == "uniface-minifasnet-antispoofing":
            engine = engines.MiniFasNetEngine(artifact_path)
        elif model_name == "uniface-mobilegaze-estimation":
            engine = engines.MobileGazeEngine(artifact_path)
        elif model_name == "uniface-pipnet-landmark":
            engine = engines.PipNetEngine(artifact_path)
        else:  # pragma: no cover
            raise RuntimeError(model_name)

        per_image = []
        correct, total = 0, 0
        for entry in manifest["images"]:
            image_path = os.path.join(golden_dir, entry["file"])
            image = cv2.imread(image_path)
            height, width = image.shape[:2]
            detections = detect_faces(engines, detector, image)

            for face_gt in entry["faces"]:
                started = time.monotonic()
                matched, dist = match_face(detections, face_gt["expected_center_px"], width, height)
                if matched is None or dist > 60:
                    per_image.append({
                        "file": entry["file"], "expected_center_px": face_gt["expected_center_px"],
                        "matched": False, "note": "no real detected face matched within 60px",
                    })
                    total += 1
                    continue

                record = {
                    "file": entry["file"], "expected_center_px": face_gt["expected_center_px"],
                    "match_distance_px": round(dist, 1), "matched": True,
                }

                if model_name == "uniface-fairface-attributes":
                    attrs = engine.predict_attributes(image, matched)
                    elapsed_ms = (time.monotonic() - started) * 1000
                    gender_ok = attrs.gender == face_gt["gender"]
                    age_ok = attrs.age_bucket not in ("0-2", "3-9", "10-19")
                    record.update({
                        "predicted_gender": attrs.gender, "predicted_gender_confidence": attrs.gender_confidence,
                        "predicted_race": attrs.race, "predicted_race_confidence": attrs.race_confidence,
                        "predicted_age_bucket": attrs.age_bucket, "predicted_age_confidence": attrs.age_confidence,
                        "expected_gender": face_gt["gender"], "gender_correct": gender_ok,
                        "expected_age_not_child": face_gt["age_not_child"], "age_not_child": age_ok,
                        "inference_ms": round(elapsed_ms, 2),
                    })
                    total += 2
                    correct += int(gender_ok) + int(age_ok)

                elif model_name == "uniface-minifasnet-antispoofing":
                    res = engine.predict_liveness(image, matched)
                    elapsed_ms = (time.monotonic() - started) * 1000
                    ok = res.is_real == face_gt["is_real"]
                    record.update({
                        "predicted_is_real": res.is_real, "predicted_label": res.label,
                        "predicted_confidence": res.confidence, "predicted_scores": list(res.scores),
                        "expected_is_real": face_gt["is_real"], "is_real_correct": ok,
                        "inference_ms": round(elapsed_ms, 2),
                    })
                    total += 1
                    correct += int(ok)

                elif model_name == "uniface-mobilegaze-estimation":
                    gaze = engine.estimate_gaze(image, matched)
                    elapsed_ms = (time.monotonic() - started) * 1000
                    tol = manifest["tolerance"]
                    ok = abs(gaze.yaw_deg) <= tol["max_abs_yaw_deg"] and abs(gaze.pitch_deg) <= tol["max_abs_pitch_deg"]
                    record.update({
                        "predicted_yaw_deg": round(gaze.yaw_deg, 2), "predicted_pitch_deg": round(gaze.pitch_deg, 2),
                        "plausible": ok, "inference_ms": round(elapsed_ms, 2),
                    })
                    total += 1
                    correct += int(ok)

                elif model_name == "uniface-pipnet-landmark":
                    lm = engine.predict_landmarks(image, matched)
                    elapsed_ms = (time.monotonic() - started) * 1000
                    xs = [p[0] * width for p in lm.points]
                    ys = [p[1] * height for p in lm.points]
                    mean_conf = float(np.mean([p[2] for p in lm.points]))
                    dx1, dy1, dx2, dy2 = matched.bbox
                    dx1, dy1, dx2, dy2 = dx1 * width, dy1 * height, dx2 * width, dy2 * height
                    margin = manifest["tolerance"]["margin_fraction"]
                    mx, my = (dx2 - dx1) * margin, (dy2 - dy1) * margin
                    within_x = (dx1 - mx) <= min(xs) and max(xs) <= (dx2 + mx)
                    within_y = (dy1 - my) <= min(ys) and max(ys) <= (dy2 + my)
                    ok = within_x and within_y
                    record.update({
                        "landmark_bbox_px": [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)],
                        "detector_bbox_px": [round(dx1, 1), round(dy1, 1), round(dx2, 1), round(dy2, 1)],
                        "mean_point_confidence": round(mean_conf, 3),
                        "landmark_bbox_plausible": ok, "inference_ms": round(elapsed_ms, 2),
                    })
                    total += 1
                    correct += int(ok)

                per_image.append(record)

        pass_rate = correct / total if total else 0.0
        status = "passed" if total > 0 and correct == total else "failed"
        metrics = {"correct": correct, "total": total, "pass_rate": round(pass_rate, 4)}
        thresholds = {"require_all_correct": True}
        print(f"  -> {status}: {correct}/{total} checks passed")
        for r in per_image:
            print(f"    {json.dumps(r, default=str)}")

        record_run(minio_client, token, version_id, status, metrics, thresholds, per_image)
        results[model_name] = {"version_id": version_id, "status": status, "metrics": metrics}

        # uploaded -> validating is always allowed regardless of pass/fail (same as the
        # intern-model script's own precedent) - real gate-2/3 evidence exists either way.
        code, body = promote(token, version_id, "validating", f"{SUITE_VERSION} run recorded: {status}")
        print(f"    promote -> validating: {code} {body.get('state', body)}")

        if status == "passed":
            code, body = promote(token, version_id, "validated", f"{SUITE_VERSION} passed all checks")
            print(f"    promote -> validated: {code} {body.get('state', body) if isinstance(body, dict) else body}")
            results[model_name]["promoted_to"] = body.get("state") if code == 200 else "validating"
            results[model_name]["promote_validated_response"] = {"status_code": code, "body": body}
        else:
            results[model_name]["promoted_to"] = "validating"

    print("\n=== summary ===")
    for name, r in results.items():
        print(f"{name}: {r['status']} ({r['metrics']['correct']}/{r['metrics']['total']}) -> {r.get('promoted_to')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
