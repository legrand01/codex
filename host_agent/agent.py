"""
Host Agent - Collects PostgreSQL telemetry and evidence from managed hosts.

Deployed on or near each PostgreSQL host to collect:
- pg_settings configuration snapshots
- pg_stat_database and pg_stat_statements query samples
- Lock information, replication lag, WAL/checkpoint metrics
- Host OS metrics (CPU, memory, disk I/O)

The agent sends collected evidence to the Control Plane via HTTP
and reports heartbeats at a configurable interval.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import asyncpg
import httpx
from collectors.locks_collector import collect_locks
from collectors.os_metrics_collector import collect_os_metrics
from collectors.pg_settings_collector import collect_pg_settings
from collectors.pg_stats_collector import collect_pg_stats
from collectors.replication_collector import collect_replication
from collectors.wal_checkpoint_collector import collect_wal_checkpoint
from config import AgentConfig

logger = logging.getLogger(__name__)

# Query to detect PostgreSQL version and role
PG_VERSION_ROLE_QUERY = """
SELECT
    version() AS pg_version,
    pg_is_in_recovery() AS is_replica;
"""


class HostAgent:
    """
    Main Host Agent service.

    Manages multiple collection loops running at different intervals,
    sends heartbeats, and detects role/version changes.
    """

    def __init__(self, config: AgentConfig, conn=None):
        """
        Initialize the Host Agent.

        Args:
            config: Agent configuration (intervals, URLs, host_id, etc.)
            conn: An asyncpg connection or pool for querying the PostgreSQL host.
                  If None, collectors requiring DB access will be skipped.
        """
        self.config = config
        self.conn = conn
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self._http_client: Optional[httpx.AsyncClient] = None

        # Role/version state
        self.pg_version: Optional[str] = None
        self.server_role: Optional[str] = None  # "primary" or "replica"

    async def ensure_registered(self) -> None:
        """
        Ensure this agent has a fleet host ID.

        If AGENT_HOST_ID is not supplied, the agent registers AGENT_HOSTNAME with
        the control plane and uses the returned UUID for heartbeats/evidence.
        """
        if self.config.host_id:
            return
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=30.0)

        hostname = self.config.hostname
        register_url = f"{self.config.control_plane_url}/api/v1/fleet/"
        response = await self._http_client.post(register_url, json={"hostname": hostname})

        if response.status_code == 201:
            self.config.host_id = response.json()["id"]
            logger.info("Registered host %s as %s", hostname, self.config.host_id)
            return

        if response.status_code == 409:
            list_response = await self._http_client.get(register_url)
            list_response.raise_for_status()
            for host in list_response.json().get("hosts", []):
                if host.get("hostname") == hostname:
                    self.config.host_id = host["id"]
                    logger.info("Using existing host %s as %s", hostname, self.config.host_id)
                    return

        response.raise_for_status()

    async def start(self) -> None:
        """
        Start the host agent collection loops.

        Launches concurrent tasks for each evidence type at its configured interval,
        a heartbeat task, and a role/version detection task.
        """
        self._running = True
        self._http_client = httpx.AsyncClient(timeout=30.0)

        await self.ensure_registered()

        # Detect role and version on startup
        await self.detect_role_version()

        # Launch collection loops
        self._tasks = [
            asyncio.create_task(
                self._collection_loop(
                    "pg_settings",
                    self.collect_pg_settings,
                    self.config.pg_settings_interval,
                )
            ),
            asyncio.create_task(
                self._collection_loop(
                    "pg_stats",
                    self.collect_pg_stats,
                    self.config.pg_stats_interval,
                )
            ),
            asyncio.create_task(
                self._collection_loop(
                    "locks",
                    self.collect_locks,
                    self.config.locks_replication_interval,
                )
            ),
            asyncio.create_task(
                self._collection_loop(
                    "replication",
                    self.collect_replication,
                    self.config.locks_replication_interval,
                )
            ),
            asyncio.create_task(
                self._collection_loop(
                    "wal_checkpoint",
                    self.collect_wal_checkpoint,
                    self.config.locks_replication_interval,
                )
            ),
            asyncio.create_task(
                self._collection_loop(
                    "os_metrics",
                    self.collect_os_metrics,
                    self.config.os_metrics_interval,
                )
            ),
            asyncio.create_task(self._heartbeat_loop()),
        ]

        logger.info(
            f"Host Agent started for host_id={self.config.host_id}, "
            f"role={self.server_role}, version={self.pg_version}"
        )

        # Wait for all tasks (they run indefinitely until stopped)
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        """Stop the host agent and cancel all collection loops."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        logger.info("Host Agent stopped")

    async def _collection_loop(
        self,
        name: str,
        collector_fn,
        interval: int,
    ) -> None:
        """
        Run a collector function repeatedly at the specified interval.

        Args:
            name: Name of the evidence type (for logging).
            collector_fn: Async function that performs collection.
            interval: Collection interval in seconds.
        """
        while self._running:
            try:
                snapshot = await collector_fn()
                if snapshot is not None:
                    await self._submit_evidence(snapshot)
            except Exception as e:
                # Log and skip - continue collecting other types (Req 6.8)
                logger.error(f"Collection error for {name}: {e}")
            await asyncio.sleep(interval)

    async def _heartbeat_loop(self) -> None:
        """Send heartbeats to the Control Plane at the configured interval."""
        while self._running:
            try:
                await self.report_heartbeat()
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
            await asyncio.sleep(self.config.heartbeat_interval)

    async def collect_pg_settings(self) -> Optional[Dict[str, Any]]:
        """Collect pg_settings configuration snapshot."""
        if self.conn is None:
            logger.warning("No DB connection available for pg_settings collection")
            return None
        return await collect_pg_settings(self.conn, self.config.host_id)

    async def collect_pg_stats(self) -> Optional[Dict[str, Any]]:
        """Collect pg_stat_database and pg_stat_statements samples."""
        if self.conn is None:
            logger.warning("No DB connection available for pg_stats collection")
            return None
        return await collect_pg_stats(self.conn, self.config.host_id, self.config.max_query_entries)

    async def collect_locks(self) -> Optional[Dict[str, Any]]:
        """Collect current lock information."""
        if self.conn is None:
            logger.warning("No DB connection available for locks collection")
            return None
        return await collect_locks(self.conn, self.config.host_id)

    async def collect_replication(self) -> Optional[Dict[str, Any]]:
        """Collect replication lag metrics."""
        if self.conn is None:
            logger.warning("No DB connection available for replication collection")
            return None
        return await collect_replication(self.conn, self.config.host_id)

    async def collect_wal_checkpoint(self) -> Optional[Dict[str, Any]]:
        """Collect WAL/checkpoint metrics."""
        if self.conn is None:
            logger.warning("No DB connection available for WAL/checkpoint collection")
            return None
        return await collect_wal_checkpoint(self.conn, self.config.host_id)

    async def collect_os_metrics(self) -> Optional[Dict[str, Any]]:
        """Collect host OS metrics (CPU, memory, disk I/O)."""
        return await collect_os_metrics(self.config.host_id)

    async def report_heartbeat(self) -> None:
        """
        Send heartbeat to Control Plane.

        POSTs to /api/v1/fleet/{host_id}/heartbeat with current role and version.
        """
        if self._http_client is None:
            return

        url = f"{self.config.control_plane_url}/api/v1/fleet/{self.config.host_id}/heartbeat"
        payload = {
            "host_id": self.config.host_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pg_version": self.pg_version,
            "server_role": self.server_role,
        }

        try:
            response = await self._http_client.post(url, json=payload)
            if response.status_code >= 400:
                logger.warning(f"Heartbeat returned status {response.status_code}: {response.text}")
        except httpx.RequestError as e:
            logger.error(f"Heartbeat request failed: {e}")
            raise

    async def detect_role_version(self) -> Optional[Dict[str, str]]:
        """
        Detect PostgreSQL version and server role (primary/replica).

        Queries for version() and pg_is_in_recovery() to determine if the
        host is a primary or replica. Reports the result to the Control Plane.

        Returns:
            Dict with 'pg_version' and 'server_role', or None on failure.
        """
        if self.conn is None:
            logger.warning("No DB connection available for role/version detection")
            return None

        try:
            row = await self.conn.fetchrow(PG_VERSION_ROLE_QUERY)
            if row is None:
                return None

            self.pg_version = row["pg_version"]
            is_replica = row["is_replica"]
            new_role = "replica" if is_replica else "primary"

            # Detect role change
            previous_role = self.server_role
            self.server_role = new_role

            result = {
                "pg_version": self.pg_version,
                "server_role": self.server_role,
            }

            if previous_role is not None and previous_role != new_role:
                logger.info(f"Role change detected: {previous_role} -> {new_role}")
                # Report role change to control plane within 10 seconds (Req 6.5)
                await self._report_role_version(result)

            elif previous_role is None:
                # Initial startup - report immediately (Req 6.5)
                await self._report_role_version(result)

            return result
        except Exception as e:
            logger.error(f"Failed to detect role/version: {e}")
            return None

    async def _report_role_version(self, role_version: Dict[str, str]) -> None:
        """Report role/version to the Control Plane."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=10.0)

        url = f"{self.config.control_plane_url}/api/v1/fleet/{self.config.host_id}/role"
        payload = {
            "host_id": self.config.host_id,
            "pg_version": role_version["pg_version"],
            "server_role": role_version["server_role"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        try:
            await self._http_client.post(url, json=payload)
        except httpx.RequestError as e:
            logger.error(f"Failed to report role/version: {e}")

    async def _submit_evidence(self, snapshot: Dict[str, Any]) -> None:
        """
        Submit an evidence snapshot to the Control Plane.

        Args:
            snapshot: The evidence snapshot dict to submit.
        """
        if self._http_client is None:
            return

        url = f"{self.config.control_plane_url}/api/v1/fleet/{self.config.host_id}/evidence"

        try:
            response = await self._http_client.post(url, json=snapshot)
            if response.status_code >= 400:
                logger.warning(f"Evidence submission returned status {response.status_code}")
        except httpx.RequestError as e:
            logger.error(f"Evidence submission failed: {e}")
            # Buffer will be handled by buffer module (task 5.2)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, stream=sys.stdout)

    async def main() -> None:
        config = AgentConfig.from_env()
        conn = await asyncpg.connect(config.pg_connection_string)
        agent = HostAgent(config, conn=conn)
        try:
            await agent.start()
        finally:
            await agent.stop()
            await conn.close()

    asyncio.run(main())
