#!/usr/bin/env python3
"""Focused tests for the worker's non-blocking host queues."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import socket
import sys
import struct
import unittest
from types import SimpleNamespace


WORKER = Path(__file__).resolve().parents[1] / "tools" / "dm_mc02_sim_worker.py"
SPEC = importlib.util.spec_from_file_location("dm_mc02_sim_worker", WORKER)
assert SPEC is not None and SPEC.loader is not None
worker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = worker
SPEC.loader.exec_module(worker)


class AlwaysBlocked:
    def send(self, data):
        del data
        raise BlockingIOError


class PartialWriter:
    def __init__(self, width: int):
        self.width = width
        self.output = bytearray()

    def send(self, data):
        count = min(self.width, len(data))
        self.output.extend(data[:count])
        return count


class StepFrameBatch:
    """Small receive stub for testing v2 control-frame ordering."""

    sock = None

    def __init__(self, frames):
        self.frames = list(frames)

    def receive(self):
        frames, self.frames = self.frames, []
        return frames

    def push_front(self, frames):
        self.frames[0:0] = list(frames)


class ImmediateRclpy:
    def ok(self):
        return True

    def spin_once(self, node, timeout_sec):
        del node, timeout_sec


class ResetFuture:
    def __init__(self, done, error=None):
        self._done = done
        self._error = error

    def done(self):
        return self._done

    def result(self):
        if self._error is not None:
            raise self._error
        return object()


class ResetClient:
    def __init__(self, future):
        self.future = future

    def call_async(self, request):
        del request
        return self.future


class WorkerTransportTests(unittest.TestCase):
    @staticmethod
    def _ros2_engine_for_reset(future, timeout):
        engine = object.__new__(worker.Ros2Engine)
        engine.motor_count = 0
        engine.motor_values = []
        engine.legacy_values = []
        engine.enabled = []
        engine.dm_commands = []
        engine.position = []
        engine.velocity = []
        engine.effort = []
        engine.effort_valid = []
        engine.gyro = (0.0, 0.0, 0.0)
        engine.accel = (0.0, 0.0, 1.0)
        engine.imu_timestamp_ns = None
        engine.message_type = lambda: SimpleNamespace(data=[])
        engine.publisher = SimpleNamespace(publish=lambda message: None)
        engine.rclpy = ImmediateRclpy()
        engine.node = object()
        engine.reset_client = ResetClient(future)
        engine.reset_service_type = SimpleNamespace(Request=lambda: object)
        engine.reset_timeout = timeout
        return engine

    def test_ros2_reset_service_has_a_timeout(self):
        engine = self._ros2_engine_for_reset(
            ResetFuture(done=False), timeout=0.0)

        with self.assertRaises(TimeoutError):
            engine.reset()

    def test_ros2_reset_service_failure_is_propagated(self):
        engine = self._ros2_engine_for_reset(
            ResetFuture(done=True, error=RuntimeError("service failed")),
            timeout=1.0)

        with self.assertRaises(RuntimeError) as raised:
            engine.reset()
        self.assertIn("reset service failed", str(raised.exception))

    def test_can_stream_trims_fixed_wire_padding(self):
        qemu, worker_socket = socket.socketpair()
        try:
            stream = worker.CanStream(qemu)
            qemu.setblocking(False)
            worker_socket.sendall(struct.pack(
                "<IIB3xQ64s", 0x123, 0, 8, 123,
                b"12345678" + b"\\0" * 56))
            self.assertEqual(stream.receive(),
                             [(0x123, 0, 8, 123, b"12345678")])
        finally:
            qemu.close()
            worker_socket.close()

    def test_realtime_origin_is_relative_to_initial_virtual_time(self):
        self.assertEqual(
            worker.realtime_target_ns(1_000_000_000, 5_010_000_000,
                                     5_000_000_000),
            1_010_000_000)

    def test_external_timestamp_mapper_establishes_origin(self):
        mapper = worker.ExternalTimestampMapper(5_000_000_000)
        self.assertEqual(mapper.map(100_000_000, 5_001_000_000),
                         5_001_000_000)
        self.assertEqual(mapper.map(101_000_000, 5_002_000_000),
                         5_002_000_000)

    def test_external_timestamp_mapper_preserves_monotonic_fallback(self):
        mapper = worker.ExternalTimestampMapper(0)
        self.assertEqual(mapper.map(10_000, 1_000), 1_000)
        self.assertEqual(mapper.map(9_000, 2_000), 2_000)
        self.assertEqual(mapper.map(None, 3_000), 3_000)
        self.assertEqual(mapper.map(11_000, 4_000), 4_000)

    def test_external_timestamp_mapper_reanchors_after_clock_reset(self):
        mapper = worker.ExternalTimestampMapper(0)
        self.assertEqual(mapper.map(10_000, 1_000), 1_000)
        self.assertEqual(mapper.map(11_000, 2_000), 2_000)
        self.assertEqual(mapper.map(0, 3_000), 3_000)
        self.assertEqual(mapper.map(1_000_000, 4_000), 4_000)
        self.assertEqual(mapper.map(1_100_000, 5_000), 104_000)

    def test_external_timestamp_mapper_recovery_ignores_stale_fallback_rate(self):
        mapper = worker.ExternalTimestampMapper(0)
        self.assertEqual(mapper.map(10_000, 10_000), 10_000)
        self.assertEqual(mapper.map(10_000, 20_000), 20_000)
        self.assertEqual(mapper.map(10_000, 30_000), 30_000)
        self.assertEqual(mapper.map(10_100, 40_000), 40_000)
        self.assertEqual(mapper.map(10_200, 50_000), 40_100)
        self.assertEqual(mapper.map(10_300, 60_000), 40_200)

    def test_qemu_timestamp_mapper_translates_v2_epoch(self):
        mapper = worker.QemuTimestampMapper(10_000, 0)
        self.assertEqual(mapper.map(10_250, 100), 250)
        self.assertEqual(mapper.map(9_999, 100), 100)
        self.assertEqual(mapper.unmap(250, 100), 10_250)
        self.assertEqual(mapper.unmap(0, 10_000), 10_000)

    def test_ros2_wait_for_imu_waits_for_first_timestamp(self):
        engine = object.__new__(worker.Ros2Engine)
        engine.motor_count = 0
        engine.gyro = (0.0, 0.0, 0.0)
        engine.accel = (0.0, 0.0, 1.0)
        engine.imu_timestamp_ns = None
        engine.wait_for_imu = True
        engine.node = object()

        class PublishingRclpy:
            def __init__(self):
                self.spin_count = 0

            def ok(self):
                return True

            def spin_once(self, node, timeout_sec):
                del node, timeout_sec
                self.spin_count += 1
                engine.imu_timestamp_ns = 123
                engine.gyro = (1.0, 2.0, 3.0)

        engine.rclpy = PublishingRclpy()
        gyro, accel = engine.step(0.01)
        self.assertEqual(engine.rclpy.spin_count, 1)
        self.assertEqual(gyro, (180.0 / 3.141592653589793,
                                360.0 / 3.141592653589793,
                                540.0 / 3.141592653589793))
        self.assertEqual(accel, (0.0, 0.0, 1.0))

    def test_framed_send_does_not_wait_on_backpressure(self):
        stream = worker.FramedSocket(AlwaysBlocked())

        stream.send(b"frame")
        self.assertTrue(stream.tx_pending)
        self.assertFalse(stream.flush())

        for _ in range(worker.MAX_PENDING_FRAMES - 1):
            stream.send(b"frame")
        with self.assertRaises(BufferError):
            stream.send(b"overflow")

    def test_framed_send_preserves_partial_frame(self):
        sock = PartialWriter(2)
        stream = worker.FramedSocket(sock)

        stream.send(b"abcdef")
        for _ in range(8):
            if stream.flush():
                break
        self.assertFalse(stream.tx_pending)
        self.assertEqual(sock.output, b"abcdef")

    def test_datagram_queue_is_bounded_without_waiting(self):
        queue = worker.DatagramQueue(AlwaysBlocked())
        frame = worker.CanFrame(0x123, data=b"x")

        for _ in range(worker.MAX_PENDING_FRAMES):
            queue.send(frame)
        with self.assertRaises(BufferError):
            queue.send(frame)

    def test_v2_wait_reports_asynchronous_qemu_reset(self):
        protocol = worker.step_protocol
        validator = protocol.StepSessionValidator(
            protocol.DIRECTION_QEMU_TO_HOST, require_step_done=False)
        validator.begin_reset(0, 0)
        reset_ack = struct.pack(
            "<IIQII", 0, protocol.CAPABILITY_MASK, 10, 0, 0)
        step_ack = struct.pack("<IIII", 0, 0, 0, 0)

        def frame(kind, step_id, timestamp, dt=0, payload=b""):
            return protocol.StepFrame(
                protocol.StepHeader(protocol.STEP_VERSION, kind,
                                    len(payload), step_id, timestamp, dt, 0),
                payload)

        stream = StepFrameBatch([
            frame(protocol.RESET, 0, 10),
            frame(protocol.RESET_ACK, 0, 10, payload=reset_ack),
            frame(protocol.STEP_ACK, 1, 11, 1, step_ack),
        ])
        with self.assertRaises(worker.V2SessionReset) as raised:
            worker.wait_v2_response(stream, validator, protocol.STEP_ACK,
                                    1, 11)
        self.assertEqual((raised.exception.step_id,
                          raised.exception.t_sim_ns), (0, 10))

    def test_v2_done_wait_ignores_replayed_ack(self):
        protocol = worker.step_protocol
        validator = protocol.StepSessionValidator(
            protocol.DIRECTION_QEMU_TO_HOST, require_step_done=True)
        validator.begin_reset(0, 0)
        reset_ack = struct.pack(
            "<IIQII", 0, protocol.CAPABILITY_MASK, 10, 0, 0)
        step_ack = struct.pack("<IIII", 0, 0, 0, 0)
        step_done = struct.pack(
            "<IIIII", 0, protocol.STEP_DONE_REQUIRED_MASK, 0, 0, 0)

        def frame(kind, payload, dt=0):
            return protocol.StepFrame(
                protocol.StepHeader(protocol.STEP_VERSION, kind,
                                    len(payload), 1, 1, dt, 0), payload)

        validator.accept(protocol.StepFrame(
            protocol.StepHeader(protocol.STEP_VERSION, protocol.RESET_ACK,
                                len(reset_ack), 0, 0, 0, 0), reset_ack))
        stream = StepFrameBatch([
            frame(protocol.STEP_ACK, step_ack, 1),
            frame(protocol.STEP_ACK, step_ack, 1),
            frame(protocol.STEP_DONE, step_done, 1),
        ])
        result = worker.wait_v2_response(
            stream, validator, protocol.STEP_DONE, 1, 1)
        self.assertEqual(result.header.kind, protocol.STEP_DONE)


if __name__ == "__main__":
    unittest.main()
