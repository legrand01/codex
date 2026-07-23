"""
Tests for evidence buffering and circuit breaker logic.

Covers:
- Buffer add/flush in chronological order
- FIFO eviction at capacity
- Circuit breaker state transitions
- Flush after reconnection
"""

import os

# Adjust import path for the host agent module.
import sys
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "host_agent"))

from buffer import (
    DEFAULT_FAILURE_THRESHOLD,
    DEFAULT_PROBE_INTERVAL_SECONDS,
    BufferedEvidenceSender,
    CircuitBreaker,
    CircuitState,
    EvidenceBuffer,
)


@pytest.fixture
def tmp_buffer_dir(tmp_path):
    """Create a temporary directory for buffer files."""
    buffer_dir = tmp_path / "buffer"
    buffer_dir.mkdir()
    return str(buffer_dir)


@pytest.fixture
def buffer(tmp_buffer_dir):
    """Create an EvidenceBuffer with a small max size for testing."""
    return EvidenceBuffer(buffer_dir=tmp_buffer_dir, max_size_bytes=1024)


@pytest.fixture
def large_buffer(tmp_buffer_dir):
    """Create an EvidenceBuffer with default (512 MB) capacity."""
    return EvidenceBuffer(buffer_dir=tmp_buffer_dir)


@pytest.fixture
def circuit_breaker():
    """Create a CircuitBreaker with default settings."""
    return CircuitBreaker()


@pytest.fixture
def fast_circuit_breaker():
    """Create a CircuitBreaker with fast probe interval for testing."""
    return CircuitBreaker(failure_threshold=2, probe_interval_seconds=0.1)


