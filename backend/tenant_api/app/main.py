from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    api_clients,
    audit,
    auth,
    branding,
    cameras,
    dashboard,
    detections,
    edge,
    exports,
    health,
    incidents,
    ingest,
    license,
    media,
    memberships,
    mfa,
    notification_policies,
    nvr,
    pipeline_assignments,
    public_branding,
    realtime,
    recipient_groups,
    reseller,
    rules,
    sites,
    support,
    webhooks,
    zones,
)
from csense_shared.config import get_settings
from csense_shared.db.postgres import create_engine, create_session_factory
from csense_shared.db.redis import create_redis_client
from csense_shared.errors import ApiError, api_error_handler, unhandled_exception_handler
from csense_shared.logging import configure_logging, get_logger
from csense_shared.middleware import CorrelationIdMiddleware, SecurityHeadersMiddleware
from csense_shared.storage.objects import create_client

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

    # One MinIO client for the process. Ingestion writes evidence on the request path, and
    # building a client per request would add a TLS handshake to every frame that opens an
    # incident. Failure is not fatal: detections and alerts still work without snapshots,
    # and refusing to start would turn a storage problem into a total outage.
    try:
        app.state.object_store = create_client(settings)
    except Exception:  # noqa: BLE001
        logger.exception("object_store_unavailable_evidence_capture_disabled")
        app.state.object_store = None

    logger.info("tenant_api_started")
    try:
        yield
    finally:
        await app.state.engine.dispose()
        await app.state.redis.aclose()
        logger.info("tenant_api_stopped")


app = FastAPI(
    title="AIRIVU CSense â€” Tenant API",
    version="0.1.0",
    lifespan=lifespan,
    # Separate OpenAPI surface per TRD Â§6.3 â€” this document only ever describes
    # tenant-scoped and auth endpoints, never admin/platform routes.
    openapi_url="/api/v1/tenant/openapi.json",
    docs_url="/api/v1/tenant/docs",
)

app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
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
app.include_router(dashboard.router)
app.include_router(audit.router)
app.include_router(auth.router)
app.include_router(public_branding.router)
app.include_router(branding.router)
app.include_router(incidents.router)
app.include_router(detections.router)
app.include_router(ingest.router)
app.include_router(sites.router)
app.include_router(zones.router)
app.include_router(rules.router)
app.include_router(cameras.router)
app.include_router(edge.router)
app.include_router(recipient_groups.router)
app.include_router(notification_policies.router)
app.include_router(pipeline_assignments.router)
app.include_router(realtime.router)
app.include_router(media.router)
app.include_router(nvr.router)
app.include_router(memberships.router)
app.include_router(reseller.router)
app.include_router(license.router)
app.include_router(mfa.router)
app.include_router(support.router)
app.include_router(webhooks.router)
app.include_router(api_clients.router)
app.include_router(exports.router)


