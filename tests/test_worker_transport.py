#!/usr/bin/env python3
"""Focused tests for the worker's non-blocking host queues."""

from __future__ import annotations

import importlib.util
import io
import errno
from pathlib import Path
import socket
import sys
import struct
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch
from types import SimpleNamespace


WORKER = Path(__file__).resolve().parents[1] / "tools" / "dm_mc02_sim_worker.py"
SPEC = importlib.util.spec_from_file_location("dm_mc02_sim_worker", WORKER)
assert SPEC is not None and SPEC.loader is not None
worker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = worker
SPEC.loader.exec_module(worker)

from sim.transports import (
    DatagramTransport, FixedRecordStream, LengthPrefixedStream, StreamTransport,
    connect_unix as transport_connect_unix, send_datagram,
)


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


class ReceiveChunks:
    """A readable stream must return to its owner after one recv opportunity."""

    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.reads = 0
        self.read_limit = 1

    def recv(self, size):
        assert size == 4096
        self.reads += 1
        if self.reads > self.read_limit:
            raise AssertionError("receive drained a continuously readable socket")
        return next(self.chunks)

    def allow_read(self):
        self.read_limit = self.reads + 1


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


class GenericTransportTests(unittest.TestCase):
    """Transport tests use opaque bytes, independent of worker codecs."""

    def test_stream_tx_retains_exact_suffix_and_frame_order(self):
        sock = PartialWriter(2)
        stream = StreamTransport(sock)
        stream.send(b"abcdef")
        self.assertEqual([bytes(view) for view in stream.tx_queue], [b"cdef"])
        stream.send(b"ghij")
        self.assertEqual([bytes(view) for view in stream.tx_queue],
                         [b"ef", b"ghij"])
        while not stream.flush():
            pass
        self.assertEqual(sock.output, b"abcdefghij")
        self.assertIs(stream.sock, sock)

    def test_stream_tx_rejects_capacity_without_losing_old_records(self):
        stream = StreamTransport(AlwaysBlocked(), max_pending=2)
        stream.send(b"first")
        stream.send(b"second")
        with self.assertRaisesRegex(BufferError, "TX queue is full"):
            stream.send(b"third")
        self.assertEqual([bytes(view) for view in stream.tx_queue],
                         [b"first", b"second"])

    def test_length_prefix_framing_retains_next_record_until_consumed(self):
        first, second = struct.pack("<I", 3) + b"one", struct.pack("<I", 3) + b"two"
        sock = ReceiveChunks([first + second])
        stream = LengthPrefixedStream(sock, min_length=1, max_length=8)
        records = stream.receive_records()
        self.assertEqual(next(records), first)
        self.assertEqual(bytes(stream.buffer), second)
        self.assertEqual(list(records), [second])
        self.assertEqual(sock.reads, 1)

    def test_fixed_records_keep_partial_bytes_between_single_reads(self):
        sock = ReceiveChunks([b"abcde", b"f"])
        stream = FixedRecordStream(sock, record_size=3)
        self.assertEqual(list(stream.receive_records()), [b"abc"])
        self.assertEqual(bytes(stream.buffer), b"de")
        sock.allow_read()
        self.assertEqual(list(stream.receive_records()), [b"def"])
        self.assertEqual(sock.reads, 2)

    def test_datagram_short_write_keeps_whole_record_and_fails(self):
        sock = PartialWriter(2)
        queue = DatagramTransport(sock)
        with self.assertRaisesRegex(ConnectionError, "short.*datagram write"):
            queue.send(b"whole")
        self.assertEqual(list(queue.tx_queue), [b"whole"])
        with self.assertRaisesRegex(ConnectionError, "short.*datagram write"):
            send_datagram(sock, b"whole")

    def test_nonfinite_timeout_is_rejected_before_socket_io(self):
        for timeout in (float("nan"), float("inf")):
            with self.subTest(timeout=timeout), \
                    patch.object(socket, "socket") as create:
                for queue_class in (StreamTransport, DatagramTransport):
                    queue = queue_class(AlwaysBlocked())
                    queue.tx_queue.append(b"pending")
                    with patch.object(queue, "flush") as flush:
                        with self.assertRaisesRegex(ValueError, "timeout"):
                            queue.drain(timeout)
                        flush.assert_not_called()
                with self.assertRaisesRegex(ValueError, "timeout"):
                    transport_connect_unix("unused", timeout)
                create.assert_not_called()

    def test_geometry_and_capacity_reject_boolean_values(self):
        for construct in (
                lambda: StreamTransport(AlwaysBlocked(), max_pending=True),
                lambda: DatagramTransport(AlwaysBlocked(), max_pending=True),
                lambda: FixedRecordStream(AlwaysBlocked(), record_size=True),
                lambda: LengthPrefixedStream(
                    AlwaysBlocked(), min_length=False, max_length=8)):
            with self.assertRaises(ValueError):
                construct()


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