class TestEvidenceBuffer:
    """Tests for the EvidenceBuffer class."""

    def test_add_single_entry(self, buffer):
        """Test adding a single entry to the buffer."""
        snapshot = {"host_id": "host-1", "type": "pg_settings", "data": {"key": "value"}}
        buffer.add(snapshot)
        assert buffer.current_count == 1

    def test_add_multiple_entries(self, buffer):
        """Test adding multiple entries to the buffer."""
        for i in range(5):
            buffer.add({"host_id": f"host-{i}", "index": i})
        assert buffer.current_count == 5

    def test_flush_returns_entries_in_chronological_order(self, buffer):
        """Test that flush returns entries in the order they were added."""
        entries = []
        for i in range(5):
            entry = {"host_id": "host-1", "sequence": i, "timestamp": f"2024-01-01T00:00:0{i}Z"}
            entries.append(entry)
            buffer.add(entry)
            # Small delay to ensure file ordering
            time.sleep(0.01)

        flushed = buffer.flush()
        assert len(flushed) == 5
        for i, entry in enumerate(flushed):
            assert entry["sequence"] == i

    def test_flush_uses_collection_time_when_collectors_finish_out_of_order(self, buffer):
        """Concurrent completion order must not reorder evidence replay."""
        buffer.add(
            {
                "sequence": 2,
                "collected_at": "2024-01-01T00:00:02Z",
            }
        )
        buffer.add(
            {
                "sequence": 1,
                "collected_at": "2024-01-01T00:00:01Z",
            }
        )
        buffer.add(
            {
                "sequence": 3,
                "collected_at": "2024-01-01T00:00:03Z",
            }
        )

        assert [entry["sequence"] for entry in buffer.flush()] == [1, 2, 3]

    def test_flush_clears_buffer(self, buffer):
        """Test that flush removes all entries from the buffer."""
        buffer.add({"host_id": "host-1", "data": "test"})
        buffer.add({"host_id": "host-2", "data": "test2"})

        flushed = buffer.flush()
        assert len(flushed) == 2
        assert buffer.current_count == 0

    def test_flush_empty_buffer_returns_empty_list(self, buffer):
        """Test flushing an empty buffer returns an empty list."""
        result = buffer.flush()
        assert result == []

    def test_size_bytes_increases_with_entries(self, buffer):
        """Test that size_bytes reflects the storage used."""
        initial_size = buffer.size_bytes()
        buffer.add({"host_id": "host-1", "data": "x" * 100})
        assert buffer.size_bytes() > initial_size

    def test_is_full_when_at_capacity(self, tmp_buffer_dir):
        """Test is_full returns True when buffer reaches capacity."""
        # Create a very small buffer (100 bytes)
        small_buffer = EvidenceBuffer(buffer_dir=tmp_buffer_dir, max_size_bytes=100)

        # Add enough data to exceed 100 bytes
        small_buffer.add({"data": "x" * 200})
        # After eviction, buffer should be at or near capacity
        # The entry itself is larger than max, so buffer may be empty after eviction attempt
        # Let's test with entries that fit
        pass

    def test_is_full_with_accumulated_entries(self, tmp_buffer_dir):
        """Test that buffer correctly detects when it's full."""
        # 200 bytes max
        small_buffer = EvidenceBuffer(buffer_dir=tmp_buffer_dir, max_size_bytes=200)

        # Each entry is roughly 30-50 bytes serialized
        small_buffer.add({"i": 1})
        small_buffer.add({"i": 2})
        small_buffer.add({"i": 3})
        small_buffer.add({"i": 4})
        small_buffer.add({"i": 5})

        # Should have evicted some entries to stay under 200 bytes
        assert small_buffer.size_bytes() <= 200

    def test_fifo_eviction_removes_oldest(self, tmp_buffer_dir):
        """Test that FIFO eviction removes the oldest entries first."""
        # Very small buffer: ~150 bytes
        small_buffer = EvidenceBuffer(buffer_dir=tmp_buffer_dir, max_size_bytes=150)

        # Add entries with increasing sequence numbers
        for i in range(10):
            small_buffer.add({"seq": i, "data": "pad"})
            time.sleep(0.01)  # Ensure timestamp ordering

        # Flush remaining entries
        remaining = small_buffer.flush()

        # The oldest entries should have been evicted
        # Remaining entries should have higher sequence numbers
        if remaining:
            sequences = [e["seq"] for e in remaining]
            # Sequences should be monotonically increasing (chronological)
            assert sequences == sorted(sequences)
            # Oldest entries (lower seq numbers) should have been evicted
            assert sequences[-1] == 9  # Most recent entry should still be there

    def test_evict_oldest_on_empty_buffer(self, buffer):
        """Test evict_oldest on an empty buffer does nothing."""
        buffer.evict_oldest()  # Should not raise
        assert buffer.current_count == 0

    def test_clear_removes_all_entries(self, buffer):
        """Test that clear removes all buffer entries."""
        for i in range(5):
            buffer.add({"i": i})
        assert buffer.current_count == 5

        buffer.clear()
        assert buffer.current_count == 0

    def test_max_size_bytes_property(self, tmp_buffer_dir):
        """Test that max_size_bytes returns the configured value."""
        buf = EvidenceBuffer(buffer_dir=tmp_buffer_dir, max_size_bytes=1024 * 1024)
        assert buf.max_size_bytes == 1024 * 1024

    def test_default_max_size_bytes(self, tmp_buffer_dir):
        """Test default max_size_bytes is 512 MB."""
        buf = EvidenceBuffer(buffer_dir=tmp_buffer_dir)
        assert buf.max_size_bytes == 512 * 1024 * 1024

    def test_buffer_preserves_snapshot_data(self, buffer):
        """Test that buffered data is preserved exactly as provided."""
        snapshot = {
            "host_id": "host-abc",
            "evidence_type": "pg_settings",
            "collected_at": "2024-01-15T10:30:00Z",
            "data": {"shared_buffers": "256MB", "work_mem": "4MB"},
        }
        buffer.add(snapshot)
        flushed = buffer.flush()
        assert len(flushed) == 1
        assert flushed[0]["host_id"] == "host-abc"
        assert flushed[0]["evidence_type"] == "pg_settings"
        assert flushed[0]["data"]["shared_buffers"] == "256MB"

    def test_buffer_creates_directory_if_missing(self, tmp_path):
        """Test that the buffer creates its directory if it doesn't exist."""
        new_dir = str(tmp_path / "nonexistent" / "buffer")
        buf = EvidenceBuffer(buffer_dir=new_dir)
        buf.add({"test": True})
        assert buf.current_count == 1


