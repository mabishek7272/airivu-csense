"""AI Runtime service (TRD §5: "Frame ingest, preprocess, infer, filter, track, rules").

This is the inference half of the AI platform: it reads deployable model versions from the
registry, fetches their artifacts from object storage with digest verification, keeps them
resident, and runs frames through them.

Scope note: this service is deliberately *internal*. It is not exposed through Traefik and
has no tenant-facing route, because it takes raw frames and returns raw detections with no
tenant scoping of its own. Tenant authorisation, ROI/rule evaluation, correlation, and
incident creation all belong to the pipeline layer that calls this (TRD §16). Wiring it to
a public edge before that layer exists would be the kind of shortcut that quietly bypasses
the tenant boundary.
"""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, UploadFile
from pydantic import BaseModel

from app.engines import (
    FACE_PARSING_LABELS,
    EngineUnavailableError,
    OutputContractUnknownError,
    PlateOcrResult,
)
from app.loader import ArtifactVerificationError, ModelArtifactCache
from app.pool import ModelPool
from app.registry import get_by_version_id, get_deployable_by_name, get_state_by_name, list_deployable
from csense_shared.config import get_settings
from csense_shared.db.postgres import create_engine, create_session_factory, platform_session
from csense_shared.errors import ApiError, api_error_handler, unhandled_exception_handler
from csense_shared.logging import configure_logging, get_logger
from csense_shared.middleware import CorrelationIdMiddleware
from csense_shared.storage.objects import create_client

settings = get_settings()
configure_logging("ai-runtime", settings.environment, settings.log_level)
logger = get_logger(__name__)

MAX_FRAME_BYTES = 12 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.engine = create_engine(settings)
    app.state.session_factory = create_session_factory(app.state.engine)
    app.state.minio = create_client(settings)
    app.state.cache = ModelArtifactCache(app.state.minio, settings.model_cache_dir)
    app.state.pool = ModelPool(app.state.cache, max_resident=settings.model_pool_size)
    logger.info("ai_runtime_started", extra={"cache_dir": settings.model_cache_dir})
    try:
        yield
    finally:
        await app.state.engine.dispose()
        logger.info("ai_runtime_stopped")


app = FastAPI(
    title="AIRIVU CSense - AI Runtime",
    version="0.1.0",
    lifespan=lifespan,
    openapi_url="/internal/v1/openapi.json",
    docs_url="/internal/v1/docs",
)
app.add_middleware(CorrelationIdMiddleware)
app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)



@asynccontextmanager
async def _registry_session():
    """The runtime reads platform-global registry tables, which carry no tenant_id."""
    async with platform_session(app.state.session_factory) as session:
        yield session


# --- Response models ----------------------------------------------------------------

class ModelSummary(BaseModel):
    model_name: str
    task_code: str
    version_label: str
    state: str
    access_classification: str
    framework: str
    runtime: str
    artifact_sha256: str
    size_bytes: int
    license: str | None
    resident: bool


class DetectionOut(BaseModel):
    class_id: int
    class_name: str
    confidence: float
    bbox: list[float]
    keypoints: list[list[float]] | None = None


class InferenceResponse(BaseModel):
    model_name: str
    version_id: str
    artifact_sha256: str
    detection_count: int
    detections: list[DetectionOut]
    inference_ms: float
    model_load_ms: float | None = None
    frame_size: list[int]


class FaceEmbeddingOut(BaseModel):
    """One face's embedding from a uniface-zoo recognition model
    (`_UNIFACE_VALIDATION_MODELS[*] == "embedding"`). Carries the full 512-d vector, not
    just a summary - the validation script needs the real numbers to compute cosine
    similarity across different golden images, the same way a real caller eventually
    would. This is a biometric template; like every other biometric output in this
    runtime, it is returned to the caller and never persisted, matched, or exposed
    through any tenant-facing API."""

    detector_confidence: float
    bbox: list[float]
    embedding_dim: int
    embedding_l2_norm: float
    embedding: list[float]


class FaceLandmarkOut(BaseModel):
    """One face's dense mesh from `uniface-facemesh-landmark`."""

    detector_confidence: float
    bbox: list[float]
    landmark_score: float
    landmarks: list[list[float]]


class MatteSummary(BaseModel):
    """Summary statistics for one `uniface-modnet-matting` alpha matte - the full (H, W)
    float array is not serialised into JSON; `mean_alpha`/`coverage_fraction` are enough
    to sanity-check the matte is neither all-zero nor all-one and roughly tracks a
    portrait-sized foreground region."""

    mean_alpha: float
    coverage_fraction: float  # fraction of pixels with alpha > 0.5
    shape: list[int]  # [height, width]


