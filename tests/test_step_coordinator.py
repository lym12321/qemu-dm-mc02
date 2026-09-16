"""Tests for virtual-time motor command scheduling."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


TOOLS = Path(__file__).resolve().parents[1] / "tools"


def load_tool(name: str):
    path = TOOLS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


adapter_module = load_tool("dm_mc02_motor_adapter")
coordinator_module = load_tool("dm_mc02_step_coordinator")


class Backend:
    def __init__(self):
        self.events = []

    def set_motor(self, index, value):
        self.events.append(("float", index, value))

    def reset_motor(self, index):
        self.events.append(("reset", index))

    def set_motor_enabled(self, index, enabled):
        self.events.append(("enable", index, enabled))

    def set_motor_dm(self, index, command):
        self.events.append(("dm", index, command))

    def motor_feedback(self, index):
        return (0.0, 0.0, 0.0)

    def step(self, dt):
        self.events.append(("step", dt))
        return (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)


def test_timestamped_commands_are_applied_at_step_boundary():
    adapter = adapter_module.DmMotorBusAdapter(1, motor_protocol="float")
    backend = Backend()
    coordinator = coordinator_module.StepCoordinator(backend, adapter)

    command = coordinator.submit_can(0x200, 0, b"\x00\x00\xc0?", 20, 0)
    assert command is not None
    coordinator.advance(10, 0.01)
    assert backend.events == [("step", 0.01)]
    coordinator.advance(20, 0.01)
    assert backend.events[1] == ("float", 0, 1.5)
    assert backend.events[2] == ("step", 0.01)


def test_equal_or_backward_step_time_is_rejected():
    adapter = adapter_module.DmMotorBusAdapter(1)
    coordinator = coordinator_module.StepCoordinator(Backend(), adapter)
    coordinator.advance(10, 0.001)
    for value in (10, 9):
        try:
            coordinator.advance(value, 0.001)
        except ValueError as exc:
            assert "increase" in str(exc)
        else:
            raise AssertionError("non-monotonic time was accepted")

