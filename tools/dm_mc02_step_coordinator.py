"""Virtual-time coordinator shared by DM-MC02 plant backends."""

from __future__ import annotations

import heapq

try:
    from dm_mc02_motor_adapter import DmMotorBusAdapter, MotorBackend, MotorCommand
except ModuleNotFoundError as exc:
    if exc.name != "dm_mc02_motor_adapter":
        raise
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path
    import sys

    adapter_path = Path(__file__).with_name("dm_mc02_motor_adapter.py")
    adapter_spec = spec_from_file_location("dm_mc02_motor_adapter", adapter_path)
    if adapter_spec is None or adapter_spec.loader is None:
        raise ImportError(f"cannot load {adapter_path}") from exc
    adapter_module = module_from_spec(adapter_spec)
    sys.modules[adapter_spec.name] = adapter_module
    adapter_spec.loader.exec_module(adapter_module)
    from dm_mc02_motor_adapter import DmMotorBusAdapter, MotorBackend, MotorCommand


class StepCoordinator:
    """Own command ordering and one monotonic plant step boundary.

    CAN arrival order is intentionally independent from execution order:
    timestamped commands are applied at the first step whose virtual time has
    reached their timestamp.
    """

    def __init__(self, backend: MotorBackend, motor_adapter: DmMotorBusAdapter):
        self.backend = backend
        self.motor_adapter = motor_adapter
        self.pending: list[tuple[int, int, MotorCommand]] = []
        self.active_indices: set[int] = set()
        self._order = 0
        self.last_time_ns: int | None = None

    def reset(self) -> None:
        """Discard queued commands and start a new virtual-time epoch."""
        self.pending.clear()
        self.active_indices.clear()
        self._order = 0
        self.last_time_ns = None

    def submit_can(self, can_id: int, flags: int, data: bytes,
                   timestamp_ns: int, fallback_time_ns: int) -> MotorCommand | None:
        command = self.motor_adapter.decode(can_id, flags, data, timestamp_ns)
        if command is None:
            return None
        due = timestamp_ns if timestamp_ns else fallback_time_ns
        self._order += 1
        heapq.heappush(self.pending, (due, self._order, command))
        if command.mode.startswith("dm-"):
            self.active_indices.add(command.index)
        return command

    def advance(self, t_sim_ns: int, dt_seconds: float):
        if self.last_time_ns is not None and t_sim_ns <= self.last_time_ns:
            raise ValueError("simulation time must increase strictly")
        while self.pending and self.pending[0][0] <= t_sim_ns:
            _, _, command = heapq.heappop(self.pending)
            self.motor_adapter.apply(self.backend, command)
        self.last_time_ns = t_sim_ns
        return self.backend.step(dt_seconds)