class FaceAttributesOut(BaseModel):
    """One face's race/gender/age scores from `uniface-fairface-attributes`
    (`engines.py`'s `FaceAttributes`). `*_scores` carry the full per-class distribution,
    not just the top-1 label, so a caller can judge how confident the pick really was -
    the same reasoning `stock_streetscene_3adults.jpg`'s own golden manifest already
    applies when it records race as "never scored", not just "correct"."""

    detector_confidence: float
    bbox: list[float]
    race: str
    race_confidence: float
    race_scores: dict[str, float]
    gender: str
    gender_confidence: float
    gender_scores: dict[str, float]
    age_bucket: str
    age_confidence: float
    age_scores: dict[str, float]


class LivenessOut(BaseModel):
    """One face's real/spoof result from `uniface-minifasnet-antispoofing`
    (`engines.py`'s `LivenessResult`). `scores` is the raw 3-class softmax
    (index 1 = real, matching `MiniFasNetEngine`'s own documented convention) -
    kept in full rather than collapsed, since `is_real`/`label` already give the
    collapsed real/fake decision."""

    detector_confidence: float
    bbox: list[float]
    is_real: bool
    label: str
    confidence: float
    scores: list[float]


class GazeOut(BaseModel):
    """One face's gaze direction from `uniface-mobilegaze-estimation`
    (`engines.py`'s `GazeEstimate`)."""

    detector_confidence: float
    bbox: list[float]
    yaw_deg: float
    pitch_deg: float


class Landmark98Out(BaseModel):
    """One face's 98 WFLW landmark points from `uniface-pipnet-landmark`
    (`engines.py`'s `LandmarkResult`) - a separate shape from `FaceLandmarkOut`
    (facemesh's 468 3D points) since the two models' outputs are not interchangeable."""

    detector_confidence: float
    bbox: list[float]
    points: list[list[float]]  # 98 x (x_norm, y_norm, confidence)


class FaceParsingOut(BaseModel):
    """One face's 19-class parsing mask summary from `uniface-bisenet-parsing`
    (`engines.py`'s `BiSeNetEngine.parse`).

    The mask itself is `crop_size[0] * crop_size[1]` uint8 class IDs - far too large to
    return inline as JSON, and not useful to a validation harness in raw form anyway. What
    is returned is the per-class pixel FRACTION of the parsed crop, which is exactly what
    a plausibility check needs: a real face crop should come back mostly `skin`/`hair`
    with small, non-zero `l_eye`/`r_eye`/`nose`/`u_lip`/`l_lip` regions, and a mask that
    collapsed to a single class (the classic silent-wrong-decode signature) is immediately
    visible as one class at ~1.0."""

    detector_confidence: float
    bbox: list[float]
    crop_size: list[int]
    class_fractions: dict[str, float]  # only classes actually present, label -> fraction
    distinct_classes: int


class FaceStateOut(BaseModel):
    """One face's 5 independent binary attributes from
    `uniface-faceattribnet-attributes` (`engines.py`'s `FaceStateResult`).

    Flat probabilities with no top-1 `label`/`confidence` pair, unlike `LivenessOut`
    above, because these five heads do not compete - they do not sum to 1 and several can
    be high at once. Collapsing them to a single winner would misrepresent the model."""

    detector_confidence: float
    bbox: list[float]
    left_eye_open: float
    right_eye_open: float
    eyeglasses: float
    mask: float
    sunglasses: float


class PlateOcrOut(BaseModel):
    """Response for `/internal/v1/infer-plate-ocr`. Two independent trust signals, not
    one collapsed score - see `PlateOcrResult`'s own docstring (`engines.py`) for why:
    `low_confidence` is the model's own uncertainty and improves with a better crop;
    `charset_verified` is always `false` because the index-to-character mapping itself
    has never been checked against a real readable plate on this artifact. A caller (or
    a UI) that shows `text` MUST show both flags next to it, not just one - a
    high-confidence result can still be the wrong characters if the assumed charset
    ordering is wrong, which `low_confidence` alone would not catch."""

    model_name: str
    text: str
    char_confidences: list[float]
    confidence: float
    low_confidence: bool
    charset_verified: bool
    inference_ms: float


