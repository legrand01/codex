"""
Base repository class providing common async CRUD patterns.

All domain-specific repositories should inherit from BaseRepository
to gain standardized database access methods using the asyncpg pool.
"""

import logging
from typing import Any, Dict, List, Optional

import asyncpg

from backend.db.pool import get_pool

logger = logging.getLogger(__name__)


class BaseRepository:
    """
    Base repository with common async CRUD methods.

    Provides fetch_one, fetch_many, execute, insert, and update methods
    that use the shared asyncpg connection pool.
    """

    def __init__(self, table_name: str):
        """
        Initialize the repository for a specific table.

        Args:
            table_name: The database table this repository operates on.
        """
        self.table_name = table_name

    def _get_pool(self) -> asyncpg.Pool:
        """
        Get the connection pool, raising an error if not available.

        Returns:
            The active asyncpg.Pool.

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

    async def fetch_one(
        self,
        query: str,
        *args: Any,
    ) -> Optional[asyncpg.Record]:
        """
        Execute a query and return a single row.

        Args:
            query: SQL query string with $1, $2, etc. placeholders.
            *args: Query parameters.

        Returns:
            A single asyncpg.Record or None if no rows match.
        """
        pool = self._get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetch_many(
        self,
        query: str,
        *args: Any,
    ) -> List[asyncpg.Record]:
        """
        Execute a query and return multiple rows.

        Args:
            query: SQL query string with $1, $2, etc. placeholders.
            *args: Query parameters.

        Returns:
            A list of asyncpg.Record objects.
        """
        pool = self._get_pool()
        async with pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def execute(
        self,
        query: str,
        *args: Any,
    ) -> str:
        """
        Execute a query that does not return rows (e.g., DELETE, DDL).

        Args:
            query: SQL query string with $1, $2, etc. placeholders.
            *args: Query parameters.

        Returns:
            The command status string (e.g., 'DELETE 1').
        """
        pool = self._get_pool()
        async with pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def insert(
        self,
        data: Dict[str, Any],
        returning: str = "id",
    ) -> Any:
        """
        Insert a row into the repository's table.

        Constructs an INSERT statement from the given data dictionary.

        Args:
            data: Column name to value mapping for the new row.
            returning: Column(s) to return after insert (default: 'id').

        Returns:
            The value of the RETURNING clause (typically the new row's ID).
        """
        columns = list(data.keys())
        values = list(data.values())
        placeholders = [f"${i + 1}" for i in range(len(columns))]

        query = (
            f"INSERT INTO {self.table_name} ({', '.join(columns)}) "
            f"VALUES ({', '.join(placeholders)}) "
            f"RETURNING {returning}"
        )

        pool = self._get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(query, *values)

    async def update(
        self,
        record_id: Any,
        data: Dict[str, Any],
        id_column: str = "id",
    ) -> Optional[asyncpg.Record]:
        """
        Update a row in the repository's table by its ID.

        Args:
            record_id: The value of the ID column for the row to update.
            data: Column name to new value mapping for the update.
            id_column: The name of the ID column (default: 'id').

        Returns:
            The updated row as an asyncpg.Record, or None if not found.
        """
        if not data:
            return None

        columns = list(data.keys())
        values = list(data.values())

        set_clauses = [f"{col} = ${i + 1}" for i, col in enumerate(columns)]
        id_placeholder = f"${len(columns) + 1}"

        query = (
            f"UPDATE {self.table_name} "
            f"SET {', '.join(set_clauses)} "
            f"WHERE {id_column} = {id_placeholder} "
            f"RETURNING *"
        )

        pool = self._get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchrow(query, *values, record_id)

    async def delete(
        self,
        record_id: Any,
        id_column: str = "id",
    ) -> str:
        """
        Delete a row from the repository's table by its ID.

        Args:
            record_id: The value of the ID column for the row to delete.
            id_column: The name of the ID column (default: 'id').

        Returns:
            The command status string (e.g., 'DELETE 1').
        """
        query = f"DELETE FROM {self.table_name} WHERE {id_column} = $1"
        pool = self._get_pool()
        async with pool.acquire() as conn:
            return await conn.execute(query, record_id)

    async def count(
        self,
        where: Optional[str] = None,
        *args: Any,
    ) -> int:
        """
        Count rows in the repository's table, optionally with a WHERE clause.

        Args:
            where: Optional WHERE clause (without the WHERE keyword).
            *args: Query parameters for the WHERE clause.

        Returns:
            The row count as an integer.
        """
        query = f"SELECT COUNT(*) FROM {self.table_name}"
        if where:
            query += f" WHERE {where}"

        pool = self._get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(query, *args)
