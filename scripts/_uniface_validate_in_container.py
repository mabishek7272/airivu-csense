"""Runs INSIDE the real ai-runtime container (via `docker exec`), against real cached
artifacts and a real image. Writes one JSON report to /tmp/uniface_validate/report.json
(NOT stdout - onnxruntime/insightface's own C++-level logging writes directly to stdout
outside Python's print() redirection, discovered while building this); progress goes to
stderr. See scripts/run_uniface_model_validation.py's own module docstring for why this
exists instead of calling /internal/v1/validate-infer-uniface over HTTP."""
import importlib.util
import json
import os
import sys

import cv2
import numpy as np
from minio import Minio

ARTIFACTS_DIR = "/tmp/uniface_validate_artifacts"
os.makedirs(ARTIFACTS_DIR, exist_ok=True)

CANDIDATE_MODULE = "/tmp/uniface_validate/engines_candidate.py"
IMAGE_PATH = "/tmp/uniface_validate/stock_streetscene_3adults.jpg"

MODEL_KEYS = {
    "insightface-buffalo-l-detect": "global/models/insightface-buffalo-l-detect/legacy-2026-08/5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91.onnx",
    "uniface-adaface-recognition": "global/models/uniface-adaface-recognition/uniface-2026-09/6b6a35772fb636cdd4fa86520c1a259d0c41472a76f70f802b351837a00d9870.onnx",
    "uniface-edgeface-recognition": "global/models/uniface-edgeface-recognition/uniface-2026-09/b56942f072c67385f44734b9458b0ccc4a2226888a113f77e0c802ad0c77b4c3.onnx",
    "uniface-mobileface-recognition": "global/models/uniface-mobileface-recognition/uniface-2026-09/38b148284dd48cc898d5d4453104252fbdcbacc105fe3f0b80e78954d9d20d89.onnx",
    "uniface-sphereface-recognition": "global/models/uniface-sphereface-recognition/uniface-2026-09/c02878cf658eb1861f580b7e7144b0d27cc29c440bcaa6a99d466d2854f14c9d.onnx",
    "uniface-facemesh-landmark": "global/models/uniface-facemesh-landmark/uniface-2026-09/3ca77cf59c18e4da0eccb46695bf604683fa564253e3385892981a5c274fb10f.onnx",
    "uniface-modnet-matting": "global/models/uniface-modnet-matting/uniface-2026-09/5069a5e306b9f5e9f4f2b0360264c9f8ea13b257c7c39943c7cf6a2ec3a102ae.onnx",
}

client = Minio(
    "minio:9000",
    access_key=os.environ["MINIO_ROOT_USER"],
    secret_key=os.environ["MINIO_ROOT_PASSWORD"],
    secure=False,
)
local_paths = {}
for name, key in MODEL_KEYS.items():
    local = f"{ARTIFACTS_DIR}/{name}.onnx"
    if not os.path.exists(local):
        client.fget_object("csense-models", key, local)
    local_paths[name] = local

spec = importlib.util.spec_from_file_location("engines_candidate", CANDIDATE_MODULE)
engines = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = engines
spec.loader.exec_module(engines)

from insightface import model_zoo

detector = model_zoo.get_model(local_paths["insightface-buffalo-l-detect"], providers=["CPUExecutionProvider"])
detector.prepare(ctx_id=-1)

image = cv2.imread(IMAGE_PATH)
assert image is not None, "failed to decode stock_streetscene_3adults.jpg"
height, width = image.shape[:2]
bboxes, kpss = detector.detect(image)
print(f"SCRFD found {bboxes.shape[0]} faces", file=sys.stderr)

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

report = {"detector": {"face_count": len(detections), "faces": [
    {"confidence": round(d.confidence, 4), "bbox": [round(v, 4) for v in d.bbox]} for d in detections
]}}