class UnifaceValidationResponse(BaseModel):
    """Response for `/internal/v1/validate-infer-uniface` - a separate response shape
    from `InferenceResponse` because none of these 12 models' outputs are a `Detection`
    list (see `engines.py`'s own engine docstrings for why `infer()` deliberately raises
    for all of them). Exactly one of `embeddings`/`landmarks`/`matte`/`attributes`/
    `liveness`/`gaze`/`landmarks98`/`parsing`/`face_state` is populated, matching which of
    the 12 models `version_id` names."""

    model_name: str
    version_id: str
    task_code: str
    frame_size: list[int]
    inference_ms: float
    face_count: int | None = None
    embeddings: list[FaceEmbeddingOut] | None = None
    landmarks: list[FaceLandmarkOut] | None = None
    matte: MatteSummary | None = None
    attributes: list[FaceAttributesOut] | None = None
    liveness: list[LivenessOut] | None = None
    gaze: list[GazeOut] | None = None
    landmarks98: list[Landmark98Out] | None = None
    parsing: list[FaceParsingOut] | None = None
    face_state: list[FaceStateOut] | None = None


# --- Endpoints -----------------------------------------------------------------------

@app.get("/healthz", tags=["health"])
async def liveness() -> dict:
    return {"status": "ok", "service": "ai-runtime"}


@app.get("/readyz", tags=["health"])
async def readiness() -> dict:
    checks: dict[str, str] = {}
    try:
        async with _registry_session() as session:
            await list_deployable(session)
        checks["registry"] = "ok"
    except Exception:
        checks["registry"] = "unavailable"

    try:
        app.state.minio.bucket_exists("csense-models")
        checks["object_storage"] = "ok"
    except Exception:
        checks["object_storage"] = "unavailable"

    status = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": status, "dependencies": checks, "resident_models": len(app.state.pool.resident())}


@app.get("/internal/v1/engines", tags=["runtime"])
async def available_engines() -> dict:
    """Which frameworks this image can actually run.

    Reported rather than assumed, so a deployment that ships without PyTorch (a likely
    edge profile) is visible instead of failing on first use of a .pt artifact.
    """
    engines: dict[str, dict] = {}
    for name, module, attr in (
        ("ultralytics", "ultralytics", "YOLO"),
        ("onnxruntime", "onnxruntime", "InferenceSession"),
        ("tflite", "ai_edge_litert.interpreter", "Interpreter"),
    ):
        try:
            imported = __import__(module, fromlist=[attr])
            version = getattr(imported, "__version__", None)
            engines[name] = {"available": True, "version": version}
        except ImportError as exc:
            engines[name] = {"available": False, "detail": str(exc)}
    return {"engines": engines}


@app.get("/internal/v1/models", response_model=list[ModelSummary], tags=["runtime"])
async def list_models() -> list[ModelSummary]:
    async with _registry_session() as session:
        models = await list_deployable(session)
    pool: ModelPool = app.state.pool
    return [
        ModelSummary(
            model_name=m.model_name,
            task_code=m.task_code,
            version_label=m.version_label,
            state=m.state,
            access_classification=m.access_classification,
            framework=m.framework,
            runtime=m.runtime,
            artifact_sha256=m.artifact_sha256,
            size_bytes=m.size_bytes,
            license=m.license_name,
            resident=pool.is_resident(m.artifact_sha256),
        )
        for m in models
    ]


@app.post("/internal/v1/models/{model_name}/load", tags=["runtime"])
async def load_model(model_name: str) -> dict:
    """Warms a model into the pool. Useful before a demo or benchmark so the first real
    frame does not pay the load cost."""
    registered = await _resolve_or_raise(model_name)
    pool: ModelPool = app.state.pool
    loaded = await _load_in_threadpool(pool, registered)
    return {
        "model_name": loaded.model_name,
        "artifact_sha256": loaded.artifact_sha256,
        "load_seconds": round(loaded.load_seconds, 3),
        "engine": {
            "framework": loaded.info.framework,
            "runtime": loaded.info.runtime,
            "input_shape": loaded.info.input_shape,
            "detail": loaded.info.detail,
            "label_count": len(loaded.info.labels),
        },
    }


def _decode_frame(payload: bytes) -> np.ndarray:
    if not payload:
        raise ApiError(status_code=400, code="empty_frame", message="No frame data received.")
    if len(payload) > MAX_FRAME_BYTES:
        raise ApiError(
            status_code=413,
            code="frame_too_large",
            message=f"Frame exceeds the {MAX_FRAME_BYTES // (1024 * 1024)} MB limit.",
        )
    image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ApiError(
            status_code=400,
            code="undecodable_frame",
            message="Frame could not be decoded as an image.",
        )
    return image


