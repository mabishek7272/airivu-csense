"""Liveness/readiness (TRD-OPS-001): never leaks secrets, only up/down per dependency."""
from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import text

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def liveness() -> dict:
    return {"status": "ok", "service": "tenant-api"}


@router.get("/readyz")
async def readiness(request: Request) -> dict:
    engine = request.app.state.engine
    checks = {}
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception:
        checks["postgres"] = "unavailable"

    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "unavailable"


    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": overall, "dependencies": checks}
