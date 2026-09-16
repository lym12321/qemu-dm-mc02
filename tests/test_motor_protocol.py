"""Regression tests for the DM-MC02 worker motor boundary."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


WORKER = Path(__file__).resolve().parents[1] / "tools" / "dm_mc02_sim_worker.py"
SPEC = importlib.util.spec_from_file_location("dm_mc02_sim_worker_motor", WORKER)
assert SPEC is not None and SPEC.loader is not None
worker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = worker
SPEC.loader.exec_module(worker)


def worker_args(**overrides):
    values = {
        "motor_protocol": "auto",
        "motor_can_base": 0x200,
        "dm_slave_id": 1,
        "dm_feedback_id": 0x11,
        "dm_p_max": 12.5,
        "dm_v_max": 30.0,
        "dm_t_max": 10.0,
        "motor_count": 1,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class MotorProtocolTests(unittest.TestCase):
    def test_dm_zero_packet_round_trips_in_physical_units(self):
        # MIT encoding of position/speed/Kp/Kd/torque ~= zero.
        packet = bytes((0x80, 0, 0x80, 0, 0, 0, 8, 0))
        command = worker.decode_dm_mit(packet)
        self.assertIsNotNone(command)
        for value in command.values():
            self.assertAlmostEqual(value, 0.0, delta=0.01)

    def test_auto_protocol_recognizes_real_dm_control_and_control_word(self):
        args = worker_args()
        enable = worker.decode_motor_command(
            1, 0, worker.DM_ENABLE_COMMAND, args)
        self.assertEqual(enable, ("dm-enable", 0, None))
        control = worker.decode_motor_command(
            1, 0, bytes((0x80, 0, 0x80, 0, 0, 0, 8, 0)), args)
        self.assertEqual(control[0:2], ("dm-control", 0))

    def test_per_motor_ids_override_contiguous_defaults(self):
        args = worker_args(motor_count=2, dm_motor_map={1: (0x31, 0x41)})
        packet = bytes((0x80, 0, 0x80, 0, 0, 0, 8, 0))
        command = worker.decode_motor_command(0x31, 0, packet, args)
        self.assertEqual(command[0:2], ("dm-control", 1))
        self.assertEqual(worker.dm_motor_ids(args, 1), (0x31, 0x41))
        self.assertEqual(worker.dm_motor_ids(args, 0), (1, 0x11))

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
        args = worker_args()
        packet = worker.struct.pack("<f", 1.25)
        self.assertEqual(
            worker.decode_motor_command(0x200, 0, packet, args),
            ("float", 0, 1.25),
        )

    def test_null_motor_produces_feedback_after_enable(self):
        args = worker_args()
        engine = worker.NullEngine(1)
        worker.apply_motor_command(
            engine, worker.decode_motor_command(
                1, 0, worker.DM_ENABLE_COMMAND, args))
        command = worker.decode_dm_mit(
            bytes((0x80, 0, 0x80, 0, 0, 0, 0x8, 0)))
        command["torque"] = 2.0
        worker.apply_motor_command(engine, ("dm-control", 0, command))
        engine.step(0.001)
        position, velocity, torque = engine.motor_feedback(0)
        self.assertGreater(velocity, 0.0)
        self.assertGreater(position, 0.0)
        self.assertAlmostEqual(torque, 2.0, places=5)
        self.assertEqual(len(worker.encode_dm_feedback(position, velocity, torque)), 8)

    def test_null_motor_honors_configured_torque_limit_and_disable(self):
        engine = worker.NullEngine(1, torque_limit=2.0)
        worker.apply_motor_command(engine, ("dm-enable", 0, None))
        command = worker.decode_dm_mit(
            bytes((0x80, 0, 0x80, 0, 0, 0, 0x8, 0)))
        command["torque"] = 9.0
        worker.apply_motor_command(engine, ("dm-control", 0, command))
        engine.step(0.001)
        self.assertAlmostEqual(engine.motor_feedback(0)[2], 2.0, places=5)
        worker.apply_motor_command(engine, ("dm-disable", 0, None))
        engine.step(0.001)
        self.assertAlmostEqual(engine.motor_feedback(0)[2], 0.0, places=5)

    def test_legacy_float_null_engine_obeys_gate_limit_and_reenable(self):
        engine = worker.NullEngine(1, torque_limit=2.0)
        worker.apply_motor_command(engine, ("float", 0, 9.0))
        engine.step(0.001)
        self.assertAlmostEqual(engine.motor_feedback(0)[2], 0.0, places=5)

        worker.apply_motor_command(engine, ("dm-enable", 0, None))
        self.assertAlmostEqual(engine.motor_feedback(0)[2], 2.0, places=5)
        engine.step(0.001)
        self.assertAlmostEqual(engine.motor_feedback(0)[2], 2.0, places=5)

        worker.apply_motor_command(engine, ("dm-disable", 0, None))
        engine.step(0.001)
        self.assertAlmostEqual(engine.motor_feedback(0)[2], 0.0, places=5)
        worker.apply_motor_command(engine, ("dm-enable", 0, None))
        self.assertAlmostEqual(engine.motor_feedback(0)[2], 2.0, places=5)

    def test_legacy_float_mujoco_adapter_obeys_gate_and_limit(self):
        engine = object.__new__(worker.MujocoEngine)
        engine.motor_count = 1
        engine.motor_values = [0.0]
        engine.legacy_values = [0.0]
        engine.torque_limit = 2.0
        engine.enabled = [False]
        engine.dm_commands = [None]
        engine.data = SimpleNamespace(ctrl=[0.0])

        worker.apply_motor_command(engine, ("float", 0, -9.0))
        self.assertEqual(engine.data.ctrl, [0.0])
        worker.apply_motor_command(engine, ("dm-enable", 0, None))
        self.assertEqual(engine.data.ctrl, [-2.0])
        worker.apply_motor_command(engine, ("dm-disable", 0, None))
        self.assertEqual(engine.data.ctrl, [0.0])
        worker.apply_motor_command(engine, ("dm-enable", 0, None))
        self.assertEqual(engine.data.ctrl, [-2.0])

    def test_legacy_float_ros2_adapter_obeys_gate_and_limit(self):
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

        worker.apply_motor_command(engine, ("float", 0, 9.0))
        self.assertEqual(published[-1], [0.0])
        worker.apply_motor_command(engine, ("dm-enable", 0, None))
        self.assertEqual(published[-1], [2.0])
        worker.apply_motor_command(engine, ("dm-disable", 0, None))
        self.assertEqual(published[-1], [0.0])
        worker.apply_motor_command(engine, ("dm-enable", 0, None))
        self.assertEqual(published[-1], [2.0])

    def test_mujoco_dm_control_obeys_enable_gate(self):
        try:
            import mujoco  # noqa: F401
        except ImportError:
            self.skipTest("MuJoCo extra is not installed")
        model = (Path(__file__).resolve().parents[1] /
                 "smoke" / "dm_mc02_mujoco_smoke.xml")
        engine = worker.MujocoEngine(str(model), 1, 0.001, torque_limit=2.0)
        command = worker.decode_dm_mit(
            bytes((0x80, 0, 0x80, 0, 0, 0, 0x8, 0)))
        command["torque"] = 9.0
        worker.apply_motor_command(engine, ("dm-control", 0, command))
        engine.step(0.001)
        self.assertAlmostEqual(engine.motor_feedback(0)[2], 0.0, places=5)
        worker.apply_motor_command(engine, ("dm-enable", 0, None))
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