async def _run_inference(registered, image: np.ndarray, confidence: float) -> InferenceResponse:
    pool: ModelPool = app.state.pool
    was_resident = pool.is_resident(registered.artifact_sha256)
    loaded = await _load_in_threadpool(pool, registered)

    started = time.monotonic()
    try:
        detections = await _infer_in_threadpool(loaded, image, confidence)
    except OutputContractUnknownError as exc:
        raise ApiError(
            status_code=501,
            code="output_contract_unknown",
            message=str(exc),
            details={"model": registered.model_name, "runtime": registered.runtime},
        ) from exc
    inference_ms = (time.monotonic() - started) * 1000

    return InferenceResponse(
        model_name=loaded.model_name,
        version_id=loaded.version_id,
        artifact_sha256=loaded.artifact_sha256,
        detection_count=len(detections),
        detections=[
            DetectionOut(
                class_id=d.class_id,
                class_name=d.class_name,
                confidence=round(d.confidence, 4),
                bbox=[round(v, 5) for v in d.bbox],
                keypoints=[[round(v, 5) for v in kp] for kp in d.keypoints] if d.keypoints else None,
            )
            for d in detections
        ],
        inference_ms=round(inference_ms, 2),
        model_load_ms=None if was_resident else round(loaded.load_seconds * 1000, 2),
        frame_size=[image.shape[1], image.shape[0]],
    )


@app.post("/internal/v1/infer", response_model=InferenceResponse, tags=["runtime"])
async def infer(
    model_name: str = Form(...),
    confidence: float = Form(0.25),
    frame: UploadFile = File(...),
) -> InferenceResponse:
    image = _decode_frame(await frame.read())
    registered = await _resolve_or_raise(model_name)
    return await _run_inference(registered, image, confidence)


@app.post("/internal/v1/validate-infer", response_model=InferenceResponse, tags=["runtime"])
async def validate_infer(
    version_id: uuid.UUID = Form(...),
    confidence: float = Form(0.25),
    frame: UploadFile = File(...),
) -> InferenceResponse:
    """Runs one frame through an exact model version, addressed by id - not by name, and
    not restricted to DEPLOYABLE_STATES the way `/internal/v1/infer` is.

    Exists for TRD gate 2-4 (load/shape compatibility, golden dataset functional tests):
    that evidence has to be producible *before* a version is promoted, but the by-name
    route above can only ever reach an already-deployable version - a real chicken-and-egg
    problem for a genuinely new upload. This route is the deliberate, narrowly-scoped way
    out of it: the caller must already know the exact version_id (from the Admin API's own
    model-version listing, itself permission-gated), so it grants no broader access than
    "if you can already see this version exists, you can also test-run it." See
    VALIDATABLE_STATES in registry.py for exactly which states that still excludes
    (revoked/deprecated, same as the by-name path never allowed).
    """
    image = _decode_frame(await frame.read())
    async with _registry_session() as session:
        registered = await get_by_version_id(session, version_id)
    if registered is None:
        raise ApiError(
            status_code=404,
            code="model_version_not_found",
            message=f"No loadable version '{version_id}' (may be revoked/deprecated, or not exist).",
        )
    return await _run_inference(registered, image, confidence)


# The 12 uniface-zoo models `/internal/v1/validate-infer-uniface` below knows how to run:
# the 6 low-risk models (`UnifaceEmbeddingEngine`/`UnifaceFaceMeshEngine`/
# `UnifaceMattingEngine`) plus the 4 cross-check models (`FairFaceEngine`/
# `MiniFasNetEngine`/`MobileGazeEngine`/`PipNetEngine`) plus the 2 resolved against the
# same public reference on 2026-09-16 (`BiSeNetEngine`/`FaceAttribNetEngine`) - see
# engines.py's own dispatch tables. Kept here rather than imported from engines.py's private dicts so this
# endpoint's supported-model list is visible and grep-able in one place, independent of
# engines.py's internal dispatch structure. The 4 cross-check models had no live HTTP path
# at all until this addition - they were validated by importing engines.py directly into a
# local venv (CHECKLIST.md's own "known gap, not a bug" note), because the container
# holding this endpoint hadn't been rebuilt yet when they were first decoded; closing that
# gap now that a rebuild has actually happened.
_UNIFACE_VALIDATION_MODELS: dict[str, str] = {
    "uniface-adaface-recognition": "embedding",
    "uniface-edgeface-recognition": "embedding",
    "uniface-mobileface-recognition": "embedding",
    "uniface-sphereface-recognition": "embedding",
    "uniface-facemesh-landmark": "landmarks",
    "uniface-modnet-matting": "matte",
    "uniface-fairface-attributes": "attributes",
    "uniface-minifasnet-antispoofing": "liveness",
    "uniface-mobilegaze-estimation": "gaze",
    "uniface-pipnet-landmark": "landmarks98",
    "uniface-bisenet-parsing": "parsing",
    "uniface-faceattribnet-attributes": "face_state",
}
# The platform's own already-verified face detector (production state, confirmed live
# this session) - reused rather than building a second face cropper, per this session's
# own instructions and CLAUDE.md's existing discipline of not re-deriving alignment maths
# that already have a working production implementation.
_FACE_DETECTOR_MODEL_NAME = "insightface-buffalo-l-detect"


