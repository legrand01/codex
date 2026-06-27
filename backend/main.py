"""
Autonomous Postgres DBA Agent Platform - FastAPI Application
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
    except Exception as e:
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

# Register API routers
from backend.api.fleet import router as fleet_router
from backend.api.ws_fleet import router as ws_fleet_router
from backend.api.audit import router as audit_router
from backend.api.evidence import router as evidence_router
from backend.api.rollback import router as rollback_router
from backend.api.plans import router as plans_router
from backend.api.runs import router as runs_router
from backend.api.reports import router as reports_router
from backend.api.demo import router as demo_router

app.include_router(fleet_router)
app.include_router(ws_fleet_router)
app.include_router(audit_router)
app.include_router(evidence_router)
app.include_router(rollback_router)
app.include_router(plans_router)
app.include_router(runs_router)
app.include_router(reports_router)
app.include_router(demo_router)


@app.get("/health", tags=["health"])
async def health_check():
    """Health check endpoint returning HTTP 200 when the service is running."""
    return {"status": "healthy", "service": "autonomous-postgres-dba-agent"}


@app.get("/", tags=["root"])
async def root():
    """Root endpoint with API information."""
    return {
        "name": "Autonomous Postgres DBA Agent Platform",
        "version": "0.1.0",
        "docs": "/docs",
    }
