"""Ordered host CAN buses composed through the protocol-neutral core.

Each step settles commands at its start, advances devices to its target, then
settles target-time feedback. Future commands cannot affect an earlier device
step. Target-time backlog stays bounded and must settle at the next step start;
exhausting the start dispatch budget freezes the coordinator before device work.
This host schedule does not establish a QEMU CPU or physical CAN time barrier.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from sim.core import Coordinator, Session, SimParticipant, StepContext, StepResult

from .can import CanBus, check_can_time
from .frame import CanFrame


class TimestampMapper(Protocol):
    def map(self, timestamp_ns: int, fallback_ns: int) -> int: ...

    def unmap(self, timestamp_ns: int, fallback_ns: int) -> int: ...


class TransportEndpoint(Protocol):
    bus: CanBus

    def bind_mapper(self, mapper: TimestampMapper) -> None: ...

    def ingest(self, frame: CanFrame, current_time_ns: int) -> None: ...


class DatagramEndpoint(Protocol):
    bus: CanBus

    def ingest(self, raw: bytes, current_time_ns: int) -> None: ...


class CanParticipant:
    """Own device execution/cleanup; borrow one bus or an ordered bus tuple.

    Each phase visits each bus once. Callbacks may enqueue on other buses but
    must not advance/reset buses or reenter this participant. Start backlog on
    any bus fails before device work, including later-to-earlier callback work.
    Target backlog remains queued; there is no inter-bus arbitration or rollback.
    """

    def __init__(self, bus: CanBus | tuple[CanBus, ...],
                 devices: Iterable[SimParticipant] = ()):
        buses = (bus,) if isinstance(bus, CanBus) else bus
        if (type(buses) is not tuple or not buses
                or any(not isinstance(item, CanBus) for item in buses)):
            raise TypeError("CAN participant requires a CanBus or nonempty CanBus tuple")
        if len({id(item) for item in buses}) != len(buses):
            raise ValueError("each CAN bus must appear once")
        self.buses = buses
        self.devices = tuple(devices)
        if len({id(device) for device in self.devices}) != len(self.devices):
            raise ValueError("each CAN device must have a single owner")
        self._methods = []
        for device in self.devices:
            methods = tuple(getattr(device, name, None)
                            for name in ("reset", "step", "close"))
            if any(not callable(method) for method in methods):
                raise TypeError("CAN device requires reset, step and close")
            self._methods.append(methods)
        self._closed = False

    @property
    def bus(self) -> CanBus:
        """Preserve singular access only when exactly one bus was admitted."""
        if len(self.buses) != 1:
            raise RuntimeError("CAN participant has multiple buses; use buses")
        return self.buses[0]

    def reset(self, session: Session) -> None:
        if self._closed:
            raise RuntimeError("CAN participant is closed")
        if not isinstance(session, Session):
            raise TypeError("CAN reset requires a Session")
        check_can_time(session.t_sim_ns)
        for bus in self.buses:
            bus.reset(session.t_sim_ns)
        for index, (reset, _, _) in enumerate(self._methods):
            try:
                reset(session)
            except BaseException as exc:
                exc.add_note(f"CAN device {index} failed during reset")
                raise

    def step(self, ctx: StepContext) -> StepResult:
        if self._closed:
            raise RuntimeError("CAN participant is closed")
        if not isinstance(ctx, StepContext):
            raise TypeError("CAN step requires a StepContext")
        check_can_time(ctx.t_sim_ns)
        start_ns = ctx.t_sim_ns - ctx.dt_ns
        if any(bus.t_sim_ns != start_ns for bus in self.buses):
            raise RuntimeError("CAN bus time does not match the step start")
        delivered_start = sum(bus.step(start_ns) for bus in self.buses)
        if any(bus.has_due(start_ns) for bus in self.buses):
            raise RuntimeError("CAN dispatch budget exhausted at step start")
        for index, (_, step, _) in enumerate(self._methods):
            try:
                result = step(ctx)
                if (not isinstance(result, StepResult) or result.status != "ok"
                        or type(result.reached_time_ns) is not int
                        or result.reached_time_ns != ctx.t_sim_ns):
                    raise RuntimeError(
                        f"CAN device {index} did not reach target: {result!r}")
            except BaseException as exc:
                exc.add_note(f"CAN device {index} failed at step {ctx.step_id}")
                raise
        delivered_target = sum(bus.step(ctx.t_sim_ns) for bus in self.buses)
        return StepResult("ok", ctx.t_sim_ns, {
            "delivered_start": delivered_start,
            "delivered_target": delivered_target,
            "pending": sum(bus.pending for bus in self.buses),
        })

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        primary = None
        for index in reversed(range(len(self._methods))):
            close = self._methods[index][2]
            try:
                close()
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    primary.add_note(
                        f"CAN device {index} close failed: {type(exc).__name__}: {exc}")
        if primary is not None:
            raise primary


class CanRuntime:
    """Unmigrated CAN composition helper; new execution uses Simulation."""

    def __init__(self, bus: CanBus, endpoint: TransportEndpoint,
                 devices: Iterable[SimParticipant] = (),
                 socket_endpoint: DatagramEndpoint | None = None):
        if (getattr(endpoint, "bus", None) is not bus
                or any(not callable(getattr(endpoint, name, None))
                       for name in ("bind_mapper", "ingest"))):
            raise ValueError("transport endpoint must belong to the runtime CAN bus")
        if socket_endpoint is not None and (
                getattr(socket_endpoint, "bus", None) is not bus
                or not callable(getattr(socket_endpoint, "ingest", None))):
            raise ValueError("SocketCAN endpoint must belong to the runtime CAN bus")
        self.bus = bus
        self.endpoint = endpoint
        self.socket_endpoint = socket_endpoint
        self.coordinator = Coordinator({"can": CanParticipant(bus, devices)})

    @property
    def t_sim_ns(self) -> int:
        return self.coordinator.t_sim_ns

    def reset(self, session_id: int, t_sim_ns: int,
              mapper: TimestampMapper) -> None:
        if self.coordinator.closed:
            raise RuntimeError("CAN runtime is closed")
        session = Session(session_id, t_sim_ns)
        self.endpoint.bind_mapper(mapper)
        self.coordinator.reset(session)

    def step(self, dt_ns: int) -> StepResult:
        return self.coordinator.step(dt_ns)["can"]

    def _check_ingress(self) -> None:
        if (self.coordinator.closed or self.coordinator.failed
                or self.coordinator.session is None):
            raise RuntimeError("CAN runtime requires a successful reset")

    def ingest_qemu(self, frame: CanFrame, current_time_ns: int) -> None:
        self._check_ingress()
        self.endpoint.ingest(frame, current_time_ns)

    def ingest_socketcan(self, raw: bytes, current_time_ns: int) -> None:
        self._check_ingress()
        if self.socket_endpoint is None:
            raise RuntimeError("CAN runtime has no SocketCAN endpoint")
        self.socket_endpoint.ingest(raw, current_time_ns)

    def close(self) -> None:
        self.coordinator.close()
