from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, detections, health, incidents
from csense_shared.config import get_settings
from csense_shared.db.postgres import create_engine, create_session_factory
from csense_shared.db.redis import create_redis_client
from csense_shared.errors import ApiError, api_error_handler, unhandled_exception_handler
from csense_shared.logging import configure_logging, get_logger
from csense_shared.middleware import CorrelationIdMiddleware

settings = get_settings()
configure_logging("tenant-api", settings.environment, settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.engine = create_engine(settings)
    app.state.session_factory = create_session_factory(app.state.engine)
    app.state.redis = create_redis_client(settings)
    # Settings on app.state so request handlers can build storage clients without
    # re-reading the environment per request.
    app.state.settings = settings
    logger.info("tenant_api_started")
    try:
        yield
    finally:
        await app.state.engine.dispose()
        await app.state.redis.aclose()
        logger.info("tenant_api_stopped")


app = FastAPI(
    title="AIRIVU CSense — Tenant API",
    version="0.1.0",
    lifespan=lifespan,
    # Separate OpenAPI surface per TRD §6.3 — this document only ever describes
    # tenant-scoped and auth endpoints, never admin/platform routes.
    openapi_url="/api/v1/tenant/openapi.json",
    docs_url="/api/v1/tenant/docs",
)

app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.customer_crm_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Correlation-ID", "Idempotency-Key"],
)

app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(incidents.router)
app.include_router(detections.router)
