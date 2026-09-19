"""Per-framework inference engines.

The legacy estate spans three runtimes - Ultralytics PyTorch (.pt), ONNX Runtime (.onnx),
and TFLite (.tflite) - so the runtime needs all three rather than a single inference path.
Each engine imports its framework lazily inside its own constructor, so the service starts
and reports honestly even when a framework is missing from the image. That matters for
edge deployments (TRD §12.1), where a device may ship with only ONNX Runtime and should
not fail to boot because PyTorch is absent.

Engines return a normalised Detection list regardless of framework, so downstream pipeline
stages never branch on model format.
"""
from __future__ import annotations

import itertools
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Detection:
    """One detected object, in normalised coordinates.

    bbox is (x1, y1, x2, y2) normalised to 0..1 against the source frame, matching the
    detection document shape in docs/05_BACKEND_SCHEMA.md §9.1. Normalised rather than
    pixel coordinates so a detection stays meaningful when the same event is rendered
    against a different stream profile - inference commonly runs on the sub-stream while
    evidence is captured from the main stream.
    """

    class_id: int
    class_name: str
    confidence: float
    bbox: tuple[float, float, float, float]
    keypoints: list[tuple[float, float, float]] | None = None


@dataclass
class EngineInfo:
    framework: str
    runtime: str
    available: bool
    detail: str = ""
    input_shape: tuple[int, ...] | None = None
    labels: dict[int, str] = field(default_factory=dict)


class InferenceEngine(Protocol):
    def info(self) -> EngineInfo: ...
    def infer(self, image: np.ndarray, *, confidence: float = 0.25) -> list[Detection]: ...


class EngineUnavailableError(RuntimeError):
    """The framework this artifact needs is not installed in this image."""


class OutputContractUnknownError(NotImplementedError):
    """The artifact ran, but its output layout is not one this runtime can decode.

    Raised instead of guessing a decode: a wrong guess produces plausible-looking boxes
    that are silently incorrect, which is far worse than an explicit failure. The fix is
    to record an output_schema on the model version in the registry.
    """

def decode_raw_yolo(
    raw: np.ndarray,
    *,
    confidence: float,
    labels: dict[int, str],
    input_size: tuple[int, int],
    iou_threshold: float = 0.45,
) -> list[Detection] | None:
    """Decodes an undecoded YOLOv8 head: (batch, 4 + num_classes, num_anchors).

    This is what a plain (non-`end2end`) export produces - the kitchen-safety TFLite model
    emits (1, 10, 13125), i.e. 4 box terms plus 6 classes across 13125 anchors, with no NMS
    applied. Boxes arrive as centre-x, centre-y, width, height in input-normalised units.

    Returns None when the array is not this layout, so callers can try other decoders
    rather than receiving a wrong answer.
    """
    if raw.ndim != 3:
        return None

    batch = raw[0]
    # Two orientations exist depending on exporter: (4+nc, anchors) or (anchors, 4+nc).
    # Anchors always vastly outnumber channels, so the smaller axis is the channel axis.
    if batch.shape[0] < batch.shape[1]:
        channels, anchors = batch.shape[0], batch.shape[1]
        predictions = batch.T
    else:
        channels, anchors = batch.shape[1], batch.shape[0]
        predictions = batch

    num_classes = channels - 4
    if num_classes < 1 or anchors < channels:
        return None

    boxes_cxcywh = predictions[:, :4]
    class_scores = predictions[:, 4:]

    best_class = np.argmax(class_scores, axis=1)
    best_score = class_scores[np.arange(len(class_scores)), best_class]

    keep = best_score >= confidence
    if not np.any(keep):
        return []

    boxes_cxcywh = boxes_cxcywh[keep]
    best_class = best_class[keep]
    best_score = best_score[keep]

    # cxcywh -> xyxy, still in input-normalised units.
    half_w = boxes_cxcywh[:, 2] / 2.0
    half_h = boxes_cxcywh[:, 3] / 2.0
    x1 = boxes_cxcywh[:, 0] - half_w
    y1 = boxes_cxcywh[:, 1] - half_h
    x2 = boxes_cxcywh[:, 0] + half_w
    y2 = boxes_cxcywh[:, 1] + half_h

    # Raw heads emit thousands of overlapping candidates; without NMS a single object
    # becomes dozens of detections and every downstream count is wrong.
    import cv2

    nms_boxes = np.stack([x1, y1, x2 - x1, y2 - y1], axis=1).tolist()
    indices = cv2.dnn.NMSBoxes(nms_boxes, best_score.astype(float).tolist(), confidence, iou_threshold)
    if len(indices) == 0:
        return []
    indices = np.array(indices).flatten()

    # Exports differ: some emit 0..1, others pixels against the input size. Decide from
    # the data rather than assuming - a wrong choice yields boxes that are either
    # microscopic or entirely off-frame.
    input_h, input_w = input_size
    if float(np.max(np.abs(boxes_cxcywh))) <= 1.5:
        scale_x = scale_y = 1.0
    else:
        scale_x, scale_y = float(input_w), float(input_h)

    return [
        Detection(
            class_id=int(best_class[i]),
            class_name=labels.get(int(best_class[i]), str(int(best_class[i]))),
            confidence=float(best_score[i]),
            bbox=(
                float(np.clip(x1[i] / scale_x, 0.0, 1.0)),
                float(np.clip(y1[i] / scale_y, 0.0, 1.0)),
                float(np.clip(x2[i] / scale_x, 0.0, 1.0)),
                float(np.clip(y2[i] / scale_y, 0.0, 1.0)),
            ),
        )
        for i in indices
    ]


# --- Ultralytics (.pt) --------------------------------------------------------------

class UltralyticsEngine:
    """YOLOv8 detection and pose models - 6 of the 14 migrated artifacts."""

    def __init__(self, artifact_path: Path, label_map: dict[int, str] | None = None) -> None:
        try:
            from ultralytics import YOLO
        except ImportError as exc:  # pragma: no cover - depends on image contents
            raise EngineUnavailableError(
                "ultralytics is not installed in this image; cannot load a .pt artifact"
            ) from exc

        self._model = YOLO(str(artifact_path))
        # Ultralytics checkpoints carry their own class names. Prefer the registry's
        # label_map when one was recorded, since that is the reviewed source of truth.
        self._labels = label_map or dict(self._model.names)

    def info(self) -> EngineInfo:
        return EngineInfo(
            framework="pytorch",
            runtime="ultralytics",
            available=True,
            labels=self._labels,
            detail=f"task={getattr(self._model, 'task', 'unknown')}",
        )

    def infer(self, image: np.ndarray, *, confidence: float = 0.25) -> list[Detection]:
        height, width = image.shape[:2]
        results = self._model.predict(image, conf=confidence, verbose=False)
        detections: list[Detection] = []

        for result in results:
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue

            # Pose models attach keypoints per detection, in the same order as boxes.
            keypoint_sets = None
            if getattr(result, "keypoints", None) is not None:
                keypoint_sets = result.keypoints.data.cpu().numpy()

            for index, box in enumerate(boxes):
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                class_id = int(box.cls[0])
                keypoints = None
                if keypoint_sets is not None and index < len(keypoint_sets):
                    keypoints = [
                        (float(kx) / width, float(ky) / height, float(score))
                        for kx, ky, score in keypoint_sets[index]
                    ]
                detections.append(
                    Detection(
                        class_id=class_id,
                        class_name=self._labels.get(class_id, str(class_id)),
                        confidence=float(box.conf[0]),
                        bbox=(x1 / width, y1 / height, x2 / width, y2 / height),
                        keypoints=keypoints,
                    )
                )
        return detections


# --- ONNX Runtime (.onnx) -----------------------------------------------------------

class OnnxEngine:
    """ONNX artifacts: the licence-plate detector/OCR pair and the InsightFace set.

    Output layouts vary by model family. This engine decodes the end-to-end detector
    layout it can recognise, and otherwise raises OutputContractUnknownError. Callers that
    know a model's contract (OCR strings, face embeddings) can use raw_infer instead.
    """

    def __init__(self, artifact_path: Path, label_map: dict[int, str] | None = None) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover
            raise EngineUnavailableError(
                "onnxruntime is not installed in this image; cannot load a .onnx artifact"
            ) from exc

        providers = ["CPUExecutionProvider"]
        if "CUDAExecutionProvider" in ort.get_available_providers():
            providers.insert(0, "CUDAExecutionProvider")

        self._session = ort.InferenceSession(str(artifact_path), providers=providers)
        self._input = self._session.get_inputs()[0]
        self._labels = label_map or {}
        self._providers = providers

    def info(self) -> EngineInfo:
        shape = tuple(d if isinstance(d, int) else -1 for d in self._input.shape)
        return EngineInfo(
            framework="onnx",
            runtime="onnxruntime",
            available=True,
            input_shape=shape,
            labels=self._labels,
            detail=f"providers={','.join(self._providers)} input={self._input.name}",
        )

    def _is_nhwc(self) -> bool:
        """Most artifacts in this estate export NCHW (batch, channels, height, width) -
        the plate detector's `[1, 3, 384, 384]`, every InsightFace model's `[.., 3, H,
        W]`. The plate OCR model does not: its real input is `[-1, 64, 128, 3]`, channels
        *last*. Reading that as NCHW would treat 128 (width) as height and 3 (channels) as
        width - resizing the frame to 3 pixels wide before ever reaching the model.

        Channel counts are always small (1, 3 or 4) and spatial dimensions in this
        estate's real artifacts never are, so that is what decides it - not a hardcoded
        per-model exception, so the next NHWC export this codebase picks up (a common
        enough layout that assuming it away would be its own quiet bug) is handled by the
        same rule. Ambiguous or non-4D shapes default to NCHW, this estate's norm.
        """
        shape = self._input.shape
        if len(shape) != 4:
            return False
        channels_first = shape[1] if isinstance(shape[1], int) else None
        channels_last = shape[3] if isinstance(shape[3], int) else None
        return channels_last in (1, 3, 4) and channels_first not in (1, 3, 4)

    def _target_size(self, image: np.ndarray) -> tuple[int, int]:
        shape = self._input.shape
        # Static square inputs are the common case (384x384 for the plate detector);
        # fall back to the frame's own size for dynamic axes.
        if self._is_nhwc():
            height = shape[1] if isinstance(shape[1], int) else image.shape[0]
            width = shape[2] if isinstance(shape[2], int) else image.shape[1]
        else:
            height = shape[2] if isinstance(shape[2], int) else image.shape[0]
            width = shape[3] if isinstance(shape[3], int) else image.shape[1]
        return int(height), int(width)

    def _preprocess(self, image: np.ndarray) -> np.ndarray:
        import cv2

        height, width = self._target_size(image)
        resized = cv2.resize(image, (width, height))
        # Every artifact but one expects normalised float32 - the plate OCR model's own
        # graph declares `tensor(uint8)` and does its own normalisation internally
        # (confirmed against its real ONNX input metadata, not assumed); feeding it a
        # 0..1 float tensor fails loudly (a dtype mismatch onnxruntime itself rejects),
        # which is how this was actually found rather than guessed at.
        if self._input.type == "tensor(uint8)":
            resized = resized.astype(np.uint8)
        else:
            resized = resized.astype(np.float32) / 255.0
        if self._is_nhwc():
            return np.expand_dims(resized, axis=0)
        chw = resized.transpose(2, 0, 1)
        return np.expand_dims(chw, axis=0)

    def infer(self, image: np.ndarray, *, confidence: float = 0.25) -> list[Detection]:
        outputs = self._session.run(None, {self._input.name: self._preprocess(image)})
        return self._decode(outputs, confidence, self._target_size(image))

    def _decode(
        self, outputs: list[np.ndarray], confidence: float, target_size: tuple[int, int]
    ) -> list[Detection]:
        """Decodes the end-to-end YOLO export layouts this estate actually uses.

        Two column layouts appear in the wild for `end2end` ONNX exports, and the legacy
        licence-plate detector uses the 7-column one:

            6 columns: x1, y1, x2, y2, class, score
            7 columns: batch_index, x1, y1, x2, y2, class, score

        Class before score, not the other way round - confirmed against the actual
        licence-plate detector artifact (yolo-v9-t-384-license-plates-end2end.onnx):
        probing its raw output directly showed the "score" column read as a constant 0.0
        across every candidate box (impossible for real confidence values on boxes an
        end2end NMS already chose to keep) while the "class" column varied plausibly with
        how well-framed each box was (0.80 on a tight crop centred on a plate, 0.03-0.15
        on the same plate distant and partial in a full 640x480 frame). Reading it the
        other way silently zeroed every detection - the plate detector never found a
        single plate, at any confidence threshold, until this was caught.

        Both may arrive as (batch, N, cols) or flattened to (N, cols) - a zero-detection
        frame commonly comes back as (0, 7), which is a valid empty result and must not be
        treated as an unknown contract.
        """
        primary = outputs[0]
        input_h, input_w = target_size

        rows = primary
        if rows.ndim == 3:
            rows = rows[0]

        if rows.ndim == 2 and rows.shape[-1] in (6, 7):
            # Drop the leading batch-index column when present.
            offset = 1 if rows.shape[-1] == 7 else 0
            detections = []
            for row in rows:
                score = float(row[offset + 5])
                if score < confidence:
                    continue
                cid = int(row[offset + 4])
                x1, y1, x2, y2 = (float(v) for v in row[offset : offset + 4])
                detections.append(
                    Detection(
                        class_id=cid,
                        class_name=self._labels.get(cid, str(cid)),
                        confidence=score,
                        bbox=(x1 / input_w, y1 / input_h, x2 / input_w, y2 / input_h),
                    )
                )
            return detections

        # Fall back to the raw YOLOv8 head layout (undecoded, needs NMS).
        raw = decode_raw_yolo(
            primary, confidence=confidence, labels=self._labels, input_size=target_size
        )
        if raw is not None:
            return raw

        raise OutputContractUnknownError(
            f"No decoder for ONNX output shape {primary.shape}. Record an output_schema on "
            "this model version before the runtime can interpret it."
        )

    def raw_infer(self, image: np.ndarray) -> list[np.ndarray]:
        """Escape hatch for models whose output contract the caller knows - plate OCR,
        face embeddings - and which do not fit the Detection shape.

        `license-plate-ocr` specifically: takes a plate crop (its own real input is
        NHWC `[-1, 64, 128, 3]`, `tensor(uint8)` - both handled by `_preprocess` above,
        found and fixed while wiring this in, not assumed correct beforehand). Output is
        `(1, 9, 37)` - confirmed empirically against the real artifact, not from
        documentation, which doesn't exist for this migrated model: each of 9 character
        positions carries its own 37-way softmax (rows sum to 1.0), and 37 matches
        exactly one plausible charset size (blank/pad + 10 digits + 26 letters).
        """
        return self._session.run(None, {self._input.name: self._preprocess(image)})

    def decode_plate_text(self, image: np.ndarray) -> PlateOcrResult:
        """Decodes `license-plate-ocr`'s `(1, 9, 37)` output into text - see
        `PlateOcrResult`'s own docstring for the two things this decode cannot promise,
        and CHECKLIST.md / CLARIFICATIONS.md for the 2026-09-19 decision to ship this
        anyway, clearly labelled, rather than leave it as a raw tensor indefinitely.
        """
        outputs = self.raw_infer(image)
        logits = outputs[0]
        if logits.ndim == 3:
            logits = logits[0]  # drop batch dim -> (9, 37)

        chars: list[str] = []
        char_confidences: list[float] = []
        for row in logits:
            index = int(np.argmax(row))
            char = _PLATE_OCR_CHARSET[index] if index < len(_PLATE_OCR_CHARSET) else ""
            if not char:
                continue  # "" marks blank/pad - excluded from both the text and its
                # confidences, so `char_confidences[i]` always lines up with `text[i]`
                # rather than carrying a confidence for a padding slot nobody reads.
            chars.append(char)
            char_confidences.append(float(row[index]))

        text = "".join(chars)
        # The weakest position sets how much the whole *visible* plate can be trusted -
        # a plate is only as readable as its worst-decoded character, not its average,
        # and a confidently-blank padding slot should not water that down.
        overall_confidence = min(char_confidences) if char_confidences else 0.0

        return PlateOcrResult(
            text=text,
            char_confidences=[round(c, 4) for c in char_confidences],
            confidence=round(overall_confidence, 4),
            low_confidence=overall_confidence < PLATE_OCR_CONFIDENCE_THRESHOLD,
        )