@app.post(
    "/internal/v1/validate-infer-uniface",
    response_model=UnifaceValidationResponse,
    tags=["runtime"],
)
async def validate_infer_uniface(
    version_id: uuid.UUID = Form(...),
    confidence: float = Form(0.25),
    frame: UploadFile = File(...),
) -> UnifaceValidationResponse:
    """Gate 2-4 validation harness for the 12 uniface-zoo models with a non-`Detection`
    output (`_UNIFACE_VALIDATION_MODELS` above) - the sibling of `/internal/v1/
    validate-infer` for models whose output is a face embedding, a dense landmark mesh, a
    portrait alpha matte, an attribute/liveness score, a gaze angle, a per-pixel parsing
    mask, or a set of independent binary attribute probabilities, none of which fit
    `InferenceResponse`/`_run_inference`. Same version_id-addressed, VALIDATABLE_STATES
    scoping as `/internal/v1/validate-infer` - see that endpoint's own docstring for why.

    Every kind except `matte` needs a detected face first: this runs the platform's own
    production face detector (`_FACE_DETECTOR_MODEL_NAME`) over the frame and then decodes
    the target model once per detected face. `uniface-modnet-matting` needs no detector
    and runs directly on the full frame.
    """
    image = _decode_frame(await frame.read())
    async with _registry_session() as session:
        registered = await get_by_version_id(session, version_id)
    if registered is None:
        raise ApiError(
            status_code=404,
            code="model_version_not_found",
            message=f"No loadable version '{version_id}' (may be revoked/deprecated, or not exist).",
        )

    kind = _UNIFACE_VALIDATION_MODELS.get(registered.model_name)
    if kind is None:
        raise ApiError(
            status_code=422,
            code="not_a_uniface_validation_model",
            message=(
                f"'{registered.model_name}' is not one of the 12 models this endpoint "
                f"validates: {sorted(_UNIFACE_VALIDATION_MODELS)}. Use /internal/v1/"
                "validate-infer for a model whose output is a Detection list."
            ),
        )

    pool: ModelPool = app.state.pool
    loaded = await _load_in_threadpool(pool, registered)

    detector_loaded = None
    if kind != "matte":
        async with _registry_session() as session:
            detector_registered = await get_deployable_by_name(session, _FACE_DETECTOR_MODEL_NAME)
        if detector_registered is None:
            raise ApiError(
                status_code=503,
                code="face_detector_unavailable",
                message=(
                    f"'{_FACE_DETECTOR_MODEL_NAME}' has no deployable version right now - "
                    f"'{registered.model_name}' needs a detected face to run against."
                ),
            )
        detector_loaded = await _load_in_threadpool(pool, detector_registered)

    started = time.monotonic()
    try:
        result = await _run_uniface_inference(loaded, detector_loaded, kind, image, confidence)
    except OutputContractUnknownError as exc:
        raise ApiError(
            status_code=501,
            code="output_contract_unknown",
            message=str(exc),
            details={"model": registered.model_name, "runtime": registered.runtime},
        ) from exc
    inference_ms = (time.monotonic() - started) * 1000

    return UnifaceValidationResponse(
        model_name=loaded.model_name,
        version_id=loaded.version_id,
        task_code=registered.task_code,
        frame_size=[image.shape[1], image.shape[0]],
        inference_ms=round(inference_ms, 2),
        **result,
    )


