"""
Local evidence buffering and flush logic for the Host Agent.

Provides file-based evidence buffering when the Host Agent loses connectivity
to the Control Plane, with FIFO eviction at capacity and circuit breaker
pattern for managing connection state transitions.

Requirements: 6.6, 6.8, 6.9
"""

import enum
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Default maximum buffer size: 512 MB
DEFAULT_MAX_BUFFER_BYTES = 512 * 1024 * 1024

# Default flush timeout: 30 seconds
DEFAULT_FLUSH_TIMEOUT_SECONDS = 30

# Default circuit breaker configuration
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_PROBE_INTERVAL_SECONDS = 30.0


class CircuitState(str, enum.Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation, evidence sent immediately
    OPEN = "open"  # Disconnected, buffer evidence locally
    HALF_OPEN = "half_open"  # Probing, attempt one send


@dataclass
class BufferEntry:
    """A single buffered evidence entry with metadata."""

    timestamp: float  # Unix timestamp for ordering
    data: dict  # The evidence snapshot
    size_bytes: int  # Size of the serialized entry on disk


class EvidenceBuffer:
    """
    File-based evidence buffer for the Host Agent.

    Buffers evidence locally when the agent cannot reach the Control Plane.
    Uses a directory of individual JSON files to store evidence, with FIFO
    eviction when the buffer reaches its maximum capacity (512 MB default).

    Evidence is flushed in chronological order upon reconnection.
    """

    def __init__(
        self,
        buffer_dir: str = "/tmp/host-agent-buffer",
        max_size_bytes: int = DEFAULT_MAX_BUFFER_BYTES,
    ):
        self._buffer_dir = Path(buffer_dir)
        self._max_size_bytes = max_size_bytes
        self._lock = threading.Lock()

        # Create buffer directory if it doesn't exist
        self._buffer_dir.mkdir(parents=True, exist_ok=True)

    @property
    def max_size_bytes(self) -> int:
        """Maximum buffer capacity in bytes."""
        return self._max_size_bytes

    @property
    def current_count(self) -> int:
        """Number of evidence entries currently in the buffer."""
        with self._lock:
            return self._count_entries()

    def _count_entries(self) -> int:
        """Count entries without acquiring lock (internal use)."""
        if not self._buffer_dir.exists():
            return 0
        return len(list(self._buffer_dir.glob("*.json")))

    def _get_sorted_files(self) -> List[Path]:
        """Get buffer files sorted by filename (chronological order)."""
        if not self._buffer_dir.exists():
            return []
        files = list(self._buffer_dir.glob("*.json"))
        files.sort(key=lambda f: f.name)
        return files

    def size_bytes(self) -> int:
        """Returns current total buffer size in bytes."""
        with self._lock:
            return self._calculate_size()

    def _calculate_size(self) -> int:
        """Calculate total size without acquiring lock (internal use)."""
        total = 0
        for f in self._get_sorted_files():
            try:
                total += f.stat().st_size
            except OSError:
                continue
        return total

    def is_full(self) -> bool:
        """Returns True if the buffer has reached or exceeded max capacity."""
        return self.size_bytes() >= self._max_size_bytes

    def add(self, snapshot: dict) -> None:
        """
        Add an evidence snapshot to the buffer.

        If the buffer is at capacity, evicts the oldest entries (FIFO)
        until there is room for the new entry.

        Args:
            snapshot: Evidence snapshot dictionary to buffer.
        """
        serialized = json.dumps(snapshot, default=str, sort_keys=True)
        entry_size = len(serialized.encode("utf-8"))

        with self._lock:
            # Evict oldest entries if adding this would exceed capacity
            current_size = self._calculate_size()
            while current_size + entry_size > self._max_size_bytes:
                evicted = self._evict_oldest_entry()
                if not evicted:
                    break  # No more entries to evict
                current_size = self._calculate_size()

            # Generate filename using timestamp for chronological ordering
            # Use monotonic counter suffix to handle sub-microsecond additions
            timestamp = time.time()
            filename = f"{timestamp:.9f}_{os.getpid()}_{id(snapshot)}.json"
            filepath = self._buffer_dir / filename

            # Ensure unique filename
            while filepath.exists():
                timestamp += 0.000000001
                filename = f"{timestamp:.9f}_{os.getpid()}_{id(snapshot)}.json"
                filepath = self._buffer_dir / filename

            filepath.write_text(serialized, encoding="utf-8")

    def flush(self) -> List[dict]:
        """
        Return all buffered evidence in chronological order and clear the buffer.

        Returns:
            List of evidence snapshot dictionaries in chronological order.
        """
        with self._lock:
            entries = []
            files = self._get_sorted_files()

            for f in files:
                try:
                    content = f.read_text(encoding="utf-8")
                    entry = json.loads(content)
                    entries.append(entry)
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"Failed to read buffer entry {f}: {e}")
                    continue
                finally:
                    # Remove file after reading
                    try:
                        f.unlink()
                    except OSError:
                        pass

            return entries

    def evict_oldest(self) -> None:
        """Remove oldest entries until buffer is under capacity."""
        with self._lock:
            while self._calculate_size() >= self._max_size_bytes:
                if not self._evict_oldest_entry():
                    break

    def _evict_oldest_entry(self) -> bool:
        """
        Remove the single oldest entry from the buffer.

        Returns:
            True if an entry was evicted, False if buffer is empty.
        """
        files = self._get_sorted_files()
        if not files:
            return False

        oldest = files[0]
        try:
            oldest.unlink()
            logger.debug(f"Evicted oldest buffer entry: {oldest.name}")
            return True
        except OSError as e:
            logger.warning(f"Failed to evict buffer entry {oldest}: {e}")
            return False

    def clear(self) -> None:
        """Remove all entries from the buffer."""
        with self._lock:
            for f in self._get_sorted_files():
                try:
                    f.unlink()
                except OSError:
                    pass