# Assumed CTC-style convention for `license-plate-ocr`'s 37-class head: blank/pad first,
# then digits, then uppercase letters - the most common ordering for this exact class
# count in open ANPR/LPR models. **Unverified against this specific artifact** - no
# ground-truth plate was ever readable enough (10-40% per-position confidence on every
# real crop tried) to confirm index 11 is really 'A' and not, say, digit-adjacent noise.
# `PlateOcrResult.charset_verified` is hardcoded False for exactly this reason: a
# confidence score describes the model's certainty about a position, not whether this
# mapping is the right one to read that position with. Revisit only by checking against a
# clearer reference image with a known plate, or the model's original training config.
_PLATE_OCR_CHARSET: tuple[str, ...] = (
    "",  # 0: blank/pad
    *"0123456789",  # 1-10
    *"ABCDEFGHIJKLMNOPQRSTUVWXYZ",  # 11-36
)

# Matches this codebase's existing default rule confidence (csense_shared/pipeline/
# rules.py) rather than inventing a separate number - "not confident enough to act on"
# means the same threshold everywhere in this platform unless a specific model's own
# measured behaviour says otherwise (see CLAUDE.md's night/IR section for that exception).
PLATE_OCR_CONFIDENCE_THRESHOLD = 0.5


@dataclass(frozen=True)
class PlateOcrResult:
    """Two independent reasons this text may still be wrong, surfaced separately rather
    than collapsed into one score - a caller (or a UI) needs to know which applies:

    `low_confidence`: the model itself was unsure about at least one character position
    (worst-position softmax below `PLATE_OCR_CONFIDENCE_THRESHOLD`). This is the normal
    "trust this less" signal and improves with a better crop.

    `charset_verified`: always `False`. This is not a confidence measure at all - it is
    an admission that `_PLATE_OCR_CHARSET`'s index-to-character mapping has never been
    checked against a real, human-readable plate on this artifact. A *high*-confidence
    result can still show the wrong character if the assumed ordering is wrong at that
    index; no amount of model certainty fixes a wrong lookup table. Only a verified
    reference image or the model's original training config can flip this to `True`.
    """

    text: str
    char_confidences: list[float]
    confidence: float
    low_confidence: bool
    charset_verified: bool = False


# --- InsightFace ONNX (SCRFD detection / ArcFace recognition) -----------------------

class InsightFaceEngine:
    """Wraps the `insightface` package's own SCRFD detector / ArcFace recognizer decode.

    Two of the five quarantined InsightFace artifacts route here instead of the generic
    OnnxEngine - see `build_engine`'s `task_code` dispatch. SCRFD's real output is multi-
    scale anchor boxes across three strides plus a separate landmark head; ArcFace's is a
    512-d embedding. Neither fits `OnnxEngine._decode`'s two known layouts (end2end,
    raw YOLOv8 head), and reimplementing either by hand from the anchor/alignment maths
    would be exactly the guessed-decode failure mode `OutputContractUnknownError` exists to
    avoid - doubly so here, where a wrong guess produces a wrong face match rather than a
    misplaced box. `insightface.model_zoo` is the reference implementation these two
    artifacts were trained and exported against, so it is used directly rather than
    re-derived from scratch.

    Loading via this engine does not change anything about deployability: these two models
    stay `revoked` in the registry, and `app/registry.py`'s `get_deployable_by_name()` only
    ever returns validated/staging/production versions - the same gate that already blocks
    every other model blocks these regardless of this class existing. This is decode-only;
    nothing here persists, matches, or exposes the output.
    """

    def __init__(
        self,
        artifact_path: Path,
        label_map: dict[int, str] | None,
        task_code: str | None = None,
    ) -> None:
        try:
            import insightface.model_zoo as model_zoo
        except ImportError as exc:  # pragma: no cover - depends on image contents
            raise EngineUnavailableError(
                "insightface is not installed in this image; cannot load a face_detection/"
                "face_recognition artifact"
            ) from exc
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover
            raise EngineUnavailableError(
                "onnxruntime is not installed in this image; cannot load a .onnx artifact"
            ) from exc

        providers = ["CPUExecutionProvider"]
        if "CUDAExecutionProvider" in ort.get_available_providers():
            providers.insert(0, "CUDAExecutionProvider")

        # insightface's own ModelRouter picks SCRFD/ArcFaceONNX/etc. by inspecting the
        # artifact's real input/output shapes, not by filename - confirmed against its
        # actual source rather than assumed. `task_code` here only decides which public
        # method this engine exposes, not which decode runs.
        self._model = model_zoo.get_model(str(artifact_path), providers=providers)
        if self._model is None:
            raise OutputContractUnknownError(
                f"insightface could not classify the artifact at {artifact_path} as a known "
                "model type (its own model router returned None). Record an output_schema "
                "on this model version before the runtime can interpret it."
            )
        self._model.prepare(ctx_id=0 if "CUDAExecutionProvider" in providers else -1)
        self._task_code = task_code
        self._labels = label_map or {}
        self._providers = providers

    def info(self) -> EngineInfo:
        return EngineInfo(
            framework="onnx",
            runtime="onnxruntime",
            available=True,
            labels=self._labels,
            detail=(
                f"insightface_model={type(self._model).__name__} "
                f"providers={','.join(self._providers)}"
            ),
        )

    def infer(self, image: np.ndarray, *, confidence: float = 0.25) -> list[Detection]:
        """Face detection only. The recognition engine raises here on purpose - a 512-d
        embedding does not fit the `Detection` shape; use `embed()` instead, with a
        `Detection` produced by the paired face_detection engine."""
        if self._task_code != "face_detection":
            raise OutputContractUnknownError(
                f"{type(self._model).__name__} produces embeddings, not detections - call "
                "embed() with a Detection from the paired face_detection model instead."
            )

        height, width = image.shape[:2]
        # SCRFD's own `.detect()` does its own letterbox/resize/blob construction against
        # the raw frame - unlike OnnxEngine, this must NOT be pre-resized or normalised
        # first, confirmed against the real source (`SCRFD.forward` builds its own blob).
        self._model.det_thresh = confidence
        bboxes, kpss = self._model.detect(image)

        detections: list[Detection] = []
        for i in range(bboxes.shape[0]):
            x1, y1, x2, y2, score = (float(v) for v in bboxes[i])
            keypoints = None
            if kpss is not None:
                # SCRFD emits no separate per-landmark confidence; the box's own detection
                # score is reused for all five points, same convention as everywhere else in
                # this file that a keypoint set shares its box's confidence.
                keypoints = [
                    (float(kx) / width, float(ky) / height, score) for kx, ky in kpss[i]
                ]
            detections.append(
                Detection(
                    class_id=0,
                    class_name=self._labels.get(0, "face"),
                    confidence=score,
                    bbox=(
                        float(np.clip(x1 / width, 0.0, 1.0)),
                        float(np.clip(y1 / height, 0.0, 1.0)),
                        float(np.clip(x2 / width, 0.0, 1.0)),
                        float(np.clip(y2 / height, 0.0, 1.0)),
                    ),
                    keypoints=keypoints,
                )
            )
        return detections

    def embed(self, image: np.ndarray, detection: Detection) -> np.ndarray:
        """512-d ArcFace embedding for one already-detected face.

        The estate's second two-stage vision pipeline - the plate detector/OCR pair is the
        first, described in the migration manifest as exactly that. `detection` must carry
        the 5-point keypoints the paired face_detection engine's own SCRFD output produces;
        alignment (`insightface.utils.face_align.norm_crop`) needs all five, in the
        detector's own order, or the crop this hands to the recognition model is wrong.

        Returns the raw feature vector `ArcFaceONNX.get_feat` itself returns - **not
        L2-normalised**, confirmed against the real source rather than assumed:
        normalisation only happens at comparison time, inside the package's own
        `compute_sim` (a plain cosine similarity that normalises both sides itself).
        Callers comparing two embeddings must divide by each vector's own norm; this output
        is not already unit length.

        This is a biometric template. The runtime does not persist it, match it, or expose
        it through any API - callers must not do so either without the promotion plus
        privacy/legal sign-off the migration manifest already requires for this model
        (`backend/migrations/legacy_model_manifest.py`).
        """
        if self._task_code != "face_recognition" or detection.keypoints is None:
            raise OutputContractUnknownError(
                "embed() needs a face_recognition engine and a Detection carrying 5-point "
                "keypoints from the paired face_detection model."
            )

        from insightface.utils import face_align

        height, width = image.shape[:2]
        landmarks = np.array(
            [(kx * width, ky * height) for kx, ky, _ in detection.keypoints],
            dtype=np.float32,
        )
        aligned = face_align.norm_crop(
            image, landmark=landmarks, image_size=self._model.input_size[0]
        )
        return self._model.get_feat(aligned).flatten()


# --- uniface-zoo ONNX (embedding / dense landmark / matting) ------------------------
#
# These three engines cover the 6 lowest-risk of the 15 `uniface-zoo` models pulled in
# 2026-09-03 (see CHECKLIST.md's "18 models pulled in `uploaded`" entry and
# backend/migrations/uniface_model_manifest.py for the full import). All 6 real gate-2
# shapes below were re-probed this session against the live artifacts (onnxruntime.
# InferenceSession inside the real `ai-runtime` container, fetched from the real
# `csense-models` MinIO bucket) - not trusted from any prior write-up - and matched
# exactly:
#   uniface-adaface-recognition:    input (batch,3,112,112) f32 -> output "output" (batch,512)
#   uniface-edgeface-recognition:   input (batch,3,112,112) f32 -> output "embedding" (batch,512)
#   uniface-mobileface-recognition: input (1,3,112,112) f32     -> output "output" (1,512)
#   uniface-sphereface-recognition: input (1,3,112,112) f32     -> output "output" (1,512)
#   uniface-facemesh-landmark:      input (batch,3,192,192) f32 -> "landmarks" (batch,468,3) + "score" (batch,1)
#   uniface-modnet-matting:         input (batch,3,H,W) f32 dynamic -> "output" (batch,1,H,W)
#
# The *preprocessing/alignment/postprocessing* math below is ported from the real public
# reference implementation these exact weights ship with - github.com/yakhyo/uniface
# (MIT), the real clone already sitting at `uniface-main/` in this repo's working tree,
# cross-checked line-for-line against the same files fetched fresh from GitHub this
# session - not reimplemented from architecture papers or guessed at from the ONNX graph
# alone. That matters here the same way it mattered for the plate detector's class/score
# column order and the child/adult label_map: a face recognition/landmark model that
# "runs" but has a subtly wrong preprocessing constant produces a plausible-looking
# embedding or landmark set that is silently wrong, with no error to catch it. One fact
# below was independently verified against the real installed packages in this container,
# not just read from uniface's own source: `uniface.face_utils.reference_alignment` (the
# 5-point 112x112 ArcFace template uniface's own alignment uses) is byte-for-byte
# identical to `insightface.utils.face_align.arcface_dst` (confirmed via `docker exec`
# against the real `ai-runtime` image) - so reusing InsightFaceEngine's already-production
# alignment path below is not an assumption of equivalence, it is the same template.
#
# task_code alone cannot pick the right decode here, unlike everywhere else in this file:
# `uniface-mobileface-recognition`, `uniface-sphereface-recognition`, `uniface-adaface-
# recognition`, and `uniface-edgeface-recognition` are all registered with task_code=
# "face_recognition" - the exact task_code InsightFaceEngine already owns for
# `insightface-buffalo-l-recognition` (confirmed against the real manifests, not assumed:
# `backend/migrations/uniface_model_manifest.py` and `legacy_model_manifest.py`). Keying
# dispatch on task_code alone, as this file did before these 15 models existed, would
# route those 4 uniface artifacts into `insightface.model_zoo.get_model()` - a decoder
# built for a completely different architecture family, which is exactly the silent-wrong-
# decode failure mode this file exists to avoid. Dispatch is therefore by the registry's
# exact model_name for both InsightFaceEngine and these three uniface engines - see
# build_engine's dispatch tables below.

# uniface.recognition.base.BaseRecognizer.preprocess resizes to 112x112, then either
# stays BGR (AdaFace only - AdaFace.preprocess overrides the base to skip the RGB swap,
# "AdaFace uses BGR color space (no RGB conversion) during preprocessing" per its own
# docstring and source) or converts to RGB (EdgeFace/MobileFace/SphereFace - none override
# preprocess, so the base class's own `swapRB=True` default applies), then in both cases
# does `(pixel - 127.5) / 127.5` per channel and transposes to NCHW. Getting the BGR/RGB
# split backwards for any one of the four would silently swap that model's red/blue
# channels - confirmed per-family against the literal upstream source (`uniface/
# recognition/{base,adaface}.py`), not assumed from symmetry with the other three.
_UNIFACE_EMBEDDING_FAMILIES: dict[str, str] = {
    "uniface-adaface-recognition": "adaface",
    "uniface-edgeface-recognition": "edgeface",
    "uniface-mobileface-recognition": "mobileface",
    "uniface-sphereface-recognition": "sphereface",
}
_UNIFACE_BGR_FAMILIES = {"adaface"}  # every other family swaps to RGB


def _uniface_recognition_blob(aligned_face_bgr: np.ndarray, family: str) -> np.ndarray:
    """Ported from `uniface.recognition.base.BaseRecognizer.preprocess` /
    `uniface.recognition.adaface.AdaFace.preprocess` (github.com/yakhyo/uniface).
    `aligned_face_bgr` must already be an aligned face crop - alignment itself is not part
    of this function, see `UnifaceEmbeddingEngine.embed`.
    """
    import cv2

    resized = cv2.resize(aligned_face_bgr, (112, 112))
    if family not in _UNIFACE_BGR_FAMILIES:
        resized = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
    normalized = (resized.astype(np.float32) - 127.5) / 127.5
    chw = normalized.transpose(2, 0, 1)
    return np.expand_dims(chw, axis=0)


class UnifaceEmbeddingEngine:
    """AdaFace / EdgeFace / MobileFace / SphereFace - 4 of the 6 low-risk uniface-zoo
    models, all a single (batch, 512) face embedding.

    Does not implement `infer()` into the `Detection` shape, deliberately, same as
    InsightFaceEngine's ArcFace path above and for the same reason: a 512-d embedding is
    not a bounding box. Use `embed()` with a `Detection` from the platform's own already-
    verified face detector (the legacy InsightFace SCRFD model, `insightface-buffalo-l-
    detect`, `production` state, already wired through InsightFaceEngine) instead of
    building a new face cropper for this.

    Alignment reuses `insightface.utils.face_align.norm_crop` - already the production
    path for the legacy ArcFace model above - rather than re-deriving the 5-point
    similarity transform uniface's own `face_utils.estimate_norm`/`face_alignment`
    implements. This is not an assumed equivalence: both templates were read and directly
    compared (`insightface.utils.face_align.arcface_dst`, checked live inside this image,
    vs. uniface's own `reference_alignment` constant) and are byte-identical
    ([[38.2946,51.6963],[73.5318,51.5014],[56.0252,71.7366],[41.5493,92.3655],
    [70.7299,92.2041]]) - the same public ArcFace 112x112 template, not a coincidence of
    two similar-looking numbers.

    Returns the **raw** embedding, not L2-normalised - same convention as
    InsightFaceEngine.embed() above (uniface's own `get_embedding()`, which this mirrors,
    is likewise raw; only its separate `get_normalized_embedding()`/`__call__` divide by
    the norm, and this engine intentionally follows the raw path so both embedding
    families behave identically for any caller that compares them). This is a biometric
    template; the runtime does not persist, match, or expose it through any API.
    """

    def __init__(
        self, artifact_path: Path, label_map: dict[int, str] | None, model_name: str
    ) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover
            raise EngineUnavailableError(
                "onnxruntime is not installed in this image; cannot load a .onnx artifact"
            ) from exc

        providers = ["CPUExecutionProvider"]
        if "CUDAExecutionProvider" in ort.get_available_providers():
            providers.insert(0, "CUDAExecutionProvider")

        self._session = ort.InferenceSession(str(artifact_path), providers=providers)
        self._input = self._session.get_inputs()[0]
        self._family = _UNIFACE_EMBEDDING_FAMILIES[model_name]
        self._labels = label_map or {}
        self._providers = providers

    def info(self) -> EngineInfo:
        shape = tuple(d if isinstance(d, int) else -1 for d in self._input.shape)
        return EngineInfo(
            framework="onnx",
            runtime="onnxruntime",
            available=True,
            input_shape=shape,
            labels=self._labels,
            detail=f"uniface_family={self._family} providers={','.join(self._providers)}",
        )

    def infer(self, image: np.ndarray, *, confidence: float = 0.25) -> list[Detection]:
        raise OutputContractUnknownError(
            f"uniface {self._family} produces a 512-d embedding, not detections - call "
            "embed() with a Detection from the paired face_detection model instead."
        )

    def embed(self, image: np.ndarray, detection: Detection) -> np.ndarray:
        """Raw 512-d embedding for one already-detected face.

        `detection` must carry the 5-point keypoints the platform's SCRFD face detector
        produces (left eye, right eye, nose, left mouth corner, right mouth corner) - the
        same order both insightface's and uniface's own alignment templates expect.
        """
        if detection.keypoints is None or len(detection.keypoints) != 5:
            raise OutputContractUnknownError(
                "embed() needs a Detection carrying 5-point keypoints from the paired "
                "face_detection model (e.g. the platform's SCRFD detector)."
            )

        from insightface.utils import face_align

        height, width = image.shape[:2]
        landmarks = np.array(
            [(kx * width, ky * height) for kx, ky, _ in detection.keypoints],
            dtype=np.float32,
        )
        aligned = face_align.norm_crop(image, landmark=landmarks, image_size=112)
        blob = _uniface_recognition_blob(aligned, self._family)
        output = self._session.run(None, {self._input.name: blob})[0]
        return output.reshape(-1).astype(np.float32)


