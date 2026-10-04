#!/usr/bin/env python3
"""Focused tests for the worker's non-blocking host queues."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import socket
import sys
import struct
import unittest
from unittest.mock import patch
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
    tx_pending = False

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

    def test_v2_wait_flushes_partial_step_when_socket_becomes_writable(self):
        protocol = worker.step_protocol
        validator = protocol.StepSessionValidator.qemu_to_host(
            require_step_done=False)
        validator.begin_reset(0, 0)
        validator.accept(protocol.StepFrame(
            protocol.StepHeader(2, 5, 24, 0, 0, 0, 0),
            struct.pack("<IIQII", 0, protocol.CAPABILITY_MASK, 0, 0, 0)))
        ack = struct.pack("<IHHIQQQI4I", protocol.STEP_MAGIC, 2, 3,
                          16, 1, 1, 1, 0, 0, 0, 0, 0)

        class Socket:
            output = bytearray()
            writable = False
            reply = struct.pack("<I", len(ack)) + ack

            def send(self, data):
                if not self.output:
                    self.output.extend(data[:2])
                    return 2
                if not self.writable:
                    raise BlockingIOError
                self.output.extend(data)
                return len(data)

            def recv(self, size):
                if not self.writable or not self.reply:
                    raise BlockingIOError
                data, self.reply = self.reply, b""
                return data

        sock = Socket()
        stream = worker.StepFramedSocket(sock)
        stream.send(b"original STEP")

        def select_writable(readable, writable, errors, timeout):
            self.assertEqual(writable, [sock])
            sock.writable = True
            return [], [sock], []

        with patch.object(worker.select, "select", select_writable):
            response = worker.wait_v2_response(stream, validator, 3, 1, 1)
        self.assertEqual(response.header.step_id, 1)
        self.assertEqual(sock.output, b"original STEP")
        self.assertFalse(stream.tx_pending)

    def test_step_admission_deadline_and_exact_replays(self):
        protocol = worker.step_protocol
        validator = protocol.StepSessionValidator.qemu_to_host(
            require_step_done=False)
        validator.begin_reset(0, 0)
        validator.accept(protocol.StepFrame(
            protocol.StepHeader(2, 5, 24, 0, 0, 0, 0),
            struct.pack("<IIQII", 0, protocol.CAPABILITY_MASK, 0, 0, 0)))
        queue_full = protocol.StepFrame(
            protocol.StepHeader(2, 3, 16, 1, 1, 1, 0),
            struct.pack("<IIII", 1, 9, 0, 0))
        ok = protocol.StepFrame(
            protocol.StepHeader(2, 3, 16, 1, 1, 1, 0), bytes(16))

        class Clock:
            now = 0.0

            def monotonic(self):
                return self.now

            def wait(self, readable, writable, errors, timeout):
                self.now += timeout
                return [], [], []

        class Stream(StepFrameBatch):
            def __init__(self, mode, clock):
                super().__init__([])
                self.sent = []
                self.mode = mode
                self.clock = clock

            def send(self, wire):
                self.sent.append(wire)
                if self.mode == "pending" and len(self.sent) == 2:
                    # The replay blocks while two delayed replies arrive.
                    self.tx_pending = True
                    self.frames.extend([queue_full, queue_full])
                    return
                self.frames.append(ok if self.mode == "recover" and
                                   len(self.sent) == 3 else queue_full)

            def receive(self):
                if self.tx_pending and self.frames:
                    self.clock.now += 0.002
                return super().receive()

            def flush(self):
                return False

        for mode in ("full", "recover", "pending"):
            with self.subTest(mode=mode):
                clock = Clock()
                stream = Stream(mode, clock)
                validator.reset()
                validator.begin_reset(0, 0)
                validator.accept(protocol.StepFrame(
                    protocol.StepHeader(2, 5, 24, 0, 0, 0, 0),
                    struct.pack("<IIQII", 0, protocol.CAPABILITY_MASK,
                                0, 0, 0)))
                with patch.object(worker.time, "monotonic", clock.monotonic), \
                        patch.object(worker.select, "select", clock.wait):
                    if mode == "recover":
                        response = worker.send_v2_step(
                            stream, validator, b"unchanged STEP", 1, 1)
                        self.assertEqual(response.payload, bytes(16))
                        self.assertEqual(clock.now, 0.002)
                        self.assertEqual(len(stream.sent), 3)
                    else:
                        with self.assertRaisesRegex(
                                TimeoutError, "STEP_ACK admission timed out: "
                                "step=1.*last_status=1.*queue_depth=9"):
                            worker.send_v2_step(
                                stream, validator, b"unchanged STEP", 1, 1)
                        if mode == "full":
                            self.assertEqual(clock.now, 2.0)
                        else:
                            self.assertEqual(len(stream.sent), 2)
                self.assertTrue(all(wire == b"unchanged STEP"
                                    for wire in stream.sent))

    def test_silent_ack_retries_do_not_copy_pending_tx(self):
        class Clock:
            now = 0.0

            def monotonic(self):
                return self.now

            def wait(self, readable, writable, errors, timeout):
                self.now += timeout
                return [], [], []

        class Stream(StepFrameBatch):
            def __init__(self, blocked):
                super().__init__([])
                self.tx_pending = blocked
                self.sent = []

            def send(self, wire):
                self.sent.append(wire)

            def flush(self):
                return False

        for blocked in (False, True):
            with self.subTest(blocked=blocked):
                stream, clock = Stream(blocked), Clock()
                with patch.object(worker.time, "monotonic", clock.monotonic), \
                        patch.object(worker.select, "select", clock.wait):
                    with self.assertRaisesRegex(
                            TimeoutError, "STEP_ACK admission timed out.*retries=3"):
                        worker.send_v2_step(stream, None, b"STEP", 1, 1)
                self.assertEqual(stream.sent, [b"STEP"] * (1 if blocked else 4))
                self.assertEqual(clock.now, 1.0)

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

    def test_v2_missing_done_is_terminal_only_in_strict_mode(self):
        protocol = worker.step_protocol
        failed_done = bytes.fromhex(
            "0200000001000000020000000600000007000000")
        reset_ack = struct.pack(
            "<IIQII", 0, protocol.CAPABILITY_MASK, 0, 0, 0)
        step_ack = struct.pack("<IIII", 0, 0, 0, 0)

        def frame(kind, step_id, payload):
            return protocol.StepFrame(
                protocol.StepHeader(protocol.STEP_VERSION, kind,
                                    len(payload), step_id, step_id,
                                    1 if step_id else 0, 0), payload)

        for strict in (True, False):
            with self.subTest(require_step_done=strict):
                validator = protocol.StepSessionValidator.qemu_to_host(
                    require_step_done=strict)
                validator.begin_reset(0, 0)
                validator.accept(frame(protocol.RESET_ACK, 0, reset_ack))
                stream = StepFrameBatch([
                    frame(protocol.STEP_ACK, 1, step_ack),
                    frame(protocol.STEP_DONE, 1, failed_done),
                    frame(protocol.STEP_ACK, 2, step_ack),
                ])
                # The first wait must retain the later DONE from this read.
                worker.wait_v2_response(
                    stream, validator, protocol.STEP_ACK, 1, 1)
                if strict:
                    with self.assertRaisesRegex(
                            RuntimeError, "STEP_DONE failed.*step=1.*status=2"
                            ".*consumed=0x1.*missing=0x2"):
                        worker.wait_v2_response(
                            stream, validator, protocol.STEP_DONE, 1, 1)
                else:
                    response = worker.wait_v2_response(
                        stream, validator, protocol.STEP_ACK, 2, 2)
                    self.assertEqual(response.header.step_id, 2)


if __name__ == "__main__":
    unittest.main()
