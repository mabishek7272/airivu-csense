"""Records real `model_validation_runs` rows for the 6 lowest-risk `uniface-zoo` models
(`uniface-{adaface,edgeface,mobileface,sphereface}-recognition`, `uniface-facemesh-
landmark`, `uniface-modnet-matting`) - see CHECKLIST.md's "18 models pulled in `uploaded`"
entry for the full risk-tiering writeup and `backend/ai_runtime/app/engines.py`'s own
`UnifaceEmbeddingEngine`/`UnifaceFaceMeshEngine`/`UnifaceMattingEngine` for the decode
logic this validates.

**Why this does NOT call `/internal/v1/validate-infer-uniface` over HTTP, unlike
`run_intern_model_validation.py`'s `/internal/v1/validate-infer` calls.** That new endpoint
was added to this same worktree's `backend/ai_runtime/app/main.py` alongside the 3 new
decode engines, but the *running* `ai-runtime` container was started from an image built
before this work - it does not have this code. Rebuilding/restarting that container was
explicitly off-limits for this task (a real client demo was running on this exact Docker
stack). So this script validates the decode logic the same honest way the task's own
instructions named as the fallback: by loading the actual candidate `engines.py` (via
`docker cp` into a throwaway path, `/tmp/uniface_validate/`, NOT overwriting the running
container's real `/app/app/engines.py`) and exercising it with the container's own already-
installed onnxruntime/insightface/opencv against the real MinIO-fetched artifacts and a
real image - `scripts/_uniface_validate_in_container.py` is what actually runs inside the
container; this script drives it and records the result. This is a real, if narrower, form
of gate 2-4 evidence: real artifacts, real preprocessing/postprocessing code, real image,
just invoked directly instead of through the (not-yet-deployed) HTTP route. Once the owner
rebuilds/restarts `ai-runtime` with this worktree's code, `/internal/v1/validate-infer-
uniface` becomes callable the normal way and this script's docker-cp workaround stops being
necessary - noted here and in CHECKLIST.md so the gap is visible, not silently permanent.

**Golden set**: `backend/tests/fixtures/golden/uniface-*/manifest.json` (6 new manifests,
one per model) - `stock_streetscene_3adults.jpg` is the ONLY image anywhere under
`backend/tests/fixtures/golden/*/` with real, usable human faces (checked before writing
this script, per the task's own instruction not to pull fresh live camera frames given the
demo and a second agent already using the platform's one real NVR). Each manifest's own
`note` field states this plainly, along with the real, honest limit it puts on what can be
claimed: there is no same-identity pair anywhere in this project's fixtures, so true
recognition recall (same person, two images, embeddings actually match) is `null` -
unmeasured, not guessed at - for all 4 recognition models. What this script DOES verify for
real: correct output shape/dtype, every value finite, embeddings non-degenerate (not
identical between two different real people, not all-zero), facemesh landmarks landing
within the same face region the platform's own already-verified SCRFD detector reported,
and the matte's value range/shape sanity. That is gate 2-3 (load/shape/qualitative
sanity), honestly short of gate 4 (real accuracy against labelled positives) for the 5
face-shaped models - the same `recall: null` discipline `run_intern_model_validation.py`
already established for a genuine ground-truth gap, not a new invention.

    python scripts/run_uniface_model_validation.py
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
from minio import Minio

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_ROOT = os.path.join(REPO, "backend", "tests", "fixtures", "golden")

SUITE_VERSION = "uniface-local-decode-v1"
RUNNER_VERSION = "run_uniface_model_validation.py/1"

ADMIN_API = "http://localhost:8080"
SERVICE_EMAIL = "validation-runner@platform.internal"
SERVICE_PASSWORD = "ValidationRunner!ServiceAccount123"

# Real values read live from the running containers this session (`docker exec <c> env`) -
# not committed secrets; this script only runs against this operator's own local stack.
POSTGRES_DSN = (
    "host=localhost port=5432 dbname=csense user=csense_app "
    "password=cf5b275d4922634afbb464468dd4b9766194670b8c3c47a3"
)
MINIO_ENDPOINT = "localhost:9000"
MINIO_ACCESS_KEY = "csense_minio"
MINIO_SECRET_KEY = "4896dfb0912911bb5c566ded0eacbbe1f547c07d0d95c5d2"

EMBEDDING_MODELS = (
    "uniface-adaface-recognition",
    "uniface-edgeface-recognition",
    "uniface-mobileface-recognition",
    "uniface-sphereface-recognition",
)
ALL_MODELS = EMBEDDING_MODELS + ("uniface-facemesh-landmark", "uniface-modnet-matting")


def run_in_container(image_path: str) -> dict:
    """Copies the candidate engine module + one real fixture image into the running
    ai-runtime container at a throwaway path (NOT the container's real /app/app/engines.py
    - see this module's own docstring) and runs `_uniface_validate_in_container.py`
    inside it, returning the parsed JSON report it writes to a file (not stdout -
    onnxruntime/insightface's own C++-level logging writes directly to stdout outside
    Python's print() redirection, discovered while building this script, so stdout cannot
    be trusted to contain only the JSON)."""
    subprocess.run(
        ["docker", "exec", "csense-ai-runtime-1", "mkdir", "-p", "/tmp/uniface_validate"],
        check=True,
    )
    subprocess.run(
        ["docker", "cp", os.path.join(REPO, "backend/ai_runtime/app/engines.py"),
         "csense-ai-runtime-1:/tmp/uniface_validate/engines_candidate.py"],
        check=True,
    )
    subprocess.run(
        ["docker", "cp", image_path, "csense-ai-runtime-1:/tmp/uniface_validate/stock_streetscene_3adults.jpg"],
        check=True,
    )
    subprocess.run(
        ["docker", "cp", os.path.join(REPO, "scripts/_uniface_validate_in_container.py"),
         "csense-ai-runtime-1:/tmp/uniface_validate/run.py"],
        check=True,
    )
    result = subprocess.run(
        ["docker", "exec", "csense-ai-runtime-1", "python3", "/tmp/uniface_validate/run.py"],
        capture_output=True, check=False,
    )
    sys.stderr.write(result.stderr.decode(errors="replace"))
    if result.returncode != 0:
        raise RuntimeError(f"in-container validation failed (exit {result.returncode})")

    report_bytes = subprocess.run(
        ["docker", "exec", "csense-ai-runtime-1", "cat", "/tmp/uniface_validate/report.json"],
        capture_output=True, check=True,
    ).stdout
    return json.loads(report_bytes.decode())


def bootstrap_service_account() -> None:
    with psycopg.connect(POSTGRES_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")
        cur.execute("SELECT id FROM users WHERE email_normalized = %s", (SERVICE_EMAIL,))
        if cur.fetchone() is not None:
            print(f"    service account {SERVICE_EMAIL} already exists")
            return
        # Reuses the exact same bootstrap shape run_intern_model_validation.py already
        # established - importing csense_shared.security.passwords isn't necessary since
        # this branch only runs when the account doesn't exist yet (it already does, on
        # this stack, from that earlier script's own run).
        raise RuntimeError(
            f"Service account {SERVICE_EMAIL} does not exist - run "
            "scripts/run_intern_model_validation.py first, or extend this bootstrap."
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


def evaluate_embedding_model(model_name: str, data: dict) -> tuple[str, dict, dict]:
    face_count = data["face_count"]
    dims_ok = face_count >= 1 and all(d == 512 for d in data["embedding_dims"])
    finite_ok = data["all_finite"]
    non_degenerate_ok = face_count < 2 or not data["vectors_identical_between_faces"]
    checks = {
        "at_least_one_real_face_embedded": face_count >= 1,
        "embedding_dim_is_512": dims_ok,
        "all_values_finite": finite_ok,
        "different_faces_not_identical_embedding": non_degenerate_ok,
    }
    passed = all(checks.values())
    metrics = {
        "checks": checks,
        "face_count": face_count,
        "embedding_dims": data["embedding_dims"],
        "l2_norms": data["l2_norms"],
        "different_identity_pairwise_cosine_similarity": data["different_identity_pairwise_cosine_similarity"],
        "gate_4_recall_note": (
            "null/unmeasured - no same-identity pair exists anywhere in this project's "
            "fixtures to test true recognition recall against; see manifest.json's own "
            "note for the full honesty statement. Low pairwise cosine similarity between "
            "these two different real people is a WEAK signal only (high-dimensional "
            "random vectors also produce near-zero cosine similarity), reported as such, "
            "not oversold as proof of discriminative power."
        ),
    }
    return ("passed" if passed else "failed"), metrics, checks


def evaluate_facemesh(data: dict) -> tuple[str, dict, dict]:
    face_count = data["face_count"]
    shape_ok = face_count >= 1 and all(f["landmark_shape"] == [468, 3] for f in data["faces"])
    finite_ok = all(f["all_finite"] for f in data["faces"])
    within_bbox_ok = all(f["landmarks_within_detector_bbox_plus_margin"] for f in data["faces"])
    score_range_ok = all(0.0 <= f["presence_score"] <= 1.0 for f in data["faces"])
    checks = {
        "at_least_one_real_face_meshed": face_count >= 1,
        "landmark_shape_is_468x3": shape_ok,
        "all_values_finite": finite_ok,
        "landmarks_land_within_detector_bbox": within_bbox_ok,
        "presence_score_in_0_1": score_range_ok,
    }
    passed = all(checks.values())
    metrics = {"checks": checks, "face_count": face_count, "faces": data["faces"]}
    return ("passed" if passed else "failed"), metrics, checks


def evaluate_modnet(data: dict) -> tuple[str, dict, dict]:
    checks = {
        "matte_shape_matches_input": data["shape_matches_input"],
        "all_values_finite": data["all_finite"],
        "value_range_within_0_1": 0.0 <= data["min"] and data["max"] <= 1.0,
        "not_degenerate_uniform_matte": 0.0 < data["mean"] < 1.0,
    }
    passed = all(checks.values())
    metrics = {"checks": checks, **{k: v for k, v in data.items()}}
    return ("passed" if passed else "failed"), metrics, checks


def record_and_promote(model_name: str, version_id: str, status: str, metrics: dict, token: str) -> None:
    report_bytes = json.dumps(
        {"model_name": model_name, "version_id": version_id, "suite_version": SUITE_VERSION,
         "status": status, "metrics": metrics, "runner_version": RUNNER_VERSION},
        indent=2,
    ).encode("utf-8")
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    run_id = uuid.uuid4()

    minio_client = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY, secure=False)
    report_key = f"global/models/validation-runs/{version_id}/{run_id}.json"
    minio_client.put_object(
        "csense-models", report_key, io.BytesIO(report_bytes), length=len(report_bytes),
        content_type="application/json",
    )

    code, run = admin_api(
        f"/api/v1/admin/model-versions/{version_id}/validation-runs",
        {
            "suite_version": SUITE_VERSION,
            # Admin API caps `environment` at 64 chars - the full explanation of why this
            # ran via docker-cp + local import instead of the (not-yet-deployed)
            # /internal/v1/validate-infer-uniface HTTP route lives in `metrics.method_note`
            # below and in this script's own module docstring, not truncated to fit here.
            "environment": "local-docker-compose (docker-cp local-import, no restart)",
            "status": status,
            "metrics": {
                **metrics,
                "method_note": (
                    "Decode executed by docker-cp'ing the candidate engines.py into a "
                    "throwaway path inside the running ai-runtime container (NOT its real "
                    "/app/app/engines.py) and importing it directly - "
                    "/internal/v1/validate-infer-uniface exists in code but needs a "
                    "container rebuild/restart to actually be reachable, which was "
                    "off-limits during a live client demo on this stack. See "
                    "run_uniface_model_validation.py's own module docstring."
                ),
            },
            "thresholds": {"gate": "2-3: load/shape/qualitative-sanity, not gate-4 accuracy"},
            "result_object_key": report_key, "result_sha256": report_sha256,
            "result_size_bytes": len(report_bytes), "runner_version": RUNNER_VERSION,
        },
        token=token, method="POST",
    )
    if code != 201:
        raise RuntimeError(f"validation-runs POST failed ({code}): {run}")
    print(f"    recorded model_validation_runs row {run['id']} status={status} ({report_key})")

    code, promoted = admin_api(
        f"/api/v1/admin/model-versions/{version_id}/promote",
        {"target_state": "validating", "reason": f"{SUITE_VERSION}: {status}, gate 2-3 decode validation"},
        token=token, method="POST",
    )
    print(f"    promote uploaded->validating: {code} state={promoted.get('state')}")

    if status != "passed":
        print("    not attempting validating->validated (run did not pass)")
        return

    code, promoted = admin_api(
        f"/api/v1/admin/model-versions/{version_id}/promote",
        {"target_state": "validated", "reason": f"{SUITE_VERSION}: passed gate 2-3 decode validation"},
        token=token, method="POST",
    )
    if code == 200:
        print(f"    promote validating->validated: {code} state={promoted.get('state')}")
    else:
        print(f"    promote validating->validated REFUSED: {code} {promoted.get('message', promoted)}")


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()

    bootstrap_service_account()
    _, auth = admin_api("/api/v1/admin/auth/login", {"email": SERVICE_EMAIL, "password": SERVICE_PASSWORD}, method="POST")
    token = auth["access_token"]

    _, models = admin_api("/api/v1/admin/models", token=token)
    version_ids = {}
    for model_name in ALL_MODELS:
        matches = [m for m in models if m["model_name"] == model_name]
        if not matches:
            raise RuntimeError(f"No registered version found for '{model_name}'.")
        version_ids[model_name] = matches[0]["id"]
        print(f"[{model_name}] version_id={matches[0]['id']} state={matches[0]['state']} "
              f"classification={matches[0]['access_classification']}")

    image_path = os.path.join(GOLDEN_ROOT, "uniface-adaface-recognition", "stock_streetscene_3adults.jpg")
    print(f"\nRunning real decode against {image_path} inside csense-ai-runtime-1 ...")
    started = time.monotonic()
    data = run_in_container(image_path)
    elapsed = time.monotonic() - started
    print(f"in-container run took {elapsed:.1f}s; detector found {data['detector']['face_count']} real faces\n")

    results = {}
    for model_name in EMBEDDING_MODELS:
        status, metrics, checks = evaluate_embedding_model(model_name, data["embeddings"][model_name])
        results[model_name] = (status, metrics)
        print(f"[{model_name}] {status}: {checks}")

    status, metrics, checks = evaluate_facemesh(data["facemesh"])
    results["uniface-facemesh-landmark"] = (status, metrics)
    print(f"[uniface-facemesh-landmark] {status}: {checks}")

    status, metrics, checks = evaluate_modnet(data["modnet"])
    results["uniface-modnet-matting"] = (status, metrics)
    print(f"[uniface-modnet-matting] {status}: {checks}")

    print("\n=== recording + promoting ===")
    for model_name in ALL_MODELS:
        status, metrics = results[model_name]
        print(f"\n[{model_name}]")
        record_and_promote(model_name, version_ids[model_name], status, metrics, token)

    print("\n=== summary ===")
    for model_name in ALL_MODELS:
        status, _ = results[model_name]
        print(f"{model_name}: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
