"""One host CAN participant composed through the protocol-neutral core.

Each step settles commands at its start, advances devices to its target, then
settles target-time feedback. Future commands cannot affect an earlier device
step. Target-time backlog stays bounded and must settle at the next step start;
exhausting the start dispatch budget freezes the coordinator before device work.
This host schedule does not establish a QEMU CPU or physical CAN time barrier.
"""

from __future__ import annotations

from collections.abc import Iterable

from sim.core import Coordinator, Session, SimParticipant, StepContext, StepResult

from .can import CanBus
from .endpoints import QemuCanEndpoint, SocketCanEndpoint, TimestampMapper
from .frame import CanFrame


class CanParticipant:
    """Own device execution and cleanup; borrow bus and transport wiring."""

    def __init__(self, bus: CanBus, devices: Iterable[SimParticipant] = ()):
        if not isinstance(bus, CanBus):
            raise TypeError("CAN participant requires a CanBus")
        self.bus = bus
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

    def reset(self, session: Session) -> None:
        if self._closed:
            raise RuntimeError("CAN participant is closed")
        if not isinstance(session, Session):
            raise TypeError("CAN reset requires a Session")
        self.bus.reset(session.t_sim_ns)
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
        start_ns = ctx.t_sim_ns - ctx.dt_ns
        if self.bus.t_sim_ns != start_ns:
            raise RuntimeError("CAN bus time does not match the step start")
        delivered_start = self.bus.step(start_ns)
        if self.bus.has_due(start_ns):
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
        delivered_target = self.bus.step(ctx.t_sim_ns)
        return StepResult("ok", ctx.t_sim_ns, {
            "delivered_start": delivered_start,
            "delivered_target": delivered_target,
            "pending": self.bus.pending,
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
    """Explicit CAN composition; the coordinator owns the sole participant."""

    def __init__(self, bus: CanBus, endpoint: QemuCanEndpoint,
                 devices: Iterable[SimParticipant] = (),
                 socket_endpoint: SocketCanEndpoint | None = None):
        if not isinstance(endpoint, QemuCanEndpoint) or endpoint.bus is not bus:
            raise ValueError("QEMU endpoint must belong to the runtime CAN bus")
        if socket_endpoint is not None and (
                not isinstance(socket_endpoint, SocketCanEndpoint)
                or socket_endpoint.bus is not bus):
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