# uniface's own MediaPipe FaceMesh ROI recipe (`uniface.landmark.facemesh.roi_from_box`/
# `warp_roi`, github.com/yakhyo/uniface): a square crop 1.5x the detector box (margin=0.25
# per side reproduces that 1.5x, matching MediaPipe's own `detection_to_roi`), rotated so
# the eye line is horizontal. Ported directly, not re-derived - this is exactly the class
# of geometry ("a wrong anchor grid produces plausible-looking but silently wrong boxes")
# this file already treats as too easy to get subtly wrong to guess at.
_FACEMESH_MARGIN = 0.25


def _facemesh_roi_from_box(
    bbox_px: tuple[float, float, float, float], eye_points_px: list[tuple[float, float]]
) -> tuple[float, float, float, float]:
    """Returns (center_x, center_y, side, angle_degrees) in full-image pixels. Mirrors
    `uniface.landmark.facemesh.roi_from_box`."""
    x1, y1, x2, y2 = bbox_px
    side = (1.0 + 2.0 * _FACEMESH_MARGIN) * max(x2 - x1, y2 - y1)
    dx = eye_points_px[1][0] - eye_points_px[0][0]
    dy = eye_points_px[1][1] - eye_points_px[0][1]
    angle = float(np.degrees(np.arctan2(dy, dx)))
    return (x1 + x2) / 2.0, (y1 + y2) / 2.0, side, angle


def _facemesh_warp_roi(
    image_bgr: np.ndarray, roi: tuple[float, float, float, float], size: int
) -> tuple[np.ndarray, np.ndarray]:
    """Single bilinear resample straight to the model's input resolution - mirrors
    MediaPipe's own ImageToTensorCalculator (`uniface.landmark.facemesh.warp_roi`);
    warp-then-resize would interpolate twice."""
    import cv2

    center_x, center_y, side, angle = roi
    matrix = cv2.getRotationMatrix2D((center_x, center_y), angle, size / side)
    matrix[0, 2] += size / 2.0 - center_x
    matrix[1, 2] += size / 2.0 - center_y
    crop = cv2.warpAffine(image_bgr, matrix, (size, size))
    return crop, cv2.invertAffineTransform(matrix)


class UnifaceFaceMeshEngine:
    """MediaPipe Face Mesh V1 (`uniface-facemesh-landmark`): 468 dense 3D landmarks + a
    presence score, from a 192x192 crop. MediaPipe's own public, stable 468-point spec -
    real gate-2 shape re-confirmed this session: `landmarks` (batch,468,3), `score`
    (batch,1).

    Does not implement `infer()` into the `Detection` shape - 468 3D points plus a
    presence score does not fit a bounding box either. Use `landmarks()` with a
    `Detection` from the platform's SCRFD face detector.
    """

    def __init__(self, artifact_path: Path, label_map: dict[int, str] | None) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover
            raise EngineUnavailableError(
                "onnxruntime is not installed in this image; cannot load a .onnx artifact"
            ) from exc

        providers = ["CPUExecutionProvider"]
        if "CUDAExecutionProvider" in ort.get_available_providers():
            providers.insert(0, "CUDAExecutionProvider")

        self._session = ort.InferenceSession(str(artifact_path), providers=providers)
        self._input = self._session.get_inputs()[0]
        self._input_size = (
            int(self._input.shape[2]) if isinstance(self._input.shape[2], int) else 192
        )
        outputs = self._session.get_outputs()
        # Read by name, not position - both are self-describing in the real artifact
        # ("landmarks", "score"), confirmed live this session, but matched explicitly
        # rather than assumed positional, unlike the fragile column-order guess this file
        # has already been bitten by once (the plate detector's class/score swap).
        names = [o.name for o in outputs]
        if "landmarks" not in names or "score" not in names:
            raise OutputContractUnknownError(
                f"Expected output names 'landmarks' and 'score', got {names}. Record an "
                "output_schema on this model version before the runtime can interpret it."
            )
        self._output_names = names
        self._landmarks_index = names.index("landmarks")
        self._score_index = names.index("score")
        self._labels = label_map or {}
        self._providers = providers

    def info(self) -> EngineInfo:
        shape = tuple(d if isinstance(d, int) else -1 for d in self._input.shape)
        return EngineInfo(
            framework="onnx",
            runtime="onnxruntime",
            available=True,
            input_shape=shape,
            labels=self._labels,
            detail=f"providers={','.join(self._providers)}",
        )

    def infer(self, image: np.ndarray, *, confidence: float = 0.25) -> list[Detection]:
        raise OutputContractUnknownError(
            "uniface-facemesh-landmark produces 468 3D landmarks plus a presence score, "
            "not detections - call landmarks() with a Detection from the paired "
            "face_detection model instead."
        )

    def landmarks(self, image: np.ndarray, detection: Detection) -> tuple[np.ndarray, float]:
        """Returns ((468, 3) landmarks in full-image pixel coordinates, presence score in
        [0, 1]).

        `detection` must carry at least the first two keypoints (the eyes, in the
        platform's SCRFD order) so the crop can be rolled level the way MediaPipe's own
        `detection_to_roi` does; without leveling, the mesh degrades on a tilted head.
        """
        if detection.keypoints is None or len(detection.keypoints) < 2:
            raise OutputContractUnknownError(
                "landmarks() needs a Detection carrying at least 2 keypoints (the eyes) "
                "from the paired face_detection model."
            )

        height, width = image.shape[:2]
        bbox_px = (
            detection.bbox[0] * width,
            detection.bbox[1] * height,
            detection.bbox[2] * width,
            detection.bbox[3] * height,
        )
        eyes_px = [(kx * width, ky * height) for kx, ky, _ in detection.keypoints[:2]]
        roi = _facemesh_roi_from_box(bbox_px, eyes_px)
        crop, inverse = _facemesh_warp_roi(image, roi, self._input_size)

        rgb = crop[:, :, ::-1].astype(np.float32) / 255.0
        chw = rgb.transpose(2, 0, 1)
        blob = np.expand_dims(chw, axis=0)

        outputs = self._session.run(self._output_names, {self._input.name: blob})
        raw_landmarks = outputs[self._landmarks_index][0]  # (468, 3), crop pixels
        raw_logit = outputs[self._score_index][0]  # (1,), a logit, not a probability

        points = raw_landmarks.astype(np.float64)
        points[:, :2] = points[:, :2] @ inverse[:, :2].T + inverse[:, 2]
        # Put z on the same pixel scale as x/y - matches uniface's own postprocess.
        points[:, 2] *= roi[2] / self._input_size
        score = float(1.0 / (1.0 + np.exp(-float(raw_logit[0]))))
        return points.astype(np.float32), score


class UnifaceMattingEngine:
    """MODNet photographic (`uniface-modnet-matting`): a single alpha matte, matching
    MODNet's own documented single output. Real gate-2 shape re-confirmed this session:
    input (batch,3,H,W) dynamic, output "output" (batch,1,H,W).

    Standard access_classification, not biometric (confirmed against the real registry
    row) - a portrait matte is a foreground/background alpha map, not an identity
    template. No face-detector pairing is needed: MODNet segments a portrait from its
    background directly on the full frame. A matte is a per-pixel alpha map for the whole
    frame, not a bounding box, so like the other two uniface engines above, this does not
    implement `infer()`.
    """

    _STRIDE = 32  # uniface.matting.modnet.MODNet's own STRIDE constant.

    def __init__(self, artifact_path: Path, label_map: dict[int, str] | None) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover
            raise EngineUnavailableError(
                "onnxruntime is not installed in this image; cannot load a .onnx artifact"
            ) from exc

        providers = ["CPUExecutionProvider"]
        if "CUDAExecutionProvider" in ort.get_available_providers():
            providers.insert(0, "CUDAExecutionProvider")

        self._session = ort.InferenceSession(str(artifact_path), providers=providers)
        self._input = self._session.get_inputs()[0]
        self._labels = label_map or {}
        self._providers = providers

    def info(self) -> EngineInfo:
        shape = tuple(d if isinstance(d, int) else -1 for d in self._input.shape)
        return EngineInfo(
            framework="onnx",
            runtime="onnxruntime",
            available=True,
            input_shape=shape,
            labels=self._labels,
            detail=f"providers={','.join(self._providers)}",
        )

    def infer(self, image: np.ndarray, *, confidence: float = 0.25) -> list[Detection]:
        raise OutputContractUnknownError(
            "uniface-modnet-matting produces a per-pixel alpha matte, not detections - "
            "call matte() instead."
        )

    def matte(self, image: np.ndarray, *, input_size: int = 512) -> np.ndarray:
        """Returns an (H, W) float32 alpha matte in [0, 1], resized back to the input
        image's own resolution.

        Ported from `uniface.matting.modnet.MODNet.preprocess`/`postprocess`: the image is
        converted to RGB, resized so its shorter side matches `input_size` (aspect ratio
        preserved, only when the image's long side is currently below `input_size` or its
        short side above it) then floored to a multiple of 32 on each side (`_STRIDE`), and
        normalised to [-1, 1] rather than the [0, 1]/mean-std conventions the other models
        in this file use - confirmed against the literal upstream source, not assumed to
        match the rest of this estate.
        """
        import cv2

        orig_h, orig_w = image.shape[:2]
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

        if max(orig_h, orig_w) < input_size or min(orig_h, orig_w) > input_size:
            if orig_w >= orig_h:
                new_h = input_size
                new_w = int(orig_w / orig_h * input_size)
            else:
                new_w = input_size
                new_h = int(orig_h / orig_w * input_size)
        else:
            new_h, new_w = orig_h, orig_w

        new_h -= new_h % self._STRIDE
        new_w -= new_w % self._STRIDE
        rgb = cv2.resize(rgb, (new_w, new_h), interpolation=cv2.INTER_AREA)

        x = rgb.astype(np.float32) / 255.0
        x = (x - 0.5) / 0.5
        chw = x.transpose(2, 0, 1)
        tensor = np.expand_dims(chw, axis=0)

        outputs = self._session.run(None, {self._input.name: tensor})
        matte = outputs[0][0, 0]
        return cv2.resize(matte, (orig_w, orig_h), interpolation=cv2.INTER_AREA)



# --- uniface-zoo: FairFace / MiniFASNet / MobileGaze / PIPNet ----------------------
#
# Four of the 15 uniface-zoo models whose output layout needed a public-repo cross-check
# before decoding (CHECKLIST.md's own "4 need a public-repo cross-check" entry) - the
# other 11 either fit an existing decode path, are anchor-based detectors needing separate
# anchor-math work, or (bisenet-parsing/faceattribnet) needed the public reference's own
# source to resolve their output semantics - decoded further down this file since
# 2026-09-16, no longer the "not decoded at all" pair this comment used to name. Every decode
# below is grounded in this project's own real upstream source for the specific artifact
# (`backend/migrations/uniface_model_manifest.py`'s own `legacy_paths`), cross-checked
# against the original upstream repo that source itself re-implements, not guessed from
# the ONNX graph alone - see CHECKLIST.md for the full citation trail and what could and
# could not be empirically verified against a real face crop.
#
# All four need a real face crop as input. Rather than building a new face cropper, they
# take a full frame plus a `Detection` produced by the paired, already-`production`
# face_detection model (`insightface-buffalo-l-detect`, via `InsightFaceEngine` above) -
# the same two-stage-pipeline shape `InsightFaceEngine.embed()` already established for
# the plate detector/OCR and face detection/recognition pairs. Like `InsightFaceEngine`'s
# `embed()`, these are decode-only: nothing here persists, matches, or exposes output
# through any API on its own, and loading via these engines does not change deployability
# - `get_deployable_by_name()`/`VALIDATABLE_STATES` in `registry.py` are the only gates
# that matter for that.


def _softmax(x: np.ndarray) -> np.ndarray:
    """Numerically-stable softmax over the last axis - every decode below that turns raw
    logits into class probabilities uses this, never a bare `exp(x) / sum(exp(x))`."""
    shifted = x - np.max(x, axis=-1, keepdims=True)
    exps = np.exp(shifted)
    return exps / np.sum(exps, axis=-1, keepdims=True)


def _crop_scrfd_aligned_chip(image: np.ndarray, detection: Detection, size: int) -> np.ndarray:
    """A `size`x`size` face chip, aligned by 5-point landmarks via this platform's own
    production ArcFace-style alignment (`insightface.utils.face_align.norm_crop`) - the
    same primitive `InsightFaceEngine.embed()` already uses for the same purpose. `size`
    must be a multiple of 112 or 128 (`norm_crop`'s own assertion) - 224 qualifies.

    FairFace's own reference implementation (`dchen236/FairFace`'s `predict.py`) instead
    uses `dlib.get_face_chips` - its own 5-point similarity-transform alignment against
    dlib's own reference template, `padding=0.25`, a 300px chip resized to 224. dlib is
    not part of this stack, and the two alignment templates are not identical, so this is
    a good-faith approximation using the best alignment primitive already in production
    here - not a pixel-exact reproduction of FairFace's own training-time preprocessing.
    Flagged here rather than silently assumed equivalent.
    """
    if detection.keypoints is None:
        raise OutputContractUnknownError(
            "Face alignment needs a Detection with 5-point keypoints from a paired "
            "face_detection model (e.g. insightface-buffalo-l-detect)."
        )
    from insightface.utils import face_align

    height, width = image.shape[:2]
    landmarks = np.array(
        [(kx * width, ky * height) for kx, ky, _ in detection.keypoints], dtype=np.float32
    )
    return face_align.norm_crop(image, landmark=landmarks, image_size=size)


def _crop_raw_bbox(image: np.ndarray, detection: Detection) -> np.ndarray:
    """The raw detector bbox, pixel-cropped with no margin - MobileGaze's own real
    upstream source (`yakhyo/gaze-estimation`'s `onnx_inference.py`) crops with
    `frame[y_min:y_max, x_min:x_max]` directly, confirmed against its real source rather
    than assumed."""
    height, width = image.shape[:2]
    x1, y1, x2, y2 = detection.bbox
    px1, py1 = max(0, int(x1 * width)), max(0, int(y1 * height))
    px2, py2 = min(width, int(x2 * width)), min(height, int(y2 * height))
    if px2 <= px1 or py2 <= py1:
        raise OutputContractUnknownError("Detection bbox collapses to an empty crop.")
    return image[py1:py2, px1:px2]


def _crop_scaled_bbox(image: np.ndarray, detection: Detection, scale: float) -> np.ndarray:
    """A `scale`x expansion of the detector bbox around its own centre, clamped to the
    frame - MiniFASNetV2's own real upstream source (`yakhyo/face-anti-spoofing`'s
    `utils.crop_face`) geometry, replicated exactly: new_w/new_h = box_w/box_h * scale
    (itself clamped so the crop never exceeds the frame), centred on the original box's
    own centre. `scale=2.7` is that repo's own documented constant for the config named
    "v2" specifically - matched against `uniface_model_manifest.py`'s own local_name for
    this artifact (`minifasnet_v2_MiniFASNetV2.onnx`)."""
    height, width = image.shape[:2]
    x1, y1, x2, y2 = detection.bbox
    px1, py1, px2, py2 = x1 * width, y1 * height, x2 * width, y2 * height
    box_w, box_h = px2 - px1, py2 - py1
    if box_w <= 0 or box_h <= 0:
        raise OutputContractUnknownError("Detection bbox collapses to an empty crop.")
    effective_scale = min((height - 1) / box_h, (width - 1) / box_w, scale)
    new_w, new_h = box_w * effective_scale, box_h * effective_scale
    center_x, center_y = px1 + box_w / 2, py1 + box_h / 2
    cx1 = max(0, int(center_x - new_w / 2))
    cy1 = max(0, int(center_y - new_h / 2))
    cx2 = min(width - 1, int(center_x + new_w / 2))
    cy2 = min(height - 1, int(center_y + new_h / 2))
    if cx2 <= cx1 or cy2 <= cy1:
        raise OutputContractUnknownError("Scaled crop collapses to an empty region.")
    return image[cy1 : cy2 + 1, cx1 : cx2 + 1]


