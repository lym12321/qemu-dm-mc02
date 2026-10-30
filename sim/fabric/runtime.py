"""CAN delivery adapters over the sole bounded :class:`CanBus` queue.

The dispatcher exposes known delivery deadlines to Simulation without owning
device execution. It does not establish a QEMU CPU or physical CAN time barrier.
"""

from __future__ import annotations

from sim.api import Session

from .can import CanBus


class BorrowedCanDispatcher:
    """Expose one borrowed bus to Simulation's dispatch phases.

    CanBus owns queue order, immutable pending deadlines, budgets and failures.
    Reset clears only its logical delivery state; devices and transport state
    remain the caller's responsibility. Close retires this adapter without
    consuming queued frames or closing any borrowed resource.
    """

    def __init__(self, bus: CanBus):
        if not isinstance(bus, CanBus):
            raise TypeError("CAN dispatcher requires one CanBus")
        self.bus = bus
        self._closed = False
        self._dispatching = False

    def _check_active(self) -> None:
        if self._closed:
            raise RuntimeError("CAN dispatcher is closed")

    def _check_mutation(self) -> None:
        self._check_active()
        if self._dispatching:
            raise RuntimeError("CAN dispatcher cannot mutate from a delivery callback")

    @property
    def next_due_ns(self) -> int | None:
        self._check_active()
        return self.bus.next_due_ns

    @property
    def dispatch_budget(self) -> int:
        self._check_active()
        return self.bus.dispatch_budget

    def reset(self, session: Session) -> None:
        self._check_mutation()
        if not isinstance(session, Session):
            raise TypeError("CAN dispatcher reset requires a Session")
        self.bus.reset(session.t_sim_ns)

    def dispatch(self, cut: int, budget: int) -> int:
        self._check_mutation()
        self._dispatching = True
        try:
            return self.bus.step(cut, max_dispatch=budget)
        finally:
            self._dispatching = False

    def close(self) -> None:
        if self._closed:
            return
        self._check_mutation()
        self._closed = True