# Models approved for direct production access via /internal/v1/infer-uniface, per an
# explicit product decision (2026-09-19) - a deliberately narrow subset of the 12 models
# `_UNIFACE_VALIDATION_MODELS` above covers. The other 9 are excluded here on purpose:
# each is either still in `validating` state, or classified `biometric` (or both), and
# exposing a facial-recognition/landmark/attribute model to unconditional production
# calls is a decision this build does not make unilaterally - see CLARIFICATIONS.md
# #15/#16 for how the two biometric decisions already made in this codebase were
# actually reached (explicit owner direction, recorded). Revisit only the same way.
_UNIFACE_PRODUCTION_MODELS = {
    "uniface-mobilegaze-estimation": "gaze",
    "uniface-bisenet-parsing": "parsing",
    "uniface-modnet-matting": "matte",
}


@app.post(
    "/internal/v1/infer-uniface",
    response_model=UnifaceValidationResponse,
    tags=["runtime"],
)
async def infer_uniface(
    model_name: str = Form(...),
    confidence: float = Form(0.25),
    frame: UploadFile = File(...),
) -> UnifaceValidationResponse:
    """Production, by-name counterpart to `/internal/v1/validate-infer-uniface`, for the
    three uniface-zoo models that are both `validated` and non-biometric
    (`_UNIFACE_PRODUCTION_MODELS` above). Deployable-state scoped like `/internal/v1/
    infer` (via `_resolve_or_raise`), not validation-scoped like the version_id-addressed
    route above.

    On-demand only, addressed directly by the caller - there is no automated per-camera
    pipeline path here. `pipeline_runtime` calls exactly one model per camera (see
    `csense_shared/pipeline/runtime.py`'s `Assignment.model_name`, a single string, and
    `admin_api/app/api/pipelines.py`'s own docstring: "Only one stage type is interpreted
    anywhere in this codebase today: infer"). Wiring one of these into an automated
    detection pipeline would need a real multi-stage pipeline concept - detector, then
    post-processor - that does not exist in this codebase yet, and no product requirement
    has asked for one. This endpoint exists so the three approved models are reachable at
    all, without building that speculatively.

    `gaze` and `parsing` still need a detected face first, same as the validation route
    above: this runs the platform's own production face detector
    (`_FACE_DETECTOR_MODEL_NAME`) - already `production`/biometric/owner-approved
    (CLARIFICATIONS.md #16), not a new biometric processing step introduced by this
    endpoint. `matte` runs on the full frame directly and needs no detector.
    """
    image = _decode_frame(await frame.read())
    kind = _UNIFACE_PRODUCTION_MODELS.get(model_name)
    if kind is None:
        raise ApiError(
            status_code=422,
            code="not_a_production_uniface_model",
            message=(
                f"'{model_name}' is not one of the models available here: "
                f"{sorted(_UNIFACE_PRODUCTION_MODELS)}. Other uniface-zoo models are "
                "reachable only via /internal/v1/validate-infer-uniface (version_id-"
                "addressed) pending validation/classification review."
            ),
        )

    registered = await _resolve_or_raise(model_name)
    pool: ModelPool = app.state.pool
    loaded = await _load_in_threadpool(pool, registered)

    detector_loaded = None
    if kind != "matte":
        async with _registry_session() as session:
            detector_registered = await get_deployable_by_name(session, _FACE_DETECTOR_MODEL_NAME)
        if detector_registered is None:
            raise ApiError(
                status_code=503,
                code="face_detector_unavailable",
                message=(
                    f"'{_FACE_DETECTOR_MODEL_NAME}' has no deployable version right now - "
                    f"'{model_name}' needs a detected face to run against."
                ),
            )
        detector_loaded = await _load_in_threadpool(pool, detector_registered)

    started = time.monotonic()
    try:
        result = await _run_uniface_inference(loaded, detector_loaded, kind, image, confidence)
    except OutputContractUnknownError as exc:
        raise ApiError(
            status_code=501,
            code="output_contract_unknown",
            message=str(exc),
            details={"model": registered.model_name, "runtime": registered.runtime},
        ) from exc
    inference_ms = (time.monotonic() - started) * 1000

    return UnifaceValidationResponse(
        model_name=loaded.model_name,
        version_id=loaded.version_id,
        task_code=registered.task_code,
        frame_size=[image.shape[1], image.shape[0]],
        inference_ms=round(inference_ms, 2),
        **result,
    )


_PLATE_OCR_MODEL_NAME = "license-plate-ocr"