def _crop_pipnet_box(image: np.ndarray, detection: Detection) -> tuple[np.ndarray, int, int]:
    """The asymmetric 1.2x detector-box expansion PIPNet's own reference uses - both the
    original `jhb86253817/PIPNet` (`lib/demo.py`, `det_box_scale=1.2`, "remove a part of
    top area for alignment, see paper for details") and this project's real upstream
    artifact source per `uniface_model_manifest.py` (`yakhyo/pipnet-onnx`, whose own numpy
    port uses the identical `pad = 0.1` formula) apply: +/-10% on left/right/bottom, but
    the *top* edge moves down (shrinks in) by 10% rather than up. Returns the crop plus
    its own top-left pixel offset, so the caller can translate normalised landmark
    coordinates back into full-frame coordinates."""
    height, width = image.shape[:2]
    x1, y1, x2, y2 = detection.bbox
    px1, py1, px2, py2 = x1 * width, y1 * height, x2 * width, y2 * height
    box_w, box_h = px2 - px1, py2 - py1
    pad_w, pad_h = box_w * 0.1, box_h * 0.1
    cx1 = max(0, int(px1 - pad_w))
    cy1 = max(0, int(py1 + pad_h))
    cx2 = min(width, int(px2 + pad_w))
    cy2 = min(height, int(py2 + pad_h))
    if cx2 <= cx1 or cy2 <= cy1:
        raise OutputContractUnknownError("PIPNet crop collapses to an empty region.")
    return image[cy1:cy2, cx1:cx2], cx1, cy1


def _onnx_session(artifact_path: Path):
    import onnxruntime as ort

    providers = ["CPUExecutionProvider"]
    if "CUDAExecutionProvider" in ort.get_available_providers():
        providers.insert(0, "CUDAExecutionProvider")
    session = ort.InferenceSession(str(artifact_path), providers=providers)
    return session, providers


# --- FairFace (race/gender/age attributes) -------------------------------------------

FAIRFACE_RACE_LABELS = (
    "White", "Black", "Latino_Hispanic", "East Asian", "Southeast Asian", "Indian",
    "Middle Eastern",
)
FAIRFACE_GENDER_LABELS = ("Male", "Female")
FAIRFACE_AGE_LABELS = (
    "0-2", "3-9", "10-19", "20-29", "30-39", "40-49", "50-59", "60-69", "70+",
)


@dataclass(frozen=True)
class FaceAttributes:
    race: str
    race_confidence: float
    race_scores: dict[str, float]
    gender: str
    gender_confidence: float
    gender_scores: dict[str, float]
    age_bucket: str
    age_confidence: float
    age_scores: dict[str, float]


