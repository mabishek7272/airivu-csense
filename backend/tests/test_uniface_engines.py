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
    FACE_ATTRIBUTE_LABELS,
    FACE_PARSING_LABELS,
    BiSeNetEngine,
    FaceAttribNetEngine,
    FaceStateResult,
    UnifaceEmbeddingEngine,
    UnifaceFaceMeshEngine,
    UnifaceMattingEngine,
    _faceattrib_letterbox,
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
    """A model_name this file doesn't recognise (the remaining undecoded uniface-zoo
    models, or anything future) must still reach the plain OnnxEngine path unchanged.

    This test used to use `uniface-retinaface-detect` as its stand-in for "unrecognised".
    That stopped being true when RetinaFace got a real decode (2026-09-16) and the test
    failed - correctly, and worth recording rather than quietly swapping the name: a
    registered model reaching `OnnxEngine` is precisely the silent-wrong-decode bug this
    suite exists to catch, so the assertion was right and its example had simply become
    stale. It now uses a name that is not, and will not be, in any dispatch table.
    """
    import app.engines as engines_mod

    unregistered = "not-a-real-model-name-for-dispatch-fallthrough"
    assert unregistered not in engines_mod._UNIFACE_ENGINES_BY_MODEL_NAME
    assert unregistered not in engines_mod._UNIFACE_EMBEDDING_FAMILIES
    assert unregistered not in engines_mod._INSIGHTFACE_MODEL_NAMES

    sentinel = object()
    monkeypatch.setitem(engines_mod.ENGINES_BY_RUNTIME, "onnxruntime", lambda path, labels: sentinel)
    result = build_engine("onnxruntime", Path("/fake.onnx"), None, "face_detection", unregistered)
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


# --- uniface-zoo face detectors: anchor maths and decode geometry ------------------------
#
# These are the checks that actually discriminate a correct anchor/decode port from a
# plausible-looking wrong one. Real-artifact inference is validated separately against real
# weights and real face images (CHECKLIST.md's own gate 3-4 entry for these 3); what a unit
# test CAN pin is the pure maths - the anchor grids, the weighted-NMS blend, and the
# coordinate conventions - with inputs whose correct answers are known independently.

def test_blazeface_anchor_grid_is_exactly_896_at_128px():
    """896 is the live artifact's own real head dimension (gate-2 probe: `regressors`
    (batch, 896, 16)). The anchor generator must land on exactly that number - this is the
    single check that catches a wrong MediaPipe SSD config, because a different stride set
    or scale count produces a different count, not subtly different boxes."""
    from app.engines import _BLAZEFACE_NUM_ANCHORS, _blazeface_anchors

    anchors = _blazeface_anchors(128)
    assert anchors.shape == (896, 2)
    assert _BLAZEFACE_NUM_ANCHORS == 896
    # 16x16 cells x 2 anchors = 512 at stride 8, 8x8 x 6 = 384 at stride 16.
    assert 512 + 384 == 896
    # Centres are normalised cell centres, so strictly inside (0, 1).
    assert anchors.min() > 0.0 and anchors.max() < 1.0


def test_blazeface_anchor_grid_refuses_a_mismatched_input_size():
    """A wrong input size silently yields a different anchor count; the port raises rather
    than decoding a 896-row head against the wrong grid."""
    from app.engines import OutputContractUnknownError, _blazeface_anchors

    with pytest.raises(OutputContractUnknownError):
        _blazeface_anchors(256)


def test_retinaface_anchor_grid_is_exactly_16800_at_640px():
    """16800 is what the real artifact returned for `loc`/`conf`/`landmarks` at a 640x640
    input in this session's own live gate-2 probe. Deriving the same number from the ported
    stride/min_size config is the evidence that the anchor grid belongs to THIS artifact
    rather than to some other RetinaFace training config."""
    from app.engines import _RETINAFACE_INPUT_SIZE, _retinaface_anchors

    priors = _retinaface_anchors(_RETINAFACE_INPUT_SIZE)
    # strides 8/16/32 over 640px -> 80x80 + 40x40 + 20x20 cells, 2 anchor sizes each.
    assert (80 * 80 + 40 * 40 + 20 * 20) * 2 == 16800
    assert priors.shape == (16800, 4)
    # Centre-offset form, normalised: cx, cy in (0,1); s_kx, s_ky are min_size/640.
    assert priors[:, :2].min() > 0.0 and priors[:, :2].max() < 1.0
    assert set(np.round(np.unique(priors[:, 2]) * 640).astype(int)) == {16, 32, 64, 128, 256, 512}