class ClosingBackend(worker.NullEngine):
    """Real null plant with an independently observable resource owner."""

    def __init__(self):
        super().__init__(1)
        self.close_calls = 0
        self.step_calls = 0
        self.close_error = None
        self.step_error = None

    def step(self, dt):
        self.step_calls += 1
        if self.step_error is not None:
            raise self.step_error
        return super().step(dt)

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class WorkerRunTests(unittest.TestCase):
    """Real worker protocol paths must release every acquired resource."""

    def setUp(self):
        self.sockets = []
        self.peers = []
        for _ in range(3):
            owned, peer = socket.socketpair()
            self.sockets.append(owned)
            self.peers.append(peer)
            peer.settimeout(0.2)
            self.addCleanup(owned.close)
            self.addCleanup(peer.close)
        self.engine = ClosingBackend()
        connections = dict(zip(("cosim", "can"), self.sockets))
        self.enterContext(patch.object(
            worker, "connect_unix", lambda path, timeout: connections[path]))
        # A stream fd suffices to verify the SocketCAN resource lifecycle;
        # these cases make no assertions about Linux raw CAN behavior.
        self.enterContext(patch.object(
            worker, "open_socketcan", lambda interface: self.sockets[2]))
        self.enterContext(patch.object(
            worker, "load_factory", lambda spec: lambda args: self.engine))
        self.enterContext(patch.object(
            worker.step_protocol, "generate_session_id", lambda: 123))
        with patch.object(sys, "argv", [
                "worker", "--cosim", "cosim", "--fdcan", "can",
                "--socketcan", "test", "--protocol", "v2",
                "--backend", "tests:factory", "--motor-count", "1",
                "--frames", "1"]):
            self.args = worker.parse_args()

    def _reset_ack(self, reserved=0):
        protocol = worker.step_protocol
        return protocol.build_step_frame(
            protocol.RESET_ACK, 0, 0, flags=123,
            payload=struct.pack("<IIQII", 0, protocol.CAPABILITY_MASK,
                                10_000_000_000, 0, reserved))

    def _assert_closed(self, close_calls=1):
        self.assertEqual([sock.fileno() for sock in self.sockets], [-1] * 3)
        self.assertEqual(self.engine.close_calls, close_calls)
        for peer in self.peers:
            # RESET/STEP already written to co-sim may precede the EOF.
            while peer.recv(4096):
                pass

    def test_initial_reset_timeout_closes_all_resources(self):
        # Cross the actual wait's deadline without a wall-clock sleep.
        with patch.object(worker.time, "monotonic", side_effect=[0.0, 3.0]):
            with self.assertRaisesRegex(TimeoutError, "v2 response"):
                worker.run(self.args)
        self.assertEqual(self.engine.step_calls, 0)
        self._assert_closed()

    def test_can_runtime_routes_real_wire_without_legacy_motor_decode(self):
        from sim.fabric import CanBus, CanFrame, VirtualCanDevice
        from sim.fabric.endpoints import QemuCanEndpoint, SocketCanEndpoint
        from sim.fabric.runtime import CanRuntime

        class Echo(VirtualCanDevice):
            def receive(self, frame):
                self.emit(CanFrame(0x201, data=b'feedback',
                                   timestamp_ns=frame.timestamp_ns))

        runtimes = []
        def factory(can, tx):
            bus = CanBus()
            endpoint = QemuCanEndpoint(bus, can)
            socket_endpoint = SocketCanEndpoint(bus, tx)
            Echo(bus)
            runtime = CanRuntime(bus, endpoint, socket_endpoint=socket_endpoint)
            runtimes.append(runtime)
            return runtime

        protocol = worker.step_protocol
        self.peers[0].sendall(self._reset_ack() + protocol.build_step_frame(
            protocol.STEP_ACK, 1, 1_000_000, dt_ns=1_000_000, flags=123,
            payload=bytes(16)))
        # 0x200 is also a legacy float motor ID; exclusive Fabric routing is
        # required. The literal is a C620 +16384 slot, not a legacy float.
        self.peers[1].sendall(struct.pack('<IIB3xQ64s', 0x200, 0, 8,
                                        10_000_000_000,
                                        bytes.fromhex('4000000000000000')))
        worker.run(self.args, can_factory=factory)
        reply = worker.unpack_wire_frame(self.peers[1].recv(84))
        self.assertEqual(reply, CanFrame(0x201, data=b'feedback',
                                       timestamp_ns=10_000_000_000))
        self.assertEqual(runtimes[0].t_sim_ns, 1_000_000)
        self.assertTrue(runtimes[0].coordinator.closed)
        self.assertEqual(self.engine.motors.torque, [0])
        self._assert_closed()

    def test_invalid_can_runtime_is_rejected_before_protocol_and_closed(self):
        calls = []
        runtime = SimpleNamespace(close=lambda: calls.append('close'))
        with self.assertRaisesRegex(TypeError, 'CAN runtime requires'):
            worker.run(self.args, can_factory=lambda can, tx: runtime)
        self.assertEqual(calls, ['close'])
        self.assertEqual(self.engine.step_calls, 0)
        # All peers see EOF without any RESET/STEP or CAN bytes.
        self.assertEqual(self.peers[0].recv(4096), b'')
        self._assert_closed()

    def test_can_runtime_factory_requires_fdcan_before_acquisition(self):
        self.args.fdcan = None
        with self.assertRaisesRegex(ValueError, 'requires callable factory'):
            worker.run(self.args, can_factory=lambda can, tx: None)
        self.assertEqual(self.engine.close_calls, 0)
        self.assertTrue(all(sock.fileno() >= 0 for sock in self.sockets))

    def test_initial_reset_invalid_ack_closes_all_resources(self):
        self.peers[0].sendall(self._reset_ack(reserved=1))
        with self.assertRaisesRegex(worker.step_protocol.StepProtocolError,
                                    "RESET_ACK reserved field"):
            worker.run(self.args)
        self.assertEqual(self.engine.step_calls, 0)
        self._assert_closed()

    def test_initial_reset_eof_closes_all_resources(self):
        # Half-close retains the peer's read side so EOF remains observable.
        self.peers[0].shutdown(socket.SHUT_WR)
        with self.assertRaisesRegex(ConnectionError, "co-sim socket closed"):
            worker.run(self.args)
        self.assertEqual(self.engine.step_calls, 0)
        self._assert_closed()

    def test_initial_reset_interrupt_closes_all_resources(self):
        interrupted = KeyboardInterrupt("RESET interrupted")
        self.engine.close_error = RuntimeError("backend close failed")
        diagnostic = io.StringIO()
        with redirect_stderr(diagnostic), patch.object(
                worker.select, "select", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt) as raised:
                worker.run(self.args)
        self.assertIs(raised.exception, interrupted)
        self.assertIn("backend close failed", diagnostic.getvalue())
        self.assertEqual(self.engine.step_calls, 0)
        self._assert_closed()

    def test_factory_interrupt_closes_already_acquired_transports(self):
        with patch.object(worker, "load_factory", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                worker.run(self.args)
        # The factory never returned a backend for the worker to own.
        self._assert_closed(close_calls=0)

    def test_backend_close_error_after_success_still_closes_transports(self):
        protocol = worker.step_protocol
        self.peers[0].sendall(self._reset_ack() + protocol.build_step_frame(
            protocol.STEP_ACK, 1, 1_000_000, dt_ns=1_000_000, flags=123,
            payload=bytes(16)))
        self.engine.close_error = RuntimeError("backend close failed")
        try:
            raise ValueError("unrelated caller exception")
        except ValueError:
            # A caller's active except block is not a worker primary error.
            with self.assertRaisesRegex(RuntimeError, "backend close failed"):
                worker.run(self.args)
        self.assertEqual(self.engine.step_calls, 1)
        self._assert_closed()

    def test_runtime_error_is_preserved_when_backend_close_also_fails(self):
        self.peers[0].sendall(self._reset_ack())
        primary = RuntimeError("plant step failed")
        self.engine.step_error = primary
        self.engine.close_error = RuntimeError("backend close failed")
        diagnostic = io.StringIO()
        with redirect_stderr(diagnostic):
            with self.assertRaises(RuntimeError) as raised:
                worker.run(self.args)
        self.assertIs(raised.exception, primary)
        self.assertIn("backend close failed", diagnostic.getvalue())
        self.assertEqual(self.engine.step_calls, 1)
        self._assert_closed()

    def test_multiple_close_errors_preserve_primary_and_close_remaining_socket(self):
        class FailingCloseSocket(socket.socket):
            def close(self):
                was_open = self.fileno() != -1
                super().close()
                if was_open:
                    raise OSError("CAN close failed")

        # Preserve a real CAN fd; only its post-close diagnostic is injected.
        can = FailingCloseSocket(fileno=self.sockets[1].detach())
        self.sockets[1] = can
        self.addCleanup(can.close)
        connections = dict(zip(("cosim", "can"), self.sockets))
        self.peers[0].sendall(self._reset_ack())
        primary = RuntimeError("plant step failed")
        self.engine.step_error = primary
        self.engine.close_error = RuntimeError("backend close failed")
        diagnostic = io.StringIO()
        with redirect_stderr(diagnostic), patch.object(
                worker, "connect_unix", lambda path, timeout: connections[path]):
            with self.assertRaises(RuntimeError) as raised:
                worker.run(self.args)
        self.assertIs(raised.exception, primary)
        self.assertIn("backend close failed", diagnostic.getvalue())
        self.assertIn("CAN close failed", diagnostic.getvalue())
        self._assert_closed()

    def _continuous_input_fixture(self, phase):
        """Actual run/parser/queues; only fd readiness, IO and time are controlled."""
        clock = SimpleNamespace(now=0.0)
        observed = SimpleNamespace(reset_started=None, steps=[], writable=set())
        test = self

        def v2_wire(kind, step_id, timestamp, payload=b"", dt=0, session=123):
            return struct.pack("<IIHHIQQQI", 40 + len(payload), 0x32434D44,
                               2, kind, len(payload), step_id, timestamp,
                               dt, session) + payload

        class InputSocket(socket.socket):
            def __init__(self, fd, index, peer):
                super().__init__(fileno=fd)
                self.index = index
                self.peer = peer
                self.reads = 0
                self.turn_reads = 0
                self.output = bytearray()
                self.blocked = index != 0
                self.first_step = True

            def recv(self, size):
                self.turn_reads += 1
                if self.turn_reads > 1:
                    raise AssertionError("continuous input prevented service return")
                self.reads += 1
                if self.index == 0:
                    if self.reads == 1:
                        data = test._reset_ack()
                    elif phase == "done" and self.reads == 2:
                        data = v2_wire(3, 1, 1_000_000, bytes(16), 1_000_000)
                    elif phase == "reset":
                        if observed.reset_started is None:
                            observed.reset_started = clock.now
                        data = v2_wire(1, 0, self.reads * 10_000_000,
                                       session=122 + self.reads)
                    else:
                        data = v2_wire(7, self.reads, self.reads,
                                       bytes(24))
                elif self.index == 1:
                    data = struct.pack("<IIB3xQ64s", 0x321, 0, 1,
                                       10_001_000_000, b"q")
                else:
                    data = struct.pack("<IB3x8s", 0x456, 1, b"s")
                self.peer.sendall(data)
                return super().recv(size)

            def send(self, data):
                if self.index == 0 and self.first_step and \
                        struct.unpack_from("<H", data, 10)[0] == 2:
                    self.first_step = False
                    self.blocked = True
                    observed.steps.append(bytes(data))
                if self.blocked:
                    raise BlockingIOError
                count = super().send(data)
                self.output.extend(data[:count])
                return count

        for index, (owned, peer) in enumerate(zip(self.sockets, self.peers)):
            sock = InputSocket(owned.detach(), index, peer)
            self.sockets[index] = sock
            self.addCleanup(sock.close)
        connections = dict(zip(("cosim", "can"), self.sockets))
        self.enterContext(patch.object(
            worker, "connect_unix", lambda path, timeout: connections[path]))

        def select_io(readable, writable, errors, timeout):
            del errors
            if self.engine.step_calls == 0:
                return [], [], []
            if phase == "reset":
                # Repeated readable RESET does not reach a blocking wait;
                # receipt/dispatch still takes real host time.
                clock.now += 0.02
            for sock in self.sockets:
                sock.turn_reads = 0
            if timeout:
                clock.now += min(timeout, 0.125)
            for sock in writable:
                sock.blocked = False
                observed.writable.add(sock.index)
            # All ready directions retain a service opportunity despite
            # indefinitely readable QEMU CAN and co-sim inputs.
            return list(readable), list(writable), []

        self.args.wait_step_done = phase == "done"
        self.enterContext(patch.object(worker.time, "monotonic", lambda: clock.now))
        self.enterContext(patch.object(worker.select, "select", select_io))
        return clock, observed

    def _assert_continuous_input_cleanup(self, observed, copies):
        self.assertEqual(self.engine.step_calls, 1)
        self.assertEqual(observed.writable, {0, 1, 2})
        self.assertGreater(self.sockets[0].reads, 2)
        self.assertGreater(self.sockets[1].reads, 1)
        self.assertGreater(self.sockets[2].reads, 1)
        self.assertEqual(bytes(self.sockets[0].output),
                         self._reset_wire() + observed.steps[0] * copies)
        # Literal bytes verify both forwarding directions and the existing
        # RESET origin mapping. This stream fd does not model Linux raw CAN.
        self.assertEqual(bytes(self.sockets[1].output), struct.pack(
            "<IIB3xQ64s", 0x456, 0, 1, 10_001_000_000, b"s") *
            self.sockets[2].reads)
        self.assertEqual(bytes(self.sockets[2].output), struct.pack(
            "<IB3x8s", 0x321, 1, b"q") * self.sockets[1].reads)
        self._assert_closed()

    def test_continuous_input_preserves_ack_deadline_and_io_service(self):
        clock, observed = self._continuous_input_fixture("ack")
        with self.assertRaisesRegex(TimeoutError,
                                    "STEP_ACK admission timed out.*retries=3"):
            worker.run(self.args)
        self.assertEqual(clock.now, 1.0)
        self._assert_continuous_input_cleanup(observed, copies=4)

    def _assert_fabric_wait_does_not_step(self, phase):
        from sim.fabric import CanBus
        from sim.fabric.endpoints import QemuCanEndpoint, SocketCanEndpoint
        from sim.fabric.runtime import CanRuntime
        _, observed = self._continuous_input_fixture(phase)
        runtimes = []
        def factory(can, tx):
            bus = CanBus()
            runtime = CanRuntime(bus, QemuCanEndpoint(bus, can),
                                 socket_endpoint=SocketCanEndpoint(bus, tx))
            runtimes.append(runtime)
            return runtime
        # Strict DONE may wait longer than ACK; continuous ingress must hit
        # the explicit bounded capacity rather than silently drop or step.
        with self.assertRaises(BufferError if phase == 'done' else TimeoutError):
            worker.run(self.args, can_factory=factory)
        runtime = runtimes[0]
        self.assertEqual(runtime.coordinator.step_id, 1)
        self.assertEqual(runtime.t_sim_ns, 1_000_000)
        self.assertEqual(runtime.bus.pending,
                         self.sockets[1].reads + self.sockets[2].reads -
                         (1 if phase == 'done' else 0))
        if phase == 'done':
            self.assertEqual(runtime.bus.pending, 256)
        self.assertGreater(runtime.bus.pending, 2)
        self.assertEqual(bytes(self.sockets[1].output), b'')
        self.assertEqual(bytes(self.sockets[2].output), b'')
        self.assertTrue(runtime.coordinator.closed)
        self.assertEqual(len(observed.steps), 1)
        self._assert_closed()

    def test_fabric_ack_wait_only_enqueues_io(self):
        self._assert_fabric_wait_does_not_step('ack')

    def test_fabric_done_wait_rejects_continuous_input_at_capacity(self):
        self._assert_fabric_wait_does_not_step('done')

    def test_fabric_repeated_reset_wait_only_enqueues_io(self):
        self._assert_fabric_wait_does_not_step('reset')

    def test_continuous_input_preserves_done_deadlines_and_io_service(self):
        clock, observed = self._continuous_input_fixture("done")
        with self.assertRaisesRegex(TimeoutError, "timed out waiting for v2 response"):
            worker.run(self.args)
        self.assertEqual(clock.now, 8.0)
        self._assert_continuous_input_cleanup(observed, copies=4)

    def test_continuous_input_preserves_repeated_reset_deadline_and_io_service(self):
        clock, observed = self._continuous_input_fixture("reset")
        with self.assertRaisesRegex(TimeoutError, "timed out waiting for v2"):
            worker.run(self.args)
        self.assertGreaterEqual(clock.now - observed.reset_started, 2.0)
        self.assertLessEqual(clock.now - observed.reset_started, 2.02)
        self._assert_continuous_input_cleanup(observed, copies=1)

    def _strict_done_fixture(self, partial=False, recover=False, closed=False,
                             backpressure=True):
        """Inject socket writability and time, retaining the real run/queues."""
        protocol = worker.step_protocol
        clock = SimpleNamespace(now=0.0)
        observations = SimpleNamespace(waits=[], pending=[], stream=None)
        ack = protocol.build_step_frame(
            protocol.STEP_ACK, 1, 1_000_000, dt_ns=1_000_000,
            flags=123, payload=bytes(16))
        done = protocol.build_step_frame(
            protocol.STEP_DONE, 1, 1_000_000, dt_ns=1_000_000,
            flags=123, payload=struct.pack("<IIIII", 0, 3, 0, 0, 0))
        peer = self.peers[0]

        class ReplaySocket(socket.socket):
            def __init__(self, fd):
                super().__init__(fileno=fd)
                self.output = bytearray()
                self.step_wire = None
                self.replay_started = False
                self.writable = False
                self.close_calls = 0
                self.done_sent = False

            def send(self, data):
                wire = bytes(data)
                if self.step_wire is None and struct.unpack_from(
                        "<H", wire, 10)[0] == protocol.STEP:
                    self.step_wire = wire
                elif self.step_wire is not None and backpressure and \
                        not self.writable:
                    if partial and not self.replay_started:
                        self.replay_started = True
                        count = super().send(wire[:7])
                        self.output.extend(wire[:count])
                        return count
                    self.replay_started = True
                    raise BlockingIOError
                count = super().send(data)
                self.output.extend(wire[:count])
                if self.writable and not self.done_sent:
                    # A recovered replay gets an idempotent ACK and DONE in
                    # one real read, exercising parser/validator retention.
                    peer.sendall(ack + done)
                    self.done_sent = True
                return count

            def close(self):
                if self.fileno() != -1:
                    self.close_calls += 1
                super().close()

        sock = ReplaySocket(self.sockets[0].detach())
        self.sockets[0] = sock
        self.addCleanup(sock.close)
        connections = dict(zip(("cosim", "can"), self.sockets))
        self.enterContext(patch.object(
            worker, "connect_unix", lambda path, timeout: connections[path]))
        stream_class = worker.StepFramedSocket

        def capture_stream(owned):
            observations.stream = stream_class(owned)
            return observations.stream

        def select_io(readable, writable, errors, timeout):
            del readable, errors
            if timeout == 0:
                return [], [], []
            observations.waits.append(timeout)
            observations.pending.append([
                bytes(view) for view in observations.stream.tx_queue])
            if recover and len(observations.waits) == 3:
                # The first replay has survived two DONE timeouts. Resume
                # its original bytes before this third deadline expires.
                clock.now += 1.0
                sock.writable = True
                self.assertIn(sock, writable)
                return [], [sock], []
            clock.now += timeout
            if closed and len(observations.waits) == 2:
                clock.now -= 1.0
                peer.shutdown(socket.SHUT_WR)
                return [sock], [], []
            return [], [], []

        self.args.wait_step_done = True
        peer.sendall(self._reset_ack() + ack)
        self.enterContext(patch.object(worker, "StepFramedSocket", capture_stream))
        self.enterContext(patch.object(worker.time, "monotonic", lambda: clock.now))
        self.enterContext(patch.object(worker.select, "select", select_io))
        return sock, clock, observations

    def _assert_strict_done_pending(self, sock, observations, partial):
        # The first wait follows an already transmitted/admitted STEP. Its
        # first timeout permits one replay; later timeouts must keep one head.
        suffix = sock.step_wire[7:] if partial else sock.step_wire
        self.assertEqual(self.engine.step_calls, 1)
        self.assertEqual(sock.close_calls, 1)
        self._assert_closed()
        self.assertEqual(observations.pending,
                         [[]] + [[suffix]] * (len(observations.waits) - 1))

    def test_strict_done_blocked_replay_is_not_copied_until_timeout(self):
        sock, clock, observations = self._strict_done_fixture()
        with self.assertRaisesRegex(TimeoutError, "timed out waiting for v2 response"):
            worker.run(self.args)
        self.assertEqual(clock.now, 8.0)
        self.assertEqual(observations.waits, [2.0] * 4)
        self._assert_strict_done_pending(sock, observations, partial=False)
        self.assertEqual(bytes(sock.output), self._reset_wire() + sock.step_wire)

    def test_strict_done_partial_replay_is_not_copied_until_timeout(self):
        sock, clock, observations = self._strict_done_fixture(partial=True)
        with self.assertRaisesRegex(TimeoutError, "timed out waiting for v2 response"):
            worker.run(self.args)
        self.assertEqual(clock.now, 8.0)
        self.assertEqual(observations.waits, [2.0] * 4)
        self._assert_strict_done_pending(sock, observations, partial=True)
        self.assertEqual(bytes(sock.output),
                         self._reset_wire() + sock.step_wire + sock.step_wire[:7])

    def test_strict_done_blocked_replay_recovers_exactly_once(self):
        sock, clock, observations = self._strict_done_fixture(recover=True)
        self.assertEqual(worker.run(self.args), 0)
        self.assertEqual(clock.now, 5.0)
        self.assertEqual(observations.waits, [2.0] * 3)
        self._assert_strict_done_pending(sock, observations, partial=False)
        self.assertFalse(observations.stream.tx_pending)
        self.assertEqual(bytes(sock.output), self._reset_wire() + sock.step_wire * 2)

    def test_strict_done_partial_replay_recovers_exactly_once(self):
        sock, clock, observations = self._strict_done_fixture(
            partial=True, recover=True)
        self.assertEqual(worker.run(self.args), 0)
        self.assertEqual(clock.now, 5.0)
        self.assertEqual(observations.waits, [2.0] * 3)
        self._assert_strict_done_pending(sock, observations, partial=True)
        self.assertFalse(observations.stream.tx_pending)
        self.assertEqual(bytes(sock.output), self._reset_wire() + sock.step_wire * 2)

    def test_strict_done_without_backpressure_preserves_three_replays(self):
        sock, clock, observations = self._strict_done_fixture(backpressure=False)
        with self.assertRaisesRegex(TimeoutError, "timed out waiting for v2 response"):
            worker.run(self.args)
        self.assertEqual(clock.now, 8.0)
        self.assertEqual(observations.waits, [2.0] * 4)
        self.assertEqual(observations.pending, [[]] * 4)
        self.assertEqual(self.engine.step_calls, 1)
        self.assertEqual(sock.close_calls, 1)
        self._assert_closed()
        self.assertEqual(bytes(sock.output), self._reset_wire() + sock.step_wire * 4)

    def test_strict_done_pending_replay_closed_propagates_and_cleans_up(self):
        sock, clock, observations = self._strict_done_fixture(closed=True)
        with self.assertRaisesRegex(ConnectionError, "co-sim socket closed"):
            worker.run(self.args)
        self.assertEqual(clock.now, 3.0)
        self.assertEqual(observations.waits, [2.0] * 2)
        self._assert_strict_done_pending(sock, observations, partial=False)
        self.assertEqual(bytes(sock.output), self._reset_wire() + sock.step_wire)

    def _pending_reset_fixture(self, prefix=7, done_phase=False,
                               outcome="recover"):
        """Keep actual queues and protocol; control only socket IO and time."""
        protocol = worker.step_protocol
        clock = SimpleNamespace(now=0.0)
        observed = SimpleNamespace(stream=None, pending=[], resets=[],
                                   reset_started=None, latest_ack_sent=False,
                                   new_waits=[])
        peer = self.peers[0]
        old_copies = 2 if done_phase else 1

        def reply(kind, session, timestamp, payload=b"", dt=0):
            return protocol.build_step_frame(
                kind, 0 if kind in (protocol.RESET, protocol.RESET_ACK) else 1,
                timestamp, dt_ns=dt, flags=session, payload=payload)

        class PendingSocket(socket.socket):
            def __init__(self, fd):
                super().__init__(fileno=fd)
                self.output = bytearray()
                self.old_wire = None
                self.new_wire = None
                self.old_offset = 0
                self.old_completed = 0
                self.pending_started = False
                self.writable = False
                self.close_calls = 0

            def send(self, data):
                wire = bytes(data)
                if self.old_wire is None:
                    if struct.unpack_from("<H", wire, 10)[0] == protocol.RESET:
                        count = super().send(data)
                        self.output.extend(wire[:count])
                        return count
                    self.old_wire = wire
                if self.old_completed < old_copies:
                    # The first DONE-phase STEP has already been admitted;
                    # its first timeout starts one exact partial replay.
                    initial_done = done_phase and self.old_completed == 0
                    if not initial_done and not self.writable:
                        if not self.pending_started and prefix:
                            count = super().send(wire[:prefix])
                            self.output.extend(wire[:count])
                            self.old_offset += count
                            self.pending_started = True
                            return count
                        self.pending_started = True
                        raise BlockingIOError
                    count = super().send(data)
                    self.output.extend(wire[:count])
                    self.old_offset += count
                    if self.old_offset == len(self.old_wire):
                        self.old_completed += 1
                        self.old_offset = 0
                        if initial_done:
                            peer.sendall(reply(protocol.STEP_ACK, 123,
                                               1_000_000, bytes(16), 1_000_000))
                    return count
                self.new_wire = wire
                count = super().send(data)
                self.output.extend(wire[:count])
                peer.sendall(reply(protocol.STEP_ACK, 125, 31_000_000,
                                   bytes(16), 1_000_000))
                if done_phase:
                    peer.sendall(reply(protocol.STEP_DONE, 125, 31_000_000,
                                       struct.pack("<IIIII", 0, 3, 0, 0, 0),
                                       1_000_000))
                return count

            def close(self):
                if self.fileno() != -1:
                    self.close_calls += 1
                super().close()

        sock = PendingSocket(self.sockets[0].detach())
        self.sockets[0] = sock
        self.addCleanup(sock.close)
        connections = dict(zip(("cosim", "can"), self.sockets))
        self.enterContext(patch.object(
            worker, "connect_unix", lambda path, timeout: connections[path]))
        stream_class = worker.StepFramedSocket

        def capture_stream(owned):
            observed.stream = stream_class(owned)
            return observed.stream

        real_reset = self.engine.reset

        def reset_backend():
            self.assertTrue(observed.latest_ack_sent)
            self.assertEqual(self.engine.step_calls, 1)
            observed.resets.append(clock.now)
            real_reset()

        reset_stage = 0

        def select_io(readable, writable, errors, timeout):
            nonlocal reset_stage
            del readable, errors
            if timeout == 0:
                return [], [], []
            if done_phase and not sock.pending_started:
                self.assertEqual(timeout, 2.0)
                clock.now += timeout
                return [], [], []
            observed.pending.append([
                bytes(view) for view in observed.stream.tx_queue])
            if reset_stage < 2:
                self.assertEqual(observed.resets, [])
                self.assertEqual(self.engine.step_calls, 1)
                clock.now += 0.1 if reset_stage == 0 else 0.4
                if reset_stage == 0:
                    observed.reset_started = clock.now
                # No ACK for the intermediate session: only the latest
                # marker may rebuild the backend, without renewing 2 s.
                peer.sendall(reply(protocol.RESET, 124 + reset_stage,
                                   20_000_000 + reset_stage * 10_000_000))
                reset_stage += 1
                return [sock], [], []
            if reset_stage == 2:
                reset_stage += 1
                self.assertEqual(observed.resets, [])
                self.assertEqual(self.engine.step_calls, 1)
                if outcome == "timeout":
                    clock.now += timeout
                    return [], [], []
                clock.now += 0.4
                if outcome == "closed":
                    peer.shutdown(socket.SHUT_WR)
                else:
                    peer.sendall(reply(
                        protocol.RESET_ACK, 125, 30_000_000,
                        struct.pack("<IIQII", 0, protocol.CAPABILITY_MASK,
                                    10_030_000_000, 0, 0)))
                    observed.latest_ack_sent = True
                return [sock], [], []
            # The ACK arrived while the old physical frame was blocked.
            # Its unchanged suffix must now precede the new-session STEP.
            self.assertEqual(len(observed.resets), 1)
            self.assertEqual(self.engine.step_calls, 2)
            observed.new_waits.append(timeout)
            if outcome == "post_ack_stall":
                clock.now += timeout
                return [], [], []
            self.assertIn(sock, writable)
            clock.now += 0.05
            sock.writable = True
            return [], [sock], []

        self.args.wait_step_done = done_phase
        peer.sendall(self._reset_ack())
        self.enterContext(patch.object(worker, "StepFramedSocket", capture_stream))
        self.enterContext(patch.object(self.engine, "reset", reset_backend))
        self.enterContext(patch.object(worker.time, "monotonic", lambda: clock.now))
        self.enterContext(patch.object(worker.select, "select", select_io))
        return sock, clock, observed

    def _assert_pending_reset(self, sock, observed, prefix, done_phase=False,
                              recovered=True):
        self.assertEqual(sock.close_calls, 1)
        self._assert_closed()
        # Independent literal header oracle, including session and times.
        self.assertEqual(struct.unpack("<IIHHIQQQI", sock.old_wire[:44]),
                         (100, 0x32434D44, 2, 2, 60, 1, 1_000_000,
                          1_000_000, 123))
        suffix = sock.old_wire[prefix:]
        self.assertEqual(observed.pending[:3], [[suffix]] * 3)
        if recovered:
            self.assertEqual(len(observed.resets), 1)
            self.assertEqual(self.engine.step_calls, 2)
            self.assertEqual(struct.unpack("<IIHHIQQQI", sock.new_wire[:44]),
                             (100, 0x32434D44, 2, 2, 60, 1, 31_000_000,
                              1_000_000, 125))
            self.assertEqual(observed.pending[3:], [[suffix, sock.new_wire]])
            copies = 2 if done_phase else 1
            self.assertEqual(bytes(sock.output), self._reset_wire() +
                             sock.old_wire * copies + sock.new_wire)
            self.assertFalse(observed.stream.tx_pending)
        else:
            copies = 1 if done_phase else 0
            self.assertEqual(bytes(sock.output), self._reset_wire() +
                             sock.old_wire * copies + sock.old_wire[:prefix])

    def test_pending_unstarted_step_survives_repeated_reset(self):
        sock, clock, observed = self._pending_reset_fixture(prefix=0)
        self.assertEqual(worker.run(self.args), 0)
        self.assertAlmostEqual(clock.now, 0.95)
        self._assert_pending_reset(sock, observed, prefix=0)

    def test_pending_partial_step_survives_repeated_reset(self):
        sock, clock, observed = self._pending_reset_fixture()
        self.assertEqual(worker.run(self.args), 0)
        self.assertAlmostEqual(clock.now, 0.95)
        self._assert_pending_reset(sock, observed, prefix=7)

    def test_pending_done_replay_survives_repeated_reset(self):
        sock, clock, observed = self._pending_reset_fixture(done_phase=True)
        self.assertEqual(worker.run(self.args), 0)
        self.assertAlmostEqual(clock.now, 2.95)
        self._assert_pending_reset(sock, observed, prefix=7, done_phase=True)

    def test_pending_reset_timeout_keeps_original_handshake_budget(self):
        sock, clock, observed = self._pending_reset_fixture(outcome="timeout")
        with self.assertRaisesRegex(TimeoutError, "timed out waiting for v2"):
            worker.run(self.args)
        self.assertAlmostEqual(clock.now - observed.reset_started, 2.0)
        self.assertEqual(observed.resets, [])
        self.assertEqual(self.engine.step_calls, 1)
        self._assert_pending_reset(sock, observed, prefix=7, recovered=False)

    def test_pending_reset_eof_propagates_and_cleans_up(self):
        sock, clock, observed = self._pending_reset_fixture(outcome="closed")
        with self.assertRaisesRegex(ConnectionError, "co-sim socket closed"):
            worker.run(self.args)
        self.assertAlmostEqual(clock.now, 0.9)
        self.assertEqual(observed.resets, [])
        self.assertEqual(self.engine.step_calls, 1)
        self._assert_pending_reset(sock, observed, prefix=7, recovered=False)

    def test_pending_step_stall_after_reset_ack_keeps_both_frames(self):
        sock, clock, observed = self._pending_reset_fixture(
            outcome="post_ack_stall")
        with self.assertRaisesRegex(TimeoutError, "STEP_ACK admission timed out"):
            worker.run(self.args)
        # Four silent ACK waits retain the pre-existing retry cap within
        # this new STEP's independent absolute 2 s admission budget.
        self.assertEqual(len(observed.new_waits), 4)
        for wait in observed.new_waits:
            self.assertAlmostEqual(wait, 0.25)
        self.assertAlmostEqual(clock.now - observed.resets[0], 1.0)
        self.assertEqual(len(observed.resets), 1)
        self.assertEqual(self.engine.step_calls, 2)
        self._assert_pending_reset(sock, observed, prefix=7, recovered=False)
        new_wire = observed.pending[3][1]
        self.assertEqual(struct.unpack("<IIHHIQQQI", new_wire[:44]),
                         (100, 0x32434D44, 2, 2, 60, 1, 31_000_000,
                          1_000_000, 125))
        self.assertEqual(observed.pending[3:],
                         [[sock.old_wire[7:], new_wire]] * 4)

    @staticmethod
    def _reset_wire():
        # Independent fixed header oracle for the initial host RESET.
        return struct.pack("<IIHHIQQQI", 40, 0x32434D44, 2, 1, 0, 0, 0, 0, 123)

    def test_closed_stderr_cannot_replace_primary_during_cleanup(self):
        self.peers[0].sendall(self._reset_ack())
        primary = RuntimeError("plant step failed")
        self.engine.step_error = primary
        self.engine.close_error = RuntimeError("backend close failed")
        diagnostic = io.StringIO()
        diagnostic.close()
        with redirect_stderr(diagnostic):
            with self.assertRaises(RuntimeError) as raised:
                worker.run(self.args)
        self.assertIs(raised.exception, primary)
        self.assertTrue(any("backend close failed" in note
                            for note in primary.__notes__))
        self._assert_closed()


class WorkerConnectionTests(unittest.TestCase):
    """Connection setup owns real fds until it returns them to the worker."""

    def _socket(self):
        class SetupSocket(socket.socket):
            connect_error = None
            configure_error = None
            close_error = None
            close_calls = 0

            def connect(self, path):
                if self.connect_error is not None:
                    raise self.connect_error

            def setblocking(self, flag):
                if self.configure_error is not None:
                    raise self.configure_error
                super().setblocking(flag)

            def close(self):
                was_open = self.fileno() != -1
                super().close()
                if was_open:
                    self.close_calls += 1
                    if self.close_error is not None:
                        raise self.close_error

        owned, peer = socket.socketpair()
        sock = SetupSocket(fileno=owned.detach())
        self.addCleanup(sock.close)
        self.addCleanup(peer.close)
        peer.settimeout(0.2)
        return sock, peer

    def test_connection_setup_interrupt_closes_owned_fd(self):
        for phase in ("connect", "configure"):
            with self.subTest(phase=phase):
                sock, peer = self._socket()
                primary = KeyboardInterrupt(phase + " interrupted")
                setattr(sock, phase + "_error", primary)
                with patch.object(worker.socket, "socket", return_value=sock):
                    with self.assertRaises(KeyboardInterrupt) as raised:
                        worker.connect_unix("fixture", 1.0)
                self.assertIs(raised.exception, primary)
                self.assertEqual(sock.fileno(), -1)
                self.assertEqual(sock.close_calls, 1)
                self.assertEqual(peer.recv(1), b"")

    def test_connection_close_failure_preserves_error_and_stops_retry(self):
        sock, peer = self._socket()
        primary = OSError(errno.ECONNREFUSED, "fixture refused")
        sock.connect_error = primary
        sock.close_error = OSError("fixture close failed")
        diagnostic = io.StringIO()
        with redirect_stderr(diagnostic), \
                patch.object(worker.socket, "socket", return_value=sock) as create, \
                patch.object(worker.time, "sleep") as sleep:
            with self.assertRaises(OSError) as raised:
                worker.connect_unix("fixture", 1.0)
        self.assertIs(raised.exception, primary)
        self.assertTrue(any("fixture close failed" in note
                            for note in primary.__notes__))
        self.assertIn("fixture close failed", diagnostic.getvalue())
        self.assertEqual(create.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(sock.close_calls, 1)
        self.assertEqual(peer.recv(1), b"")

    def test_retry_closes_failed_socket_and_transfers_success(self):
        first, peer = self._socket()
        second, unused_peer = self._socket()
        first.connect_error = OSError(errno.ECONNREFUSED, "fixture refused")
        with patch.object(worker.socket, "socket", side_effect=[first, second]), \
                patch.object(worker.time, "monotonic", side_effect=[0.0, 0.0, 0.01]), \
                patch.object(worker.time, "sleep") as sleep:
            returned = worker.connect_unix("fixture", 0.05)
        self.assertIs(returned, second)
        self.assertEqual(first.close_calls, 1)
        self.assertEqual(peer.recv(1), b"")
        self.assertNotEqual(second.fileno(), -1)
        self.assertFalse(second.getblocking())
        self.assertEqual(second.close_calls, 0)
        sleep.assert_called_once_with(0.01)

    def test_retry_deadline_does_not_retain_failed_socket(self):
        sock, peer = self._socket()
        sock.connect_error = OSError(errno.ECONNREFUSED, "fixture refused")
        with patch.object(worker.socket, "socket", return_value=sock) as create, \
                patch.object(worker.time, "monotonic", side_effect=[0.0, 0.0, 0.02]), \
                patch.object(worker.time, "sleep"):
            with self.assertRaisesRegex(TimeoutError, "cannot connect.*fixture refused"):
                worker.connect_unix("fixture", 0.01)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(sock.close_calls, 1)
        self.assertEqual(peer.recv(1), b"")

    def test_worker_second_connection_interrupt_closes_both_owners(self):
        cosim, cosim_peer = self._socket()
        can, can_peer = self._socket()
        interrupted = KeyboardInterrupt("CAN connect interrupted")
        can.connect_error = interrupted
        with patch.object(sys, "argv", [
                "worker", "--cosim", "fixture", "--fdcan", "can", "--frames", "1"]):
            args = worker.parse_args()
        with patch.object(worker.socket, "socket", side_effect=[cosim, can]), \
                patch.object(worker, "create_engine") as factory:
            with self.assertRaises(KeyboardInterrupt) as raised:
                worker.run(args)
        self.assertIs(raised.exception, interrupted)
        factory.assert_not_called()
        self.assertEqual((cosim.close_calls, can.close_calls), (1, 1))
        self.assertEqual(cosim_peer.recv(1), b"")
        self.assertEqual(can_peer.recv(1), b"")


class WorkerTransportTests(unittest.TestCase):
    @staticmethod
    def _legacy_wire(seq=1):
        # Independent legacy header; the payload is deliberately opaque.
        return struct.pack("<IIHHIQQ", 29, 0x32434D44, 1, 3, 1,
                           seq, seq * 10) + b"x"

    @staticmethod
    def _step_ack_wire(step_id=1):
        # Independent fixed v2 ACK header and its four zero status fields.
        return struct.pack("<IIHHIQQQI4I", 56, 0x32434D44, 2, 3, 16,
                           step_id, step_id * 10, 10, 123, 0, 0, 0, 0)

    def test_legacy_receive_returns_before_another_read(self):
        sock = ReceiveChunks([self._legacy_wire()])
        stream = worker.FramedSocket(sock)
        self.assertEqual(stream.receive(), [(3, 1, 10, b"x")])
        self.assertEqual(sock.reads, 1)

    def test_v2_receive_returns_before_another_read(self):
        sock = ReceiveChunks([self._step_ack_wire()])
        frames = worker.StepFramedSocket(sock).receive()
        self.assertEqual(len(frames), 1)
        self.assertEqual((frames[0].header.kind, frames[0].header.step_id,
                          frames[0].header.t_sim_ns, frames[0].payload),
                         (3, 1, 10, bytes(16)))
        self.assertEqual(sock.reads, 1)

    def test_can_receive_returns_before_another_read(self):
        wire = struct.pack("<IIB3xQ64s", 0x123, 0, 2, 10, b"ab")
        sock = ReceiveChunks([wire])
        self.assertEqual(worker.CanStream(sock).receive(),
                         [(0x123, 0, 2, 10, b"ab")])
        self.assertEqual(sock.reads, 1)

    def test_stream_receive_retains_fragments_order_and_eof(self):
        for stream_class, first, second in (
                (worker.FramedSocket, self._legacy_wire(1),
                 self._legacy_wire(2)),
                (worker.StepFramedSocket, self._step_ack_wire(1),
                 self._step_ack_wire(2)),
                (worker.CanStream,
                 struct.pack("<IIB3xQ64s", 1, 0, 1, 10, b"a"),
                 struct.pack("<IIB3xQ64s", 2, 0, 1, 20, b"b"))):
            with self.subTest(stream=stream_class.__name__):
                sock = ReceiveChunks([
                    first[:3], first[3:] + second[:7], second[7:], b""])
                stream = stream_class(sock)
                self.assertEqual(stream.receive(), [])
                self.assertEqual(bytes(stream.buffer), first[:3])
                sock.allow_read()
                frames = stream.receive()
                self.assertEqual(len(frames), 1)
                self.assertEqual(bytes(stream.buffer), second[:7])
                sock.allow_read()
                next_frames = stream.receive()
                self.assertEqual(len(next_frames), 1)
                if stream_class is worker.StepFramedSocket:
                    identities = [f.header.step_id for f in frames + next_frames]
                else:
                    identities = [f[1] if stream_class is worker.FramedSocket
                                  else f[0] for f in frames + next_frames]
                self.assertEqual(identities, [1, 2])
                self.assertEqual(stream.buffer, b"")
                sock.allow_read()
                with self.assertRaisesRegex(ConnectionError, "socket closed"):
                    stream.receive()

    def test_v2_push_front_returns_old_batch_without_reading(self):
        sock = ReceiveChunks([
            self._step_ack_wire(1) + self._step_ack_wire(2),
            self._step_ack_wire(3)])
        stream = worker.StepFramedSocket(sock)
        frames = stream.receive()
        self.assertEqual([f.header.step_id for f in frames], [1, 2])
        stream.push_front(frames[1:])
        self.assertEqual(stream.receive(), frames[1:])
        self.assertEqual(sock.reads, 1)
        sock.allow_read()
        self.assertEqual([f.header.step_id for f in stream.receive()], [3])
        self.assertEqual(sock.reads, 2)

    def test_stream_receive_rejects_invalid_chunk_without_another_read(self):
        for stream_class, wire, message in (
                (worker.FramedSocket, struct.pack("<I", 27), "frame length"),
                (worker.StepFramedSocket,
                 struct.pack("<I", 27), "frame length"),
                (worker.CanStream,
                 struct.pack("<IIB3xQ64s", 0x123, 0, 65, 10, b"x"),
                 "DLC")):
            with self.subTest(stream=stream_class.__name__):
                stream = stream_class(ReceiveChunks([wire]))
                with self.assertRaisesRegex(ValueError, message):
                    stream.receive()

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