@app.post("/internal/v1/infer-plate-ocr", response_model=PlateOcrOut, tags=["runtime"])
async def infer_plate_ocr(
    frame: UploadFile = File(...),
) -> PlateOcrOut:
    """Decodes a plate crop into text via `OnnxEngine.decode_plate_text` - see that
    method's own docstring and `PlateOcrResult`'s for what this can and cannot promise.
    2026-09-19 decision to ship this (CHECKLIST.md), after `raw_infer` alone left the
    model producing a well-formed but un-decoded tensor with no consumer at all.

    Takes an already-cropped plate image, not a full frame - the caller is expected to
    have run `license-plate-detector` (via `/internal/v1/infer`) first and cropped its
    detected box. This endpoint does not run the detector itself, matching `raw_infer`'s
    own "takes a plate crop" contract rather than introducing a second detect-then-crop
    path alongside the one `/internal/v1/infer` already provides for the detector model.

    `PlateOcrOut.charset_verified` is always `false` - callers and any UI built on this
    endpoint must surface that alongside `low_confidence`, not just the confidence
    number, per this response model's own docstring.
    """
    import anyio

    image = _decode_frame(await frame.read())
    registered = await _resolve_or_raise(_PLATE_OCR_MODEL_NAME)
    pool: ModelPool = app.state.pool
    loaded = await _load_in_threadpool(pool, registered)

    started = time.monotonic()
    result: PlateOcrResult = await anyio.to_thread.run_sync(loaded.engine.decode_plate_text, image)
    inference_ms = (time.monotonic() - started) * 1000

    return PlateOcrOut(
        model_name=loaded.model_name,
        text=result.text,
        char_confidences=result.char_confidences,
        confidence=result.confidence,
        low_confidence=result.low_confidence,
        charset_verified=result.charset_verified,
        inference_ms=round(inference_ms, 2),
    )


# --- Helpers --------------------------------------------------------------------------

async def _resolve_or_raise(model_name: str):
    async with _registry_session() as session:
        registered = await get_deployable_by_name(session, model_name)
        if registered is not None:
            return registered
        # Distinguish "no such model" from "exists but not deployable" - the second is a
        # governance outcome the caller should see, not a mysterious 404.
        state = await get_state_by_name(session, model_name)

    if state is None:
        raise ApiError(status_code=404, code="model_not_found", message=f"No model named '{model_name}'.")
    raise ApiError(
        status_code=409,
        code="model_not_deployable",
        message=(
            f"Model '{model_name}' is in state '{state}' and is not available for inference. "
            "Promote it through the Admin API first."
        ),
        details={"model": model_name, "state": state},
    )


async def _load_in_threadpool(pool: ModelPool, registered):
    """Model loading is blocking and CPU/IO heavy; keep it off the event loop."""
    import anyio

    try:
        return await anyio.to_thread.run_sync(pool.get, registered)
    except ArtifactVerificationError as exc:
        raise ApiError(
            status_code=502,
            code="artifact_verification_failed",
            message=str(exc),
            details={"model": registered.model_name},
        ) from exc
    except EngineUnavailableError as exc:
        raise ApiError(
            status_code=501,
            code="engine_unavailable",
            message=str(exc),
            details={"model": registered.model_name, "runtime": registered.runtime},
        ) from exc


async def _infer_in_threadpool(loaded, image, confidence: float):
    import anyio

    def _run():
        loaded.inference_count += 1
        return loaded.engine.infer(image, confidence=confidence)

    return await anyio.to_thread.run_sync(_run)