def test_retinaface_decode_boxes_recovers_the_prior_when_the_offset_is_zero():
    """A zero location prediction must decode to exactly the prior box itself. This pins
    both variances and the log-space width/height term: with loc=0, `exp(0)=1` leaves the
    prior size untouched and the centre unmoved, whatever the variance values are - so any
    decode that mangles the centre/size algebra shows up immediately."""
    from app.engines import _retinaface_decode_boxes

    priors = np.array([[0.5, 0.5, 0.2, 0.4]], dtype=np.float32)
    boxes = _retinaface_decode_boxes(np.zeros((1, 4), dtype=np.float32), priors)
    # cx=0.5, cy=0.5, w=0.2, h=0.4 -> xyxy
    assert np.allclose(boxes[0], [0.4, 0.3, 0.6, 0.7], atol=1e-6)


def test_retinaface_decode_landmarks_recovers_the_prior_centre_when_offsets_are_zero():
    """All five points collapse onto the prior centre at zero offset, and only the FIRST
    variance (0.1) scales them - landmarks have no log-space size term, unlike boxes."""
    from app.engines import _RETINAFACE_VARIANCES, _retinaface_decode_landmarks

    priors = np.array([[0.5, 0.25, 0.2, 0.4]], dtype=np.float32)
    points = _retinaface_decode_landmarks(np.zeros((1, 10), dtype=np.float32), priors)
    assert points.shape == (1, 5, 2)
    assert np.allclose(points[0], np.tile([0.5, 0.25], (5, 1)), atol=1e-6)

    # A unit offset on point 0's x moves it by variance[0] * prior width, nothing else.
    pred = np.zeros((1, 10), dtype=np.float32)
    pred[0, 0] = 1.0
    moved = _retinaface_decode_landmarks(pred, priors)
    assert np.isclose(moved[0, 0, 0], 0.5 + _RETINAFACE_VARIANCES[0] * 0.2, atol=1e-6)
    assert np.isclose(moved[0, 0, 1], 0.25, atol=1e-6)
    assert np.allclose(moved[0, 1:], np.tile([0.5, 0.25], (4, 1)), atol=1e-6)


def test_uniface_nms_keeps_the_best_box_and_drops_its_duplicate():
    """The ported NMS takes an already-score-sorted input and uses upstream's `+1` area
    convention. Two near-identical boxes must collapse to one; a distant box survives."""
    from app.engines import _uniface_nms

    dets = np.array(
        [
            [10, 10, 50, 50, 0.9],
            [11, 11, 51, 51, 0.8],  # ~same box, lower score -> suppressed
            [200, 200, 240, 240, 0.7],  # elsewhere -> kept
        ],
        dtype=np.float32,
    )
    keep = _uniface_nms(dets, 0.4)
    assert keep == [0, 2]


def test_blazeface_weighted_nms_blends_rather_than_discards():
    """MediaPipe's weighted NMS score-averages overlapping candidates into the winner
    instead of dropping them - the reason the plain IoU-discard NMS above cannot be
    substituted. Two overlapping boxes must produce ONE row whose centre is the
    score-weighted average of both, not simply the higher-scoring box's own centre."""
    from app.engines import _blazeface_weighted_nms

    # rows: (score, cx, cy, w, h) - keypoint columns omitted, the blend is column-agnostic.
    rows = np.array(
        [
            [0.8, 0.50, 0.50, 0.2, 0.2],
            [0.4, 0.54, 0.50, 0.2, 0.2],
        ]
    )
    out = _blazeface_weighted_nms(rows, 0.3)
    assert out.shape[0] == 1
    assert out[0, 0] == 0.8  # winner keeps its own score
    # centre_x = (0.8*0.50 + 0.4*0.54) / 1.2
    assert np.isclose(out[0, 1], (0.8 * 0.50 + 0.4 * 0.54) / 1.2)
    # A discard-style NMS would have left 0.50 untouched.
    assert not np.isclose(out[0, 1], 0.50)


def test_blazeface_weighted_nms_terminates_on_a_zero_area_box():
    """Upstream's `merge[0] = True` guard: a zero-area box has an IoU of 0 against itself,
    so relying on self-overlap to clear the threshold would loop forever."""
    from app.engines import _blazeface_weighted_nms

    rows = np.array([[0.9, 0.5, 0.5, 0.0, 0.0]])
    out = _blazeface_weighted_nms(rows, 0.3)
    assert out.shape[0] == 1


