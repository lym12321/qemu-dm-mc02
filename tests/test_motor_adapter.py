"""Tests for the reusable host-side motor bus adapter."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import struct
import sys


MODULE = Path(__file__).resolve().parents[1] / "tools" / \
    "dm_mc02_motor_adapter.py"
SPEC = importlib.util.spec_from_file_location("dm_mc02_motor_adapter_test",
                                              MODULE)
assert SPEC is not None and SPEC.loader is not None
adapter_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = adapter_module
SPEC.loader.exec_module(adapter_module)


def test_dm_and_float_frames_decode_to_protocol_neutral_commands():
    adapter = adapter_module.DmMotorBusAdapter(2)
    mit_zero = bytes((0x80, 0, 0x80, 0, 0, 0, 8, 0))

    command = adapter.decode(1, 0, mit_zero, 123)
    assert command is not None
    assert command.mode == "dm-control"
    assert command.index == 0
    assert command.timestamp_ns == 123
    assert abs(command.position) < 0.01
    assert abs(command.velocity) < 0.01

    float_command = adapter.decode(0x200, 0, struct.pack("<f", 1.5), 456)
    assert float_command is not None
    assert (float_command.mode, float_command.index,
            float_command.value, float_command.timestamp_ns) == (
                "float", 0, 1.5, 456)


def test_control_words_and_backend_application_are_explicit():
    adapter = adapter_module.DmMotorBusAdapter(1)
    events = []

    class Backend:
        def set_motor(self, index, value):
            events.append(("float", index, value))

        def reset_motor(self, index):
            events.append(("reset", index))

        def set_motor_enabled(self, index, enabled):
            events.append(("enable", index, enabled))

        def set_motor_dm(self, index, command):
            events.append(("dm", index, command))

        def motor_feedback(self, index):
            return (1.0, 2.0, 3.0)

    backend = Backend()
    for payload in (adapter_module.DM_ENABLE_COMMAND,
                    adapter_module.DM_DISABLE_COMMAND,
                    adapter_module.DM_RESET_COMMAND):
        command = adapter.decode(1, 0, payload)
        assert command is not None
        adapter.apply(backend, command)
    adapter.apply(backend, adapter_module.MotorCommand("float", 0, 4.0))

    assert events[:3] == [("enable", 0, True), ("enable", 0, False),
                          ("reset", 0)]
    assert events[3] == ("float", 0, 4.0)


def test_feedback_uses_configured_ids_and_wire_limits():
    adapter = adapter_module.DmMotorBusAdapter(
        2, dm_motor_map={1: (0x31, 0x41)}, t_max=2.0)
    state = adapter_module.MotorState(1, 99.0, -99.0, 9.0, 10)

    feedback_id, payload = adapter.encode_feedback(state)

    assert feedback_id == 0x41
    assert len(payload) == 8
    assert payload[0] == (0x31 & 0x0f)