class FairFaceEngine:
    """FairFace race/gender/age attribute model (`uniface-fairface-attributes`).

    Output tensor NAMES (`race_output`/`gender_output`/`age_output`) are self-describing
    from the ONNX graph; the per-column LABEL ORDER within each is not - it is the public
    FairFace repo's own documented convention. Cross-checked against two independent real
    sources before writing this decode: the original `github.com/dchen236/FairFace`
    `predict.py` (`race_outputs = outputs[:7]`, `gender_outputs = outputs[7:9]`,
    `age_outputs = outputs[9:18]`, each mapped through its own hardcoded label list in the
    order above) and this project's own real upstream artifact source per
    `uniface_model_manifest.py` (`github.com/yakhyo/fairface-onnx`), whose
    `models/predictor.py` defines the identical three label lists in the identical order,
    independently. FairFace is CC BY 4.0 - attribution required wherever this output is
    surfaced to a user (`uniface_model_manifest.py`'s own `_CC_BY` metadata), not just in
    source code; that obligation belongs to whatever UI ever renders this, not to this
    decode-only engine.
    """

    def __init__(self, artifact_path: Path, label_map: dict[int, str] | None = None) -> None:
        self._session, self._providers = _onnx_session(artifact_path)
        self._input = self._session.get_inputs()[0]

    def info(self) -> EngineInfo:
        shape = tuple(d if isinstance(d, int) else -1 for d in self._input.shape)
        return EngineInfo(
            framework="onnx", runtime="onnxruntime", available=True, input_shape=shape,
            detail=f"providers={','.join(self._providers)} input={self._input.name}",
        )

    def infer(self, image: np.ndarray, *, confidence: float = 0.25) -> list[Detection]:
        raise OutputContractUnknownError(
            "FairFace produces race/gender/age attribute scores, not detections - call "
            "predict_attributes() with a Detection from a paired face_detection model "
            "instead."
        )

    def predict_attributes(self, image: np.ndarray, detection: Detection) -> FaceAttributes:
        import cv2

        chip = _crop_scrfd_aligned_chip(image, detection, size=224)
        rgb = cv2.cvtColor(chip, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        normalized = (rgb - mean) / std
        tensor = np.expand_dims(normalized.transpose(2, 0, 1), axis=0).astype(np.float32)

        race_out, gender_out, age_out = self._session.run(
            ["race_output", "gender_output", "age_output"], {self._input.name: tensor}
        )
        race_probs = _softmax(race_out[0])
        gender_probs = _softmax(gender_out[0])
        age_probs = _softmax(age_out[0])

        race_idx, gender_idx, age_idx = (
            int(np.argmax(race_probs)), int(np.argmax(gender_probs)), int(np.argmax(age_probs)),
        )
        return FaceAttributes(
            race=FAIRFACE_RACE_LABELS[race_idx],
            race_confidence=float(race_probs[race_idx]),
            race_scores=dict(zip(FAIRFACE_RACE_LABELS, (float(v) for v in race_probs), strict=True)),
            gender=FAIRFACE_GENDER_LABELS[gender_idx],
            gender_confidence=float(gender_probs[gender_idx]),
            gender_scores=dict(
                zip(FAIRFACE_GENDER_LABELS, (float(v) for v in gender_probs), strict=True)
            ),
            age_bucket=FAIRFACE_AGE_LABELS[age_idx],
            age_confidence=float(age_probs[age_idx]),
            age_scores=dict(zip(FAIRFACE_AGE_LABELS, (float(v) for v in age_probs), strict=True)),
        )


# --- MiniFASNet (anti-spoofing / liveness) --------------------------------------------

@dataclass(frozen=True)
class LivenessResult:
    is_real: bool
    label: str
    confidence: float
    scores: tuple[float, float, float]


class MiniFasNetEngine:
    """MiniFASNetV2 anti-spoofing/liveness model (`uniface-minifasnet-antispoofing`).

    3-class output; index 1 = real/live, indices 0 and 2 = two different spoof-attack
    types (print/replay) collapsed to "fake" here since this platform only needs the
    real/spoof decision, not the attack type. Convention confirmed against two
    independent real sources: the original `github.com/minivision-ai/
    Silent-Face-Anti-Spoofing` `test.py` (`label = np.argmax(prediction)`, `if label == 1:
    ... "is Real Face"`, else "is Fake Face") and this project's own real upstream
    artifact source per `uniface_model_manifest.py` (`github.com/yakhyo/
    face-anti-spoofing`), whose `main.py` has the identical `"Real" if label_idx == 1 else
    "Fake"` convention, independently. Softmax applied here to match both sources' own
    `predict()`/`main.py` (`F.softmax(result)` / `torch.softmax(output, dim=1)`) -
    confirmed those apply it before argmax, not after.
    """

    def __init__(self, artifact_path: Path, label_map: dict[int, str] | None = None) -> None:
        self._session, self._providers = _onnx_session(artifact_path)
        self._input = self._session.get_inputs()[0]

    def info(self) -> EngineInfo:
        shape = tuple(d if isinstance(d, int) else -1 for d in self._input.shape)
        return EngineInfo(
            framework="onnx", runtime="onnxruntime", available=True, input_shape=shape,
            detail=f"providers={','.join(self._providers)} input={self._input.name}",
        )

    def infer(self, image: np.ndarray, *, confidence: float = 0.25) -> list[Detection]:
        raise OutputContractUnknownError(
            "MiniFASNet produces a 3-class liveness score, not detections - call "
            "predict_liveness() with a Detection from a paired face_detection model "
            "instead."
        )

    def predict_liveness(self, image: np.ndarray, detection: Detection) -> LivenessResult:
        import cv2

        crop = _crop_scaled_bbox(image, detection, scale=2.7)
        resized = cv2.resize(crop, (80, 80)).astype(np.float32) / 255.0
        tensor = np.expand_dims(resized.transpose(2, 0, 1), axis=0).astype(np.float32)

        raw = self._session.run(None, {self._input.name: tensor})[0][0]
        probs = _softmax(raw)
        label_idx = int(np.argmax(probs))
        return LivenessResult(
            is_real=label_idx == 1,
            label="real" if label_idx == 1 else "fake",
            confidence=float(probs[label_idx]),
            scores=(float(probs[0]), float(probs[1]), float(probs[2])),
        )


# --- MobileGaze (yaw/pitch gaze estimation) -------------------------------------------

_GAZE_BINS = 90
_GAZE_BIN_WIDTH_DEG = 4.0
_GAZE_ANGLE_OFFSET_DEG = 180.0


@dataclass(frozen=True)
class GazeEstimate:
    yaw_deg: float
    pitch_deg: float


class MobileGazeEngine:
    """MobileGaze (ResNet-18) gaze estimation model (`uniface-mobilegaze-estimation`).

    90-bin classification-to-angle scheme (the L2CS-Net family): softmax over each of the
    90 bins, then a weighted-expectation (NOT argmax) over bin index, scaled to degrees.
    Formula and constants (90 bins, 4-degree bin width, -180 degree offset) confirmed
    against this project's own real upstream artifact source per
    `uniface_model_manifest.py` (`github.com/yakhyo/gaze-estimation`, built on L2CS-Net):
    `yaw = np.sum(yaw_probs * idx_tensor, axis=1) * self._binwidth - self._angle_offset`
    with `_bins=90`, `_binwidth=4`, `_angle_offset=180` - independently cross-checked
    against the original `github.com/Ahmednull/L2CS-Net` `test.py`/`train.py`, which use
    the identical `* 4 - 180` formula for their own 90-bin Gaze360 configuration.
    """

    def __init__(self, artifact_path: Path, label_map: dict[int, str] | None = None) -> None:
        self._session, self._providers = _onnx_session(artifact_path)
        self._input = self._session.get_inputs()[0]
        self._bin_index = np.arange(_GAZE_BINS, dtype=np.float32)

    def info(self) -> EngineInfo:
        shape = tuple(d if isinstance(d, int) else -1 for d in self._input.shape)
        return EngineInfo(
            framework="onnx", runtime="onnxruntime", available=True, input_shape=shape,
            detail=f"providers={','.join(self._providers)} input={self._input.name}",
        )

    def infer(self, image: np.ndarray, *, confidence: float = 0.25) -> list[Detection]:
        raise OutputContractUnknownError(
            "MobileGaze produces a continuous yaw/pitch estimate, not detections - call "
            "estimate_gaze() with a Detection from a paired face_detection model instead."
        )

    def estimate_gaze(self, image: np.ndarray, detection: Detection) -> GazeEstimate:
        import cv2

        crop = _crop_raw_bbox(image, detection)
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(rgb, (448, 448)).astype(np.float32) / 255.0
        mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        normalized = (resized - mean) / std
        tensor = np.expand_dims(normalized.transpose(2, 0, 1), axis=0).astype(np.float32)

        yaw_raw, pitch_raw = self._session.run(["yaw", "pitch"], {self._input.name: tensor})
        yaw_probs = _softmax(yaw_raw[0])
        pitch_probs = _softmax(pitch_raw[0])
        yaw_deg = float(
            np.sum(yaw_probs * self._bin_index) * _GAZE_BIN_WIDTH_DEG - _GAZE_ANGLE_OFFSET_DEG
        )
        pitch_deg = float(
            np.sum(pitch_probs * self._bin_index) * _GAZE_BIN_WIDTH_DEG - _GAZE_ANGLE_OFFSET_DEG
        )
        return GazeEstimate(yaw_deg=yaw_deg, pitch_deg=pitch_deg)


# --- PIPNet (98-point facial landmarks) ------------------------------------------------

# WFLW 98-point mean face (98 x,y pairs, normalised 0..1 within a face-only crop) - the
# reference geometry the original PIPNet repo's own `get_meanface()` needs to build its
# neighbour-voting table. Copied verbatim from `github.com/jhb86253817/PIPNet`'s own
# `data/WFLW/meanface.txt`; this project's real PIPNet artifact source
# (`github.com/yakhyo/pipnet-onnx`, per `uniface_model_manifest.py`) loads the identical
# file (`get_meanface_info()`) rather than deriving its own.
_WFLW98_MEANFACE = (
    0.07960419395480703, 0.3921576875344978, 0.08315055593117261, 0.43509551571809146, 0.08675705281580391, 0.47810288286566444, 0.09141892980469117, 0.5210356946467262,
    0.09839925903528965, 0.5637522280060038, 0.10871037524559955, 0.6060410614977951, 0.12314562992759207, 0.6475338700558225, 0.14242389255404694, 0.6877152027028081,
    0.16706295456951875, 0.7259564546408682, 0.19693946055282413, 0.761730578566735, 0.23131827931527224, 0.7948205670466106, 0.2691730934906831, 0.825332081636482,
    0.3099415030959131, 0.853325959406618, 0.3535202097901413, 0.8782538906229107, 0.40089023799272033, 0.8984102434399625, 0.4529251732310723, 0.9112191359814178,
    0.5078640056794708, 0.9146712690731943, 0.5616519666079889, 0.9094327772020283, 0.6119216923689698, 0.8950540037623425, 0.6574617882337107, 0.8738084866764846,
    0.6994820494908942, 0.8482660530943744, 0.7388135339780575, 0.8198750461527688, 0.775158750479601, 0.788989141243473, 0.8078785221990765, 0.7555462713420953,
    0.8361052138935441, 0.7195542055115057, 0.8592123871172533, 0.6812759034843933, 0.8771159986952748, 0.6412243940605555, 0.8902481006481506, 0.5999743595282084,
    0.8992952868651163, 0.5580032282594118, 0.9050110573289222, 0.5156548913779377, 0.908338439928252, 0.4731336721500472, 0.9104896075281127, 0.4305382486815422,
    0.9124796341441906, 0.38798192678294363, 0.18465941635742913, 0.35063191749632183, 0.24110421889338157, 0.31190394310826886, 0.3003235400132397, 0.30828189837331976,
    0.3603094923651325, 0.3135606490643205, 0.4171060234289877, 0.32433417646045615, 0.416842139562573, 0.3526729965541497, 0.36011177591813404, 0.3439660526998693,
    0.3000863121140166, 0.33890077494044946, 0.24116055928407834, 0.34065620413845005, 0.5709736930161899, 0.321407825750195, 0.6305694459247149, 0.30972642336729495,
    0.6895161625920927, 0.3036453838462943, 0.7488591859761683, 0.3069143844433495, 0.8030471337135181, 0.3435156012309415, 0.7485083446528741, 0.3348759588212388,
    0.6893025057931884, 0.33403402013776456, 0.6304822892126991, 0.34038458762875695, 0.5710009285609654, 0.34988479902594455, 0.4954171902473609, 0.40202330022004634,
    0.49604903449415433, 0.4592869389138444, 0.49644391662771625, 0.5162862508677217, 0.4981161256057368, 0.5703284628419502, 0.40749001573145566, 0.5983629921847019,
    0.4537396729649631, 0.6057169923583451, 0.5007345777827058, 0.6116695615531077, 0.5448481727980428, 0.6044131443745976, 0.5882140504891681, 0.5961738788380111,
    0.24303324896316683, 0.40721003719912746, 0.27771706732644313, 0.3907171413930685, 0.31847706697401107, 0.38417234007271117, 0.3621792860449715, 0.3900847721320633,
    0.3965299162804086, 0.41071434661355205, 0.3586805562211872, 0.4203724421417311, 0.31847860588240934, 0.4237674602252073, 0.2789458001651631, 0.41942757306509065,
    0.5938514626567266, 0.4090628827047304, 0.6303565516542536, 0.3864501652756091, 0.6774844732813035, 0.3809319896905685, 0.7150854850525555, 0.3875173254527522,
    0.747519807465081, 0.4025187328459307, 0.7155172856447009, 0.4145958479293519, 0.680051949453018, 0.420041513473271, 0.6359056750107122, 0.41803782782566573,
    0.33916483987223056, 0.6968581311227738, 0.40008790639758807, 0.6758101185779204, 0.47181947887764153, 0.6678850445191217, 0.5025394453374782, 0.6682917934792593,
    0.5337748367911458, 0.6671949030019636, 0.6015915330083903, 0.6742535357237751, 0.6587068892667173, 0.6932163943648724, 0.6192795131720007, 0.7283129162844936,
    0.5665923267827963, 0.7550248076404299, 0.5031303335863617, 0.7648348885181623, 0.4371030429958871, 0.7572539606688756, 0.3814909500115824, 0.7320595346122074,
    0.35129809553480984, 0.6986839074746692, 0.4247987356100664, 0.69127609583798, 0.5027677238758598, 0.6911145821740593, 0.576997542122097, 0.6896269708051024,
    0.6471352843446794, 0.6948977432227927, 0.5799932528781817, 0.7185288017567538, 0.5024914756021335, 0.7285408331555782, 0.4218115644247556, 0.7209126133193829,
    0.3219750495122499, 0.40376441481225156, 0.6751136343101699, 0.40023415216110797,
)

_PIPNET_NUM_NB = 10
_PIPNET_GRID = 8
_PIPNET_INPUT_SIZE = 256
_PIPNET_NUM_LANDMARKS = 98


def _pipnet_reverse_index(num_nb: int = _PIPNET_NUM_NB) -> tuple[np.ndarray, np.ndarray, int]:
    """Faithful reimplementation of the original PIPNet repo's own
    `lib/functions.py::get_meanface` (also independently confirmed present, in numpy form,
    in this project's real artifact source `yakhyo/pipnet-onnx`'s own `get_meanface_info`)
    - for each of the 98 landmarks, which OTHER landmarks' neighbour-offset heads vote for
    its position (a landmark predicts its own `num_nb` nearest neighbours in the mean
    face; this inverts that mapping so each landmark knows who predicts *it*).

    The original pads every landmark's vote list to a common `max_len` by cyclically
    repeating its own real votes ("trick, make them have equal length" - a GPU-batching
    convenience for a fixed-shape gather, not a semantic re-weighting) so every landmark's
    final average is computed over the same number of terms. Replicated exactly here
    (rather than simplified to a plain variable-length average) for fidelity to the
    reference decode - mathematically identical to a plain average of the real votes
    whenever `max_len` is a whole multiple of a landmark's own real vote count, and a
    disclosed, bounded approximation of it otherwise (the same trade the original authors
    made). Computed once per engine instance from the fixed mean-face geometry above, never
    per inference.
    """
    meanface = np.array(_WFLW98_MEANFACE, dtype=np.float64).reshape(-1, 2)
    n = meanface.shape[0]

    own_neighbors = []
    for i in range(n):
        dists = np.sum((meanface[i] - meanface) ** 2, axis=1)
        order = np.argsort(dists)
        own_neighbors.append(order[1 : 1 + num_nb])

    reversed_i: dict[int, list[int]] = {i: [] for i in range(n)}
    reversed_j: dict[int, list[int]] = {i: [] for i in range(n)}
    for i in range(n):
        for j in range(num_nb):
            target = int(own_neighbors[i][j])
            reversed_i[target].append(i)
            reversed_j[target].append(j)

    max_len = max(len(reversed_i[i]) for i in range(n))

    reverse_index1: list[int] = []
    reverse_index2: list[int] = []
    for i in range(n):
        votes_i, votes_j = reversed_i[i], reversed_j[i]
        if not votes_i:
            # Empirically, no landmark in this exact WFLW-98 mean face lacks a real voter
            # - guarded anyway rather than divide by zero on an unexpected geometry.
            reverse_index1.extend([i] * max_len)
            reverse_index2.extend([0] * max_len)
            continue
        repeats = max_len // len(votes_i) + 1
        reverse_index1.extend((votes_i * repeats)[:max_len])
        reverse_index2.extend((votes_j * repeats)[:max_len])

    return (
        np.array(reverse_index1, dtype=np.int64),
        np.array(reverse_index2, dtype=np.int64),
        max_len,
    )


@dataclass(frozen=True)
class LandmarkResult:
    # (x_norm, y_norm, confidence) x 98, normalised to the FULL FRAME - the same
    # convention `Detection.bbox`/`keypoints` use elsewhere in this file.
    points: tuple[tuple[float, float, float], ...]


class PipNetEngine:
    """PIPNet (ResNet-18, WFLW 98-point) facial landmark model (`uniface-pipnet-landmark`).

    Decode - heatmap peak + sub-cell offset + neighbour-vote averaging - follows the
    original `github.com/jhb86253817/PIPNet` reference (`lib/functions.py::forward_pip`
    for the peak/offset step, `lib/demo.py`'s own merge step, `lib/data_utils.py::
    get_meanface` for the neighbour-vote table), independently cross-checked against this
    project's own real upstream artifact source per `uniface_model_manifest.py`
    (`github.com/yakhyo/pipnet-onnx`), whose own numpy port implements the identical
    argmax-peak / offset-gather / reverse-index-merge algorithm end to end.

    One assumption not confirmed from either source: `cls_map` is treated as raw logits
    and passed through a sigmoid purely to report a 0..1 confidence per point. Peak
    *location* (argmax) is invariant to that choice either way, so this only affects the
    reported confidence number, not landmark position - flagged rather than presented as
    a confirmed fact.
    """

    def __init__(self, artifact_path: Path, label_map: dict[int, str] | None = None) -> None:
        self._session, self._providers = _onnx_session(artifact_path)
        self._input = self._session.get_inputs()[0]
        self._reverse_index1, self._reverse_index2, self._max_len = _pipnet_reverse_index()

    def info(self) -> EngineInfo:
        shape = tuple(d if isinstance(d, int) else -1 for d in self._input.shape)
        return EngineInfo(
            framework="onnx", runtime="onnxruntime", available=True, input_shape=shape,
            detail=f"providers={','.join(self._providers)} input={self._input.name}",
        )

    def infer(self, image: np.ndarray, *, confidence: float = 0.25) -> list[Detection]:
        raise OutputContractUnknownError(
            "PIPNet produces 98 landmark points, not detections - call "
            "predict_landmarks() with a Detection from a paired face_detection model "
            "instead."
        )

    def predict_landmarks(self, image: np.ndarray, detection: Detection) -> LandmarkResult:
        import cv2

        crop, offset_x, offset_y = _crop_pipnet_box(image, detection)
        crop_h, crop_w = crop.shape[:2]
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(rgb, (_PIPNET_INPUT_SIZE, _PIPNET_INPUT_SIZE))
        resized = resized.astype(np.float32) / 255.0
        mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        normalized = (resized - mean) / std
        tensor = np.expand_dims(normalized.transpose(2, 0, 1), axis=0).astype(np.float32)

        cls_map, off_x_map, off_y_map, nb_x_map, nb_y_map = self._session.run(
            ["cls_map", "offset_x", "offset_y", "nb_x", "nb_y"], {self._input.name: tensor}
        )
        n, grid = _PIPNET_NUM_LANDMARKS, _PIPNET_GRID
        cls_flat = cls_map[0].reshape(n, grid * grid)
        off_x_flat = off_x_map[0].reshape(n, grid * grid)
        off_y_flat = off_y_map[0].reshape(n, grid * grid)
        nb_x_flat = nb_x_map[0].reshape(n, _PIPNET_NUM_NB, grid * grid)
        nb_y_flat = nb_y_map[0].reshape(n, _PIPNET_NUM_NB, grid * grid)

        lm_range = np.arange(n)
        max_ids = np.argmax(cls_flat, axis=1)
        peak_scores = 1.0 / (1.0 + np.exp(-cls_flat[lm_range, max_ids]))  # sigmoid, reporting only
        cols = (max_ids % grid).astype(np.float64)
        rows = (max_ids // grid).astype(np.float64)
        own_off_x = off_x_flat[lm_range, max_ids]
        own_off_y = off_y_flat[lm_range, max_ids]
        direct_x = (cols + own_off_x) / grid
        direct_y = (rows + own_off_y) / grid

        nb_range = np.arange(_PIPNET_NUM_NB)
        nb_own_x = nb_x_flat[lm_range[:, None], nb_range[None, :], max_ids[:, None]]
        nb_own_y = nb_y_flat[lm_range[:, None], nb_range[None, :], max_ids[:, None]]
        # (98, num_nb) - vote FROM landmark i for the position of its j-th nearest
        # mean-face neighbour, at landmark i's own peak grid cell (the "regression
        # module" idea: a landmark's own feature also predicts where nearby landmarks are).
        nb_pred_x = (cols[:, None] + nb_own_x) / grid
        nb_pred_y = (rows[:, None] + nb_own_y) / grid

        gather = self._reverse_index1 * _PIPNET_NUM_NB + self._reverse_index2
        votes_x = nb_pred_x.reshape(-1)[gather].reshape(n, self._max_len)
        votes_y = nb_pred_y.reshape(-1)[gather].reshape(n, self._max_len)

        merged_x = np.mean(np.concatenate([direct_x[:, None], votes_x], axis=1), axis=1)
        merged_y = np.mean(np.concatenate([direct_y[:, None], votes_y], axis=1), axis=1)

        full_h, full_w = image.shape[:2]
        points = tuple(
            (
                float(np.clip((offset_x + merged_x[i] * crop_w) / full_w, 0.0, 1.0)),
                float(np.clip((offset_y + merged_y[i] * crop_h) / full_h, 0.0, 1.0)),
                float(peak_scores[i]),
            )
            for i in range(n)
        )
        return LandmarkResult(points=points)


# --- uniface-zoo face detectors (BlazeFace / CenterFace / RetinaFace) -----------------
#
# The last 3 of the 15 `uniface-zoo` models pulled 2026-09-03, and the highest-risk decode
# of the set: CHECKLIST.md's own tiering calls them out as the group where "a wrong anchor
# grid produces plausible-looking but silently wrong boxes". Every constant below is ported
# from the real reference implementation these exact weights ship with - github.com/yakhyo/
# uniface (MIT), the clone at `uniface-main/` in this repo's working tree - not re-derived
# from the architecture papers and not inferred from the ONNX graph in isolation.
#
# Real gate-2 shapes, re-probed live this session against the real artifacts (a fresh
# `onnxruntime.InferenceSession` inside the running `csense-ai-runtime-1` container, each
# artifact fetched from the real `csense-models` MinIO bucket and sha256-verified against
# its own registry row before loading) - NOT trusted from any prior write-up:
#   uniface-blazeface-detect:  input "input" (batch,3,128,128) f32
#                              -> "regressors" (batch,896,16) + "scores" (batch,896,1)
#   uniface-centerface-detect: input "input" (batch,3,height,width) f32, fully dynamic
#                              -> "heatmap" (b,1,h/4,w/4), "scale" (b,2,h/4,w/4),
#                                 "offset" (b,2,h/4,w/4), "landmarks" (b,10,h/4,w/4)
#   uniface-retinaface-detect: input "input" (batch,3,height,width) f32, fully dynamic
#                              -> "loc" (b,N,4), "conf" (b,N,2), "landmarks" (b,N,10)
#
# Two of those probes are load-bearing confirmations of the anchor maths, not just shape
# bookkeeping, because the anchor count is the one thing a wrong anchor config cannot fake:
#   * RetinaFace at a 640x640 input returned N=16800 priors. The ported `_retinaface_
#     anchors((640,640))` generates exactly 16800 - strides (8,16,32) give 80x80 + 40x40 +
#     20x20 = 8400 cells, 2 min_sizes per cell. A mismatched training config (a different
#     stride set, 3 sizes per level, a different input size) would land on a different
#     number and the decode would fail loudly on the shape rather than silently mis-place
#     boxes. This is why RetinaFace runs at a fixed 640x640 here even though the graph's
#     own H/W axes are dynamic: the priors are a function of the input size, so the size
#     the anchors were built for and the size actually fed to the model must be the same.
#   * BlazeFace's head is a literal (896, 16) - the ported `_blazeface_anchors(128)`
#     asserts on exactly 896, and 16 = 4 box terms + 6 keypoints x 2, which is what fixes
#     the keypoint count at 6 rather than the 5 every other face model in this file uses.
#
# `uniface-centerface-detect` is grouped with the other two in CHECKLIST.md's risk tiering
# but is NOT actually anchor-based - confirmed by reading the real source rather than
# assuming the grouping was right. It is anchor-free/CenterNet-style: faces are peaks in a
# stride-4 heatmap, with separate scale/offset/landmark maps read at each peak's own cell.
# There is no anchor grid to get wrong; the equivalent trap is the stride (4) and the
# per-axis rounding, both taken from the reference below.
#
# All three are dispatched by exact model_name (see `_UNIFACE_ENGINES_BY_MODEL_NAME`), and
# all three carry task_code="face_detection" - the same task_code `insightface-buffalo-l-
# detect` (SCRFD) already owns, on a completely unrelated architecture. Nothing here
# branches on task_code, deliberately: that would re-open exactly the dispatch collision
# already found and closed for the face_recognition task_code.

# --- BlazeFace -----------------------------------------------------------------------
#
# Ported from `uniface/detection/blazeface.py`. MediaPipe's SsdAnchorsCalculator config
# for `face_detection_short_range`; hardcoded upstream (and here) because every output
# dimension except the input size is a dynamic symbol in the ONNX metadata - re-confirmed
# by the live probe above, whose only concrete input dims were 3x128x128.
_BLAZEFACE_ANCHOR_STRIDES = (8, 16, 16, 16)
_BLAZEFACE_NUM_ANCHORS = 896
_BLAZEFACE_NUM_KEYPOINTS = 6
# MediaPipe's own score_clipping_thresh, applied BEFORE the sigmoid: strongly-negative
# anchor logits overflow np.exp otherwise.
_BLAZEFACE_SCORE_CLIP = 100.0
# uniface's own BlazeFace default. Its NMS is MediaPipe's *weighted* variant, so this is a
# blend threshold, not a discard threshold - see `_blazeface_weighted_nms`.
_BLAZEFACE_NMS_THRESHOLD = 0.3


def _blazeface_anchors(input_size: int) -> np.ndarray:
    """SSD anchor centres for the short-range face detector.

    Ported verbatim from `uniface.detection.blazeface._generate_face_anchors`. Layers
    sharing a stride stack their anchors per cell, giving 16x16x2 + 8x8x6 = 896 anchors at
    128px (aspect ratio 1.0, one interpolated scale, fixed anchor size).

    Returns normalised (896, 2) anchor centres. Raises if the count does not match the
    exported head - the one check that actually catches a wrong anchor config.
    """
    anchors: list[tuple[float, float]] = []
    idx = 0
    while idx < len(_BLAZEFACE_ANCHOR_STRIDES):
        last = idx
        while (
            last < len(_BLAZEFACE_ANCHOR_STRIDES)
            and _BLAZEFACE_ANCHOR_STRIDES[last] == _BLAZEFACE_ANCHOR_STRIDES[idx]
        ):
            last += 1
        repeats = 2 * (last - idx)
        cells = input_size // _BLAZEFACE_ANCHOR_STRIDES[idx]
        for y in range(cells):
            for x in range(cells):
                anchors.extend([((x + 0.5) / cells, (y + 0.5) / cells)] * repeats)
        idx = last

    if len(anchors) != _BLAZEFACE_NUM_ANCHORS:
        raise OutputContractUnknownError(
            f"Expected {_BLAZEFACE_NUM_ANCHORS} BlazeFace anchors for a {input_size}px input, "
            f"generated {len(anchors)}. The anchor config does not match this artifact's head."
        )
    return np.array(anchors, dtype=np.float64)


def _blazeface_weighted_nms(detections: np.ndarray, iou_threshold: float) -> np.ndarray:
    """MediaPipe's *weighted* non-maximum suppression, ported from the same reference.

    Overlapping candidates are score-averaged into the winner rather than discarded, boxes
    and keypoints alike - so the plain IoU-discard NMS the rest of this file uses (and
    `cv2.dnn.NMSBoxes` in `decode_raw_yolo`) cannot be substituted without changing the
    output. Rows are (score, cx, cy, w, h, kp0x, kp0y, ...), normalised.

    The winner joins its own blend unconditionally: relying on its self-IoU clearing the
    threshold loops forever when it cannot (iou_threshold=1.0 fails the strict `>`, and a
    zero-area box scores an IoU of 0 against itself). That guard is upstream's own.
    """
    remaining = detections[np.argsort(-detections[:, 0])]
    output = []
    while len(remaining):
        top = remaining[0]
        x1 = remaining[:, 1] - remaining[:, 3] / 2
        y1 = remaining[:, 2] - remaining[:, 4] / 2
        x2 = remaining[:, 1] + remaining[:, 3] / 2
        y2 = remaining[:, 2] + remaining[:, 4] / 2
        tx1, ty1 = top[1] - top[3] / 2, top[2] - top[4] / 2
        tx2, ty2 = top[1] + top[3] / 2, top[2] + top[4] / 2

        inter = np.maximum(0, np.minimum(x2, tx2) - np.maximum(x1, tx1)) * np.maximum(
            0, np.minimum(y2, ty2) - np.maximum(y1, ty1)
        )
        union = (x2 - x1) * (y2 - y1) + (tx2 - tx1) * (ty2 - ty1) - inter
        iou = inter / np.maximum(union, 1e-9)

        merge = iou > iou_threshold
        merge[0] = True

        overlapping = remaining[merge]
        weights = overlapping[:, :1]
        blended = top.copy()
        total_weight = weights.sum()
        if total_weight > 0:
            blended[1:] = (overlapping[:, 1:] * weights).sum(axis=0) / total_weight
        output.append(blended)
        remaining = remaining[~merge]

    return np.array(output)


class BlazeFaceEngine:
    """MediaPipe BlazeFace short-range face detector (`uniface-blazeface-detect`).

    An SSD detector over two anchor grids on a 128x128 letterboxed image. Preprocessing,
    anchor generation, decode and weighted NMS are all ported from `uniface/detection/
    blazeface.py`; see this section's header comment for the live gate-2 evidence that the
    896-anchor grid matches this exact artifact's head.

    **Emits 6 keypoints, not the 5-point alignment template** (right eye, left eye, nose
    tip, mouth centre, right ear, left ear - MediaPipe's own order). This is a real,
    load-bearing difference from every other face model in this file, and upstream marks it
    the same way (`supports_alignment = False`): these keypoints cannot be fed to
    `InsightFaceEngine.embed()` / `UnifaceEmbeddingEngine.embed()` / the aligned-chip
    croppers, which all need the 5-point ArcFace template in the detector's own order. Both
    `embed()` paths already refuse a non-5-point Detection explicitly, so a BlazeFace
    Detection fails loudly there rather than producing a silently mis-aligned crop - checked
    against those methods' own guards rather than assumed.

    Short-range covers faces within roughly 2m and scores below SCRFD on WIDER FACE; it is
    a 460KB model, not a replacement for the platform's production SCRFD detector.
    """

    def __init__(self, artifact_path: Path, label_map: dict[int, str] | None = None) -> None:
        self._session, self._providers = _onnx_session(artifact_path)
        self._input = self._session.get_inputs()[0]
        self._output_names = [o.name for o in self._session.get_outputs()]
        self._labels = label_map or {}
        # Only the input size survives export as a concrete dimension (upstream's own note,
        # confirmed by the live probe: ("batch", 3, 128, 128)).
        size = self._input.shape[2]
        if not isinstance(size, int):
            raise OutputContractUnknownError(
                "BlazeFace artifact declares a dynamic spatial input dimension; the 896-anchor "
                "grid is a function of a fixed input size and cannot be generated without it."
            )
        self._input_size = int(size)
        self._anchors = _blazeface_anchors(self._input_size)

    def info(self) -> EngineInfo:
        shape = tuple(d if isinstance(d, int) else -1 for d in self._input.shape)
        return EngineInfo(
            framework="onnx", runtime="onnxruntime", available=True, input_shape=shape,
            labels=self._labels,
            detail=(
                f"blazeface anchors={len(self._anchors)} input_size={self._input_size} "
                f"keypoints={_BLAZEFACE_NUM_KEYPOINTS} providers={','.join(self._providers)}"
            ),
        )

    def infer(self, image: np.ndarray, *, confidence: float = 0.25) -> list[Detection]:
        import cv2

        height, width = image.shape[:2]
        size = self._input_size

        # Aspect-preserving letterbox onto a square canvas, centred - upstream warps the
        # input itself rather than going through the shared resize helper.
        scale = size / max(height, width)
        pad_x, pad_y = (size - width * scale) / 2.0, (size - height * scale) / 2.0
        matrix = np.array([[scale, 0.0, pad_x], [0.0, scale, pad_y]])
        canvas = cv2.warpAffine(image, matrix, (size, size))

        # BlazeFace expects RGB normalised to [-1, 1] (not the [0,1] OnnxEngine._preprocess
        # applies, and not the Caffe BGR mean RetinaFace uses) - per its own preprocess().
        rgb = canvas[:, :, ::-1].astype(np.float32)
        blob = np.transpose((rgb - 127.5) / 127.5, (2, 0, 1))[np.newaxis]

        regressors, logits = self._session.run(self._output_names, {self._input.name: blob})

        raw = np.clip(
            logits[0].ravel().astype(np.float64), -_BLAZEFACE_SCORE_CLIP, _BLAZEFACE_SCORE_CLIP
        )
        scores = 1.0 / (1.0 + np.exp(-raw))
        keep = scores >= confidence
        if not keep.any():
            return []

        reg, anchor = regressors[0][keep].astype(np.float64), self._anchors[keep]
        rows = np.empty((int(keep.sum()), 5 + 2 * _BLAZEFACE_NUM_KEYPOINTS))
        rows[:, 0] = scores[keep]
        rows[:, 1:3] = reg[:, 0:2] / size + anchor  # centre x, y
        rows[:, 3:5] = reg[:, 2:4] / size  # width, height
        for k in range(_BLAZEFACE_NUM_KEYPOINTS):
            rows[:, 5 + 2 * k : 7 + 2 * k] = reg[:, 4 + 2 * k : 6 + 2 * k] / size + anchor

        rows = _blazeface_weighted_nms(rows, _BLAZEFACE_NMS_THRESHOLD)

        def unletterbox(xy_normalised: np.ndarray) -> np.ndarray:
            return (xy_normalised * size - (pad_x, pad_y)) / scale

        centres = unletterbox(rows[:, 1:3])
        halves = rows[:, 3:5] * size / scale / 2.0
        bboxes = np.concatenate([centres - halves, centres + halves], axis=1)
        keypoints = unletterbox(rows[:, 5:].reshape(-1, _BLAZEFACE_NUM_KEYPOINTS, 2))

        return _face_detections(
            bboxes, rows[:, 0], keypoints, width, height, self._labels
        )


# --- CenterFace ----------------------------------------------------------------------
#
# Ported from `uniface/detection/centerface.py`.
# CenterFace predicts on a single feature map downsampled 4x from the input; the FPN needs
# both input sides to be a multiple of 32. Both constants are upstream's own.
_CENTERFACE_STRIDE = 4
_CENTERFACE_SIZE_DIVISOR = 32
_CENTERFACE_NMS_THRESHOLD = 0.3
# Upper bound on inference resolution, not a fixed shape: larger frames are scaled down to
# fit, smaller ones are left at native resolution. uniface's own default; it matters here
# because the graph's H/W are fully dynamic, so without a cap a 2592x1520 mainstream frame
# would run the FPN at full resolution on a CPU-only box (see CLAUDE.md's core budget).
_CENTERFACE_MAX_SIZE = (640, 640)


class CenterFaceEngine:
    """CenterFace anchor-free face detector (`uniface-centerface-detect`).

    **Not actually anchor-based**, despite CHECKLIST.md's own risk grouping - established by
    reading the real source, not by trusting the grouping. Faces are peaks in a stride-4
    centre heatmap, with box scale, centre offset and 5-point landmarks read from three more
    maps at each peak's own cell (CenterNet-style). The live gate-2 probe agrees: the four
    outputs are named `heatmap`/`scale`/`offset`/`landmarks` with 1/2/2/10 channels at
    exactly a quarter of the input resolution.

    The traps here are the ones a decode can still get silently wrong, and all are taken
    from the reference rather than re-derived:
      * box size is `exp(scale) * 4` - a log-space prediction, so dropping the exp yields
        boxes a few pixels across that still look like plausible detections;
      * landmark pairs are stored (dy, dx), NOT (dx, dy) - `points[1::2]` is x and
        `points[0::2]` is y. Swapping them mirrors every landmark about the box diagonal,
        which on a roughly-square face crop still lands inside the box;
      * each side is rounded UP independently to a multiple of 32, so the horizontal and
        vertical scale factors are different numbers and must be applied per axis.

    Landmarks come from a single coarse feature-map cell per face and are less precise than
    SCRFD/RetinaFace; upstream's own note adds that accuracy degrades past ~20-30 degrees of
    in-plane rotation.
    """

    def __init__(self, artifact_path: Path, label_map: dict[int, str] | None = None) -> None:
        self._session, self._providers = _onnx_session(artifact_path)
        self._input = self._session.get_inputs()[0]
        self._output_names = [o.name for o in self._session.get_outputs()]
        self._labels = label_map or {}

    def info(self) -> EngineInfo:
        shape = tuple(d if isinstance(d, int) else -1 for d in self._input.shape)
        return EngineInfo(
            framework="onnx", runtime="onnxruntime", available=True, input_shape=shape,
            labels=self._labels,
            detail=(
                f"centerface stride={_CENTERFACE_STRIDE} max_size={_CENTERFACE_MAX_SIZE} "
                f"anchor_free=true providers={','.join(self._providers)}"
            ),
        )

    @staticmethod
    def _resize(image: np.ndarray) -> tuple[np.ndarray, float, float]:
        """Mirrors `CenterFace._resize`: scale down to fit the cap, never up, then round
        each side up to a multiple of 32 independently. Returns (resized, scale_w, scale_h)
        - two separate factors, because that independent rounding skews the axes."""
        import cv2

        height, width = image.shape[:2]
        max_width, max_height = _CENTERFACE_MAX_SIZE
        ratio = min(1.0, max_width / width, max_height / height)

        divisor = _CENTERFACE_SIZE_DIVISOR
        new_width = max(divisor, int(np.ceil(width * ratio / divisor) * divisor))
        new_height = max(divisor, int(np.ceil(height * ratio / divisor) * divisor))

        resized = cv2.resize(image, (new_width, new_height))
        return resized, new_width / width, new_height / height

    def infer(self, image: np.ndarray, *, confidence: float = 0.25) -> list[Detection]:
        original_height, original_width = image.shape[:2]
        resized, scale_w, scale_h = self._resize(image)

        # CenterFace consumes raw RGB pixel values - no mean subtraction, no /255 scaling.
        # Confirmed against its own preprocess(); normalising would silently darken every
        # input by 255x and the heatmap would simply go quiet.
        blob = resized[:, :, ::-1].astype(np.float32).transpose(2, 0, 1)[np.newaxis]
        heatmap, scale, offset, lms = self._session.run(
            self._output_names, {self._input.name: np.ascontiguousarray(blob)}
        )

        height, width = resized.shape[:2]
        heat = heatmap[0, 0]
        cy, cx = np.where(heat > confidence)
        if len(cy) == 0:
            return []

        scores = heat[cy, cx]
        box_h = np.exp(scale[0, 0, cy, cx]) * _CENTERFACE_STRIDE
        box_w = np.exp(scale[0, 1, cy, cx]) * _CENTERFACE_STRIDE
        offset_y = offset[0, 0, cy, cx]
        offset_x = offset[0, 1, cy, cx]

        x1 = np.clip((cx + offset_x + 0.5) * _CENTERFACE_STRIDE - box_w / 2, 0, width)
        y1 = np.clip((cy + offset_y + 0.5) * _CENTERFACE_STRIDE - box_h / 2, 0, height)
        x2 = np.clip(x1 + box_w, 0, width)
        y2 = np.clip(y1 + box_h, 0, height)
        bboxes = np.stack([x1, y1, x2, y2], axis=1)

        # Landmarks are predicted relative to the box as (dy, dx) pairs per point.
        points = lms[0][:, cy, cx]  # (10, N)
        kps_x = points[1::2].T * box_w[:, None] + x1[:, None]
        kps_y = points[0::2].T * box_h[:, None] + y1[:, None]
        landmarks = np.stack([kps_x, kps_y], axis=2)  # (N, 5, 2)

        bboxes[:, 0::2] /= scale_w
        bboxes[:, 1::2] /= scale_h
        landmarks[..., 0] /= scale_w
        landmarks[..., 1] /= scale_h

        order = scores.argsort()[::-1]
        pre_det = np.hstack((bboxes, scores[:, None])).astype(np.float32, copy=False)[order]
        landmarks = landmarks[order]

        keep = _uniface_nms(pre_det, _CENTERFACE_NMS_THRESHOLD)
        pre_det, landmarks = pre_det[keep], landmarks[keep]

        return _face_detections(
            pre_det[:, :4], pre_det[:, 4], landmarks,
            original_width, original_height, self._labels,
        )


# --- RetinaFace ----------------------------------------------------------------------
#
# Ported from `uniface/detection/retinaface.py` plus `uniface/common.py`'s own
# `generate_anchors`/`decode_boxes`/`decode_landmarks`/`non_max_suppression`/`resize_image`.
# The FPN strides and per-level anchor sizes ARE the training config this section's header
# warns about; they are copied, not reconstructed.
_RETINAFACE_STEPS = (8, 16, 32)
_RETINAFACE_MIN_SIZES = ((16, 32), (64, 128), (256, 512))
# The offset-regression variances the model was trained with. A wrong pair here is the
# textbook silent failure: boxes still land near the face, just consistently the wrong size.
_RETINAFACE_VARIANCES = (0.1, 0.2)
# Fixed, because the priors are a function of it - see the header comment. Upstream's own
# default, and the size whose 16800 priors the live probe confirmed.
_RETINAFACE_INPUT_SIZE = (640, 640)
_RETINAFACE_NMS_THRESHOLD = 0.4
_RETINAFACE_PRE_NMS_TOPK = 5000
_RETINAFACE_POST_NMS_TOPK = 750
# Caffe-order BGR channel means. RetinaFace does NOT swap to RGB - unlike BlazeFace and
# CenterFace above, which both do. Per-model, read from each one's own preprocess().
_RETINAFACE_BGR_MEAN = (104.0, 117.0, 123.0)


def _retinaface_anchors(image_size: tuple[int, int]) -> np.ndarray:
    """Ported verbatim from `uniface.common.generate_anchors` (RetinaFace-specific).

    Returns (num_anchors, 4) priors in centre-offset form, normalised: cx, cy, s_kx, s_ky.
    At 640x640 this is exactly 16800 rows, matching the live probe's own N - the check that
    the anchor grid belongs to this artifact.

    Note upstream's axis convention, kept as-is rather than "fixed": the feature-map extents
    are derived from `image_size[0]`/`image_size[1]` in the opposite order to the scale
    terms. That is invisible for the square input this engine always uses, and rewriting it
    would be changing the reference on a guess about which reading was intended.
    """
    anchors: list[float] = []
    feature_maps = [
        [math.ceil(image_size[0] / step), math.ceil(image_size[1] / step)]
        for step in _RETINAFACE_STEPS
    ]

    for k, (map_height, map_width) in enumerate(feature_maps):
        step = _RETINAFACE_STEPS[k]
        for i, j in itertools.product(range(map_height), range(map_width)):
            for min_size in _RETINAFACE_MIN_SIZES[k]:
                s_kx = min_size / image_size[1]
                s_ky = min_size / image_size[0]
                dense_cx = [x * step / image_size[1] for x in [j + 0.5]]
                dense_cy = [y * step / image_size[0] for y in [i + 0.5]]
                for cy, cx in itertools.product(dense_cy, dense_cx):
                    anchors += [cx, cy, s_kx, s_ky]

    return np.array(anchors, dtype=np.float32).reshape(-1, 4)


def _retinaface_decode_boxes(loc: np.ndarray, priors: np.ndarray) -> np.ndarray:
    """Ported from `uniface.common.decode_boxes`. Undoes the offset regression encoding
    used at train time; returns normalised xyxy."""
    var0, var1 = _RETINAFACE_VARIANCES
    cxcy = priors[:, :2] + loc[:, :2] * var0 * priors[:, 2:]
    wh = priors[:, 2:] * np.exp(loc[:, 2:] * var1)
    boxes = np.zeros_like(loc)
    boxes[:, :2] = cxcy - wh / 2
    boxes[:, 2:] = cxcy + wh / 2
    return boxes


def _retinaface_decode_landmarks(predictions: np.ndarray, priors: np.ndarray) -> np.ndarray:
    """Ported from `uniface.common.decode_landmarks`. Only the first variance applies to
    landmarks - they are pure centre offsets with no log-space size term."""
    var0 = _RETINAFACE_VARIANCES[0]
    reshaped = predictions.reshape(predictions.shape[0], 5, 2)
    priors_xy = np.repeat(priors[:, :2][:, np.newaxis, :], 5, axis=1)
    priors_wh = np.repeat(priors[:, 2:][:, np.newaxis, :], 5, axis=1)
    return priors_xy + reshaped * var0 * priors_wh


def _uniface_nms(dets: np.ndarray, threshold: float) -> list[int]:
    """Ported from `uniface.common.non_max_suppression`, used by RetinaFace and CenterFace.

    Kept rather than reusing `cv2.dnn.NMSBoxes` (what `decode_raw_yolo` uses for the plate
    detector) because this one computes areas with the `+1` pixel convention upstream uses
    and takes an already-score-sorted input; the two do not return identical keep sets.
    """
    x1, y1, x2, y2, scores = dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3], dets[:, 4]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]

    keep: list[int] = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1 + 1)
        h = np.maximum(0.0, yy2 - yy1 + 1)
        inter = w * h
        ovr = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[np.where(ovr <= threshold)[0] + 1]

    return keep


