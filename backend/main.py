"""
Autonomous Postgres DBA Agent Platform - FastAPI Application
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from backend.api.audit import router as audit_router
from backend.api.baselines import router as baselines_router
from backend.api.candidates import router as candidates_router
from backend.api.configurations import router as configurations_router
from backend.api.demo import router as demo_router
from backend.api.events import router as events_router
from backend.api.evidence import router as evidence_router
from backend.api.fingerprints import router as fingerprints_router
from backend.api.fleet import router as fleet_router
from backend.api.parameter_catalog import router as parameter_catalog_router
from backend.api.plans import router as plans_router
from backend.api.reports import router as reports_router
from backend.api.rollback import router as rollback_router
from backend.api.runs import router as runs_router
from backend.api.ws_fleet import router as ws_fleet_router
from backend.config import settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler for startup and shutdown events.

    On startup: creates the database connection pool and Redis client.
    On shutdown: closes all connections gracefully.

    If database/Redis are unreachable, the app still starts (degraded mode)
    so that the /health endpoint can report status.
    """
    # --- Startup ---
    # Attempt to initialize database pool (non-fatal on failure)
    try:
        from backend.db.pool import create_pool

        await create_pool()
        logger.info("Database connection pool initialized.")
        from backend.security import validate_security_configuration

        await validate_security_configuration()
    except Exception as e:
        if settings.environment == "production":
            logger.exception("Production startup security/database validation failed")
            raise
        logger.warning(f"Failed to create database pool (app will run degraded): {e}")

    # Attempt to initialize Redis client (non-fatal on failure)
    try:
        from backend.db.redis_manager import create_redis_client

        await create_redis_client()
        logger.info("Redis client initialized.")
    except Exception as e:
        logger.warning(f"Failed to create Redis client (app will run degraded): {e}")

    yield

    # --- Shutdown ---
    from backend.db.pool import close_pool
    from backend.db.redis_manager import close_redis_client

    await close_pool()
    await close_redis_client()
    logger.info("All connections closed.")


app = FastAPI(
    title="Autonomous Postgres DBA Agent Platform",
    description="Web-based control plane for autonomous PostgreSQL investigation and tuning loops",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware for frontend communication
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def authentication_boundary(request: Request, call_next):
    from backend.security import authenticate_request, is_public_or_agent_path

    if is_public_or_agent_path(request):
        return await call_next(request)
    principal = await authenticate_request(request)
    if principal is None:
        return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})
    request.state.principal = principal
    return await call_next(request)

app.include_router(fleet_router)
app.include_router(ws_fleet_router)
app.include_router(audit_router)
app.include_router(baselines_router)
app.include_router(candidates_router)
app.include_router(configurations_router)
app.include_router(evidence_router)
app.include_router(events_router)
app.include_router(fingerprints_router)
app.include_router(parameter_catalog_router)
app.include_router(rollback_router)
app.include_router(plans_router)
app.include_router(runs_router)
app.include_router(reports_router)
app.include_router(demo_router)


@app.get("/health", tags=["health"])
async def health_check():
    """Backward-compatible process liveness check."""
    return {"status": "healthy", "service": "autonomous-postgres-dba-agent"}


@app.get("/health/live", tags=["health"])
async def liveness_check():
    """Return HTTP 200 whenever the API process can serve requests."""
    return {"status": "alive", "service": "autonomous-postgres-dba-agent"}


@app.get("/health/ready", tags=["health"])
async def readiness_check():
    """Return HTTP 503 until both durable dependencies are reachable."""
    from backend.services.operational_health import dependency_status

    result = await dependency_status()
    return JSONResponse(
        status_code=200 if result["status"] == "ready" else 503,
        content=result,
    )


@app.get("/metrics", tags=["health"])
async def metrics():
    """Internal, low-cardinality Prometheus metrics.

    Production ingress must not proxy this path outside the private network.
    """
    from backend.services.operational_health import prometheus_metrics

    return PlainTextResponse(
        await prometheus_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/", tags=["root"])
async def root():
    """Root endpoint with API information."""
    return {
        "name": "Autonomous Postgres DBA Agent Platform",
        "version": "0.1.0",
        "docs": "/docs",
    }
