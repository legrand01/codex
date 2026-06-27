"""
Asyncpg connection pool lifecycle management.

Provides functions to create, retrieve, and close the asyncpg connection pool.
Integrates with FastAPI's lifespan context for proper startup/shutdown handling.
"""

import logging
from typing import Optional

import asyncpg

from backend.config import settings

logger = logging.getLogger(__name__)

# Module-level pool reference
_pool: Optional[asyncpg.Pool] = None


async def create_pool() -> asyncpg.Pool:
    """
    Create and return an asyncpg connection pool.

    Uses settings from backend.config for DSN, min_size, and max_size.
    Stores the pool in the module-level variable for later retrieval.

    Returns:
        The created asyncpg.Pool instance.

    Raises:
        Exception: If the pool cannot be created (e.g., database unreachable).
    """
    global _pool
    if _pool is not None:
        logger.warning("Connection pool already exists. Returning existing pool.")
        return _pool

    logger.info("Creating asyncpg connection pool...")
    _pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    logger.info(
        f"Connection pool created (min={settings.db_pool_min_size}, "
        f"max={settings.db_pool_max_size})."
    )
    return _pool


def get_pool() -> Optional[asyncpg.Pool]:
    """
    Get the current connection pool.

    Returns:
        The asyncpg.Pool instance, or None if not yet created.
    """
    return _pool


async def close_pool() -> None:
    """
    Close the asyncpg connection pool and release all connections.

    Safe to call even if the pool is not initialized.
    """
    global _pool
    if _pool is not None:
        logger.info("Closing asyncpg connection pool...")
        await _pool.close()
        _pool = None
        logger.info("Connection pool closed.")
    else:
        logger.debug("No connection pool to close.")