class RetinaFaceEngine:
    """RetinaFace (MobileNetV2 backbone) face detector (`uniface-retinaface-detect`).

    The classic `loc`/`conf`/`landmarks` head with 5-point landmarks, decoded against a
    fixed 640x640 anchor grid ported from `uniface.common.generate_anchors`. The live gate-2
    probe returned exactly 16800 priors at that input size, which is what the ported anchor
    generator produces - see this section's header for why that number is the real check.

    Two details that would each be a silent wrong-box bug if guessed:
      * the face score is `conf[:, 1]`, not `conf[:, 0]` - column 0 is background. Reading
        column 0 inverts every score and the detector "finds" faces in empty sky;
      * preprocessing subtracts the Caffe BGR mean (104, 117, 123) with NO RGB swap and no
        /255 - the opposite of both other detectors in this section.

    Unlike BlazeFace, these 5 points are the alignment template (left eye, right eye, nose,
    left mouth corner, right mouth corner) in the same order the platform's SCRFD detector
    emits, so a RetinaFace `Detection` is structurally usable by the `embed()` /
    aligned-chip paths. **That structural fit is not itself validated accuracy** - landmark
    precision against a ground truth was never measured here (CHECKLIST.md states the gap).
    """

    def __init__(self, artifact_path: Path, label_map: dict[int, str] | None = None) -> None:
        self._session, self._providers = _onnx_session(artifact_path)
        self._input = self._session.get_inputs()[0]
        self._output_names = [o.name for o in self._session.get_outputs()]
        self._labels = label_map or {}
        self._priors = _retinaface_anchors(_RETINAFACE_INPUT_SIZE)

    def info(self) -> EngineInfo:
        shape = tuple(d if isinstance(d, int) else -1 for d in self._input.shape)
        return EngineInfo(
            framework="onnx", runtime="onnxruntime", available=True, input_shape=shape,
            labels=self._labels,
            detail=(
                f"retinaface priors={len(self._priors)} input_size={_RETINAFACE_INPUT_SIZE} "
                f"providers={','.join(self._providers)}"
            ),
        )

    def infer(self, image: np.ndarray, *, confidence: float = 0.25) -> list[Detection]:
        import cv2

        original_height, original_width = image.shape[:2]
        target_w, target_h = _RETINAFACE_INPUT_SIZE

        # `uniface.common.resize_image`: aspect-preserving resize pasted at the TOP-LEFT of a
        # zero canvas, not centred. That is why the inverse below is a plain divide with no
        # pad offset - a centred letterbox would need one, and adding it "for safety" would
        # shift every box by half the padding.
        im_ratio = float(original_height) / original_width
        model_ratio = target_h / target_w
        if im_ratio > model_ratio:
            new_height = target_h
            new_width = int(new_height / im_ratio)
        else:
            new_width = target_w
            new_height = int(new_width * im_ratio)
        resize_factor = float(new_height) / original_height
        canvas = np.zeros((target_h, target_w, 3), dtype=np.uint8)
        canvas[:new_height, :new_width, :] = cv2.resize(image, (new_width, new_height))

        mean = np.array(_RETINAFACE_BGR_MEAN, dtype=np.float32)
        blob = (canvas.astype(np.float32) - mean).transpose(2, 0, 1)[np.newaxis]

        outputs = self._session.run(self._output_names, {self._input.name: np.ascontiguousarray(blob)})
        loc, conf, landmark_pred = (outputs[0].squeeze(0), outputs[1].squeeze(0), outputs[2].squeeze(0))

        if loc.shape[0] != self._priors.shape[0]:
            raise OutputContractUnknownError(
                f"RetinaFace returned {loc.shape[0]} predictions but the ported anchor grid for "
                f"{_RETINAFACE_INPUT_SIZE} generates {self._priors.shape[0]} priors. The anchor "
                "config does not match this artifact - decoding anyway would produce "
                "plausible-looking but wrong boxes."
            )

        boxes = _retinaface_decode_boxes(loc, self._priors)
        landmarks = _retinaface_decode_landmarks(landmark_pred, self._priors)

        # Normalised canvas coords -> canvas pixels -> original-image pixels.
        boxes = boxes * np.array([target_w, target_h] * 2, dtype=np.float32) / resize_factor
        landmarks = landmarks * np.array([target_w, target_h], dtype=np.float32) / resize_factor

        scores = conf[:, 1]  # column 1 is face, column 0 is background
        mask = scores > confidence
        boxes, landmarks, scores = boxes[mask], landmarks[mask], scores[mask]
        if scores.size == 0:
            return []

        order = scores.argsort()[::-1][:_RETINAFACE_PRE_NMS_TOPK]
        boxes, landmarks, scores = boxes[order], landmarks[order], scores[order]

        detections = np.hstack((boxes, scores[:, np.newaxis])).astype(np.float32, copy=False)
        keep = _uniface_nms(detections, _RETINAFACE_NMS_THRESHOLD)
        detections = detections[keep][:_RETINAFACE_POST_NMS_TOPK]
        landmarks = landmarks[keep][:_RETINAFACE_POST_NMS_TOPK]

        return _face_detections(
            detections[:, :4], detections[:, 4], landmarks,
            original_width, original_height, self._labels,
        )


