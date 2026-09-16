"""Runs INSIDE an ai-runtime container (via `docker exec`), driving the REAL
`/internal/v1/validate-infer` HTTP route over localhost.

Why in-container rather than from the host, like run_uniface_model_validation_crosscheck.py
does: ai-runtime has **no Traefik route, by design** (infra/docker-compose.yml says so
explicitly - it takes raw frames and returns raw detections with no tenant scoping, so
exposing it publicly would bypass the tenant boundary, TRD §16). There is therefore no host
port to call. Running the client inside the container is what makes this the real deployed
HTTP path rather than a local `import engines` - which is a genuine step up from how the 4
cross-check models had to be validated.

Reads a JSON job file (version ids + image paths + what to grade), writes a JSON report.
Progress goes to stderr: onnxruntime's C++-level logging writes straight to stdout, outside
Python's own redirection, so stdout is not trustworthy for machine-readable output here.
"""
import json
import os
import sys

import cv2
import httpx

BASE = "http://127.0.0.1:8000"


def iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter)


def detect(version_id, jpeg_bytes, confidence):
    """One real HTTP call to the real validate-infer route."""
    resp = httpx.post(
        f"{BASE}/internal/v1/validate-infer",
        data={"version_id": version_id, "confidence": str(confidence)},
        files={"frame": ("frame.jpg", jpeg_bytes, "image/jpeg")},
        timeout=300.0,
    )
    if resp.status_code != 200:
        return {"status_code": resp.status_code, "error": resp.text[:1000], "detections": []}
    dets = sorted(resp.json().get("detections", []), key=lambda d: -d["confidence"])
    return {
        "status_code": 200,
        "detections": [
            {
                "confidence": round(d["confidence"], 4),
                "bbox": [round(v, 4) for v in d["bbox"]],
                "keypoints": (
                    [[round(k[0], 4), round(k[1], 4)] for k in d["keypoints"]]
                    if d.get("keypoints")
                    else None
                ),
            }
            for d in dets
        ],
    }


def short_range_crop(image, box_norm, factor):
    """Crop a portrait-style framing around one face. Returns (jpeg_bytes, truth_bbox_in_crop).

    BlazeFace short-range cannot see a face that is a few percent of the frame; grading its
    recall on a wide street scene would measure the wrong thing. The crop factor is fixed by
    the manifest, applied identically to every face - not tuned per face to make something
    pass.
    """
    height, width = image.shape[:2]
    x1, y1, x2, y2 = (
        box_norm[0] * width, box_norm[1] * height, box_norm[2] * width, box_norm[3] * height,
    )
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    half = factor * max(x2 - x1, y2 - y1) / 2
    cx1, cy1 = max(0, int(cx - half)), max(0, int(cy - half))
    cx2, cy2 = min(width, int(cx + half)), min(height, int(cy + half))
    crop = image[cy1:cy2, cx1:cx2]
    ch, cw = crop.shape[:2]
    truth = [(x1 - cx1) / cw, (y1 - cy1) / ch, (x2 - cx1) / cw, (y2 - cy1) / ch]
    return cv2.imencode(".jpg", crop)[1].tobytes(), truth, [cw, ch]