embedding_models = {
    "uniface-adaface-recognition": "adaface",
    "uniface-edgeface-recognition": "edgeface",
    "uniface-mobileface-recognition": "mobileface",
    "uniface-sphereface-recognition": "sphereface",
}
report["embeddings"] = {}
for name in embedding_models:
    eng = engines.UnifaceEmbeddingEngine(local_paths[name], None, name)
    vectors = [eng.embed(image, d) for d in detections]
    norms = [float(np.linalg.norm(v)) for v in vectors]
    pairwise = []
    for i in range(len(vectors)):
        for j in range(i + 1, len(vectors)):
            a, b = vectors[i], vectors[j]
            cos = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))
            pairwise.append(round(cos, 4))
    report["embeddings"][name] = {
        "face_count": len(vectors),
        "embedding_dims": [int(v.shape[0]) for v in vectors],
        "l2_norms": [round(n, 4) for n in norms],
        "all_finite": bool(all(np.isfinite(v).all() for v in vectors)),
        "vectors_identical_between_faces": bool(
            len(vectors) >= 2 and np.allclose(vectors[0], vectors[1])
        ),
        "different_identity_pairwise_cosine_similarity": pairwise,
    }
    print(f"[{name}] done", file=sys.stderr)
    del eng

mesh = engines.UnifaceFaceMeshEngine(local_paths["uniface-facemesh-landmark"], None)
mesh_faces = []
for i, d in enumerate(detections):
    points, score = mesh.landmarks(image, d)
    bx1, by1, bx2, by2 = (d.bbox[0] * width, d.bbox[1] * height, d.bbox[2] * width, d.bbox[3] * height)
    # A small margin around the detector box - the mesh legitimately extends a bit
    # beyond it (jaw/forehead), this only catches a badly wrong transform.
    margin = 0.35 * max(bx2 - bx1, by2 - by1)
    within = bool(
        points[:, 0].min() >= bx1 - margin and points[:, 0].max() <= bx2 + margin and
        points[:, 1].min() >= by1 - margin and points[:, 1].max() <= by2 + margin
    )
    mesh_faces.append({
        "landmark_shape": list(points.shape),
        "presence_score": round(score, 4),
        "x_range_px": [round(float(points[:, 0].min()), 1), round(float(points[:, 0].max()), 1)],
        "y_range_px": [round(float(points[:, 1].min()), 1), round(float(points[:, 1].max()), 1)],
        "detector_bbox_px": [round(bx1, 1), round(by1, 1), round(bx2, 1), round(by2, 1)],
        "landmarks_within_detector_bbox_plus_margin": within,
        "all_finite": bool(np.isfinite(points).all()),
    })
report["facemesh"] = {"face_count": len(mesh_faces), "faces": mesh_faces}
print("[facemesh] done", file=sys.stderr)
del mesh

modnet = engines.UnifaceMattingEngine(local_paths["uniface-modnet-matting"], None)
matte = modnet.matte(image)
report["modnet"] = {
    "matte_shape": list(matte.shape),
    "input_shape": [height, width],
    "shape_matches_input": list(matte.shape) == [height, width],
    "dtype": str(matte.dtype),
    "min": round(float(matte.min()), 4),
    "max": round(float(matte.max()), 4),
    "mean": round(float(matte.mean()), 4),
    "coverage_fraction_gt_0.5": round(float((matte > 0.5).mean()), 4),
    "all_finite": bool(np.isfinite(matte).all()),
}
print("[modnet] done", file=sys.stderr)
del modnet

# Written to a file rather than printed to stdout: onnxruntime/insightface's own C++-level
# logging ("Applied providers: ...") writes directly to stdout too, outside Python's own
# print() redirection - a real thing discovered while building this script, not assumed -
# so stdout cannot be trusted to contain only this JSON. The driver script
# (run_uniface_model_validation.py) reads this file back via `docker exec cat` instead.
with open("/tmp/uniface_validate/report.json", "w") as f:
    json.dump(report, f)
print("wrote /tmp/uniface_validate/report.json", file=sys.stderr)