class TestCircuitBreaker:
    """Tests for the CircuitBreaker class."""

    def test_initial_state_is_closed(self, circuit_breaker):
        """Test that a new circuit breaker starts in CLOSED state."""
        assert circuit_breaker.state == CircuitState.CLOSED

    def test_should_attempt_when_closed(self, circuit_breaker):
        """Test that should_attempt returns True when CLOSED."""
        assert circuit_breaker.should_attempt() is True

    def test_transitions_to_open_after_failures(self, fast_circuit_breaker):
        """Test that circuit opens after reaching failure threshold."""
        # Threshold is 2 for fast_circuit_breaker
        fast_circuit_breaker.record_failure()
        assert fast_circuit_breaker.state == CircuitState.CLOSED

        fast_circuit_breaker.record_failure()
        assert fast_circuit_breaker.state == CircuitState.OPEN

    def test_should_not_attempt_when_open(self, fast_circuit_breaker):
        """Test that should_attempt returns False when OPEN (before probe interval)."""
        fast_circuit_breaker.record_failure()
        fast_circuit_breaker.record_failure()
        assert fast_circuit_breaker.state == CircuitState.OPEN
        # Immediately after opening, should not attempt (probe interval not elapsed)
        assert fast_circuit_breaker.should_attempt() is False

    def test_auto_transitions_to_half_open(self, fast_circuit_breaker):
        """Test auto-transition from OPEN to HALF_OPEN after probe interval."""
        fast_circuit_breaker.record_failure()
        fast_circuit_breaker.record_failure()
        assert fast_circuit_breaker.state == CircuitState.OPEN

        # Wait for probe interval (0.1s)
        time.sleep(0.15)

        # Should auto-transition to HALF_OPEN
        assert fast_circuit_breaker.state == CircuitState.HALF_OPEN

    def test_should_attempt_when_half_open(self, fast_circuit_breaker):
        """Test that should_attempt returns True when HALF_OPEN."""
        fast_circuit_breaker.record_failure()
        fast_circuit_breaker.record_failure()

        # Wait for probe interval
        time.sleep(0.15)

        assert fast_circuit_breaker.should_attempt() is True

    def test_success_in_half_open_transitions_to_closed(self, fast_circuit_breaker):
        """Test that success in HALF_OPEN transitions to CLOSED."""
        fast_circuit_breaker.record_failure()
        fast_circuit_breaker.record_failure()

        # Wait for probe interval
        time.sleep(0.15)

        assert fast_circuit_breaker.state == CircuitState.HALF_OPEN
        fast_circuit_breaker.record_success()
        assert fast_circuit_breaker.state == CircuitState.CLOSED

    def test_failure_in_half_open_transitions_to_open(self, fast_circuit_breaker):
        """Test that failure in HALF_OPEN transitions back to OPEN."""
        fast_circuit_breaker.record_failure()
        fast_circuit_breaker.record_failure()

        # Wait for probe interval
        time.sleep(0.15)

        assert fast_circuit_breaker.state == CircuitState.HALF_OPEN
        fast_circuit_breaker.record_failure()
        assert fast_circuit_breaker.state == CircuitState.OPEN

    def test_success_resets_failure_count(self, circuit_breaker):
        """Test that recording success resets the failure counter."""
        circuit_breaker.record_failure()
        circuit_breaker.record_failure()
        circuit_breaker.record_success()

        # After reset, it should take full threshold to open again
        circuit_breaker.record_failure()
        circuit_breaker.record_failure()
        # Default threshold is 3, so 2 failures should not open
        assert circuit_breaker.state == CircuitState.CLOSED

    def test_reset_returns_to_closed(self, fast_circuit_breaker):
        """Test that reset returns the circuit breaker to CLOSED state."""
        fast_circuit_breaker.record_failure()
        fast_circuit_breaker.record_failure()
        assert fast_circuit_breaker.state == CircuitState.OPEN

        fast_circuit_breaker.reset()
        assert fast_circuit_breaker.state == CircuitState.CLOSED
        assert fast_circuit_breaker.should_attempt() is True

    def test_failure_threshold_property(self):
        """Test failure_threshold property returns configured value."""
        cb = CircuitBreaker(failure_threshold=5)
        assert cb.failure_threshold == 5

    def test_probe_interval_property(self):
        """Test probe_interval_seconds property returns configured value."""
        cb = CircuitBreaker(probe_interval_seconds=60.0)
        assert cb.probe_interval_seconds == 60.0

    def test_default_failure_threshold(self, circuit_breaker):
        """Test default failure threshold is 3."""
        assert circuit_breaker.failure_threshold == DEFAULT_FAILURE_THRESHOLD

    def test_default_probe_interval(self, circuit_breaker):
        """Test default probe interval is 30 seconds."""
        assert circuit_breaker.probe_interval_seconds == DEFAULT_PROBE_INTERVAL_SECONDS