def main() -> int:
    with open(sys.argv[1]) as fh:
        job = json.load(fh)
    scrfd_version = job["scrfd_version_id"]
    confidence = job.get("confidence", 0.5)
    report = {"models": {}}

    for model_name, spec in job["models"].items():
        version_id = spec["version_id"]
        per_image = []
        for item in spec["images"]:
            path = item["path"]
            with open(path, "rb") as fh:
                raw = fh.read()
            image = cv2.imread(path)
            entry = {
                "file": os.path.basename(path),
                "expected_face_count": item["expected_face_count"],
                "grade_on": item.get("grade_on", "full_frame"),
            }

            # Always record the full-frame result, even when recall is graded on crops:
            # "BlazeFace finds nothing on the wide frame" is itself a finding worth having
            # on the record rather than skipped because it isn't the graded condition.
            full = detect(version_id, raw, confidence)
            scrfd_full = detect(scrfd_version, raw, confidence)
            entry["full_frame"] = {
                "status_code": full["status_code"],
                "detection_count": len(full["detections"]),
                "scrfd_detection_count": len(scrfd_full["detections"]),
                "detections": full["detections"][:10],
            }
            if full.get("error"):
                entry["full_frame"]["error"] = full["error"]

            if item["expected_face_count"] == 0:
                # Negative control: any box at all is a false positive.
                entry["false_positives"] = len(full["detections"])
            elif entry["grade_on"] == "short_range_crop":
                faces = []
                for ref in scrfd_full["detections"]:
                    blob, truth, crop_size = short_range_crop(
                        image, ref["bbox"], job["crop_factor"]
                    )
                    got = detect(version_id, blob, confidence)
                    # SCRFD is re-run ON THE CROP, not reused from the full frame: the
                    # reference keypoints have to be in the same coordinate space as the
                    # ones being compared to them, or the recorded numbers look like a
                    # gross landmark error when they are only a change of frame.
                    scrfd_crop = detect(scrfd_version, blob, confidence)
                    best = max((iou(truth, d["bbox"]) for d in got["detections"]), default=0.0)
                    faces.append({
                        "crop_size": crop_size,
                        "scrfd_truth_bbox_in_crop": [round(v, 4) for v in truth],
                        "detection_count": len(got["detections"]),
                        "best_iou_vs_scrfd": round(best, 4),
                        "confidence": (
                            got["detections"][0]["confidence"] if got["detections"] else 0.0
                        ),
                        "keypoint_count": (
                            len(got["detections"][0]["keypoints"] or [])
                            if got["detections"] else 0
                        ),
                        "keypoints": (
                            got["detections"][0]["keypoints"] if got["detections"] else None
                        ),
                        "scrfd_keypoints_in_crop": (
                            scrfd_crop["detections"][0]["keypoints"]
                            if scrfd_crop["detections"] else None
                        ),
                    })
                entry["per_face"] = faces
            else:
                faces = []
                for ref in scrfd_full["detections"]:
                    best = max((iou(ref["bbox"], d["bbox"]) for d in full["detections"]), default=0.0)
                    match = max(
                        full["detections"],
                        key=lambda d: iou(ref["bbox"], d["bbox"]),
                        default=None,
                    )
                    faces.append({
                        "scrfd_bbox": ref["bbox"],
                        "best_iou_vs_scrfd": round(best, 4),
                        "confidence": match["confidence"] if match else 0.0,
                        "keypoint_count": len(match["keypoints"] or []) if match else 0,
                        "keypoints": match["keypoints"] if match else None,
                        "scrfd_keypoints": ref.get("keypoints"),
                    })
                entry["per_face"] = faces

            per_image.append(entry)
            print(
                f"{model_name} {entry['file']}: full_n={entry['full_frame']['detection_count']} "
                f"scrfd_n={entry['full_frame']['scrfd_detection_count']}",
                file=sys.stderr,
            )
        report["models"][model_name] = {"version_id": version_id, "images": per_image}

    # The detection-range sweep that turns "BlazeFace returned 0" from an assumption into a
    # measurement. Run once, on the first positive image, for every model - the comparison
    # across models is the point.
    sweep_spec = job.get("sweep")
    if sweep_spec:
        image = cv2.imread(sweep_spec["path"])
        with open(sweep_spec["path"], "rb") as fh:
            scrfd = detect(scrfd_version, fh.read(), confidence)
        ref = scrfd["detections"][0]["bbox"] if scrfd["detections"] else None
        rows = []
        if ref:
            for factor in sweep_spec["factors"]:
                blob, truth, crop_size = short_range_crop(image, ref, factor)
                row = {
                    "crop_factor": factor,
                    "crop_size": crop_size,
                    "face_fraction_of_crop_width": round(truth[2] - truth[0], 4),
                    "models": {},
                }
                for model_name, spec in job["models"].items():
                    got = detect(spec["version_id"], blob, confidence)
                    hits = [d for d in got["detections"] if iou(truth, d["bbox"]) > 0.3]
                    row["models"][model_name] = {
                        "found_real_face": bool(hits),
                        "confidence": round(max((h["confidence"] for h in hits), default=0.0), 4),
                    }
                rows.append(row)
                print(f"sweep factor={factor} facefrac={row['face_fraction_of_crop_width']}",
                      file=sys.stderr)
        report["detection_range_sweep"] = rows

    with open(job["report_path"], "w") as fh:
        json.dump(report, fh, indent=2)
    print("report written", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