class CircuitBreaker:
    """
    Circuit breaker for managing Control Plane connectivity.

    States:
      - CLOSED: Normal operation. Evidence is sent immediately to the Control Plane.
      - OPEN: Disconnected. Evidence is buffered locally.
      - HALF_OPEN: Probing. One attempt is made to send; success transitions
        to CLOSED, failure transitions back to OPEN.

    The circuit breaker transitions from OPEN to HALF_OPEN automatically
    after the probe interval elapses.
    """

    def __init__(
        self,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        probe_interval_seconds: float = DEFAULT_PROBE_INTERVAL_SECONDS,
    ):
        self._failure_threshold = failure_threshold
        self._probe_interval_seconds = probe_interval_seconds
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: Optional[float] = None
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        """Current circuit breaker state (with auto-transition check)."""
        with self._lock:
            self._check_auto_transition()
            return self._state

    @property
    def failure_threshold(self) -> int:
        """Number of consecutive failures before opening the circuit."""
        return self._failure_threshold

    @property
    def probe_interval_seconds(self) -> float:
        """Seconds to wait in OPEN state before probing (HALF_OPEN)."""
        return self._probe_interval_seconds

    def _check_auto_transition(self) -> None:
        """
        Check if OPEN state should auto-transition to HALF_OPEN.
        Must be called with lock held.
        """
        if self._state == CircuitState.OPEN and self._last_failure_time is not None:
            elapsed = time.time() - self._last_failure_time
            if elapsed >= self._probe_interval_seconds:
                self._state = CircuitState.HALF_OPEN
                logger.info(
                    f"Circuit breaker auto-transitioned to HALF_OPEN "
                    f"after {elapsed:.1f}s probe interval"
                )

    def record_success(self) -> None:
        """
        Record a successful send operation.

        Transitions to CLOSED state and resets the failure counter.
        """
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._last_failure_time = None
            logger.debug("Circuit breaker: success recorded, state -> CLOSED")

    def record_failure(self) -> None:
        """
        Record a failed send operation.

        Increments failure count. If the failure threshold is reached,
        transitions to OPEN state.
        """
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                # Failed probe, go back to OPEN
                self._state = CircuitState.OPEN
                logger.info("Circuit breaker: probe failed, state -> OPEN")
            elif self._failure_count >= self._failure_threshold:
                self._state = CircuitState.OPEN
                logger.info(
                    f"Circuit breaker: failure threshold ({self._failure_threshold}) "
                    f"reached, state -> OPEN"
                )

    def should_attempt(self) -> bool:
        """
        Determine if a send attempt should be made.

        Returns:
            True if the circuit allows an attempt:
            - CLOSED: always True
            - OPEN: True only if probe interval has elapsed (auto-transitions to HALF_OPEN)
            - HALF_OPEN: True (one probe attempt allowed)
        """
        with self._lock:
            self._check_auto_transition()

            if self._state == CircuitState.CLOSED:
                return True
            elif self._state == CircuitState.HALF_OPEN:
                return True
            else:
                # OPEN state, not yet time for probe
                return False

    def reset(self) -> None:
        """Reset the circuit breaker to initial CLOSED state."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._last_failure_time = None


class BufferedEvidenceSender:
    """
    Integration logic combining EvidenceBuffer and CircuitBreaker.

    When the circuit is CLOSED, evidence is sent immediately to the Control Plane.
    When OPEN, evidence is buffered locally.
    When transitioning back to CLOSED, buffered evidence is flushed within 30 seconds.
    """

    def __init__(
        self,
        buffer: EvidenceBuffer,
        circuit_breaker: CircuitBreaker,
        flush_timeout_seconds: float = DEFAULT_FLUSH_TIMEOUT_SECONDS,
    ):
        self._buffer = buffer
        self._circuit_breaker = circuit_breaker
        self._flush_timeout_seconds = flush_timeout_seconds

    @property
    def buffer(self) -> EvidenceBuffer:
        """The evidence buffer."""
        return self._buffer

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """The circuit breaker."""
        return self._circuit_breaker

    @property
    def flush_timeout_seconds(self) -> float:
        """Maximum time allowed for flushing buffered evidence (seconds)."""
        return self._flush_timeout_seconds

    def send_evidence(self, snapshot: dict, send_fn) -> bool:
        """
        Send evidence to the Control Plane, with buffering on failure.

        If the circuit breaker allows an attempt, tries to send immediately.
        On success, transitions to CLOSED and flushes any buffered evidence.
        On failure, buffers the evidence locally.

        Args:
            snapshot: Evidence snapshot dictionary to send.
            send_fn: Callable that sends evidence to the Control Plane.
                     Should return True on success, False on failure.

        Returns:
            True if the evidence was sent successfully, False if buffered.
        """
        if not self._circuit_breaker.should_attempt():
            # Circuit is OPEN, buffer locally
            self._buffer.add(snapshot)
            logger.debug("Circuit OPEN: evidence buffered locally")
            return False

        # Attempt to send
        try:
            success = send_fn(snapshot)
        except Exception as e:
            logger.warning(f"Send failed with exception: {e}")
            success = False

        if success:
            self._circuit_breaker.record_success()
            # Flush any previously buffered evidence
            self._flush_buffered_evidence(send_fn)
            return True
        else:
            self._circuit_breaker.record_failure()
            self._buffer.add(snapshot)
            logger.debug("Send failed: evidence buffered locally")
            return False

    def _flush_buffered_evidence(self, send_fn) -> None:
        """
        Flush all buffered evidence to the Control Plane.

        Must complete within flush_timeout_seconds (default 30s).
        If individual evidence items fail to send, they are logged and skipped.

        Args:
            send_fn: Callable that sends evidence to the Control Plane.
        """
        start_time = time.time()
        buffered = self._buffer.flush()

        if not buffered:
            return

        logger.info(f"Flushing {len(buffered)} buffered evidence entries")

        for entry in buffered:
            # Check timeout
            elapsed = time.time() - start_time
            if elapsed >= self._flush_timeout_seconds:
                # Re-buffer remaining entries
                logger.warning(
                    f"Flush timeout ({self._flush_timeout_seconds}s) reached. "
                    f"Re-buffering remaining entries."
                )
                # Re-buffer entries that weren't sent yet
                remaining_idx = buffered.index(entry)
                for remaining in buffered[remaining_idx:]:
                    self._buffer.add(remaining)
                break

            try:
                success = send_fn(entry)
                if not success:
                    logger.warning("Failed to flush evidence entry, skipping")
            except Exception as e:
                logger.warning(f"Exception during flush: {e}, skipping entry")