async def _run_uniface_inference(loaded, detector_loaded, kind: str, image, confidence: float) -> dict:
    """Off the event loop, same reasoning as `_infer_in_threadpool`: ONNX Runtime calls
    (and, for every `kind` except `matte`, the SCRFD detector's own `.detect()`) are
    blocking CPU work.
    """
    import anyio

    def _run():
        loaded.inference_count += 1

        if kind == "matte":
            matte = loaded.engine.matte(image)
            return {
                "matte": MatteSummary(
                    mean_alpha=round(float(matte.mean()), 6),
                    coverage_fraction=round(float((matte > 0.5).mean()), 6),
                    shape=[int(matte.shape[0]), int(matte.shape[1])],
                )
            }

        detector_loaded.inference_count += 1
        detections = detector_loaded.engine.infer(image, confidence=confidence)

        if kind == "embedding":
            embeddings = []
            for d in detections:
                vector = loaded.engine.embed(image, d)
                embeddings.append(
                    FaceEmbeddingOut(
                        detector_confidence=round(d.confidence, 4),
                        bbox=[round(v, 5) for v in d.bbox],
                        embedding_dim=int(vector.shape[0]),
                        embedding_l2_norm=round(float(np.linalg.norm(vector)), 6),
                        embedding=[round(float(v), 6) for v in vector],
                    )
                )
            return {"face_count": len(detections), "embeddings": embeddings}

        if kind == "landmarks":
            faces = []
            for d in detections:
                points, score = loaded.engine.landmarks(image, d)
                faces.append(
                    FaceLandmarkOut(
                        detector_confidence=round(d.confidence, 4),
                        bbox=[round(v, 5) for v in d.bbox],
                        landmark_score=round(score, 4),
                        landmarks=[[round(float(c), 3) for c in pt] for pt in points.tolist()],
                    )
                )
            return {"face_count": len(detections), "landmarks": faces}

        if kind == "attributes":
            attrs = []
            for d in detections:
                a = loaded.engine.predict_attributes(image, d)
                attrs.append(
                    FaceAttributesOut(
                        detector_confidence=round(d.confidence, 4),
                        bbox=[round(v, 5) for v in d.bbox],
                        race=a.race, race_confidence=round(a.race_confidence, 4),
                        race_scores={k: round(v, 4) for k, v in a.race_scores.items()},
                        gender=a.gender, gender_confidence=round(a.gender_confidence, 4),
                        gender_scores={k: round(v, 4) for k, v in a.gender_scores.items()},
                        age_bucket=a.age_bucket, age_confidence=round(a.age_confidence, 4),
                        age_scores={k: round(v, 4) for k, v in a.age_scores.items()},
                    )
                )
            return {"face_count": len(detections), "attributes": attrs}

        if kind == "liveness":
            results = []
            for d in detections:
                r = loaded.engine.predict_liveness(image, d)
                results.append(
                    LivenessOut(
                        detector_confidence=round(d.confidence, 4),
                        bbox=[round(v, 5) for v in d.bbox],
                        is_real=r.is_real, label=r.label,
                        confidence=round(r.confidence, 4),
                        scores=[round(float(s), 4) for s in r.scores],
                    )
                )
            return {"face_count": len(detections), "liveness": results}

        if kind == "gaze":
            results = []
            for d in detections:
                g = loaded.engine.estimate_gaze(image, d)
                results.append(
                    GazeOut(
                        detector_confidence=round(d.confidence, 4),
                        bbox=[round(v, 5) for v in d.bbox],
                        yaw_deg=round(g.yaw_deg, 2),
                        pitch_deg=round(g.pitch_deg, 2),
                    )
                )
            return {"face_count": len(detections), "gaze": results}

        if kind == "landmarks98":
            results = []
            for d in detections:
                r = loaded.engine.predict_landmarks(image, d)
                results.append(
                    Landmark98Out(
                        detector_confidence=round(d.confidence, 4),
                        bbox=[round(v, 5) for v in d.bbox],
                        points=[[round(float(c), 5) for c in pt] for pt in r.points],
                    )
                )
            return {"face_count": len(detections), "landmarks98": results}

        if kind == "parsing":
            results = []
            for d in detections:
                mask = loaded.engine.parse(image, d)
                ids, counts = np.unique(mask, return_counts=True)
                total = float(mask.size)
                results.append(
                    FaceParsingOut(
                        detector_confidence=round(d.confidence, 4),
                        bbox=[round(v, 5) for v in d.bbox],
                        crop_size=[int(mask.shape[1]), int(mask.shape[0])],
                        class_fractions={
                            FACE_PARSING_LABELS[int(i)]: round(float(c) / total, 6)
                            # strict=True: np.unique(return_counts=True) always returns
                            # two equal-length arrays, so a length mismatch would be a
                            # real bug rather than something to silently truncate.
                            for i, c in zip(ids.tolist(), counts.tolist(), strict=True)
                        },
                        distinct_classes=int(ids.size),
                    )
                )
            return {"face_count": len(detections), "parsing": results}

        if kind == "face_state":
            results = []
            for d in detections:
                s = loaded.engine.predict_face_state(image, d)
                results.append(
                    FaceStateOut(
                        detector_confidence=round(d.confidence, 4),
                        bbox=[round(v, 5) for v in d.bbox],
                        left_eye_open=round(s.left_eye_open, 6),
                        right_eye_open=round(s.right_eye_open, 6),
                        eyeglasses=round(s.eyeglasses, 6),
                        mask=round(s.mask, 6),
                        sunglasses=round(s.sunglasses, 6),
                    )
                )
            return {"face_count": len(detections), "face_state": results}

        raise OutputContractUnknownError(f"No uniface validation path for kind '{kind}'.")

    return await anyio.to_thread.run_sync(_run)
