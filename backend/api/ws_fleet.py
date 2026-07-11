"""
WebSocket endpoint for real-time fleet status updates.

Provides a WebSocket at /ws/fleet that:
- Accepts WebSocket connections from the frontend
- Subscribes to Redis pub/sub channels for fleet events
- Forwards fleet status change events (connection and health) to all connected clients
- Handles graceful disconnection and cleanup
- Manages multiple concurrent WebSocket connections (connection manager pattern)

Requirements: 1.3, 2.2
"""

import asyncio
import json
import logging
from typing import Set
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.db.redis_manager import get_redis_client

logger = logging.getLogger(__name__)

router = APIRouter()

# Redis pub/sub channels for fleet events (matches fleet_service.py)
FLEET_STATUS_CHANNEL = "fleet:status_changes"
HEALTH_CHANGE_CHANNEL = "fleet:health_changes"


class FleetConnectionManager:
    """
    Manages multiple concurrent WebSocket connections for fleet status updates.

    Handles:
    - Adding/removing connections
    - Broadcasting messages to all connected clients
    - Graceful cleanup on disconnection
    """

    def __init__(self):
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        """Accept a WebSocket connection and register it."""
        await websocket.accept(subprotocol="dbtune-auth")
        self.active_connections.add(websocket)
        logger.info(
            f"Fleet WebSocket client connected. Total connections: {len(self.active_connections)}"
        )

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection from the active set."""
        self.active_connections.discard(websocket)
        logger.info(
            f"Fleet WebSocket client disconnected. "
            f"Total connections: {len(self.active_connections)}"
        )

    async def broadcast(self, message: str) -> None:
        """
        Send a message to all connected WebSocket clients.

        Handles individual connection failures gracefully by removing
        disconnected clients without affecting other connections.
        """
        disconnected: Set[WebSocket] = set()
        for connection in self.active_connections.copy():
            try:
                await connection.send_text(message)
            except Exception:
                disconnected.add(connection)

        # Clean up any connections that failed during broadcast
        for conn in disconnected:
            self.active_connections.discard(conn)


# Module-level connection manager instance
fleet_manager = FleetConnectionManager()


async def _redis_listener(websocket: WebSocket, organization_id: UUID) -> None:
    """
    Listen to Redis pub/sub channels and forward events to the WebSocket client.

    Creates a dedicated pub/sub subscription per WebSocket connection to ensure
    each client receives all events independently.

    Args:
        websocket: The WebSocket connection to forward events to.
    """
    redis_client = get_redis_client()
    if redis_client is None:
        logger.warning("Redis client not available. WebSocket will not receive fleet events.")
        # Keep connection alive but don't forward events
        while True:
            await asyncio.sleep(1)
        return

    # Create a dedicated pub/sub instance for this connection
    pubsub = redis_client.pubsub()
    try:
        await pubsub.subscribe(FLEET_STATUS_CHANNEL, HEALTH_CHANGE_CHANNEL)
        logger.debug(
            f"Redis pub/sub subscribed to {FLEET_STATUS_CHANNEL} and {HEALTH_CHANGE_CHANNEL}"
        )

        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message is not None and message["type"] == "message":
                data = message["data"]
                # Data may be bytes or string depending on Redis config
                if isinstance(data, bytes):
                    data = data.decode("utf-8")

                # Filter every fleet event through the authenticated tenant.
                try:
                    payload = json.loads(data)
                    host_id = UUID(payload["host_id"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                from backend.db.pool import get_pool

                pool = get_pool()
                if pool is None:
                    continue
                async with pool.acquire() as conn:
                    owned = await conn.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM hosts "
                        "WHERE id = $1 AND organization_id = $2)",
                        host_id,
                        organization_id,
                    )
                if owned:
                    await websocket.send_text(data)
            else:
                # Small sleep to prevent tight loop when no messages
                await asyncio.sleep(0.1)
    finally:
        await pubsub.unsubscribe(FLEET_STATUS_CHANNEL, HEALTH_CHANGE_CHANNEL)
        await pubsub.close()


@router.websocket("/ws/fleet")
async def websocket_fleet_status(websocket: WebSocket) -> None:
    """
    WebSocket endpoint for fleet status updates.

    Accepts a WebSocket connection, subscribes to Redis pub/sub channels
    for fleet events (connection status changes and health status changes),
    and forwards them to the client in real-time.

    Message format pushed to clients:
    {
        "event_type": "connection_status_change" | "health_status_change",
        "host_id": "uuid",
        "hostname": "string",
        "old_status": "string",
        "new_status": "string",
        "timestamp": "iso-string"
    }
    """
    from backend.security import authenticate_websocket

    principal = await authenticate_websocket(websocket)
    if principal is None:
        await websocket.close(code=4401, reason="Authentication is required")
        return
    await fleet_manager.connect(websocket)

    # Create a task for listening to Redis pub/sub
    listener_task = asyncio.create_task(
        _redis_listener(websocket, principal.organization_id)
    )

    try:
        # Keep the WebSocket connection alive by reading incoming messages
        # (clients may send ping/pong or other control messages)
        while True:
            # Wait for any incoming message (or disconnect)
            await websocket.receive_text()
    except WebSocketDisconnect:
        logger.debug("Fleet WebSocket client disconnected normally.")
    except Exception as e:
        logger.warning(f"Fleet WebSocket error: {e}")
    finally:
        # Cancel the Redis listener task
        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

        # Remove from connection manager
        fleet_manager.disconnect(websocket)
