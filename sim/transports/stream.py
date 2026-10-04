"""Bounded non-blocking byte streams with protocol-neutral record framing."""

from __future__ import annotations

from collections import deque
import math
import select
import time
from typing import Iterator


class StreamTransport:
    """Borrow a socket and retain ordered partial writes without blocking.

    The caller owns socket lifetime. Receive performs one 4096-byte read per
    call; incomplete records and unsent suffixes remain attached to this
    connection, including when the caller resets its application session.
    """

    def __init__(self, sock, *, max_pending: int = 256,
                 queue_name: str = "stream", endpoint_name: str = "stream",
                 writable_name: str | None = None, clock=time, poller=select):
        if type(max_pending) is not int or max_pending <= 0:
            raise ValueError("max_pending must be a positive integer")
        self.sock = sock
        self.buffer = bytearray()
        self.tx_queue: deque[memoryview] = deque()
        self.max_pending = max_pending
        self.queue_name = queue_name
        self.endpoint_name = endpoint_name
        self.writable_name = writable_name or endpoint_name
        self.clock = clock
        self.poller = poller

    def _check_tx_capacity(self) -> None:
        if len(self.tx_queue) >= self.max_pending:
            self.flush()
        if len(self.tx_queue) >= self.max_pending:
            raise BufferError(f"{self.queue_name} TX queue is full")

    def _enqueue(self, data: bytes) -> None:
        """Enqueue after capacity admission and immediately attempt a write."""
        self.tx_queue.append(memoryview(data))
        self.flush()

    def send(self, data: bytes) -> None:
        self._check_tx_capacity()
        self._enqueue(data)

    @property
    def tx_pending(self) -> bool:
        return bool(self.tx_queue)

    def flush(self) -> bool:
        while self.tx_queue:
            view = self.tx_queue[0]
            try:
                written = self.sock.send(view)
            except BlockingIOError:
                return False
            if written == 0:
                raise ConnectionError(f"{self.endpoint_name} socket closed")
            if written < len(view):
                self.tx_queue[0] = view[written:]
                return False
            self.tx_queue.popleft()
        return True

    def drain(self, timeout: float = 1.0) -> None:
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be finite and non-negative")
        deadline = self.clock.monotonic() + timeout
        while not self.flush():
            remaining = deadline - self.clock.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{self.queue_name} TX queue did not drain")
            _, writable, _ = self.poller.select([], [self.sock], [], remaining)
            if not writable:
                raise TimeoutError(f"{self.writable_name} socket is not writable")

    def read_once(self) -> None:
        try:
            data = self.sock.recv(4096)
        except BlockingIOError:
            return
        if not data:
            raise ConnectionError(f"{self.endpoint_name} socket closed")
        self.buffer.extend(data)


class LengthPrefixedStream(StreamTransport):
    """Frame records with a four-byte little-endian body length.

    Records include the prefix. Yielding each record before taking the next
    lets a consumer validate it without losing later buffered records.
    """

    def __init__(self, sock, *, min_length: int, max_length: int,
                 length_error: str = "invalid stream frame length", **kwargs):
        if (type(min_length) is not int or type(max_length) is not int or
                not 0 <= min_length <= max_length):
            raise ValueError("invalid stream frame length range")
        super().__init__(sock, **kwargs)
        self.min_length = min_length
        self.max_length = max_length
        self.length_error = length_error

    def receive_records(self) -> Iterator[bytes]:
        self.read_once()
        while len(self.buffer) >= 4:
            length = int.from_bytes(self.buffer[:4], "little")
            if not self.min_length <= length <= self.max_length:
                raise ValueError(self.length_error)
            if len(self.buffer) < 4 + length:
                break
            raw = bytes(self.buffer[:4 + length])
            del self.buffer[:4 + length]
            yield raw


class FixedRecordStream(StreamTransport):
    """Frame opaque fixed-width records without interpreting their bytes."""

    def __init__(self, sock, *, record_size: int, **kwargs):
        if type(record_size) is not int or record_size <= 0:
            raise ValueError("record_size must be a positive integer")
        super().__init__(sock, **kwargs)
        self.record_size = record_size

    def receive_records(self) -> Iterator[bytes]:
        self.read_once()
        while len(self.buffer) >= self.record_size:
            raw = bytes(self.buffer[:self.record_size])
            del self.buffer[:self.record_size]
            yield raw
