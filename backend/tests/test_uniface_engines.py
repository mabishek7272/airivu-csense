"""uniface-zoo decode: model_name dispatch, and the pure preprocessing/geometry math these
6 engines own (real artifact inference itself is validated separately, against real
weights and a real image - see scripts/run_uniface_model_validation.py and CHECKLIST.md's
"18 models pulled in `uploaded`" entry; that is not something a unit test can substitute
for, and this file does not try to).

What these tests DO pin, with synthetic inputs and known-correct answers, same discipline
as test_ai_runtime_decoder.py: the dispatch table that decides which engine class a given
model_name gets (the exact bug class this file exists to prevent - task_code alone would
have routed 4 of these 6 models into InsightFaceEngine, a decoder for a different
architecture entirely, see engines.py's own comment on `_INSIGHTFACE_MODEL_NAMES`), the
AdaFace-vs-everyone-else BGR/RGB preprocessing split, and the FaceMesh ROI/inverse-affine
geometry (a wrong sign or axis swap here produces landmarks that "work" - the model runs,
returns finite numbers - but sit nowhere near the actual face, exactly the silent-wrong-
decode failure mode this project has already been bitten by twice).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

AI_RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "ai_runtime"
if str(AI_RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_RUNTIME_ROOT))

from app.engines import (  # noqa: E402
    UnifaceEmbeddingEngine,
    UnifaceFaceMeshEngine,
    UnifaceMattingEngine,
    _facemesh_roi_from_box,
    _facemesh_warp_roi,
    _uniface_recognition_blob,
    build_engine,
)

# --- dispatch ---------------------------------------------------------------------------

def test_build_engine_dispatches_insightface_by_model_name(monkeypatch):
    """The 2 legacy artifacts still reach InsightFaceEngine - dispatch moved from
    task_code to model_name, but these 2 names must still resolve the same way."""
    import app.engines as engines_mod

    calls = []
    monkeypatch.setattr(
        engines_mod, "InsightFaceEngine",
        lambda path, labels, task_code: calls.append((path, labels, task_code)) or "insightface-engine",
    )
    result = build_engine(
        "onnxruntime", Path("/fake.onnx"), None, "face_detection", "insightface-buffalo-l-detect"
    )
    assert result == "insightface-engine"
    assert calls == [(Path("/fake.onnx"), None, "face_detection")]


@pytest.mark.parametrize(
    "model_name",
    [
        "uniface-adaface-recognition",
        "uniface-edgeface-recognition",
        "uniface-mobileface-recognition",
        "uniface-sphereface-recognition",
    ],
)
def test_build_engine_face_recognition_task_code_does_not_reach_insightface(model_name, monkeypatch):
    """Regression test for the real dispatch collision this session found: these 4 models
    share task_code="face_recognition" with insightface-buffalo-l-recognition (confirmed
    against the real manifests), so a task_code-only dispatch would have routed them into
    InsightFaceEngine - a decoder for an entirely different architecture. Patches
    onnxruntime.InferenceSession out (no real artifact needed) and asserts the class
    actually instantiated is UnifaceEmbeddingEngine, not InsightFaceEngine."""
    import app.engines as engines_mod

    class _FakeInput:
        name = "input"
        shape = ["batch_size", 3, 112, 112]

    class _FakeSession:
        def __init__(self, *a, **k):
            pass

        def get_inputs(self):
            return [_FakeInput()]

    def _boom(*a, **k):
        raise AssertionError("InsightFaceEngine must not be constructed for a uniface model")

    monkeypatch.setattr(engines_mod, "InsightFaceEngine", _boom)
    monkeypatch.setitem(sys.modules, "onnxruntime", None)  # ensure a fresh import each time
    import types

    fake_ort = types.SimpleNamespace(
        InferenceSession=_FakeSession, get_available_providers=lambda: ["CPUExecutionProvider"]
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)

    engine = build_engine("onnxruntime", Path("/fake.onnx"), None, "face_recognition", model_name)
    assert isinstance(engine, UnifaceEmbeddingEngine)
    assert engine._family == model_name.split("-")[1]  # e.g. "uniface-adaface-recognition" -> "adaface"


def test_build_engine_falls_through_to_generic_onnx_for_unknown_model_name(monkeypatch):
    """A model_name this file doesn't recognise (the other 9 uniface-zoo models, or
    anything future) must still reach the plain OnnxEngine path unchanged."""
    import app.engines as engines_mod

    sentinel = object()
    monkeypatch.setitem(engines_mod.ENGINES_BY_RUNTIME, "onnxruntime", lambda path, labels: sentinel)
    result = build_engine("onnxruntime", Path("/fake.onnx"), None, "face_detection", "uniface-retinaface-detect")
    assert result is sentinel


# --- recognition preprocessing (AdaFace BGR vs. everyone-else RGB) ----------------------

def test_recognition_blob_adaface_keeps_bgr_others_convert_to_rgb():
    """A 2x2 crop with distinct, recognisable channel values at each corner - if the BGR/
    RGB split were backwards for either branch, the red and blue channels would swap and
    this would catch it immediately (same failure class as the plate detector's class/
    score column swap - confirmed against literal upstream source in engines.py's own
    module comment, not assumed from symmetry)."""
    # BGR crop: pure blue top-left, pure green top-right, pure red bottom-left.
    crop = np.zeros((2, 2, 3), dtype=np.uint8)
    crop[0, 0] = (255, 0, 0)  # BGR blue
    crop[0, 1] = (0, 255, 0)  # BGR green
    crop[1, 0] = (0, 0, 255)  # BGR red

    adaface_blob = _uniface_recognition_blob(crop, "adaface")
    other_blob = _uniface_recognition_blob(crop, "mobileface")

    assert adaface_blob.shape == (1, 3, 112, 112)
    assert other_blob.shape == (1, 3, 112, 112)

    # AdaFace: channel 0 (of the NCHW blob) is still BGR's blue channel -> top-left pixel
    # normalises to (255-127.5)/127.5 = 1.0 on channel 0, and to -1.0 on channels 1/2.
    assert adaface_blob[0, 0, 0, 0] == pytest.approx(1.0)
    assert adaface_blob[0, 1, 0, 0] == pytest.approx(-1.0)
    assert adaface_blob[0, 2, 0, 0] == pytest.approx(-1.0)

    # Everyone else: converted to RGB first, so channel 0 is now the RED channel - the
    # same top-left pixel (BGR blue, i.e. RGB (0,0,255)) is -1.0 on channel 0 (red) and
    # 1.0 on channel 2 (blue).
    assert other_blob[0, 0, 0, 0] == pytest.approx(-1.0)
    assert other_blob[0, 2, 0, 0] == pytest.approx(1.0)


# --- FaceMesh ROI / inverse-affine geometry ----------------------------------------------

def test_facemesh_roi_from_box_axis_aligned():
    """A square box with level eyes (same y) must produce angle=0 and a side 1.5x the
    box's own longer edge (uniface's own MediaPipe-matching 1.5x/margin=0.25 recipe)."""
    bbox = (100.0, 100.0, 200.0, 200.0)  # 100x100 box, center (150, 150)
    eyes = [(120.0, 130.0), (180.0, 130.0)]  # level eyes -> angle 0
    cx, cy, side, angle = _facemesh_roi_from_box(bbox, eyes)
    assert cx == pytest.approx(150.0)
    assert cy == pytest.approx(150.0)
    assert side == pytest.approx(150.0)  # (1 + 2*0.25) * 100
    assert angle == pytest.approx(0.0)


def test_facemesh_roi_from_box_rotated_eyes_produce_nonzero_angle():
    """A 45-degree eye tilt (right eye lower than left) must roll the ROI - this is the
    exact geometry a leveling bug would silently drop, degrading the mesh on any tilted
    head without ever raising an error."""
    bbox = (0.0, 0.0, 100.0, 100.0)
    eyes = [(0.0, 0.0), (100.0, 100.0)]  # a straight 45-degree diagonal
    _, _, _, angle = _facemesh_roi_from_box(bbox, eyes)
    assert angle == pytest.approx(45.0)


def test_facemesh_warp_roi_inverse_maps_center_back_to_itself():
    """The inverse affine `warp_roi` returns must map the crop's own center back to the
    ROI's center in full-image coordinates - the exact property `landmarks()` relies on to
    place crop-space landmarks back onto the real frame. A sign error or transposed axis
    in the forward/inverse matrix construction would fail this immediately."""
    image = np.zeros((400, 400, 3), dtype=np.uint8)
    roi = (200.0, 200.0, 100.0, 0.0)  # center (200,200), side 100, no rotation
    size = 50
    _, inverse = _facemesh_warp_roi(image, roi, size)

    crop_center = np.array([size / 2.0, size / 2.0, 1.0])
    mapped = inverse @ crop_center
    assert mapped[0] == pytest.approx(200.0, abs=1e-6)
    assert mapped[1] == pytest.approx(200.0, abs=1e-6)


# --- construction sanity (no real artifact needed) ---------------------------------------

def test_uniface_facemesh_engine_landmarks_requires_two_keypoints():
    engine = UnifaceFaceMeshEngine.__new__(UnifaceFaceMeshEngine)
    from app.engines import Detection, OutputContractUnknownError

    detection = Detection(class_id=0, class_name="face", confidence=0.9, bbox=(0, 0, 1, 1), keypoints=None)
    with pytest.raises(OutputContractUnknownError):
        engine.landmarks(np.zeros((10, 10, 3), dtype=np.uint8), detection)


def test_uniface_embedding_engine_embed_requires_five_keypoints():
    engine = UnifaceEmbeddingEngine.__new__(UnifaceEmbeddingEngine)
    from app.engines import Detection, OutputContractUnknownError

    detection = Detection(
        class_id=0, class_name="face", confidence=0.9, bbox=(0, 0, 1, 1),
        keypoints=[(0.1, 0.1, 0.9), (0.2, 0.2, 0.9)],  # only 2, not 5
    )
    with pytest.raises(OutputContractUnknownError):
        engine.embed(np.zeros((10, 10, 3), dtype=np.uint8), detection)


def test_uniface_matting_engine_infer_raises_output_contract_unknown():
    engine = UnifaceMattingEngine.__new__(UnifaceMattingEngine)
    from app.engines import OutputContractUnknownError

    with pytest.raises(OutputContractUnknownError):
        engine.infer(np.zeros((10, 10, 3), dtype=np.uint8))
