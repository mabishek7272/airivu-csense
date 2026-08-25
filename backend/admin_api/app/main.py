from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, health, organizations
from csense_shared.config import get_settings
from csense_shared.db.mongo import create_mongo_client, get_database
from csense_shared.db.postgres import create_engine, create_session_factory
from csense_shared.db.redis import create_redis_client
from csense_shared.errors import ApiError, api_error_handler, unhandled_exception_handler
from csense_shared.logging import configure_logging, get_logger
from csense_shared.middleware import CorrelationIdMiddleware

settings = get_settings()
configure_logging("admin-api", settings.environment, settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.engine = create_engine(settings)
    app.state.session_factory = create_session_factory(app.state.engine)
    app.state.redis = create_redis_client(settings)
    app.state.mongo_client = create_mongo_client(settings)
    app.state.mongo_db = get_database(app.state.mongo_client, settings)
    logger.info("admin_api_started")
    try:
        yield
    finally:
        await app.state.engine.dispose()
        await app.state.redis.aclose()
        app.state.mongo_client.close()
        logger.info("admin_api_stopped")


app = FastAPI(
    title="AIRIVU CSense — Admin API",
    version="0.1.0",
    lifespan=lifespan,
    # Separate OpenAPI surface per TRD §6.3 — platform-only, never merged with the
    # customer-facing document.
    openapi_url="/api/v1/admin/openapi.json",
    docs_url="/api/v1/admin/docs",
)

app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.developer_console_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Correlation-ID", "Idempotency-Key"],
)

app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(organizations.router)
