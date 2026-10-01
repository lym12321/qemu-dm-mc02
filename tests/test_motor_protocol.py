"""Regression tests for the DM-MC02 worker motor boundary."""

from __future__ import annotations

from dataclasses import replace
import importlib.util
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


WORKER = Path(__file__).resolve().parents[1] / "tools" / "dm_mc02_sim_worker.py"
ADAPTER = Path(__file__).resolve().parents[1] / "tools" / \
    "dm_mc02_motor_adapter.py"
ADAPTER_SPEC = importlib.util.spec_from_file_location(
    "dm_mc02_motor_adapter_protocol_test", ADAPTER)
assert ADAPTER_SPEC is not None and ADAPTER_SPEC.loader is not None
adapter_module = importlib.util.module_from_spec(ADAPTER_SPEC)
sys.modules[ADAPTER_SPEC.name] = adapter_module
ADAPTER_SPEC.loader.exec_module(adapter_module)

SPEC = importlib.util.spec_from_file_location("dm_mc02_sim_worker_motor", WORKER)
assert SPEC is not None and SPEC.loader is not None
worker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = worker
SPEC.loader.exec_module(worker)


def decode_packet(adapter, can_id, payload):
    command = adapter.decode(can_id, 0, payload)
    if command is None:
        raise AssertionError("valid motor packet was rejected")
    return command


def apply_packet(adapter, backend, can_id, payload):
    adapter.apply(backend, decode_packet(adapter, can_id, payload))


