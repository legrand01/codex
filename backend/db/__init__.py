"""
Database package providing connection pool, repository base, and Redis management.

Exports:
    - Pool lifecycle: create_pool, get_pool, close_pool
    - Repository: BaseRepository
    - Redis: create_redis_client, get_redis_client, close_redis_client
"""

from backend.db.pool import close_pool, create_pool, get_pool
from backend.db.redis_manager import close_redis_client, create_redis_client, get_redis_client
from backend.db.repository import BaseRepository

__all__ = [
    "create_pool",
    "get_pool",
    "close_pool",
    "BaseRepository",
    "create_redis_client",
    "get_redis_client",
    "close_redis_client",
]