def _face_detections(
    boxes_px: np.ndarray,
    scores: np.ndarray,
    keypoints_px: np.ndarray,
    width: int,
    height: int,
    labels: dict[int, str],
) -> list[Detection]:
    """Shared tail for the three detectors above: pixel boxes/keypoints in the ORIGINAL
    frame's coordinate space -> this file's normalised `Detection` shape.

    Keypoints carry the box's own detection score, the same convention `InsightFaceEngine`
    already uses for SCRFD (none of these three models emits a per-landmark confidence).
    Keypoints are NOT clipped to the frame while boxes are: a real jaw or ear point can sit
    just outside a tight box or a frame edge, and clamping it to 0.0/1.0 would quietly
    fabricate a landmark on the border rather than reporting where the model actually put
    it. Boxes are clipped because every downstream consumer of `Detection.bbox` in this
    codebase treats it as an in-frame region.
    """
    detections: list[Detection] = []
    for i in range(boxes_px.shape[0]):
        x1, y1, x2, y2 = (float(v) for v in boxes_px[i])
        score = float(scores[i])
        detections.append(
            Detection(
                class_id=0,
                class_name=labels.get(0, "face"),
                confidence=score,
                bbox=(
                    float(np.clip(x1 / width, 0.0, 1.0)),
                    float(np.clip(y1 / height, 0.0, 1.0)),
                    float(np.clip(x2 / width, 0.0, 1.0)),
                    float(np.clip(y2 / height, 0.0, 1.0)),
                ),
                keypoints=[
                    (float(kx) / width, float(ky) / height, score) for kx, ky in keypoints_px[i]
                ],
            )
        )
    return detections
# --- uniface-zoo: BiSeNet face parsing / FaceAttribNet 5 binary attributes -------------
#
# The last 2 of the 15 uniface-zoo models, and the 2 CHECKLIST.md previously recorded as
# "should not be decoded on a guess at all, and are not" - because at the time the only
# evidence available was the ONNX graph itself, which genuinely does not say which of
# bisenet's 3 identically-shaped outputs is the real class map, nor what the 5 columns of
# faceattribnet's `probability` tensor mean. That entry was correct about the evidence it
# had and wrong about the conclusion: the MIT-licensed public reference implementation
# these exact weights ship with (`github.com/yakhyo/uniface`) has full source for both, and
# resolves each question outright rather than by inference. Both decodes below are ported
# from that source, the same discipline the 4 cross-check models above already follow - not
# reimplemented from a paper, a description, or the graph in isolation.
#
# What the reference resolves, specifically:
#   * `uniface/parsing/bisenet.py::parse`/`postprocess` only ever reads `outputs[0]` - the
#     FIRST tensor `session.run(...)` returns, i.e. the ONNX graph's own first declared
#     output, which our real gate-2 probe confirms is the semantically-named `output`. The
#     other two (`414`/`424`) are auxiliary BiSeNet context-path heads, present only because
#     the export traced the training-time forward pass; they carry raw ONNX node IDs for
#     exactly that reason, while the real head kept its name. This is consistent with, but
#     NOT proven by, their being identically shaped - see the note in `parse()` below.
#   * `uniface/attribute/faceattribnet.py::postprocess` unpacks the `(1, 5)` tensor as
#     `left_eye_open, right_eye_open, eyeglasses, mask, sunglasses` - five INDEPENDENT
#     binary classifier heads, not a 5-way softmax. They do not sum to 1 and several can be
#     high at once (a face can wear a mask and sunglasses); threshold each separately.

# `uniface.draw.FACE_PARSING_LABELS`, the CelebAMask-HQ 19-class scheme this checkpoint was
# trained on. Ported verbatim so a caller can name a class id without re-deriving the list.
FACE_PARSING_LABELS: tuple[str, ...] = (
    "background", "skin", "l_brow", "r_brow", "l_eye", "r_eye", "eye_g", "l_ear", "r_ear",
    "ear_r", "nose", "mouth", "u_lip", "l_lip", "neck", "neck_l", "cloth", "hair", "hat",
)
_BISENET_NUM_CLASSES = len(FACE_PARSING_LABELS)  # 19
_BISENET_FALLBACK_INPUT_SIZE = 512  # only used if the graph declares a symbolic spatial dim

# How far to expand a detector's face box before parsing, as a fraction of the box's own
# width/height per side (so 0.35 -> a crop 1.7x the box in each dimension).
#
# NOT from the reference - `uniface`'s own example crops the raw detector box. It is here
# because parsing the raw box was MEASURED to fail outright on this platform's own detector
# output: the SCRFD boxes on the project's one real multi-face fixture are 36x52 and 40x50
# px, and at margin 0.0 BiSeNet returns a single-class, 100%-`background` mask for both -
# a degenerate result that a caller could easily mistake for "no face here". Measured on
# those 2 real faces, live, across 7 margins (each value = distinct classes / skin fraction
# / background fraction):
#
#   margin  face1                     face2
#   0.00    1  / 0.000 / 1.000        1  / 0.000 / 1.000   <- degenerate, both
#   0.15    6  / 0.047 / 0.908        2  / 0.017 / 0.983
#   0.25    7  / 0.270 / 0.619        10 / 0.144 / 0.780
#   0.35    10 / 0.284 / 0.476        10 / 0.294 / 0.430   <- best on both
#   0.50    10 / 0.207 / 0.558        12 / 0.200 / 0.377
#   0.65    6  / 0.156 / 0.584        11 / 0.155 / 0.451
#   0.80    9  / 0.117 / 0.618        12 / 0.126 / 0.518
#
# The mechanism is not a tuning accident: BiSeNet is trained on CelebAMask-HQ, whose crops
# frame the whole HEAD - hair, ears, neck - while a face detector's box is tight to the
# face by construction and excludes exactly those classes. Expanding aligns the input with
# the training distribution rather than compensating for a decode error (a broken decode
# was ruled out separately: the same code produces an anatomically coherent parse at
# margin >= 0.25, and `eye_g` fires on both faces, which really are wearing sunglasses).
#
# **Weakly grounded, deliberately flagged**: 0.35 is the best value across 2 real faces
# from 1 photograph. That is enough to reject 0.0, not enough to call 0.35 tuned. Revisit
# against a real portrait set. A square crop was tested too and did not clearly help, so
# the crop stays rectangular, matching the reference.
_BISENET_DEFAULT_CROP_MARGIN = 0.35


