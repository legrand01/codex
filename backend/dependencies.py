"""
FastAPI dependency injection utilities.

Provides shared dependencies for database connections, Redis clients,
and service instances used across API endpoints.
"""

from typing import AsyncGenerator

import asyncpg
import redis.asyncio as aioredis

from backend.db.pool import get_pool
from backend.db.redis_manager import get_redis_client


async def get_db() -> AsyncGenerator[asyncpg.Connection, None]:
    """
    Dependency that provides a database connection from the pool.

    Acquires a connection from the shared pool and releases it
    after the request completes.

    Yields:
        An asyncpg.Connection from the pool.

    Raises:
        RuntimeError: If the connection pool has not been initialized.
    """
    pool = get_pool()
    if pool is None:
        raise RuntimeError(
            "Database connection pool is not initialized. "
            "Ensure the application lifespan has started."
        )
    async with pool.acquire() as connection:
        yield connection


async def get_redis() -> AsyncGenerator[aioredis.Redis, None]:
    """
    Dependency that provides the shared Redis client.

    Yields:
        An aioredis.Redis client instance.

    Raises:
        RuntimeError: If the Redis client has not been initialized.
    """
    client = get_redis_client()
    if client is None:
        raise RuntimeError(
            "Redis client is not initialized. Ensure the application lifespan has started."
        )
    yield client


async def get_db_pool_dependency() -> asyncpg.Pool:
    """
    Dependency that provides the connection pool itself (for bulk operations).

    Returns:
        The asyncpg.Pool instance.

    Raises:
        RuntimeError: If the connection pool has not been initialized.
    """
    pool = get_pool()
    if pool is None:
        raise RuntimeError(
            "Database connection pool is not initialized. "
            "Ensure the application lifespan has started."
        )
    return pool
