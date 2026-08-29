"""Raw YOLOv8 head decoding.

A wrong decode is the dangerous failure mode here: it produces confident, plausible-looking
boxes that are silently in the wrong place, and nothing downstream can tell. These tests
pin the three things most likely to go wrong - axis orientation, NMS, and coordinate
normalisation - using synthetic tensors with known-correct answers.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

AI_RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "ai_runtime"
if str(AI_RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_RUNTIME_ROOT))

from app.engines import OnnxEngine, decode_raw_yolo  # noqa: E402

LABELS = {0: "person", 1: "helmet"}
INPUT_SIZE = (640, 640)


def _onnx_engine(labels=None):
    """Builds an OnnxEngine for testing `_decode` without loading a real ONNX model -
    `__init__` needs onnxruntime and an artifact file, and `_decode` only touches
    `self._labels`."""
    engine = OnnxEngine.__new__(OnnxEngine)
    engine._labels = labels if labels is not None else {0: "license_plate"}
    return engine


def _head(boxes_and_scores, num_classes=2, num_anchors=100, channels_first=True):
    """Builds a (1, 4+nc, anchors) tensor with the given detections placed at anchor 0..n.

    Coordinates are pixel-space centre-x, centre-y, width, height, matching a plain
    (non-end2end) YOLOv8 export.
    """
    channels = 4 + num_classes
    raw = np.zeros((channels, num_anchors), dtype=np.float32)
    for index, (cx, cy, w, h, class_id, score) in enumerate(boxes_and_scores):
        raw[0, index] = cx
        raw[1, index] = cy
        raw[2, index] = w
        raw[3, index] = h
        raw[4 + class_id, index] = score
    batched = raw[np.newaxis, ...]
    return batched if channels_first else batched.transpose(0, 2, 1)


def test_decodes_a_single_box_to_normalised_corners():
    # Centre (320, 320), 64x64 box in a 640x640 input -> corners (288, 352) px -> 0.45..0.55.
    raw = _head([(320.0, 320.0, 64.0, 64.0, 0, 0.9)])
    detections = decode_raw_yolo(raw, confidence=0.25, labels=LABELS, input_size=INPUT_SIZE)

    assert len(detections) == 1
    d = detections[0]
    assert d.class_name == "person"
    assert d.confidence == pytest.approx(0.9, abs=1e-6)
    assert d.bbox[0] == pytest.approx(0.45, abs=1e-6)
    assert d.bbox[1] == pytest.approx(0.45, abs=1e-6)
    assert d.bbox[2] == pytest.approx(0.55, abs=1e-6)
    assert d.bbox[3] == pytest.approx(0.55, abs=1e-6)


def test_handles_both_axis_orientations():
    """Exporters disagree on whether the tensor is (channels, anchors) or the transpose.
    Both must decode identically, or half the estate silently produces garbage."""
    detections = [(320.0, 320.0, 64.0, 64.0, 0, 0.9)]
    a = decode_raw_yolo(
        _head(detections, channels_first=True), confidence=0.25, labels=LABELS, input_size=INPUT_SIZE
    )
    b = decode_raw_yolo(
        _head(detections, channels_first=False), confidence=0.25, labels=LABELS, input_size=INPUT_SIZE
    )
    assert [(d.class_name, d.bbox) for d in a] == [(d.class_name, d.bbox) for d in b]


def test_below_threshold_candidates_are_dropped():
    raw = _head([(320.0, 320.0, 64.0, 64.0, 0, 0.10)])
    assert decode_raw_yolo(raw, confidence=0.25, labels=LABELS, input_size=INPUT_SIZE) == []


def test_nms_collapses_overlapping_candidates():
    """A raw head emits thousands of near-duplicate boxes for one object. Without NMS a
    single person becomes dozens of detections and every downstream count is wrong."""
    duplicates = [(320.0 + i, 320.0 + i, 64.0, 64.0, 0, 0.9 - i * 0.01) for i in range(12)]
    detections = decode_raw_yolo(
        _head(duplicates), confidence=0.25, labels=LABELS, input_size=INPUT_SIZE
    )
    assert len(detections) == 1, f"NMS should collapse 12 overlapping boxes, got {len(detections)}"
    assert detections[0].confidence == pytest.approx(0.9, abs=1e-6), "highest-scoring candidate should survive"


def test_distinct_objects_are_both_kept():
    far_apart = [
        (100.0, 100.0, 50.0, 50.0, 0, 0.9),
        (500.0, 500.0, 50.0, 50.0, 1, 0.8),
    ]
    detections = decode_raw_yolo(
        _head(far_apart), confidence=0.25, labels=LABELS, input_size=INPUT_SIZE
    )
    assert len(detections) == 2
    assert {d.class_name for d in detections} == {"person", "helmet"}


def test_already_normalised_output_is_not_rescaled():
    """Some exports emit 0..1 directly. Dividing those by the input size again would
    produce boxes about 640x too small."""
    raw = _head([(0.5, 0.5, 0.1, 0.1, 0, 0.9)])
    detections = decode_raw_yolo(raw, confidence=0.25, labels=LABELS, input_size=INPUT_SIZE)

    assert len(detections) == 1
    assert abs(detections[0].bbox[0] - 0.45) < 1e-6
    assert abs(detections[0].bbox[2] - 0.55) < 1e-6


def test_boxes_are_clamped_to_frame():
    """An object at the frame edge can produce corners outside 0..1; downstream crop and
    masking logic assumes normalised coordinates stay in range."""
    raw = _head([(10.0, 10.0, 200.0, 200.0, 0, 0.9)])
    detections = decode_raw_yolo(raw, confidence=0.25, labels=LABELS, input_size=INPUT_SIZE)
    assert detections[0].bbox[0] == 0.0
    assert detections[0].bbox[1] == 0.0


def test_returns_none_for_unrecognised_shape():
    """Not-this-layout must be distinguishable from no-detections, so the caller can try
    another decoder instead of reporting an empty frame."""
    assert decode_raw_yolo(np.zeros((5, 5)), confidence=0.25, labels=LABELS, input_size=INPUT_SIZE) is None
    assert (
        decode_raw_yolo(np.zeros((1, 2, 2)), confidence=0.25, labels=LABELS, input_size=INPUT_SIZE)
        is None
    )


def test_unknown_class_id_falls_back_to_its_index():
    raw = _head([(320.0, 320.0, 64.0, 64.0, 1, 0.9)], num_classes=2)
    detections = decode_raw_yolo(raw, confidence=0.25, labels={}, input_size=INPUT_SIZE)
    assert detections[0].class_name == "1"


# --- OnnxEngine._decode: end-to-end (post-NMS) export layouts -----------------------
#
# These pin the licence-plate detector's real contract - class before score, not score
# before class - after probing the actual artifact
# (yolo-v9-t-384-license-plates-end2end.onnx) showed the column read as "score" was a
# constant 0.0 across every candidate while the column read as "class" varied plausibly
# (0.03-0.80 depending on how large the plate was in frame). Reading it the wrong way
# round silently zeroed every detection - see engines.py's _decode docstring.


def test_end2end_seven_column_reads_class_before_score():
    engine = _onnx_engine()
    # batch_index, x1, y1, x2, y2, class, score - a single "license_plate" candidate.
    rows = np.array([[0.0, 10.0, 20.0, 30.0, 40.0, 0.0, 0.80]], dtype=np.float32)
    detections = engine._decode([rows], confidence=0.25, target_size=(100, 100))

    assert len(detections) == 1
    d = detections[0]
    assert d.class_name == "license_plate"
    assert d.confidence == pytest.approx(0.80, abs=1e-6)
    assert d.bbox == pytest.approx((0.10, 0.20, 0.30, 0.40), abs=1e-6)


def test_end2end_six_column_reads_class_before_score():
    engine = _onnx_engine()
    # x1, y1, x2, y2, class, score - no leading batch-index column.
    rows = np.array([[10.0, 20.0, 30.0, 40.0, 0.0, 0.80]], dtype=np.float32)
    detections = engine._decode([rows], confidence=0.25, target_size=(100, 100))

    assert len(detections) == 1
    assert detections[0].confidence == pytest.approx(0.80, abs=1e-6)


def test_end2end_candidates_the_export_already_filtered_are_not_dropped_as_zero_score():
    """The regression this pins: a real end2end export narrows its output to genuine
    candidates before the runtime ever sees them. If the runtime reads the class column
    (always 0.0 for a single-class model) as the score, every one of those genuine
    candidates silently evaluates to confidence 0.0 and gets dropped - the model looks
    like it never detects anything, at any threshold, even though it is working."""
    engine = _onnx_engine()
    rows = np.array(
        [
            [0.0, 6.3, 4.3, 93.7, 26.7, 0.0, 0.1564],
            [0.0, 330.3, 310.3, 383.4, 381.9, 0.0, 0.0630],
        ],
        dtype=np.float32,
    )
    detections = engine._decode([rows], confidence=0.01, target_size=(384, 384))
    assert len(detections) == 2
    assert {round(d.confidence, 4) for d in detections} == {0.1564, 0.0630}


def test_end2end_below_threshold_is_still_dropped():
    engine = _onnx_engine()
    rows = np.array([[10.0, 20.0, 30.0, 40.0, 0.0, 0.10]], dtype=np.float32)
    assert engine._decode([rows], confidence=0.25, target_size=(100, 100)) == []


def test_end2end_empty_output_is_a_valid_zero_detection_result():
    engine = _onnx_engine()
    rows = np.zeros((0, 7), dtype=np.float32)
    assert engine._decode([rows], confidence=0.25, target_size=(100, 100)) == []


def test_end2end_falls_back_to_raw_yolo_head_for_unrecognised_column_count():
    """A plain (non-end2end) export in disguise - `_decode` must hand off to
    `decode_raw_yolo` rather than misreading it as a 6/7-column end2end result."""
    engine = _onnx_engine(labels=LABELS)
    raw = _head([(320.0, 320.0, 64.0, 64.0, 0, 0.9)])
    detections = engine._decode([raw], confidence=0.25, target_size=INPUT_SIZE)
    assert len(detections) == 1
    assert detections[0].class_name == "person"