class MotorProtocolTests(unittest.TestCase):
    def test_dm_zero_packet_round_trips_in_physical_units(self):
        # MIT encoding of position/speed/Kp/Kd/torque ~= zero.
        packet = bytes((0x80, 0, 0x80, 0, 0, 0, 8, 0))
        adapter = adapter_module.DmMotorBusAdapter(1, motor_protocol="dm-mit")
        command = decode_packet(adapter, 1, packet)
        for value in (command.position, command.velocity, command.kp,
                      command.kd, command.torque):
            self.assertAlmostEqual(value, 0.0, delta=0.01)

    def test_auto_protocol_recognizes_real_dm_control_and_control_word(self):
        adapter = adapter_module.DmMotorBusAdapter(1)
        enable = decode_packet(adapter, 1, adapter_module.DM_ENABLE_COMMAND)
        self.assertEqual((enable.mode, enable.index), ("dm-enable", 0))
        control = decode_packet(
            adapter, 1, bytes((0x80, 0, 0x80, 0, 0, 0, 8, 0)))
        self.assertEqual((control.mode, control.index), ("dm-control", 0))

    def test_per_motor_ids_override_contiguous_defaults(self):
        adapter = adapter_module.DmMotorBusAdapter(
            2, dm_motor_map={1: (0x31, 0x41)})
        packet = bytes((0x80, 0, 0x80, 0, 0, 0, 8, 0))
        command = decode_packet(adapter, 0x31, packet)
        self.assertEqual((command.mode, command.index), ("dm-control", 1))
        self.assertEqual(adapter.motor_ids(1), (0x31, 0x41))
        self.assertEqual(adapter.motor_ids(0), (1, 0x11))

    def test_argument_parser_rejects_effective_id_collision(self):
        argv = [
            "dm_mc02_sim_worker.py", "--cosim", "/tmp/cosim",
            "--motor-count", "2", "--dm-motor", "0:2:0x12",
        ]
        with patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit):
                worker.parse_args()

    def test_argument_parser_rejects_effective_id_overflow(self):
        argv = [
            "dm_mc02_sim_worker.py", "--cosim", "/tmp/cosim",
            "--motor-count", "2", "--dm-feedback-id", "0x7ff",
        ]
        with patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit):
                worker.parse_args()

    def test_legacy_float_command_remains_supported(self):
        adapter = adapter_module.DmMotorBusAdapter(1)
        command = decode_packet(adapter, 0x200, struct.pack("<f", 1.25))
        self.assertEqual((command.mode, command.index, command.value),
                         ("float", 0, 1.25))

    def test_null_motor_produces_feedback_after_enable(self):
        adapter = adapter_module.DmMotorBusAdapter(1, motor_protocol="dm-mit")
        engine = worker.NullEngine(1)
        apply_packet(adapter, engine, 1, adapter_module.DM_ENABLE_COMMAND)
        command = decode_packet(
            adapter, 1, bytes((0x80, 0, 0x80, 0, 0, 0, 0x8, 0)))
        command = replace(command, torque=2.0)
        adapter.apply(engine, command)
        engine.step(0.001)
        position, velocity, torque = engine.motor_feedback(0)
        self.assertGreater(velocity, 0.0)
        self.assertGreater(position, 0.0)
        self.assertAlmostEqual(torque, 2.0, places=5)
        feedback_id, payload = adapter.encode_feedback(
            adapter.state(engine, 0))
        self.assertEqual(feedback_id, 0x11)
        self.assertEqual(len(payload), 8)

    def test_null_motor_honors_configured_torque_limit_and_disable(self):
        adapter = adapter_module.DmMotorBusAdapter(1, motor_protocol="dm-mit")
        engine = worker.NullEngine(1, torque_limit=2.0)
        apply_packet(adapter, engine, 1, adapter_module.DM_ENABLE_COMMAND)
        command = decode_packet(
            adapter, 1, bytes((0x80, 0, 0x80, 0, 0, 0, 0x8, 0)))
        adapter.apply(engine, replace(command, torque=9.0))
        engine.step(0.001)
        self.assertAlmostEqual(engine.motor_feedback(0)[2], 2.0, places=5)
        apply_packet(adapter, engine, 1, adapter_module.DM_DISABLE_COMMAND)
        engine.step(0.001)
        self.assertAlmostEqual(engine.motor_feedback(0)[2], 0.0, places=5)

    def test_legacy_float_null_engine_obeys_gate_limit_and_reenable(self):
        adapter = adapter_module.DmMotorBusAdapter(1)
        engine = worker.NullEngine(1, torque_limit=2.0)
        apply_packet(adapter, engine, 0x200, struct.pack("<f", 9.0))
        engine.step(0.001)
        self.assertAlmostEqual(engine.motor_feedback(0)[2], 0.0, places=5)

        apply_packet(adapter, engine, 1, adapter_module.DM_ENABLE_COMMAND)
        self.assertAlmostEqual(engine.motor_feedback(0)[2], 2.0, places=5)
        engine.step(0.001)
        self.assertAlmostEqual(engine.motor_feedback(0)[2], 2.0, places=5)

        apply_packet(adapter, engine, 1, adapter_module.DM_DISABLE_COMMAND)
        engine.step(0.001)
        self.assertAlmostEqual(engine.motor_feedback(0)[2], 0.0, places=5)
        apply_packet(adapter, engine, 1, adapter_module.DM_ENABLE_COMMAND)
        self.assertAlmostEqual(engine.motor_feedback(0)[2], 2.0, places=5)

    def test_legacy_float_mujoco_adapter_obeys_gate_and_limit(self):
        adapter = adapter_module.DmMotorBusAdapter(1)
        engine = object.__new__(worker.MujocoEngine)
        engine.motor_count = 1
        engine.motor_values = [0.0]
        engine.legacy_values = [0.0]
        engine.torque_limit = 2.0
        engine.enabled = [False]
        engine.dm_commands = [None]
        engine.data = SimpleNamespace(ctrl=[0.0])

        apply_packet(adapter, engine, 0x200, struct.pack("<f", -9.0))
        self.assertEqual(engine.data.ctrl, [0.0])
        apply_packet(adapter, engine, 1, adapter_module.DM_ENABLE_COMMAND)
        self.assertEqual(engine.data.ctrl, [-2.0])
        apply_packet(adapter, engine, 1, adapter_module.DM_DISABLE_COMMAND)
        self.assertEqual(engine.data.ctrl, [0.0])
        apply_packet(adapter, engine, 1, adapter_module.DM_ENABLE_COMMAND)
        self.assertEqual(engine.data.ctrl, [-2.0])

    def test_legacy_float_ros2_adapter_obeys_gate_and_limit(self):
        adapter = adapter_module.DmMotorBusAdapter(1)
        engine = object.__new__(worker.Ros2Engine)
        engine.motor_count = 1
        engine.motor_values = [0.0]
        engine.legacy_values = [0.0]
        engine.torque_limit = 2.0
        engine.enabled = [False]
        engine.dm_commands = [None]
        published = []
        engine.message_type = lambda: SimpleNamespace(data=None)
        engine.publisher = SimpleNamespace(
            publish=lambda message: published.append(list(message.data)))

        apply_packet(adapter, engine, 0x200, struct.pack("<f", 9.0))
        self.assertEqual(published[-1], [0.0])
        apply_packet(adapter, engine, 1, adapter_module.DM_ENABLE_COMMAND)
        self.assertEqual(published[-1], [2.0])
        apply_packet(adapter, engine, 1, adapter_module.DM_DISABLE_COMMAND)
        self.assertEqual(published[-1], [0.0])
        apply_packet(adapter, engine, 1, adapter_module.DM_ENABLE_COMMAND)
        self.assertEqual(published[-1], [2.0])

    def test_mujoco_dm_control_obeys_enable_gate(self):
        try:
            import mujoco  # noqa: F401
        except ImportError:
            self.skipTest("MuJoCo extra is not installed")
        model = (Path(__file__).resolve().parents[1] /
                 "smoke" / "dm_mc02_mujoco_smoke.xml")
        adapter = adapter_module.DmMotorBusAdapter(1, motor_protocol="dm-mit")
        engine = worker.MujocoEngine(str(model), 1, 0.001, torque_limit=2.0)
        command = decode_packet(
            adapter, 1, bytes((0x80, 0, 0x80, 0, 0, 0, 0x8, 0)))
        adapter.apply(engine, replace(command, torque=9.0))
        engine.step(0.001)
        self.assertAlmostEqual(engine.motor_feedback(0)[2], 0.0, places=5)
        apply_packet(adapter, engine, 1, adapter_module.DM_ENABLE_COMMAND)
        engine.step(0.001)
        self.assertAlmostEqual(engine.motor_feedback(0)[2], 2.0, places=5)

    def test_joint_state_missing_fields_do_not_retain_old_feedback(self):
        engine = object.__new__(worker.Ros2Engine)
        engine.motor_count = 2
        engine.position = [1.0, 2.0]
        engine.velocity = [3.0, 4.0]
        engine.effort = [5.0, 6.0]
        engine.effort_valid = [True, True]
        message = SimpleNamespace(position=[0.25], velocity=[], effort=[])

        engine._joint_state_callback(message)

        self.assertEqual(engine.position, [0.25, 0.0])
        self.assertEqual(engine.velocity, [0.0, 0.0])
        self.assertEqual(engine.effort, [0.0, 0.0])
        self.assertEqual(engine.effort_valid, [False, False])


if __name__ == "__main__":
    unittest.main()
