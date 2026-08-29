"""Renders the public demo site's showcase images.

Source frames are curated real snapshots pulled from a separate, legacy CSense
deployment's own camera archive (a real, live customer's vehicle-lot camera) - see the
demo-site plan for how and why, and CHECKLIST.md once this lands. They live outside this
repo, never committed: point INPUT_DIR at wherever you downloaded the curated set.

**The `general` and `person` categories use the source frames as-is** (plates blurred,
nothing else changed) rather than re-annotating with this project's own `draw_detections`.
That was the first thing tried, and it looked exactly as bad as it sounds: the legacy
system's own boxes and captions are baked into the pixels, and drawing a second set on top
in a different style produced a cluttered, unprofessional double layer - proven by
actually rendering one and looking at it, not assumed. The legacy annotation itself is
worth keeping regardless: the artifact weights that produced it were migrated 1:1 into
this project's own registry (SHA-256-verified), so it already represents *this product's*
detection, not a system being retired.

**The `plates` category is the one place this project's own rendering is added**, because
nothing in the source frame already marks a plate - `license-plate-detector` runs, then
`mask_regions` blurs every detected plate, then `draw_detections` draws a single grey
(non-alert) box around each one, visually distinct from the legacy boxes' green: "detected
and automatically redacted," not "here is another vehicle."

Inference runs inside the `ai-runtime` container - it is deliberately not reachable from
the host (see backend/ai_runtime/app/main.py's own docstring on why) - via `docker compose
exec`, the same technique scripts/e2e_detection_to_incident.py already established. Image
bytes go over stdin so nothing needs a bind mount. Drawing/blurring reuse
`csense_shared.pipeline.evidence`'s existing pure functions unchanged.

    python scripts/build_demo_assets.py [--input DIR] [--output DIR]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import cv2
import numpy as np
from csense_shared.pipeline.evidence import (
    Annotation,
    draw_detections,
    encode_jpeg,
    mask_regions,
)

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DEFAULT_INPUT = os.path.join(REPO, "..", "curate-review", "final")
DEFAULT_OUTPUT = os.path.join(REPO, "frontend", "demo-site", "public", "showcase")

# Categories whose source frames are used as-is (plates blurred, nothing redrawn).
PASSTHROUGH_CATEGORIES = ("general", "person")
# The one category this project's own model/drawing actually adds something to the frame.
PLATE_CATEGORY = "plates"
PLATE_MODEL = "license-plate-detector"

# The plate detector's ONNX export is already end-to-end (its own NMS baked in), so it
# only ever hands back a handful of candidates it already considers real - there is no
# flood of raw, un-NMS'd boxes to worry about the way there is for a plain YOLO head. That
# means there is little cost to reading it at a very low confidence: on a real curated
# frame, a genuine plate on a car at typical camera distance scored as low as 0.06 (it
# would have been silently dropped at the 0.4 this script started with). Every frame gets
# blurred at this low, recall-favouring threshold regardless of category - missing a real
# plate is a privacy failure, blurring a spot that turns out not to be one is not.
BLUR_CONFIDENCE = "0.03"
# A single degenerate candidate covering ~90% of the frame showed up at this same low
# confidence (an artifact of the export, not a plate at any real camera distance) -
# rejected by area rather than by raising the confidence back up, which would have thrown
# the real low-confidence plate out again too.
MAX_PLATE_AREA_FRACTION = 0.12

# Reads image bytes from stdin, POSTs to ai-runtime's own internal-only /infer endpoint,
# prints the JSON response. Model name and confidence come through argv, not templated
# into the script text, so this string never needs per-call escaping.
_INFER_SCRIPT = r"""
import sys, urllib.request, urllib.error

model_name, confidence = sys.argv[1], sys.argv[2]
data = sys.stdin.buffer.read()
boundary = "democonfigboundary"

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
        [
            "docker", "compose", "--env-file", "../.env", "exec", "-T", "ai-runtime",
            "python3", "-c", _INFER_SCRIPT, model_name, confidence,
        ],
        cwd=os.path.join(REPO, "infra"),
        input=image_bytes,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"inference failed for {model_name}: {result.stderr.decode()[:300]}"
        )
    return json.loads(result.stdout.decode())


def _box_area_fraction(bbox: tuple[float, float, float, float]) -> float:
    x1, y1, x2, y2 = bbox
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def find_plates(image_bytes: bytes) -> list[dict]:
    """Runs the plate detector once, at a low recall-favouring confidence, and drops
    candidates too large to plausibly be a plate (see MAX_PLATE_AREA_FRACTION). The
    result is reused for both blurring (every category) and, for the plates category,
    drawing boxes - one inference call, one filter, no risk of the two disagreeing."""
    result = infer(image_bytes, PLATE_MODEL, BLUR_CONFIDENCE)
    return [
        d for d in result["detections"]
        if _box_area_fraction(tuple(d["bbox"])) <= MAX_PLATE_AREA_FRACTION
    ]


def blur_plates(image_bytes: bytes) -> tuple[np.ndarray, list[dict]]:
    """Every frame goes through this, regardless of category - a plate must never be
    visible incidentally, only when the plate category deliberately shows one redacted."""
    image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("could not decode frame")
    detections = find_plates(image_bytes)
    boxes = [tuple(d["bbox"]) for d in detections]
    return mask_regions(image, boxes), detections


def render_passthrough(image_bytes: bytes) -> bytes:
    masked, detections = blur_plates(image_bytes)
    print(f"    {len(detections)} plate(s) blurred, legacy annotation kept as-is")
    return encode_jpeg(masked, quality=88)


def render_plate_showcase(image_bytes: bytes) -> bytes:
    masked, detections = blur_plates(image_bytes)
    annotations = [
        Annotation(bbox=tuple(d["bbox"]), label="license_plate", confidence=d["confidence"], triggered=False)
        for d in detections
    ]
    annotated = draw_detections(masked, annotations)
    print(f"    {len(detections)} plate(s) detected, blurred, and boxed")
    return encode_jpeg(annotated, quality=88)


def main() -> int:
    input_dir = DEFAULT_INPUT
    output_dir = DEFAULT_OUTPUT
    args = sys.argv[1:]
    if "--input" in args:
        input_dir = args[args.index("--input") + 1]
    if "--output" in args:
        output_dir = args[args.index("--output") + 1]

    manifest: dict[str, list[str]] = {}
    categories = [*PASSTHROUGH_CATEGORIES, PLATE_CATEGORY]
    for category in categories:
        src_dir = os.path.join(input_dir, category)
        if not os.path.isdir(src_dir):
            print(f"[{category}] no source directory at {src_dir} - skipping")
            continue
        dst_dir = os.path.join(output_dir, category)
        os.makedirs(dst_dir, exist_ok=True)

        files = sorted(f for f in os.listdir(src_dir) if f.lower().endswith(".jpg"))
        print(f"\n[{category}] {len(files)} source frame(s)")
        manifest[category] = [f"{i:02d}.jpg" for i in range(1, len(files) + 1)]
        for index, filename in enumerate(files, start=1):
            with open(os.path.join(src_dir, filename), "rb") as handle:
                raw = handle.read()
            print(f"  {index:02d}/{len(files)}  {filename}")
            rendered = (
                render_plate_showcase(raw)
                if category == PLATE_CATEGORY
                else render_passthrough(raw)
            )
            out_path = os.path.join(dst_dir, f"{index:02d}.jpg")
            with open(out_path, "wb") as handle:
                handle.write(rendered)

    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w") as handle:
        json.dump(manifest, handle, indent=2)
    print(f"\nWrote {manifest_path}")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
