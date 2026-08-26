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
from contextlib import asynccontextmanager

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, UploadFile
from pydantic import BaseModel

from app.engines import EngineUnavailableError, OutputContractUnknownError
from app.loader import ArtifactVerificationError, ModelArtifactCache
from app.pool import ModelPool
from app.registry import get_deployable_by_name, get_state_by_name, list_deployable
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


@app.post("/internal/v1/infer", response_model=InferenceResponse, tags=["runtime"])
async def infer(
    model_name: str = Form(...),
    confidence: float = Form(0.25),
    frame: UploadFile = File(...),
) -> InferenceResponse:
    payload = await frame.read()
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

    registered = await _resolve_or_raise(model_name)
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
            details={"model": model_name, "runtime": registered.runtime},
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