class BiSeNetEngine:
    """BiSeNet (ResNet-18) face parsing (`uniface-bisenet-parsing`): a per-pixel class-id
    mask over the 19 CelebAMask-HQ facial-component classes (`FACE_PARSING_LABELS`).

    Decode ported from `uniface/parsing/bisenet.py::preprocess`/`postprocess` - BGR->RGB,
    resize to the model's OWN declared input size (read from ONNX metadata, not hardcoded),
    scale to [0,1], ImageNet mean/std normalisation, CHW, then `argmax` over the class axis
    and a nearest-neighbour resize back to the input image's own dimensions. Nearest, not
    bilinear: these are class IDs, and interpolating between class 4 (`l_eye`) and class 6
    (`eye_g`) would invent class 5 (`r_eye`) at every boundary pixel.

    Does not implement `infer()` into the `Detection` shape, same as the other non-box
    uniface engines above: a segmentation mask is not a bounding box. Call `parse()`.
    """

    def __init__(self, artifact_path: Path, label_map: dict[int, str] | None = None) -> None:
        self._session, self._providers = _onnx_session(artifact_path)
        self._input = self._session.get_inputs()[0]
        outputs = self._session.get_outputs()
        names = [o.name for o in outputs]
        # The reference takes `outputs[0]` positionally. Positional alone would be a fragile
        # contract for an artifact with three same-shaped outputs, so this additionally
        # asserts the first one is the semantically-named `output` our real gate-2 probe
        # recorded - if a future re-export reorders them, this fails loudly instead of
        # silently decoding an auxiliary training head as if it were the real class map.
        if not names or names[0] != "output":
            raise OutputContractUnknownError(
                f"Expected the first BiSeNet output to be named 'output', got {names}. The "
                "reference implementation reads outputs[0]; a reordered export would make "
                "that silently wrong. Record an output_schema on this model version first."
            )
        self._output_names = names
        # (N, C, H, W) -> the reference's own (width, height) convention for cv2.resize.
        shape = self._input.shape
        height = shape[2] if isinstance(shape[2], int) else _BISENET_FALLBACK_INPUT_SIZE
        width = shape[3] if isinstance(shape[3], int) else _BISENET_FALLBACK_INPUT_SIZE
        self._input_size = (int(width), int(height))
        self._mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        self._std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        self._labels = label_map or {}

    def info(self) -> EngineInfo:
        shape = tuple(d if isinstance(d, int) else -1 for d in self._input.shape)
        return EngineInfo(
            framework="onnx", runtime="onnxruntime", available=True, input_shape=shape,
            labels=self._labels,
            detail=(
                f"providers={','.join(self._providers)} input={self._input.name} "
                f"input_size={self._input_size[0]}x{self._input_size[1]} "
                f"classes={_BISENET_NUM_CLASSES}"
            ),
        )

    def infer(self, image: np.ndarray, *, confidence: float = 0.25) -> list[Detection]:
        raise OutputContractUnknownError(
            "uniface-bisenet-parsing produces a per-pixel 19-class segmentation mask, not "
            "detections - call parse() instead (optionally with a Detection from a paired "
            "face_detection model, to parse one face rather than the whole frame)."
        )

    def parse(
        self,
        image: np.ndarray,
        detection: Detection | None = None,
        *,
        margin: float = _BISENET_DEFAULT_CROP_MARGIN,
    ) -> np.ndarray:
        """Returns a `(H, W)` uint8 mask of class IDs in `0..18`, indexable into
        `FACE_PARSING_LABELS`.

        With no `detection`, this is the reference's own `parse(image)` exactly: the whole
        supplied image is treated as the region to parse, and the mask comes back at that
        image's own dimensions. `margin` is ignored in that case - there is no box to
        expand. Note that a full, un-cropped scene is NOT what this model is for: measured
        live on the project's own street-scene fixture, the whole frame parses to 95%
        `hat`, which is meaningless. It is a face-crop model.

        With a `detection`, the box is expanded by `margin` per side and the mask comes
        back at the CROP's dimensions. See `_BISENET_DEFAULT_CROP_MARGIN` for the real
        measurement behind the default and why `margin=0.0` - the reference's own framing -
        is a genuinely bad default against this platform's own detector. `margin=0.0`
        remains reachable for anyone who wants the reference's exact behaviour.

        One thing this engine deliberately does NOT claim: that `output` is the primary head
        because the other two are auxiliary. All three tensors are `(1, 19, H, W)`, measured
        live - shape cannot tell them apart. The grounds are that the reference reads
        `outputs[0]` and that `output` is the only one of the three with a semantic name;
        the auxiliary-head reading explains that asymmetry but is not independently
        verified here. What IS measured: the three are never equal on a real image (max
        absolute difference 1.77-3.17 across the crops tested), so they are genuinely
        distinct heads rather than a duplicated export.
        """
        import cv2

        if detection is None:
            region = image
        elif margin > 0.0:
            region = _crop_scaled_bbox(image, detection, scale=1.0 + 2.0 * margin)
        else:
            region = _crop_raw_bbox(image, detection)
        if region.ndim != 3 or region.shape[2] != 3 or region.dtype != np.uint8:
            raise OutputContractUnknownError(
                f"parse() expects an 8-bit 3-channel BGR image, got shape {region.shape} "
                f"dtype {region.dtype}."
            )
        original_size = (region.shape[1], region.shape[0])  # (width, height)

        rgb = cv2.cvtColor(region, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(rgb, self._input_size, interpolation=cv2.INTER_LINEAR)
        normalized = (resized.astype(np.float32) / 255.0 - self._mean) / self._std
        tensor = np.expand_dims(normalized.transpose(2, 0, 1), axis=0).astype(np.float32)

        outputs = self._session.run(self._output_names, {self._input.name: tensor})
        predicted = outputs[0].squeeze(0).argmax(0).astype(np.uint8)
        return cv2.resize(predicted, original_size, interpolation=cv2.INTER_NEAREST)


# `uniface/attribute/faceattribnet.py::postprocess`'s own unpack order, verbatim. This is
# the single fact CHECKLIST.md previously recorded as unrecoverable ("5 of *what*, in *what
# order*, is not recoverable from the graph") - it is recoverable, just not from the graph.
FACE_ATTRIBUTE_LABELS: tuple[str, ...] = (
    "left_eye_open", "right_eye_open", "eyeglasses", "mask", "sunglasses",
)
_FACEATTRIB_FALLBACK_INPUT_SIZE = 128


@dataclass(frozen=True)
class FaceStateResult:
    """Five INDEPENDENT binary-classifier probabilities in [0, 1] - deliberately not a
    single `label`/`confidence` pair like `LivenessResult`, because these heads do not
    compete: they do not sum to 1 and several can be high at once. Threshold each
    separately. Named after the reference's own `uniface.types.FaceStateResult`.
    """

    left_eye_open: float
    right_eye_open: float
    eyeglasses: float
    mask: float
    sunglasses: float

    def as_dict(self) -> dict[str, float]:
        return {name: float(getattr(self, name)) for name in FACE_ATTRIBUTE_LABELS}


def _faceattrib_letterbox(image_bgr: np.ndarray, target_size: int) -> np.ndarray:
    """Ported from `uniface.common.letterbox_resize(image, target_size, fill_value=0)`,
    which is what `FaceAttribNet.preprocess` calls: aspect-preserving resize onto a centred
    square canvas of zeros, BGR->RGB, scaled to [0, 1], NCHW float32.

    Zero padding, not the 114 grey `letterbox_resize` defaults to - `preprocess` passes
    `fill_value=0` explicitly. Scaling to [0,1] is the ONLY normalisation: FaceAttribNet's
    mean/std normalisation is baked into the ONNX graph itself (the reference's own class
    docstring says so), so applying ImageNet mean/std here the way BiSeNet above does would
    normalise the input twice.
    """
    import cv2

    if image_bgr.ndim != 3 or image_bgr.shape[2] != 3 or image_bgr.dtype != np.uint8:
        raise OutputContractUnknownError(
            f"FaceAttribNet expects an 8-bit 3-channel BGR crop, got shape "
            f"{image_bgr.shape} dtype {image_bgr.dtype}."
        )
    img_h, img_w = image_bgr.shape[:2]
    scale = min(target_size / img_h, target_size / img_w)
    new_h, new_w = int(img_h * scale), int(img_w * scale)
    resized = cv2.resize(image_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

    canvas = np.full((target_size, target_size, 3), 0, dtype=np.uint8)
    pad_h = (target_size - new_h) // 2
    pad_w = (target_size - new_w) // 2
    canvas[pad_h : pad_h + new_h, pad_w : pad_w + new_w] = resized

    rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    return np.ascontiguousarray(np.expand_dims(rgb.transpose(2, 0, 1), axis=0))


class FaceAttribNetEngine:
    """FaceAttribNet (Qualcomm) 5 binary face attributes (`uniface-faceattribnet-
    attributes`): left/right eye openness, eyeglasses, face mask, sunglasses.

    Decode ported from `uniface/attribute/faceattribnet.py`. The column order
    (`FACE_ATTRIBUTE_LABELS`) comes from that file's `postprocess()`, which is the only
    place it is written down - the graph names the tensor `probability` and nothing more.

    No softmax, deliberately: these are five independent binary heads, per the reference's
    own class docstring. Applying one would be the same class of plausible-but-silently-
    wrong decode `OutputContractUnknownError` exists to prevent, and would look fine -
    five numbers in [0,1] - while being meaningless.

    Needs a face bbox, matching the reference's `predict(image, face)`/`face.bbox`. Takes a
    full frame plus a `Detection` from the platform's own already-`production` face detector
    (`insightface-buffalo-l-detect`), the same two-stage shape as every other engine here;
    unlike the alignment-based ones it needs only `bbox`, no keypoints.
    """

    # The reference's own default `margin=0.0` - no crop expansion. Unlike BiSeNet above,
    # this default was measured to work as-is on this platform's own detector boxes (real
    # 36x52 / 40x50 px SCRFD crops of two sunglass-wearing faces both scored `sunglasses`
    # 0.98-1.00 and `mask` <= 0.02), so there is no reason to depart from the reference here.
    _MARGIN = 0.0

    def __init__(self, artifact_path: Path, label_map: dict[int, str] | None = None) -> None:
        self._session, self._providers = _onnx_session(artifact_path)
        self._input = self._session.get_inputs()[0]
        outputs = self._session.get_outputs()
        if len(outputs) != 1 or list(outputs[0].shape)[-1] != len(FACE_ATTRIBUTE_LABELS):
            raise OutputContractUnknownError(
                f"Expected a single (batch, {len(FACE_ATTRIBUTE_LABELS)}) output, got "
                f"{[(o.name, list(o.shape)) for o in outputs]}. The column meanings ported "
                "here are only valid for that exact layout."
            )
        shape = self._input.shape
        height = shape[2] if isinstance(shape[2], int) else _FACEATTRIB_FALLBACK_INPUT_SIZE
        width = shape[3] if isinstance(shape[3], int) else _FACEATTRIB_FALLBACK_INPUT_SIZE
        if int(height) != int(width):
            # The reference refuses a non-square input_size for exactly this reason: its
            # preprocessing letterboxes onto a SQUARE canvas, so a non-square request
            # cannot be honoured.
            raise OutputContractUnknownError(
                f"FaceAttribNet preprocessing letterboxes onto a square canvas, but this "
                f"artifact declares a {width}x{height} input."
            )
        self._input_size = int(height)
        self._labels = label_map or {}

    def info(self) -> EngineInfo:
        shape = tuple(d if isinstance(d, int) else -1 for d in self._input.shape)
        return EngineInfo(
            framework="onnx", runtime="onnxruntime", available=True, input_shape=shape,
            labels=self._labels,
            detail=(
                f"providers={','.join(self._providers)} input={self._input.name} "
                f"attributes={','.join(FACE_ATTRIBUTE_LABELS)}"
            ),
        )

    def infer(self, image: np.ndarray, *, confidence: float = 0.25) -> list[Detection]:
        raise OutputContractUnknownError(
            "uniface-faceattribnet-attributes produces 5 independent binary attribute "
            "probabilities, not detections - call predict_face_state() with a Detection "
            "from a paired face_detection model instead."
        )

    def predict_face_state(
        self, image: np.ndarray, detection: Detection, *, margin: float | None = None
    ) -> FaceStateResult:
        """Five independent attribute probabilities for one already-detected face.

        Named `predict_face_state` rather than `predict_attributes` on purpose, even though
        both this and `FairFaceEngine` carry task_code="face_attribute": the two return
        entirely different things (5 independent binary heads here; competing race/gender/
        age distributions there), and reusing one name for two incompatible return types
        would make a call site's correctness depend on which model happened to be loaded.
        `face state` is the reference's own word for this model's output
        (`uniface.types.FaceStateResult`, `BaseAttribute.predict`).

        `margin` expands the crop by that fraction of the box per side, matching the
        reference's own `margin` constructor argument; `None` uses `_MARGIN` (0.0, the
        reference's default), which crops the raw box exactly as `FaceAttribNet.preprocess`
        does.
        """
        effective_margin = self._MARGIN if margin is None else margin
        crop = (
            _crop_scaled_bbox(image, detection, scale=1.0 + 2.0 * effective_margin)
            if effective_margin > 0.0
            else _crop_raw_bbox(image, detection)
        )
        blob = _faceattrib_letterbox(crop, self._input_size)
        raw = self._session.run(None, {self._input.name: blob})[0]
        left_eye_open, right_eye_open, eyeglasses, mask, sunglasses = (
            np.squeeze(raw).astype(float).tolist()
        )
        return FaceStateResult(
            left_eye_open=left_eye_open,
            right_eye_open=right_eye_open,
            eyeglasses=eyeglasses,
            mask=mask,
            sunglasses=sunglasses,
        )


# --- TFLite (.tflite) ---------------------------------------------------------------

class TfliteEngine:
    """The kitchen-safety model is the estate's only TFLite artifact."""

    def __init__(self, artifact_path: Path, label_map: dict[int, str] | None = None) -> None:
        interpreter_cls = self._resolve_interpreter()
        self._interpreter = interpreter_cls(model_path=str(artifact_path))
        self._interpreter.allocate_tensors()
        self._input_detail = self._interpreter.get_input_details()[0]
        self._output_details = self._interpreter.get_output_details()
        self._labels = label_map or {}

    @staticmethod
    def _resolve_interpreter():
        """Finds a TFLite interpreter from whichever package this image ships.

        Three providers, newest first: ai-edge-litert is Google's current package and the
        only one publishing wheels for Python 3.12+; tflite_runtime is the older slim
        build; full TensorFlow also exposes an interpreter. Accepting all three keeps the
        edge image slim without forcing the cloud image to pin the same one.
        """
        try:
            from ai_edge_litert.interpreter import Interpreter

            return Interpreter
        except ImportError:
            pass
        try:
            from tflite_runtime.interpreter import Interpreter

            return Interpreter
        except ImportError:
            pass
        try:
            import tensorflow as tf

            return tf.lite.Interpreter
        except ImportError as exc:  # pragma: no cover
            raise EngineUnavailableError(
                "No TFLite interpreter available (tried ai_edge_litert, tflite_runtime, "
                "tensorflow); cannot load a .tflite artifact"
            ) from exc

    def info(self) -> EngineInfo:
        return EngineInfo(
            framework="tflite",
            runtime="tflite",
            available=True,
            input_shape=tuple(int(d) for d in self._input_detail["shape"]),
            labels=self._labels,
            detail=f"outputs={len(self._output_details)}",
        )

    def infer(self, image: np.ndarray, *, confidence: float = 0.25) -> list[Detection]:
        import cv2

        _, height, width, _ = self._input_detail["shape"]
        resized = cv2.resize(image, (int(width), int(height)))
        tensor = np.expand_dims(resized.astype(np.float32) / 255.0, axis=0)

        self._interpreter.set_tensor(self._input_detail["index"], tensor)
        self._interpreter.invoke()
        outputs = [self._interpreter.get_tensor(d["index"]) for d in self._output_details]

        # The kitchen-safety model emits a raw YOLOv8 head, (1, 4+nc, anchors).
        for output in outputs:
            raw = decode_raw_yolo(
                output,
                confidence=confidence,
                labels=self._labels,
                input_size=(int(height), int(width)),
            )
            if raw is not None:
                return raw

        raise OutputContractUnknownError(
            f"No decoder for TFLite output shapes {[o.shape for o in outputs]}. Record an "
            "output_schema on this model version before the runtime can interpret it."
        )


ENGINES_BY_RUNTIME: dict[str, type] = {
    "ultralytics": UltralyticsEngine,
    "onnxruntime": OnnxEngine,
    "tflite": TfliteEngine,
}

# The two legacy InsightFace artifacts this runtime can decode carry runtime="onnxruntime",
# same as the plate detector/OCR pair and the uniface-zoo models above - dispatched by
# exact model_name, not by task_code. task_code alone used to be a safe dispatch key when
# these were the *only* two face_detection/face_recognition artifacts in the registry, but
# the 15 uniface-zoo models (2026-09-03) added more of both task_codes on entirely
# different architectures (confirmed against the real manifests: `uniface-mobileface-
# recognition`/`uniface-sphereface-recognition`/`uniface-adaface-recognition`/`uniface-
# edgeface-recognition` are all task_code="face_recognition" too) that InsightFaceEngine's
# `insightface.model_zoo.get_model()` was never built to decode. Keying off model_name
# instead means only these exact two legacy artifacts - both real, currently-`production`/
# revocable-independently rows in the registry - ever reach this class.
_INSIGHTFACE_MODEL_NAMES = ("insightface-buffalo-l-detect", "insightface-buffalo-l-recognition")

# The 4 uniface-zoo models that needed a public-repo cross-check before decoding
# (fairface/minifasnet/mobilegaze/pipnet - see CHECKLIST.md's "4 need a public-repo
# cross-check" entry), plus the 5 resolved after them, 2026-09-16: blazeface/centerface/
# retinaface (the 3 anchor-based detectors), and bisenet/faceattribnet (previously the 2
# "no-guess" models - the same public reference resolves both outright, see each engine's
# own class docstring for what changed). Dispatched by model_name for the same reason as
# everything else in this function: task_code alone is ambiguous or shared for at least
# some of these (`face_attribute` is shared between `uniface-fairface-attributes` and
# `uniface-faceattribnet-attributes`, two models whose "attributes" are entirely different
# things; `face_landmark` is shared with `uniface-facemesh-landmark`, a different model
# with a different output shape, decoded separately above; `face_parsing` is unique to
# bisenet, but dispatched by model_name anyway, for the same reason as everything else in
# this table).
#
# The 3 face detectors are the one group here that DOES produce a `Detection` list from
# `infer()`, so unlike every other entry they are reachable through the ordinary
# `/internal/v1/infer` and `/internal/v1/validate-infer` paths with no special endpoint.
# All 3 carry task_code="face_detection" - the exact task_code `insightface-buffalo-l-
# detect` already owns, on an unrelated architecture (SCRFD vs BlazeFace/CenterFace/
# RetinaFace). Dispatch by model_name keeps them apart: `build_engine` checks
# `_INSIGHTFACE_MODEL_NAMES` first, by exact name, so the two legacy InsightFace artifacts
# still reach `InsightFaceEngine` and these 3 never do. Nothing in these engines branches on
# task_code, deliberately - the face_recognition collision this file already found and
# closed is the same bug one task_code branch away.
_UNIFACE_ENGINES_BY_MODEL_NAME: dict[str, type] = {
    "uniface-fairface-attributes": FairFaceEngine,
    "uniface-minifasnet-antispoofing": MiniFasNetEngine,
    "uniface-mobilegaze-estimation": MobileGazeEngine,
    "uniface-pipnet-landmark": PipNetEngine,
    "uniface-blazeface-detect": BlazeFaceEngine,
    "uniface-centerface-detect": CenterFaceEngine,
    "uniface-retinaface-detect": RetinaFaceEngine,
    "uniface-bisenet-parsing": BiSeNetEngine,
    "uniface-faceattribnet-attributes": FaceAttribNetEngine,
}


def build_engine(
    runtime: str,
    artifact_path: Path,
    label_map: dict[int, str] | None = None,
    task_code: str | None = None,
    model_name: str | None = None,
):
    if model_name in _INSIGHTFACE_MODEL_NAMES:
        return InsightFaceEngine(artifact_path, label_map, task_code)
    if model_name in _UNIFACE_EMBEDDING_FAMILIES:
        return UnifaceEmbeddingEngine(artifact_path, label_map, model_name)
    if model_name == "uniface-facemesh-landmark":
        return UnifaceFaceMeshEngine(artifact_path, label_map)
    if model_name == "uniface-modnet-matting":
        return UnifaceMattingEngine(artifact_path, label_map)
    uniface_engine_cls = _UNIFACE_ENGINES_BY_MODEL_NAME.get(model_name)
    if uniface_engine_cls is not None:
        return uniface_engine_cls(artifact_path, label_map)
    engine_cls = ENGINES_BY_RUNTIME.get(runtime)
    if engine_cls is None:
        raise EngineUnavailableError(f"No engine registered for runtime '{runtime}'")
    return engine_cls(artifact_path, label_map)
