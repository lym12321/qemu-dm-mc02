"""Borrowed QEMU CAN transport; ingress never advances virtual time.

The caller supplies the existing RESET-origin mapper. This endpoint owns
neither transport queues nor sockets or their cleanup.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from sim.fabric.can import CanBus, check_can_time
from sim.fabric.frame import CanFrame


class QemuCanStream(Protocol):
    def send(self, can_id: int, flags: int, dlc: int,
             timestamp_ns: int, data: bytes) -> None: ...


class TimestampMapper(Protocol):
    qemu_origin_ns: int

    def map(self, qemu_timestamp_ns: int, fallback_ns: int) -> int: ...

    def unmap(self, worker_timestamp_ns: int, fallback_ns: int) -> int: ...


class QemuCanEndpoint:
    """Translate fixed-record transport timestamps to/from one CAN bus epoch."""

    def __init__(self, bus: CanBus, stream: QemuCanStream,
                 name: str = "qemu"):
        if not isinstance(bus, CanBus):
            raise TypeError("QEMU CAN endpoint requires a CanBus")
        send = getattr(stream, "send", None)
        if not callable(send):
            raise TypeError("QEMU CAN stream requires callable send")
        self.bus = bus
        self._stream = stream
        self._send = send
        self._mapper: TimestampMapper | None = None
        self._submitted_frames = 0
        bus.attach(self, name)

    @property
    def stream(self) -> QemuCanStream:
        """Return the actual borrowed stream without transferring ownership."""
        return self._stream

    @property
    def submitted_frames(self) -> int:
        """Count successful send returns, including records still queued.

        Logical resets and mapper changes preserve this endpoint-lifetime
        count. A failed send may have IO effects without incrementing it;
        callers must freeze after failure rather than infer receipt or retry.
        """
        return self._submitted_frames

    def bind_mapper(self, mapper: TimestampMapper) -> None:
        """Bind a validated RESET mapper without changing transport bytes."""
        if any(not callable(getattr(mapper, name, None))
               for name in ("map", "unmap")):
            raise TypeError("QEMU CAN mapper requires callable map and unmap")
        check_can_time(mapper.qemu_origin_ns)
        self._mapper = mapper

    def ingest(self, frame: CanFrame, current_time_ns: int) -> None:
        """Enqueue QEMU ingress; future/late delivery is decided by the bus."""
        if not isinstance(frame, CanFrame):
            raise TypeError("QEMU CAN ingress requires a CanFrame")
        check_can_time(current_time_ns)
        if self._mapper is None:
            raise RuntimeError("QEMU CAN endpoint requires a RESET mapper")
        timestamp_ns = self._mapper.map(frame.timestamp_ns, current_time_ns)
        check_can_time(timestamp_ns)
        self.bus.send(self, replace(frame, timestamp_ns=timestamp_ns))

    def receive(self, frame: CanFrame) -> None:
        if self._mapper is None:
            raise RuntimeError("QEMU CAN endpoint requires a RESET mapper")
        timestamp_ns = self._mapper.unmap(
            frame.timestamp_ns, self._mapper.qemu_origin_ns)
        check_can_time(timestamp_ns)
        self._send(frame.can_id, frame.flags, frame.dlc,
                   timestamp_ns, frame.data)
        self._submitted_frames += 1