def test_face_detections_clips_boxes_but_not_keypoints():
    """Deliberate asymmetry, documented in `_face_detections`: a box is clipped to the
    frame because every downstream consumer treats `Detection.bbox` as an in-frame region,
    while a real ear/jaw landmark can genuinely sit outside the frame and clamping it would
    fabricate a landmark on the border rather than report where the model put it."""
    from app.engines import _face_detections

    boxes = np.array([[-20.0, -10.0, 120.0, 90.0]])
    scores = np.array([0.77])
    keypoints = np.array([[[-20.0, 50.0], [110.0, 50.0]]])
    dets = _face_detections(boxes, scores, keypoints, width=100, height=100, labels={})

    assert len(dets) == 1
    assert dets[0].class_name == "face"
    assert dets[0].bbox == (0.0, 0.0, 1.0, 0.9)  # clipped
    assert dets[0].keypoints[0][0] == pytest.approx(-0.2)  # NOT clipped
    assert dets[0].keypoints[1][0] == pytest.approx(1.1)
    # Keypoints carry the box's own score - none of these models emits a per-point one.
    assert all(kp[2] == pytest.approx(0.77) for kp in dets[0].keypoints)


@pytest.mark.parametrize(
    "model_name,expected_cls_name",
    [
        ("uniface-blazeface-detect", "BlazeFaceEngine"),
        ("uniface-centerface-detect", "CenterFaceEngine"),
        ("uniface-retinaface-detect", "RetinaFaceEngine"),
    ],
)
def test_build_engine_dispatches_the_three_detectors_by_model_name(
    model_name, expected_cls_name, monkeypatch
):
    """All 3 carry task_code="face_detection" - the same task_code `insightface-buffalo-l-
    detect` (SCRFD) already owns. They must dispatch by exact model_name and must NOT reach
    InsightFaceEngine, whose `insightface.model_zoo` router is built for a different
    architecture entirely. Same regression this suite already pins for face_recognition."""
    import app.engines as engines_mod

    def _boom(*args, **kwargs):
        raise AssertionError(f"{model_name} must not reach InsightFaceEngine")

    monkeypatch.setattr(engines_mod, "InsightFaceEngine", _boom)

    class _Tensor:
        name = "input"
        shape = ["batch", 3, 128, 128]

    class _Session:
        def get_inputs(self):
            return [_Tensor()]

        def get_outputs(self):
            return [_Tensor()]

    monkeypatch.setattr(
        engines_mod, "_onnx_session", lambda path: (_Session(), ["CPUExecutionProvider"])
    )

    engine = build_engine(
        "onnxruntime", Path("/fake.onnx"), None, "face_detection", model_name
    )
    assert type(engine).__name__ == expected_cls_name


def test_the_three_detectors_are_registered_and_insightface_still_wins_its_own_names():
    """Belt-and-braces on the dispatch table itself: the 3 new names are present, and
    adding them did not disturb the 2 legacy InsightFace names that share their task_code."""
    import app.engines as engines_mod

    table = engines_mod._UNIFACE_ENGINES_BY_MODEL_NAME
    assert table["uniface-blazeface-detect"] is engines_mod.BlazeFaceEngine
    assert table["uniface-centerface-detect"] is engines_mod.CenterFaceEngine
    assert table["uniface-retinaface-detect"] is engines_mod.RetinaFaceEngine
    assert "insightface-buffalo-l-detect" not in table
    assert "insightface-buffalo-l-detect" in engines_mod._INSIGHTFACE_MODEL_NAMES


def test_blazeface_six_keypoints_are_rejected_by_the_five_point_alignment_paths():
    """BlazeFace emits 6 MediaPipe keypoints, not the 5-point ArcFace alignment template.
    A BlazeFace Detection must therefore FAIL loudly at `embed()` rather than being
    silently mis-aligned - upstream marks the same distinction as
    `supports_alignment = False`. This asserts the existing guard actually covers the
    6-point case, not just the too-few case the older test pins."""
    from app.engines import Detection, OutputContractUnknownError

    engine = UnifaceEmbeddingEngine.__new__(UnifaceEmbeddingEngine)
    six_point = Detection(
        class_id=0, class_name="face", confidence=0.9, bbox=(0.1, 0.1, 0.5, 0.5),
        keypoints=[(0.1 * i, 0.1 * i, 0.9) for i in range(6)],
    )
    with pytest.raises(OutputContractUnknownError):
        engine.embed(np.zeros((10, 10, 3), dtype=np.uint8), six_point)


