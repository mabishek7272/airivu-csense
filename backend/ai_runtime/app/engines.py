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

import logging
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

        **No decode into actual plate text is implemented here.** The exact index-to-
        character mapping is not verified: every real plate crop from this camera's
        640x480 source tried during this work was too low-resolution for either a human
        or the model to confidently read (per-position confidence 10-40% except a run of
        trailing blank/pad positions), so there was no ground truth to check a charset
        guess against. Shipping a guessed mapping would be exactly the failure mode
        `OutputContractUnknownError` above exists to avoid elsewhere in this file - a
        wrong guess here produces a plausible-looking plate number that is silently
        wrong, not an error. Decoding this into text needs either the model's original
        training config (the charset order) or a clearer reference image with a known
        answer to validate against - tracked in CHECKLIST.md, not guessed at here.
        """
        return self._session.run(None, {self._input.name: self._preprocess(image)})


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

# The two InsightFace artifacts this runtime can decode carry runtime="onnxruntime", same
# as the plate detector/OCR pair - dispatched by task_code instead of a new runtime value,
# so every other onnxruntime model is untouched by this.
_INSIGHTFACE_TASK_CODES = ("face_detection", "face_recognition")


def build_engine(
    runtime: str,
    artifact_path: Path,
    label_map: dict[int, str] | None = None,
    task_code: str | None = None,
):
    if task_code in _INSIGHTFACE_TASK_CODES:
        return InsightFaceEngine(artifact_path, label_map, task_code)
    engine_cls = ENGINES_BY_RUNTIME.get(runtime)
    if engine_cls is None:
        raise EngineUnavailableError(f"No engine registered for runtime '{runtime}'")
    return engine_cls(artifact_path, label_map)
