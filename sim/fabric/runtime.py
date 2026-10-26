"""Ordered host CAN buses composed through the protocol-neutral core.

Each step settles commands at its start, advances devices to its target, then
settles target-time feedback. Future commands cannot affect an earlier device
step. Target-time backlog stays bounded and must settle at the next step start;
exhausting the start dispatch budget freezes the coordinator before device work.
This host schedule does not establish a QEMU CPU or physical CAN time barrier.
"""

from __future__ import annotations

from collections.abc import Iterable

from sim.core import Session, SimParticipant, StepContext, StepResult

from .can import CanBus, check_can_time

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