def test_centerface_resize_rounds_each_axis_up_to_a_multiple_of_32_independently():
    """CenterFace's FPN needs both sides divisible by 32, and each side is rounded up
    independently - which is exactly why the decode must carry TWO scale factors and apply
    them per axis. A single shared factor would skew every box on a non-square frame."""
    from app.engines import CenterFaceEngine

    engine = CenterFaceEngine.__new__(CenterFaceEngine)
    image = np.zeros((519, 713, 3), dtype=np.uint8)
    resized, scale_w, scale_h = engine._resize(image)

    assert resized.shape[0] % 32 == 0 and resized.shape[1] % 32 == 0
    assert scale_w == pytest.approx(resized.shape[1] / 713)
    assert scale_h == pytest.approx(resized.shape[0] / 519)
    assert scale_w != scale_h  # the whole point: the axes really do differ


def test_centerface_resize_caps_large_frames_but_never_upscales_small_ones():
    """The 640x640 cap bounds CPU cost on a mainstream frame (CLAUDE.md: no GPU on the
    prod box); anything already inside the cap runs at its own resolution, never upscaled.

    The real 704x576 substream this platform actually infers on is recorded here because
    it is NOT left alone - 704 exceeds the 640 cap, so it is scaled to 640x544. That was
    this test's own first (wrong) assumption, caught by the test failing; the engine was
    right. Worth pinning as real behaviour rather than deleting: the substream is the
    platform's normal inference source, so "CenterFace downscales it slightly" is a fact
    about production, not a corner case.
    """
    from app.engines import CenterFaceEngine

    engine = CenterFaceEngine.__new__(CenterFaceEngine)

    big, _, _ = engine._resize(np.zeros((1520, 2592, 3), dtype=np.uint8))
    assert big.shape[1] <= 640 + 31

    # The real 704x576 substream: capped on width, so both axes scale.
    substream, scale_w, scale_h = engine._resize(np.zeros((576, 704, 3), dtype=np.uint8))
    assert substream.shape[:2] == (544, 640)
    assert scale_w < 1.0 and scale_h < 1.0

    # Genuinely inside the cap and already 32-aligned: untouched, both factors exactly 1.
    small, scale_w, scale_h = engine._resize(np.zeros((480, 640, 3), dtype=np.uint8))
    assert small.shape[:2] == (480, 640)
    assert (scale_w, scale_h) == (1.0, 1.0)
# --- BiSeNet parsing / FaceAttribNet attributes -------------------------------------------
#
# The 2 models CHECKLIST.md previously recorded as "should not be decoded on a guess at
# all". Same discipline as everything above: real artifact inference is validated
# separately against real weights and a real image (scripts/
# run_uniface_model_validation_parsing_attrib.py), and these tests pin only what a unit
# test genuinely can - the dispatch table, the ported label orders, and the pure
# preprocessing math.


@pytest.mark.parametrize(
    ("model_name", "expected_cls"),
    [
        ("uniface-bisenet-parsing", BiSeNetEngine),
        ("uniface-faceattribnet-attributes", FaceAttribNetEngine),
    ],
)
def test_build_engine_dispatches_the_two_resolved_models(model_name, expected_cls, monkeypatch):
    """These 2 must reach their own engines, not the generic OnnxEngine fall-through they
    used to hit. That fall-through is not a harmless no-op: `OnnxEngine._decode` only
    understands the YOLO 6/7-column layout, and the cross-check work already found it
    silently returning zero detections rather than erroring when a non-YOLO output
    happened to have a matching column count."""
    import app.engines as engines_mod

    sentinel = object()
    monkeypatch.setitem(engines_mod.ENGINES_BY_RUNTIME, "onnxruntime", lambda path, labels: sentinel)
    monkeypatch.setattr(expected_cls, "__init__", lambda self, path, labels=None: None)

    engine = build_engine("onnxruntime", Path("/fake.onnx"), None, "face_parsing", model_name)
    assert isinstance(engine, expected_cls)
    assert engine is not sentinel


def test_face_parsing_labels_match_the_reference_19_class_scheme():
    """Ported verbatim from the reference's own `uniface/draw.py::FACE_PARSING_LABELS`.
    Index 0 must be `background` (argmax returns a class id, and the endpoint indexes this
    tuple directly), and the tuple must be exactly 19 long - the model's own class axis."""
    assert len(FACE_PARSING_LABELS) == 19
    assert FACE_PARSING_LABELS[0] == "background"
    assert FACE_PARSING_LABELS[1] == "skin"
    assert FACE_PARSING_LABELS[6] == "eye_g"
    assert FACE_PARSING_LABELS[17] == "hair"
    assert len(set(FACE_PARSING_LABELS)) == 19  # no duplicate would-be-ambiguous names


