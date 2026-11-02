"""Public detection demo: a phone (or the marketing site) sends one photo or camera frame
and gets bounding boxes back from the real registered models.

This endpoint is unauthenticated on purpose, so it is defended in depth instead:

  - **Per-IP rate limit** (`csense_shared.security.rate_limit`, keyed by a hash of the IP,
    so no address is stored) and a **global in-flight cap** that refuses instead of queueing,
    because the production box has no GPU and also serves real cameras. A refused frame is
    simply skipped by the app; the next one is sent in a moment.
  - **Allow-listed modes only**; the caller never names a model.
  - **Nothing is stored or logged**: the image lives in memory for the length of the request,
    and the log line carries the mode and timing only. Faces of passers-by are personal data.
  - **`/detect`'s `face` mode is detection only** — it calls the detector, never the
    embedding/recognition route, so no biometric template is produced there. `/detect-face`
    is different by design: it returns a raw embedding to the caller for the app's
    session-only, client-side face-matching feature (see
    `docs/superpowers/specs/2026-09-25-apti-demo-v2-design.md`) — the embedding is never
    stored or compared here, only computed and returned.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.modes import MODES
from csense_shared.config import get_settings
from csense_shared.db.postgres import create_engine, create_session_factory
from csense_shared.db.redis import create_redis_client
from csense_shared.logging import configure_logging
from csense_shared.security.rate_limit import RateLimitExceededError, check_and_increment
from csense_shared.storage.objects import BUCKET_AUDIT_ARCHIVE, create_client as create_minio_client, demo_login_selfie_key

logger = logging.getLogger("demo_api")

MAX_IMAGE_BYTES = 1_500_000
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}
PER_IP_LIMIT_PER_MINUTE = 150
MAX_IN_FLIGHT = 4
_IP_NAMESPACE = uuid.UUID("6f0f6f0e-2c3a-4f7a-9d1e-0c5b8a1d7e42")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging("demo-api", settings.environment, settings.log_level)
    app.state.redis = create_redis_client(settings)
    app.state.http = httpx.AsyncClient(base_url=settings.ai_runtime_base_url, timeout=15.0)
    app.state.slots = asyncio.Semaphore(MAX_IN_FLIGHT)
    app.state.db_engine = create_engine(settings)
    app.state.db_session_factory = create_session_factory(app.state.db_engine)
    app.state.minio = create_minio_client(settings)
    yield
    await app.state.http.aclose()
    await app.state.redis.aclose()
    await app.state.db_engine.dispose()


_origins = [o for o in os.environ.get("DEMO_CORS_ORIGINS", "https://demo.3rdi.in").split(",") if o]

app = FastAPI(title="CSense demo", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    CORSMiddleware, allow_origins=_origins, allow_methods=["GET", "POST"], allow_headers=["*"], max_age=600
)


def _client_key(request: Request) -> uuid.UUID:
    """Traefik appends the connecting address to X-Forwarded-For, so the last entry is the
    one it saw and cannot be forged by the caller. Hashed: the address itself is not kept."""
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[-1].strip() or (request.client.host if request.client else "unknown")
    return uuid.uuid5(_IP_NAMESPACE, hashlib.sha256(ip.encode()).hexdigest())


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"code": code, "message": message}, status_code=status, headers=headers)


class ModeOut(BaseModel):
    code: str
    label: str
    description: str
    available: bool
    note: str | None = None


class BoxOut(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float


class DetectionOut(BaseModel):
    label: str
    confidence: float
    box: BoxOut


class DetectOut(BaseModel):
    mode: str
    width: int
    height: int
    inference_ms: float
    detections: list[DetectionOut]


class FaceOut(BaseModel):
    box: BoxOut
    detector_confidence: float
    embedding: list[float]


class DetectFaceOut(BaseModel):
    width: int
    height: int
    inference_ms: float
    faces: list[FaceOut]


@app.get("/api/v1/demo/modes", response_model=list[ModeOut])
async def list_modes() -> list[ModeOut]:
    return [
        ModeOut(code=m.code, label=m.label, description=m.description, available=m.available,
                note=m.unavailable_reason)
        for m in MODES.values()
    ]


def _pretty(class_name: str) -> str:
    return class_name.replace("_", " ").replace(":", " ").strip().capitalize()


@app.post("/api/v1/demo/detect", response_model=DetectOut)
async def detect(request: Request, mode: str = Form(...), image: UploadFile = File(...)):
    chosen = MODES.get(mode)
    if chosen is None:
        return _error(422, "unknown_mode", "Unknown detection mode.")
    if not chosen.available:
        return _error(409, "mode_unavailable", f"{chosen.label}: {chosen.unavailable_reason or 'not available'}.")
    if image.content_type not in ALLOWED_TYPES:
        return _error(415, "unsupported_image", "Send a JPEG, PNG or WebP image.")

    try:
        await check_and_increment(
            request.app.state.redis, settings, client_id=_client_key(request),
            limit_per_minute=PER_IP_LIMIT_PER_MINUTE,
        )
    except RateLimitExceededError as exc:
        return _error(429, "rate_limited", "Too many requests. Slow down a little.",
                      {"Retry-After": str(exc.retry_after_seconds)})

    data = await image.read(MAX_IMAGE_BYTES + 1)
    if not data:
        return _error(400, "empty_image", "No image received.")
    if len(data) > MAX_IMAGE_BYTES:
        return _error(413, "image_too_large", "Image is larger than 1.5 MB. Send a smaller photo.")

    slots: asyncio.Semaphore = request.app.state.slots
    if slots.locked():
        return _error(503, "busy", "The demo is busy. Try again in a moment.", {"Retry-After": "1"})

    async with slots:
        started = time.monotonic()
        try:
            response = await request.app.state.http.post(
                "/internal/v1/infer",
                data={"model_name": chosen.model_name, "confidence": str(chosen.min_confidence)},
                files={"frame": ("frame.jpg", data, image.content_type)},
            )
        except httpx.HTTPError:
            logger.warning("demo_inference_unreachable", extra={"mode": mode})
            return _error(502, "inference_unavailable", "Detection is unavailable right now.")

    if response.status_code == 400:
        return _error(400, "bad_image", "That image could not be read.")
    if response.status_code != 200:
        logger.warning("demo_inference_failed", extra={"mode": mode, "status": response.status_code})
        return _error(502, "inference_failed", "Detection failed. Try again.")

    payload = response.json()
    detections = [
        DetectionOut(
            label=(chosen.labels or {}).get(d["class_name"]) or _pretty(d["class_name"]), confidence=round(float(d["confidence"]), 3),
            box=BoxOut(x1=d["bbox"][0], y1=d["bbox"][1], x2=d["bbox"][2], y2=d["bbox"][3]),
        )
        for d in payload.get("detections", [])
        if chosen.only_classes is None or d["class_name"] in chosen.only_classes
    ]
    width, height = payload.get("frame_size", [0, 0])
    logger.info("demo_detect", extra={
        "mode": mode, "detections": len(detections), "ms": round((time.monotonic() - started) * 1000)})
    return DetectOut(mode=mode, width=width, height=height,
                     inference_ms=float(payload.get("inference_ms", 0)), detections=detections)


# Costs roughly 2x /detect per request (two model passes - detector + recognizer -
# instead of one), sharing the same MAX_IN_FLIGHT cap and the same ai-runtime container
# that also serves real tenant camera inference. Not yet measured against real demo
# traffic; revisit MAX_IN_FLIGHT or give this route its own cap if it turns out to
# meaningfully compete with real camera traffic under load.
@app.post("/api/v1/demo/detect-face", response_model=DetectFaceOut)
async def detect_face(request: Request, image: UploadFile = File(...)):
    if image.content_type not in ALLOWED_TYPES:
        return _error(415, "unsupported_image", "Send a JPEG, PNG or WebP image.")

    try:
        await check_and_increment(
            request.app.state.redis, settings, client_id=_client_key(request),
            limit_per_minute=PER_IP_LIMIT_PER_MINUTE,
        )
    except RateLimitExceededError as exc:
        return _error(429, "rate_limited", "Too many requests. Slow down a little.",
                      {"Retry-After": str(exc.retry_after_seconds)})

    data = await image.read(MAX_IMAGE_BYTES + 1)
    if not data:
        return _error(400, "empty_image", "No image received.")
    if len(data) > MAX_IMAGE_BYTES:
        return _error(413, "image_too_large", "Image is larger than 1.5 MB. Send a smaller photo.")

    slots: asyncio.Semaphore = request.app.state.slots
    if slots.locked():
        return _error(503, "busy", "The demo is busy. Try again in a moment.", {"Retry-After": "1"})

    async with slots:
        started = time.monotonic()
        try:
            response = await request.app.state.http.post(
                "/internal/v1/infer-face",
                data={"confidence": "0.5"},
                files={"frame": ("frame.jpg", data, image.content_type)},
            )
        except httpx.HTTPError:
            logger.warning("demo_infer_face_unreachable")
            return _error(502, "inference_unavailable", "Detection is unavailable right now.")

    if response.status_code == 400:
        return _error(400, "bad_image", "That image could not be read.")
    if response.status_code != 200:
        logger.warning("demo_infer_face_failed", extra={"status": response.status_code})
        return _error(502, "inference_failed", "Detection failed. Try again.")

    payload = response.json()
    width, height = payload.get("frame_size", [0, 0])
    faces = [
        FaceOut(
            box=BoxOut(x1=f["bbox"][0], y1=f["bbox"][1], x2=f["bbox"][2], y2=f["bbox"][3]),
            detector_confidence=float(f["detector_confidence"]),
            embedding=f["embedding"],
        )
        for f in payload.get("faces", [])
    ]
    logger.info("demo_detect_face", extra={
        "faces": len(faces), "ms": round((time.monotonic() - started) * 1000)})
    return DetectFaceOut(width=width, height=height,
                          inference_ms=float(payload.get("inference_ms", 0)), faces=faces)


class AuditLoginOut(BaseModel):
    id: str


# The login screen's own credential is a shared, public one (see the mobile app's
# LoginScreen.tsx) - this endpoint is not an access-control boundary, so it accepts every
# well-formed submission. Its only job is recording who tried the demo, and it must not be
# the place that decides whether they were allowed to.
@app.post("/api/v1/demo/audit-login", response_model=AuditLoginOut)
async def audit_login(
    request: Request,
    name: str = Form(...),
    place: str = Form(...),
    company: str = Form(...),
    selfie: UploadFile = File(...),
):
    if selfie.content_type not in ALLOWED_TYPES:
        return _error(415, "unsupported_image", "Send a JPEG, PNG or WebP image.")
    if not name.strip() or not place.strip() or not company.strip():
        return _error(422, "missing_field", "Name, place and company are all required.")

    data = await selfie.read(MAX_IMAGE_BYTES + 1)
    if not data:
        return _error(400, "empty_image", "No selfie received.")
    if len(data) > MAX_IMAGE_BYTES:
        return _error(413, "image_too_large", "Image is larger than 1.5 MB. Send a smaller photo.")

    login_id = uuid.uuid4()
    object_key = demo_login_selfie_key(login_id)
    try:
        request.app.state.minio.put_object(
            BUCKET_AUDIT_ARCHIVE, object_key, io.BytesIO(data), length=len(data), content_type="image/jpeg",
        )
    except Exception:
        logger.exception("demo_audit_login_selfie_upload_failed")
        return _error(502, "storage_unavailable", "Could not save your visit. Try again.")

    try:
        async with request.app.state.db_session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO demo_login_audit (id, name, place, company, selfie_object_key) "
                        "VALUES (:id, :name, :place, :company, :selfie_object_key)"
                    ),
                    {
                        "id": str(login_id), "name": name.strip()[:200], "place": place.strip()[:200],
                        "company": company.strip()[:200], "selfie_object_key": object_key,
                    },
                )
    except Exception:
        logger.exception("demo_audit_login_db_write_failed")
        return _error(502, "storage_unavailable", "Could not save your visit. Try again.")

    logger.info("demo_audit_login", extra={"id": str(login_id)})
    return AuditLoginOut(id=str(login_id))


# Not wired to a real camera yet - always refuses until a specific site's camera is
# provisioned. Shaped as a real 503/Retry-After so the client's existing Throttled handling
# (used for the busy-in-flight-cap case elsewhere in this file) treats it the same way: a
# quiet retry, not a hard error.
@app.get("/api/v1/demo/monitor/remote-frame")
async def monitor_remote_frame():
    return _error(503, "remote_camera_not_configured", "The remote camera is not connected yet.",
                  {"Retry-After": "5"})


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
