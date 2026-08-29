"""InsightFace decode: SCRFD detection glue, ArcFace recognition glue.

Neither model's real decode math is re-tested here - that is insightface's own job, and the
whole reason `InsightFaceEngine` delegates to it rather than re-deriving SCRFD's anchor
decode or ArcFace's alignment by hand (see the class docstring in engines.py). What these
tests pin is the glue code this file actually owns: converting the package's own `(bboxes,
kpss)` output into this runtime's `Detection` shape - correct normalisation, orientation,
empty-result handling - and the reverse for `embed()` (a `Detection`'s normalised keypoints
back to pixel-space landmarks before alignment). None of this needs the real `insightface`
package or real artifact bytes - `detect()`/`get_feat()` are mocked out, same as the real
ONNX session is bypassed in test_ai_runtime_decoder.py's `__new__` pattern.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest

AI_RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "ai_runtime"
if str(AI_RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_RUNTIME_ROOT))

from app.engines import Detection, InsightFaceEngine, OutputContractUnknownError  # noqa: E402

IMAGE = np.zeros((480, 640, 3), dtype=np.uint8)  # height=480, width=640


class _FakeSCRFD:
    """Stands in for insightface.model_zoo's own SCRFD wrapper - only `.detect()` and the
    `.det_thresh` attribute InsightFaceEngine.infer() actually touches."""

    def __init__(self, bboxes: np.ndarray, kpss: np.ndarray | None):
        self.det_thresh = 0.5
        self._bboxes = bboxes
        self._kpss = kpss
        self.detect_calls: list[float] = []

    def detect(self, image):
        self.detect_calls.append(self.det_thresh)
        return self._bboxes, self._kpss


class _FakeArcFace:
    """Stands in for insightface.model_zoo's own ArcFaceONNX wrapper - only `.get_feat()`
    and `.input_size` are touched."""

    def __init__(self, feat: np.ndarray):
        self.input_size = (112, 112)
        self._feat = feat
        self.get_feat_calls: list[np.ndarray] = []

    def get_feat(self, aligned_image):
        self.get_feat_calls.append(aligned_image)
        return self._feat


def _detection_engine(bboxes, kpss=None, label_map=None):
    engine = InsightFaceEngine.__new__(InsightFaceEngine)
    engine._model = _FakeSCRFD(bboxes, kpss)
    engine._task_code = "face_detection"
    engine._labels = label_map or {}
    return engine


def _recognition_engine(feat):
    engine = InsightFaceEngine.__new__(InsightFaceEngine)
    engine._model = _FakeArcFace(feat)
    engine._task_code = "face_recognition"
    engine._labels = {}
    return engine


def _install_fake_face_align(monkeypatch, norm_crop):
    """Injects a fake insightface.utils.face_align module into sys.modules so
    InsightFaceEngine.embed()'s lazy `from insightface.utils import face_align` resolves
    without the real (not installed in CI) insightface package present."""
    fake_face_align = types.ModuleType("insightface.utils.face_align")
    fake_face_align.norm_crop = norm_crop
    fake_utils = types.ModuleType("insightface.utils")
    fake_utils.face_align = fake_face_align
    fake_insightface = types.ModuleType("insightface")
    fake_insightface.utils = fake_utils
    monkeypatch.setitem(sys.modules, "insightface", fake_insightface)
    monkeypatch.setitem(sys.modules, "insightface.utils", fake_utils)
    monkeypatch.setitem(sys.modules, "insightface.utils.face_align", fake_face_align)


# --- detection --------------------------------------------------------------------------

def test_detection_bboxes_and_keypoints_normalised_correctly():
    bboxes = np.array([[64.0, 48.0, 128.0, 144.0, 0.91]], dtype=np.float32)
    kpss = np.array(
        [[[70, 60], [110, 60], [90, 90], [75, 120], [115, 120]]], dtype=np.float32
    )
    engine = _detection_engine(bboxes, kpss)

    detections = engine.infer(IMAGE, confidence=0.4)

    assert len(detections) == 1
    d = detections[0]
    assert d.class_id == 0
    assert d.class_name == "face"
    assert d.confidence == pytest.approx(0.91)
    # 640x480 frame: x's divided by width (640), y's divided by height (480).
    assert d.bbox == pytest.approx((64.0 / 640, 48.0 / 480, 128.0 / 640, 144.0 / 480))
    assert d.keypoints is not None
    assert len(d.keypoints) == 5
    # Each keypoint reuses the box's own detection score - SCRFD emits no separate
    # per-landmark confidence.
    assert d.keypoints[0] == pytest.approx((70 / 640, 60 / 480, 0.91))


def test_confidence_threshold_is_forwarded_to_the_wrapped_model():
    engine = _detection_engine(np.empty((0, 5), dtype=np.float32), None)
    engine.infer(IMAGE, confidence=0.73)
    assert engine._model.detect_calls == [0.73]


def test_empty_detection_result_is_a_valid_empty_list_not_an_error():
    engine = _detection_engine(
        np.empty((0, 5), dtype=np.float32), np.empty((0, 5, 2), dtype=np.float32)
    )
    assert engine.infer(IMAGE, confidence=0.5) == []


def test_none_keypoints_does_not_crash_a_variant_without_a_landmark_head():
    bboxes = np.array([[10.0, 10.0, 50.0, 50.0, 0.6]], dtype=np.float32)
    engine = _detection_engine(bboxes, kpss=None)

    detections = engine.infer(IMAGE, confidence=0.4)

    assert len(detections) == 1
    assert detections[0].keypoints is None


def test_recognition_engine_refuses_infer():
    """A 512-d embedding does not fit the Detection shape `.infer()` promises callers."""
    engine = _recognition_engine(np.zeros(512, dtype=np.float32))
    with pytest.raises(OutputContractUnknownError):
        engine.infer(IMAGE)


def test_detection_engine_refuses_embed():
    engine = _detection_engine(np.empty((0, 5), dtype=np.float32), None)
    detection = Detection(
        class_id=0,
        class_name="face",
        confidence=0.9,
        bbox=(0.0, 0.0, 0.1, 0.1),
        keypoints=[(0.1, 0.1, 0.9)] * 5,
    )
    with pytest.raises(OutputContractUnknownError):
        engine.embed(IMAGE, detection)


# --- recognition / embed -----------------------------------------------------------------

def test_embed_denormalises_keypoints_to_pixel_space_before_alignment(monkeypatch):
    captured: dict = {}

    def fake_norm_crop(image, landmark, image_size):
        captured["landmark"] = landmark
        captured["image_size"] = image_size
        return np.zeros((image_size, image_size, 3), dtype=np.uint8)

    _install_fake_face_align(monkeypatch, fake_norm_crop)

    expected_feat = np.arange(512, dtype=np.float32)
    engine = _recognition_engine(expected_feat)

    # Normalised keypoints (x/width, y/height, score) - matches what the paired
    # face_detection engine's own infer() actually produces.
    keypoints = [
        (70 / 640, 60 / 480, 0.9),
        (110 / 640, 60 / 480, 0.9),
        (90 / 640, 90 / 480, 0.9),
        (75 / 640, 120 / 480, 0.9),
        (115 / 640, 120 / 480, 0.9),
    ]
    detection = Detection(
        class_id=0,
        class_name="face",
        confidence=0.9,
        bbox=(0.1, 0.1, 0.3, 0.4),
        keypoints=keypoints,
    )

    result = engine.embed(IMAGE, detection)

    np.testing.assert_array_equal(result, expected_feat)
    assert captured["image_size"] == 112  # engine._model.input_size[0]
    np.testing.assert_allclose(
        captured["landmark"],
        [[70, 60], [110, 60], [90, 90], [75, 120], [115, 120]],
        atol=1e-3,
    )


def test_embed_requires_keypoints_on_the_detection():
    engine = _recognition_engine(np.zeros(512, dtype=np.float32))
    detection = Detection(
        class_id=0, class_name="face", confidence=0.9, bbox=(0.0, 0.0, 0.1, 0.1), keypoints=None
    )
    with pytest.raises(OutputContractUnknownError):
        engine.embed(IMAGE, detection)
