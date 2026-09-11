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

from app.engines import EngineUnavailableError, OutputContractUnknownError
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


class UnifaceValidationResponse(BaseModel):
    """Response for `/internal/v1/validate-infer-uniface` - a separate response shape
    from `InferenceResponse` because none of these 6 models' outputs are a `Detection`
    list (see `engines.py`'s own `UnifaceEmbeddingEngine`/`UnifaceFaceMeshEngine`/
    `UnifaceMattingEngine` docstrings for why `infer()` deliberately raises for all
    three). Exactly one of `embeddings`/`landmarks`/`matte` is populated, matching which
    of the 6 models `version_id` names."""

    model_name: str
    version_id: str
    task_code: str
    frame_size: list[int]
    inference_ms: float
    face_count: int | None = None
    embeddings: list[FaceEmbeddingOut] | None = None
    landmarks: list[FaceLandmarkOut] | None = None
    matte: MatteSummary | None = None


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


# The 6 uniface-zoo models `/internal/v1/validate-infer-uniface` below knows how to run -
# exactly the 6 low-risk models `engines.py`'s `UnifaceEmbeddingEngine`/
# `UnifaceFaceMeshEngine`/`UnifaceMattingEngine` decode (see that module's own dispatch
# tables). Kept here rather than imported from `engines.py`'s private dicts so this
# endpoint's supported-model list is visible and grep-able in one place, independent of
# engines.py's internal dispatch structure.
_UNIFACE_VALIDATION_MODELS: dict[str, str] = {
    "uniface-adaface-recognition": "embedding",
    "uniface-edgeface-recognition": "embedding",
    "uniface-mobileface-recognition": "embedding",
    "uniface-sphereface-recognition": "embedding",
    "uniface-facemesh-landmark": "landmarks",
    "uniface-modnet-matting": "matte",
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
    """Gate 2-4 validation harness for the 6 low-risk uniface-zoo models
    (`_UNIFACE_VALIDATION_MODELS` above) - the sibling of `/internal/v1/validate-infer`
    for models whose output is not a `Detection` list at all (a face embedding, a dense
    landmark mesh, a portrait alpha matte) and therefore cannot go through
    `InferenceResponse`/`_run_inference`. Same version_id-addressed, VALIDATABLE_STATES
    scoping as `/internal/v1/validate-infer` - see that endpoint's own docstring for why.

    For the 4 embedding models and the facemesh model, this first runs the platform's own
    production face detector (`_FACE_DETECTOR_MODEL_NAME`) over the frame and then decodes
    the target model once per detected face; `uniface-modnet-matting` needs no detector
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
                f"'{registered.model_name}' is not one of the 6 models this endpoint "
                f"validates: {sorted(_UNIFACE_VALIDATION_MODELS)}. Use /internal/v1/"
                "validate-infer for a model whose output is a Detection list."
            ),
        )

    pool: ModelPool = app.state.pool
    loaded = await _load_in_threadpool(pool, registered)

    detector_loaded = None
    if kind in ("embedding", "landmarks"):
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
    (and, for `kind in ("embedding", "landmarks")`, the SCRFD detector's own `.detect()`)
    are blocking CPU work.
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

        raise OutputContractUnknownError(f"No uniface validation path for kind '{kind}'.")

    return await anyio.to_thread.run_sync(_run)