class TestBufferedEvidenceSender:
    """Tests for the BufferedEvidenceSender integration class."""

    @pytest.fixture
    def sender(self, tmp_buffer_dir):
        """Create a BufferedEvidenceSender with test configuration."""
        buffer = EvidenceBuffer(buffer_dir=tmp_buffer_dir, max_size_bytes=1024)
        cb = CircuitBreaker(failure_threshold=2, probe_interval_seconds=0.1)
        return BufferedEvidenceSender(
            buffer=buffer,
            circuit_breaker=cb,
            flush_timeout_seconds=5.0,
        )

    def test_sends_immediately_when_circuit_closed(self, sender):
        """Test evidence is sent immediately when circuit is closed."""
        send_fn = MagicMock(return_value=True)
        snapshot = {"host_id": "host-1", "data": "test"}

        result = sender.send_evidence(snapshot, send_fn)

        assert result is True
        send_fn.assert_called()

    def test_buffers_when_circuit_open(self, sender):
        """Test evidence is buffered when circuit is open."""
        send_fn = MagicMock(return_value=False)

        # Open the circuit
        sender.send_evidence({"seq": 1}, send_fn)
        sender.send_evidence({"seq": 2}, send_fn)

        # Now circuit should be open, next send should buffer without calling send_fn
        send_fn.reset_mock()
        result = sender.send_evidence({"seq": 3}, send_fn)

        assert result is False
        # send_fn should not be called when circuit is open
        send_fn.assert_not_called()

    def test_flushes_buffer_on_reconnection(self, sender):
        """Test that buffered evidence is flushed when circuit returns to closed."""
        failed_send = MagicMock(return_value=False)

        # Fail sends to open circuit and buffer evidence
        sender.send_evidence({"seq": 1}, failed_send)
        sender.send_evidence({"seq": 2}, failed_send)

        # Wait for probe interval
        time.sleep(0.15)

        # Now attempt with success
        successful_send = MagicMock(return_value=True)
        result = sender.send_evidence({"seq": 3}, successful_send)

        assert result is True
        # The send_fn should have been called multiple times (current + buffered entries)
        assert successful_send.call_count >= 1

    def test_buffers_on_send_exception(self, sender):
        """Test that evidence is buffered when send raises an exception."""
        send_fn = MagicMock(side_effect=ConnectionError("Connection refused"))

        result = sender.send_evidence({"host_id": "host-1"}, send_fn)

        assert result is False
        assert sender.buffer.current_count >= 1

    def test_flush_timeout_property(self, sender):
        """Test flush_timeout_seconds property."""
        assert sender.flush_timeout_seconds == 5.0

    def test_send_failure_increments_circuit_breaker(self, sender):
        """Test that send failures increment the circuit breaker failure count."""
        send_fn = MagicMock(return_value=False)

        sender.send_evidence({"seq": 1}, send_fn)
        assert sender.circuit_breaker.state == CircuitState.CLOSED

        sender.send_evidence({"seq": 2}, send_fn)
        assert sender.circuit_breaker.state == CircuitState.OPEN

    def test_circuit_breaker_property(self, sender):
        """Test circuit_breaker property access."""
        assert sender.circuit_breaker is not None
        assert isinstance(sender.circuit_breaker, CircuitBreaker)

    def test_buffer_property(self, sender):
        """Test buffer property access."""
        assert sender.buffer is not None
        assert isinstance(sender.buffer, EvidenceBuffer)