def test_face_attribute_labels_are_the_reference_column_order():
    """The one fact that was previously called unrecoverable. This exact order is what
    `uniface/attribute/faceattribnet.py::postprocess` unpacks the (1,5) tensor into; a
    reordering here would silently mislabel every attribute the model reports."""
    assert FACE_ATTRIBUTE_LABELS == (
        "left_eye_open", "right_eye_open", "eyeglasses", "mask", "sunglasses",
    )


def test_face_state_result_as_dict_round_trips_in_label_order():
    result = FaceStateResult(
        left_eye_open=0.1, right_eye_open=0.2, eyeglasses=0.3, mask=0.4, sunglasses=0.5
    )
    assert result.as_dict() == {
        "left_eye_open": 0.1, "right_eye_open": 0.2, "eyeglasses": 0.3,
        "mask": 0.4, "sunglasses": 0.5,
    }
    assert tuple(result.as_dict()) == FACE_ATTRIBUTE_LABELS


def test_faceattrib_letterbox_pads_with_zeros_and_preserves_aspect_ratio():
    """A 2:1 landscape crop must land centred in a square canvas with ZERO padding (the
    reference passes `fill_value=0`, not `letterbox_resize`'s own 114 grey default) and
    with its aspect ratio intact - a plain resize would stretch the face, and grey padding
    would feed the model a border it never saw in training."""
    crop = np.full((64, 128, 3), 255, dtype=np.uint8)  # white, 2:1
    blob = _faceattrib_letterbox(crop, 128)

    assert blob.shape == (1, 3, 128, 128)
    assert blob.dtype == np.float32
    # Scaled to [0,1] only - no mean/std here, that is baked into the ONNX graph.
    assert blob.max() == pytest.approx(1.0)
    # The image occupies the middle 64 rows; the rows above/below are zero padding.
    assert blob[0, :, 0, :].max() == pytest.approx(0.0)
    assert blob[0, :, 127, :].max() == pytest.approx(0.0)
    assert blob[0, :, 64, :].min() == pytest.approx(1.0)


def test_faceattrib_letterbox_converts_bgr_to_rgb():
    """`letterbox_resize` converts BGR->RGB before normalising. A pure-blue BGR crop must
    therefore come back as channel 2 (blue in RGB), not channel 0."""
    crop = np.zeros((32, 32, 3), dtype=np.uint8)
    crop[:, :, 0] = 255  # BGR blue
    blob = _faceattrib_letterbox(crop, 128)

    centre = blob[0, :, 64, 64]
    assert centre[0] == pytest.approx(0.0)  # R
    assert centre[2] == pytest.approx(1.0)  # B


def test_faceattrib_letterbox_rejects_a_non_uint8_crop():
    """Pasting a float image onto the uint8 canvas would truncate it to zeros and the model
    would return confident nonsense with no error - the reference guards this for the same
    reason (`uniface.common.validate_image`)."""
    from app.engines import OutputContractUnknownError

    with pytest.raises(OutputContractUnknownError):
        _faceattrib_letterbox(np.zeros((32, 32, 3), dtype=np.float32), 128)


def test_bisenet_engine_infer_raises_output_contract_unknown():
    engine = BiSeNetEngine.__new__(BiSeNetEngine)
    from app.engines import OutputContractUnknownError

    with pytest.raises(OutputContractUnknownError, match="parse"):
        engine.infer(np.zeros((10, 10, 3), dtype=np.uint8))


def test_faceattribnet_engine_infer_raises_output_contract_unknown():
    engine = FaceAttribNetEngine.__new__(FaceAttribNetEngine)
    from app.engines import OutputContractUnknownError

    with pytest.raises(OutputContractUnknownError, match="predict_face_state"):
        engine.infer(np.zeros((10, 10, 3), dtype=np.uint8))


def test_bisenet_default_crop_margin_is_nonzero():
    """Regression guard for a real, measured finding: at the reference's own margin of 0.0
    this model returns a degenerate 100%-`background` mask on both real faces in this
    project's only face fixture (SCRFD boxes of 36x52 and 40x50 px). Dropping the expansion
    back to 0 would silently reintroduce that - a mask a caller could easily read as
    "no face here". See engines.py's own _BISENET_DEFAULT_CROP_MARGIN comment for the full
    7-value sweep behind the default."""
    from app.engines import _BISENET_DEFAULT_CROP_MARGIN

    assert _BISENET_DEFAULT_CROP_MARGIN > 0.0
