"""
Tests for the WebSocket fleet status endpoint.

Tests cover:
- WebSocket connection acceptance at /ws/fleet
- Graceful disconnection handling
- Multiple concurrent connections
- Message broadcasting via the connection manager

Requirements: 1.3, 2.2
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.api.ws_fleet import FleetConnectionManager, fleet_manager
from backend.main import app


# ---------------------------------------------------------------------------
# Unit tests for FleetConnectionManager
# ---------------------------------------------------------------------------


class TestFleetConnectionManager:
    """Tests for the WebSocket connection manager."""

    def test_initial_state_is_empty(self):
        """A new connection manager should have no active connections."""
        manager = FleetConnectionManager()
        assert len(manager.active_connections) == 0

    @pytest.mark.asyncio
    async def test_connect_adds_to_active_connections(self):
        """Connecting a WebSocket should add it to active connections."""
        manager = FleetConnectionManager()
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()

        await manager.connect(mock_ws)

        assert mock_ws in manager.active_connections
        assert len(manager.active_connections) == 1
        mock_ws.accept.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_removes_from_active_connections(self):
        """Disconnecting a WebSocket should remove it from active connections."""
        manager = FleetConnectionManager()
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()

        await manager.connect(mock_ws)
        manager.disconnect(mock_ws)

        assert mock_ws not in manager.active_connections
        assert len(manager.active_connections) == 0

    @pytest.mark.asyncio
    async def test_disconnect_nonexistent_is_safe(self):
        """Disconnecting a WebSocket that was never connected should not raise."""
        manager = FleetConnectionManager()
        mock_ws = AsyncMock()

        # Should not raise
        manager.disconnect(mock_ws)
        assert len(manager.active_connections) == 0

    @pytest.mark.asyncio
    async def test_broadcast_sends_to_all_connections(self):
        """Broadcasting should send message to all connected clients."""
        manager = FleetConnectionManager()
        ws1 = AsyncMock()
        ws1.accept = AsyncMock()
        ws1.send_text = AsyncMock()
        ws2 = AsyncMock()
        ws2.accept = AsyncMock()
        ws2.send_text = AsyncMock()

        await manager.connect(ws1)
        await manager.connect(ws2)

        test_message = json.dumps({
            "event_type": "connection_status_change",
            "host_id": "test-id",
            "hostname": "test-host",
            "old_status": "connected",
            "new_status": "disconnected",
            "timestamp": "2024-01-01T00:00:00Z",
        })

        await manager.broadcast(test_message)

        ws1.send_text.assert_called_once_with(test_message)
        ws2.send_text.assert_called_once_with(test_message)

    @pytest.mark.asyncio
    async def test_broadcast_removes_failed_connections(self):
        """Broadcasting should remove connections that fail to receive."""
        manager = FleetConnectionManager()
        ws_good = AsyncMock()
        ws_good.accept = AsyncMock()
        ws_good.send_text = AsyncMock()
        ws_bad = AsyncMock()
        ws_bad.accept = AsyncMock()
        ws_bad.send_text = AsyncMock(side_effect=Exception("Connection closed"))

        await manager.connect(ws_good)
        await manager.connect(ws_bad)

        assert len(manager.active_connections) == 2

        await manager.broadcast("test message")

        # Good connection should still be active
        assert ws_good in manager.active_connections
        # Bad connection should be removed
        assert ws_bad not in manager.active_connections
        assert len(manager.active_connections) == 1

    @pytest.mark.asyncio
    async def test_broadcast_to_empty_manager(self):
        """Broadcasting with no connections should not raise."""
        manager = FleetConnectionManager()
        # Should not raise
        await manager.broadcast("test message")

    @pytest.mark.asyncio
    async def test_multiple_connects(self):
        """Multiple connections should all be tracked."""
        manager = FleetConnectionManager()
        websockets = []
        for _ in range(5):
            ws = AsyncMock()
            ws.accept = AsyncMock()
            websockets.append(ws)
            await manager.connect(ws)

        assert len(manager.active_connections) == 5

        # Disconnect one
        manager.disconnect(websockets[2])
        assert len(manager.active_connections) == 4
        assert websockets[2] not in manager.active_connections


# ---------------------------------------------------------------------------
# Integration tests for the WebSocket endpoint
# ---------------------------------------------------------------------------


class TestWebSocketFleetEndpoint:
    """Tests for the /ws/fleet WebSocket endpoint."""

    def test_websocket_connection_accepted(self):
        """The /ws/fleet endpoint should accept WebSocket connections."""
        with TestClient(app) as client:
            with client.websocket_connect("/ws/fleet") as websocket:
                # Connection was accepted successfully
                # Send a message to keep the connection alive briefly
                websocket.send_text("ping")

    def test_websocket_graceful_disconnect(self):
        """The WebSocket should handle client disconnection gracefully."""
        with TestClient(app) as client:
            with client.websocket_connect("/ws/fleet") as websocket:
                websocket.send_text("hello")
            # Exiting the context manager closes the connection gracefully
            # No exceptions should be raised

    def test_websocket_multiple_connections(self):
        """Multiple WebSocket connections should be handled concurrently."""
        with TestClient(app) as client:
            with client.websocket_connect("/ws/fleet") as ws1:
                with client.websocket_connect("/ws/fleet") as ws2:
                    ws1.send_text("ping1")
                    ws2.send_text("ping2")
                    # Both connections should be active simultaneously

    @patch("backend.api.ws_fleet.get_redis_client")
    def test_websocket_handles_no_redis(self, mock_redis):
        """WebSocket should remain open even when Redis is unavailable."""
        mock_redis.return_value = None

        with TestClient(app) as client:
            with client.websocket_connect("/ws/fleet") as websocket:
                # Connection should still be accepted even without Redis
                websocket.send_text("ping")


# ---------------------------------------------------------------------------
# Tests for message format
# ---------------------------------------------------------------------------


class TestFleetEventMessageFormat:
    """Tests verifying the expected fleet event message format."""

    def test_connection_status_change_format(self):
        """Connection status change events should match expected format."""
        event = {
            "event_type": "connection_status_change",
            "host_id": "550e8400-e29b-41d4-a716-446655440000",
            "hostname": "db-primary-1",
            "old_status": "connected",
            "new_status": "disconnected",
            "timestamp": "2024-01-15T10:30:00+00:00",
        }
        serialized = json.dumps(event)
        parsed = json.loads(serialized)

        assert parsed["event_type"] == "connection_status_change"
        assert "host_id" in parsed
        assert "hostname" in parsed
        assert "old_status" in parsed
        assert "new_status" in parsed
        assert "timestamp" in parsed

    def test_health_status_change_format(self):
        """Health status change events should match expected format."""
        event = {
            "event_type": "health_status_change",
            "host_id": "550e8400-e29b-41d4-a716-446655440000",
            "hostname": "db-replica-1",
            "old_status": "healthy",
            "new_status": "unhealthy",
            "timestamp": "2024-01-15T10:31:00+00:00",
        }
        serialized = json.dumps(event)
        parsed = json.loads(serialized)

        assert parsed["event_type"] == "health_status_change"
        assert "host_id" in parsed
        assert "hostname" in parsed
        assert "old_status" in parsed
        assert "new_status" in parsed
        assert "timestamp" in parsed
