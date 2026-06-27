"""
Database initialization script that applies migrations on startup.

This module provides functionality to:
1. Connect to the PostgreSQL database
2. Create a schema_migrations tracking table if it doesn't exist
3. Apply any pending migration files in order
4. Track which migrations have been applied

Usage:
    From application startup:
        await init_database()

    Standalone:
        python -m backend.db.init_db
"""

import asyncio
import logging
import os
from pathlib import Path

import asyncpg

from backend.config import settings

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# SQL to create the migrations tracking table (idempotent)
CREATE_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id SERIAL PRIMARY KEY,
    filename VARCHAR(255) NOT NULL UNIQUE,
    applied_at TIMESTAMPTZ DEFAULT NOW()
);
"""


async def get_connection() -> asyncpg.Connection:
    """Create a connection to the database."""
    return await asyncpg.connect(settings.database_url)


async def ensure_migrations_table(conn: asyncpg.Connection) -> None:
    """Ensure the schema_migrations tracking table exists."""
    await conn.execute(CREATE_MIGRATIONS_TABLE)


async def get_applied_migrations(conn: asyncpg.Connection) -> set:
    """Get set of already-applied migration filenames."""
    rows = await conn.fetch("SELECT filename FROM schema_migrations ORDER BY id")
    return {row["filename"] for row in rows}


def get_migration_files() -> list:
    """Get sorted list of migration SQL files from the migrations directory."""
    if not MIGRATIONS_DIR.exists():
        logger.warning(f"Migrations directory not found: {MIGRATIONS_DIR}")
        return []

    migration_files = sorted(
        f for f in MIGRATIONS_DIR.iterdir()
        if f.suffix == ".sql" and not f.name.startswith("_")
    )
    return migration_files


async def apply_migration(conn: asyncpg.Connection, migration_file: Path) -> None:
    """Apply a single migration file within a transaction."""
    sql = migration_file.read_text(encoding="utf-8")
    filename = migration_file.name

    logger.info(f"Applying migration: {filename}")

    async with conn.transaction():
        await conn.execute(sql)
        await conn.execute(
            "INSERT INTO schema_migrations (filename) VALUES ($1)",
            filename,
        )

    logger.info(f"Successfully applied migration: {filename}")


async def init_database() -> None:
    """
    Initialize the database by applying all pending migrations.

    This function:
    1. Connects to the database
    2. Ensures the migrations tracking table exists
    3. Determines which migrations haven't been applied yet
    4. Applies pending migrations in filename order
    """
    logger.info("Starting database initialization...")

    conn = None
    try:
        conn = await get_connection()
        await ensure_migrations_table(conn)

        applied = await get_applied_migrations(conn)
        migration_files = get_migration_files()

        pending = [f for f in migration_files if f.name not in applied]

        if not pending:
            logger.info("No pending migrations to apply.")
            return

        logger.info(f"Found {len(pending)} pending migration(s) to apply.")

        for migration_file in pending:
            await apply_migration(conn, migration_file)

        logger.info("Database initialization complete.")

    except asyncpg.InvalidCatalogNameError:
        logger.error(
            f"Database does not exist. Please create it first. "
            f"Connection string: {settings.database_url}"
        )
        raise
    except asyncpg.InvalidPasswordError:
        logger.error("Invalid database credentials.")
        raise
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise
    finally:
        if conn:
            await conn.close()


async def reset_database() -> None:
    """
    Drop all tables and reapply migrations. USE WITH CAUTION.

    This is intended for development and testing only.
    """
    logger.warning("Resetting database - dropping all tables!")

    conn = None
    try:
        conn = await get_connection()

        # Drop all tables in reverse dependency order
        await conn.execute("""
            DROP TABLE IF EXISTS schema_migrations CASCADE;
            DROP TABLE IF EXISTS dba_reports CASCADE;
            DROP TABLE IF EXISTS audit_log CASCADE;
            DROP TABLE IF EXISTS guardrail_allowlist CASCADE;
            DROP TABLE IF EXISTS plans CASCADE;
            DROP TABLE IF EXISTS evidence_snapshots CASCADE;
            DROP TABLE IF EXISTS loop_runs CASCADE;
            DROP TABLE IF EXISTS agent_config CASCADE;
            DROP TABLE IF EXISTS guardrail_config CASCADE;
            DROP TABLE IF EXISTS hosts CASCADE;
        """)

        logger.info("All tables dropped. Reapplying migrations...")
    finally:
        if conn:
            await conn.close()

    # Reapply all migrations
    await init_database()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    asyncio.run(init_database())
