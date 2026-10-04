"""Deterministic host CAN delivery, without electrical arbitration.

Frames are broadcast in attachment order after a fixed virtual delay. Timestamp
metadata never advances a CPU or creates a global simulation barrier. The caller
owns endpoints and any streaming log sink; the bus retains only pending frames.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import heapq
from typing import Callable, Protocol

from sim.core.clock import check_time

from .frame import CanFrame


class CanEndpoint(Protocol):
    def receive(self, frame: CanFrame) -> None: ...


@dataclass(frozen=True)
class CanDelivery:
    """One destination decision; ``frame`` retains its original timestamp."""

    source: str
    destination: str
    frame: CanFrame
    sequence: int
    timestamp_ns: int
    due_ns: int
    delivered_ns: int
    late: bool
    dropped: bool


@dataclass(frozen=True)
class _Attached:
    endpoint: CanEndpoint
    name: str
    receive: Callable[[CanFrame], None]


def _can_time(t_sim_ns: int, previous_ns: int | None = None) -> None:
    check_time(t_sim_ns, previous_ns, strict=False)
    if t_sim_ns > 0xFFFFFFFFFFFFFFFF:
        raise ValueError("CAN time must fit the unsigned 64-bit timestamp")


class CanBus:
    """Bounded virtual CAN broadcasts ordered by (due time, enqueue sequence).

    A step dispatches at most ``dispatch_budget`` queued broadcasts, including
    callback-generated broadcasts. Remaining frames are visible through
    ``pending``. Late input is delivered at the current bus time. Callback errors
    propagate; an already popped broadcast is not replayed or rolled back.
    """

    def __init__(self, *, delay_ns: int = 0, loopback: bool = False,
                 drop: Callable[[CanEndpoint, CanEndpoint, CanFrame], bool]
                 | None = None,
                 log: Callable[[CanDelivery], None] | None = None,
                 max_pending: int = 256, dispatch_budget: int = 256):
        _can_time(delay_ns)
        if type(loopback) is not bool:
            raise ValueError("loopback must be a boolean")
        if drop is not None and not callable(drop):
            raise ValueError("drop must be callable or None")
        if log is not None and not callable(log):
            raise ValueError("log must be callable or None")
        for name, value in (("max_pending", max_pending),
                            ("dispatch_budget", dispatch_budget)):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.delay_ns = delay_ns
        self.loopback = loopback
        self.drop = drop
        self.log = log
        self.max_pending = max_pending
        self.dispatch_budget = dispatch_budget
        self.t_sim_ns = 0
        self._endpoints: dict[int, _Attached] = {}
        self._queue: list[tuple[int, int, _Attached, CanFrame]] = []
        self._sequence = 0
        self._started = False
        self._dispatching = False

    @property
    def pending(self) -> int:
        return len(self._queue)

    def has_due(self, t_sim_ns: int) -> bool:
        """Query due work without consuming frames or advancing virtual time."""
        _can_time(t_sim_ns)
        return bool(self._queue and self._queue[0][0] <= t_sim_ns)

    def attach(self, endpoint: CanEndpoint, name: str | None = None) -> None:
        """Borrow one endpoint; wiring is fixed after the first send or step."""
        if self._started:
            raise RuntimeError("CAN endpoints must attach before the first send or step")
        if id(endpoint) in self._endpoints:
            raise ValueError("CAN endpoint is already attached")
        receive = getattr(endpoint, "receive", None)
        if not callable(receive):
            raise ValueError("CAN endpoint must expose callable receive(frame)")
        if name is None:
            name = f"endpoint-{len(self._endpoints) + 1}"
        if not isinstance(name, str) or not name:
            raise ValueError("CAN endpoint name must be a nonempty string")
        if any(item.name == name for item in self._endpoints.values()):
            raise ValueError("CAN endpoint name is already attached")
        self._endpoints[id(endpoint)] = _Attached(endpoint, name, receive)

    def send(self, source: CanEndpoint, frame: CanFrame) -> None:
        """Enqueue one complete frame or explicitly reject admission."""
        attached = self._endpoints.get(id(source))
        if attached is None or attached.endpoint is not source:
            raise ValueError("CAN source is not attached")
        if not isinstance(frame, CanFrame):
            raise ValueError("CAN send requires a CanFrame")
        due_ns = frame.timestamp_ns + self.delay_ns
        _can_time(due_ns)
        if len(self._queue) >= self.max_pending:
            raise BufferError("CAN pending frame capacity exhausted")
        heapq.heappush(self._queue, (due_ns, self._sequence, attached, frame))
        self._sequence += 1
        self._started = True

    def step(self, t_sim_ns: int) -> int:
        """Advance virtual delivery time, returning dispatched broadcast count."""
        _can_time(t_sim_ns, self.t_sim_ns)
        if self._dispatching:
            raise RuntimeError("CAN step cannot be called from a delivery callback")
        self.t_sim_ns = t_sim_ns
        self._started = True
        dispatched = 0
        self._dispatching = True
        try:
            while (self._queue and self._queue[0][0] <= t_sim_ns
                   and dispatched < self.dispatch_budget):
                due_ns, sequence, source, original = heapq.heappop(self._queue)
                delivered_ns = max(due_ns, self.t_sim_ns)
                frame = replace(original, timestamp_ns=delivered_ns)
                dispatched += 1
                for destination in self._endpoints.values():
                    if not self.loopback and destination.endpoint is source.endpoint:
                        continue
                    dropped = bool(self.drop and self.drop(
                        source.endpoint, destination.endpoint, original))
                    if self.log is not None:
                        self.log(CanDelivery(
                            source.name, destination.name, original, sequence,
                            original.timestamp_ns, due_ns, delivered_ns,
                            due_ns < delivered_ns, dropped))
                    if not dropped:
                        destination.receive(frame)
        finally:
            self._dispatching = False
        return dispatched

    def reset(self, t_sim_ns: int = 0) -> None:
        """Clear logical delivery state while preserving borrowed wiring."""
        _can_time(t_sim_ns)
        if self._dispatching:
            raise RuntimeError("CAN reset cannot be called from a delivery callback")
        self._queue.clear()
        self._sequence = 0
        self.t_sim_ns = t_sim_ns


class VirtualCanDevice:
    """Borrow a bus; concrete devices implement their own receive behavior."""

    def __init__(self, bus: CanBus, name: str | None = None):
        self.bus = bus
        bus.attach(self, name)

    def emit(self, frame: CanFrame) -> None:
        self.bus.send(self, frame)

    def receive(self, frame: CanFrame) -> None:
        raise NotImplementedError
