"""Bounded opaque datagram writes; partial datagrams are never retried."""

from __future__ import annotations

from collections import deque
import math
import select
import time


def send_datagram(sock, payload: bytes, *, name: str = "datagram") -> None:
    try:
        written = sock.send(payload)
    except BlockingIOError as exc:
        raise BufferError(f"{name} TX is backpressured") from exc
    if written != len(payload):
        raise ConnectionError(f"short {name} datagram write")


class DatagramTransport:
    """Borrow a datagram socket and queue complete opaque records."""

    def __init__(self, sock, *, max_pending: int = 256,
                 name: str = "datagram", writable_name: str = "datagram",
                 clock=time, poller=select):
        if type(max_pending) is not int or max_pending <= 0:
            raise ValueError("max_pending must be a positive integer")
        self.sock = sock
        self.tx_queue: deque[bytes] = deque()
        self.max_pending = max_pending
        self.name = name
        self.writable_name = writable_name
        self.clock = clock
        self.poller = poller

    def _check_tx_capacity(self) -> None:
        if len(self.tx_queue) >= self.max_pending:
            self.flush()
        if len(self.tx_queue) >= self.max_pending:
            raise BufferError(f"{self.name} TX queue is full")

    def _enqueue(self, payload: bytes) -> None:
        self.tx_queue.append(payload)
        self.flush()

    def send(self, payload: bytes) -> None:
        self._check_tx_capacity()
        self._enqueue(payload)

    @property
    def tx_pending(self) -> bool:
        return bool(self.tx_queue)

    def flush(self) -> bool:
        while self.tx_queue:
            try:
                written = self.sock.send(self.tx_queue[0])
            except BlockingIOError:
                return False
            if written != len(self.tx_queue[0]):
                raise ConnectionError(f"short {self.name} datagram write")
            self.tx_queue.popleft()
        return True

    def drain(self, timeout: float = 1.0) -> None:
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be finite and non-negative")
        deadline = self.clock.monotonic() + timeout
        while not self.flush():
            remaining = deadline - self.clock.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{self.name} TX queue did not drain")
            _, writable, _ = self.poller.select([], [self.sock], [], remaining)
            if not writable:
                raise TimeoutError(f"{self.writable_name} is not writable")
