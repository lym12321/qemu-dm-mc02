"""Direct consumers of host CAN delivery and retained transport codecs.

Socketpairs exercise the actual worker queues and byte formats, without a
physical SocketCAN device or any claim about QEMU CPU synchronization.
"""

import importlib.util
from pathlib import Path
import socket
import struct
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sim.core import Coordinator, Session, StepResult
from sim.fabric import CanBus, CanFrame, VirtualCanDevice
from sim.fabric.endpoints import QemuCanEndpoint, SocketCanEndpoint
from sim.fabric.frame import WIRE_FD, WIRE_BRS
from sim.fabric.runtime import CanParticipant, CanRuntime

SPEC = importlib.util.spec_from_file_location(
    "endpoint_worker", ROOT / "tools" / "dm_mc02_sim_worker.py")
assert SPEC is not None and SPEC.loader is not None
worker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = worker
SPEC.loader.exec_module(worker)


def wire(can_id, flags, dlc, timestamp, data):
    # The existing public fixed84 format, independently expressed as bytes.
    return struct.pack("<IIB3xQ64s", can_id, flags, dlc, timestamp,
                       data.ljust(64, b"\0"))


class Device(VirtualCanDevice):
    def __init__(self, bus, name="device"):
        self.commands = []
        self.steps = []
        self.close_calls = 0
        self.result = None
        self.reset_error = None
        self.close_error = None
        super().__init__(bus, name)

    def receive(self, frame):
        self.commands.append(frame)

    def reset(self, session):
        if self.reset_error:
            raise self.reset_error
        self.commands.clear()
        self.steps.clear()

    def step(self, ctx):
        self.steps.append((ctx, tuple(self.commands)))
        self.emit(CanFrame(0x201, data=b"state", timestamp_ns=ctx.t_sim_ns))
        return self.result or StepResult("ok", ctx.t_sim_ns)

    def close(self):
        self.close_calls += 1
        if self.close_error:
            raise self.close_error


@pytest.fixture
def streams():
    left, right = socket.socketpair()
    left.setblocking(False)
    right.setblocking(False)
    try:
        yield worker.CanStream(left), right
    finally:
        left.close()
        right.close()


def runtime_for(stream, *, bus=None, devices=None):
    bus = CanBus() if bus is None else bus
    endpoint = QemuCanEndpoint(bus, stream)
    devices = [Device(bus)] if devices is None else devices
    return CanRuntime(bus, endpoint, devices), devices


@pytest.mark.parametrize("flags,dlc,data", [
    (0, 8, bytes(range(8))),
    (WIRE_FD | WIRE_BRS, 15, bytes(range(64))),
])
def test_fixed84_classic_and_fd_ingress_and_reset_epoch_egress(
        streams, flags, dlc, data):
    stream, peer = streams
    runtime, (device,) = runtime_for(stream)
    assert runtime.endpoint.submitted_frames == 0
    runtime.reset(7, 100, worker.QemuTimestampMapper(9000, 100))
    peer.sendall(wire(0x310, flags, dlc, 9005, data))
    (can_id, received_flags, received_dlc, timestamp, payload), = stream.receive()
    runtime.ingest_qemu(CanFrame(can_id, received_flags, received_dlc,
                                payload, timestamp), 100)
    assert runtime.t_sim_ns == runtime.bus.t_sim_ns == 100
    assert device.commands == []
    runtime.step(5)
    assert device.commands == [CanFrame(0x310, flags, dlc, data, 105)]
    assert runtime.endpoint.submitted_frames == 1
    assert peer.recv(84) == wire(0x201, 0, 5, 9005, b"state")
    runtime.close()
    assert stream.sock.fileno() >= 0 and peer.fileno() >= 0


def test_old_qemu_epoch_uses_current_fallback_without_advancing_bus(streams):
    stream, _ = streams
    runtime, (device,) = runtime_for(stream)
    runtime.reset(2, 50, worker.QemuTimestampMapper(1000, 50))
    runtime.ingest_qemu(CanFrame(0x200, data=b"old", timestamp_ns=999), 50)
    assert runtime.bus.pending == 1 and runtime.bus.t_sim_ns == 50
    runtime.step(1)
    assert device.steps[0][1] == (CanFrame(0x200, data=b"old", timestamp_ns=50),)
    runtime.close()


def test_partial_rx_survives_runtime_reset_and_uses_new_mapper(streams):
    stream, peer = streams
    runtime, (device,) = runtime_for(stream)
    runtime.reset(1, 0, worker.QemuTimestampMapper(1000, 0))
    raw = wire(0x200, 0, 8, 1005, b"12345678")
    peer.sendall(raw[:23])
    assert stream.receive() == []
    assert bytes(stream.buffer) == raw[:23]
    runtime.reset(2, 10, worker.QemuTimestampMapper(2000, 10))
    assert bytes(stream.buffer) == raw[:23]
    peer.sendall(raw[23:])
    (can_id, flags, dlc, timestamp, data), = stream.receive()
    runtime.ingest_qemu(CanFrame(can_id, flags, dlc, data, timestamp), 10)
    runtime.step(1)
    assert device.steps[0][1] == (CanFrame(0x200, 0, 8, b"12345678", 10),)
    runtime.close()


class ShortSocket:
    """A deterministic short-send injection over a real socketpair."""

    def __init__(self, sock):
        self.sock = sock
        self.limit = 7

    def send(self, data):
        return self.sock.send(data[:self.limit])

    def recv(self, size):
        return self.sock.recv(size)


def test_partial_tx_retains_old_wire_suffix_across_new_reset(streams):
    original, peer = streams
    short = ShortSocket(original.sock)
    stream = worker.CanStream(short)
    runtime, (device,) = runtime_for(stream)
    runtime.reset(1, 0, worker.QemuTimestampMapper(1000, 0))
    runtime.step(1)
    old = wire(0x201, 0, 5, 1001, b"state")
    assert bytes(stream.tx_queue[0]) == old[7:]
    assert runtime.endpoint.submitted_frames == 1
    runtime.reset(2, 0, worker.QemuTimestampMapper(2000, 0))
    assert bytes(stream.tx_queue[0]) == old[7:]
    assert runtime.endpoint.submitted_frames == 1
    runtime.step(1)
    new = wire(0x201, 0, 5, 2001, b"state")
    assert runtime.endpoint.submitted_frames == 2
    short.limit = 4096
    assert stream.flush()
    assert peer.recv(168) == old + new
    assert runtime.endpoint.submitted_frames == 2
    assert len(device.steps) == 1
    runtime.close()


def test_successful_submission_counts_queued_record_but_not_refusal_or_flush(streams):
    stream, peer = streams
    stream.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
    stream.max_pending = 1
    bus = CanBus()
    endpoint = QemuCanEndpoint(bus, stream)
    endpoint.bind_mapper(worker.QemuTimestampMapper(1000, 0))
    frame = CanFrame(0x201, data=b"state", timestamp_ns=10)
    for total in range(1, 129):
        endpoint.receive(frame)
        if stream.tx_pending:
            break
    else:
        pytest.fail("finite CAN prefill did not reach socket backpressure")
    assert endpoint.submitted_frames == total
    expected = wire(0x201, 0, 5, 1010, b"state") * total
    suffix = stream.tx_queue[0]
    assert len(stream.tx_queue) == 1 and suffix and expected.endswith(bytes(suffix))
    with pytest.raises(BufferError, match="FDCAN TX queue is full"):
        endpoint.receive(frame)
    assert endpoint.submitted_frames == total and stream.tx_queue[0] is suffix
    assert not stream.flush() and stream.tx_queue[0] is suffix
    endpoint.bind_mapper(worker.QemuTimestampMapper(2000, 0))
    bus.reset(50)
    assert endpoint.submitted_frames == total and stream.tx_queue[0] is suffix

    received = bytearray()
    for _ in range(32):
        try:
            received.extend(peer.recv(4096))
        except BlockingIOError:
            pass
        stream.flush()
        if len(received) == len(expected):
            break
    assert bytes(received) == expected and not stream.tx_pending
    assert endpoint.submitted_frames == total
    assert stream.sock.fileno() >= 0 and peer.fileno() >= 0


def test_socketcan_native_datagram_both_directions_current_timestamp(streams):
    stream, peer = streams
    left, right = socket.socketpair(type=socket.SOCK_DGRAM)
    left.setblocking(False)
    right.setblocking(False)
    try:
        bus = CanBus()
        qemu = QemuCanEndpoint(bus, stream)
        queue = worker.DatagramQueue(left)
        native = SocketCanEndpoint(bus, queue)
        runtime = CanRuntime(bus, qemu, socket_endpoint=native)
        runtime.reset(1, 20, worker.QemuTimestampMapper(1000, 20))
        raw = struct.pack("=IB3x8s", 0x123, 8, b"abcdefgh")
        right.send(raw)
        runtime.ingest_socketcan(left.recv(72), 20)
        assert bus.t_sim_ns == 20 and bus.pending == 1
        runtime.step(1)
        assert peer.recv(84) == wire(0x123, 0, 8, 1000, b"abcdefgh")
        payload = bytes(range(64))
        runtime.ingest_qemu(CanFrame(0x321, WIRE_FD | WIRE_BRS, 15,
                                    payload, 1001), 21)
        runtime.step(1)
        assert right.recv(72) == struct.pack("=IBBBB64s", 0x321, 64,
                                             0x05, 0, 0, payload)
        runtime.close()
        assert left.fileno() >= 0 and stream.sock.fileno() >= 0
    finally:
        left.close()
        right.close()


def test_step_start_commands_then_devices_then_target_delivery(streams):
    stream, peer = streams
    runtime, (device,) = runtime_for(stream)
    runtime.reset(1, 0, worker.QemuTimestampMapper(1000, 0))
    runtime.ingest_qemu(CanFrame(0x200, data=b"now", timestamp_ns=1000), 0)
    runtime.ingest_qemu(CanFrame(0x200, data=b"future", timestamp_ns=1005), 0)
    assert runtime.endpoint.submitted_frames == 0
    result = runtime.step(10)
    assert device.steps[0][1] == (CanFrame(0x200, data=b"now"),)
    assert [f.data for f in device.commands] == [b"now", b"future"]
    assert result == StepResult("ok", 10, {
        "delivered_start": 1, "delivered_target": 2, "pending": 0})
    assert peer.recv(84) == wire(0x201, 0, 5, 1010, b"state")
    assert runtime.endpoint.submitted_frames == 1
    runtime.step(10)
    assert [f.data for f in device.steps[1][1]] == [b"now", b"future"]
    assert runtime.t_sim_ns == runtime.bus.t_sim_ns == 20
    assert runtime.endpoint.submitted_frames == 2
    runtime.close()


def test_dropped_feedback_does_not_submit_and_recovery_preserves_count(streams):
    stream, peer = streams
    bus = CanBus(drop=lambda _source, _destination, frame: frame.timestamp_ns == 20)
    runtime, (device,) = runtime_for(stream, bus=bus)
    try:
        runtime.reset(1, 0, worker.QemuTimestampMapper(1000, 0))
        runtime.step(10)
        assert peer.recv(84) == wire(0x201, 0, 5, 1010, b"state")
        runtime.step(10)
        assert runtime.endpoint.submitted_frames == 1 and len(device.steps) == 2
        with pytest.raises(BlockingIOError):
            peer.recv(84)
        runtime.step(10)
        assert peer.recv(84) == wire(0x201, 0, 5, 1030, b"state")
        assert runtime.endpoint.submitted_frames == 2
    finally:
        runtime.close()


def test_delay_changes_keep_pending_due_and_reset_epoch_with_one_runtime_owner(streams):
    stream, peer = streams
    deliveries = []
    bus = CanBus(delay_ns=20, log=deliveries.append)
    runtime, (device,) = runtime_for(stream, bus=bus)
    try:
        runtime.reset(7, 100, worker.QemuTimestampMapper(9000, 100))
        peer.sendall(wire(0x200, 0, 3, 9000, b"old"))
        (can_id, flags, dlc, stamp, payload), = stream.receive()
        runtime.ingest_qemu(CanFrame(can_id, flags, dlc, payload, stamp), 100)
        assert runtime.step(10) == StepResult("ok", 110, {
            "delivered_start": 0, "delivered_target": 0, "pending": 2})
        assert device.steps[0][1] == () and device.commands == []
        assert runtime.endpoint.submitted_frames == 0
        with pytest.raises(BlockingIOError):
            peer.recv(84)

        bus.delay_ns = 0
        peer.sendall(wire(0x200, 0, 3, 9010, b"new"))
        (can_id, flags, dlc, stamp, payload), = stream.receive()
        runtime.ingest_qemu(CanFrame(can_id, flags, dlc, payload, stamp), 110)
        assert runtime.step(10) == StepResult("ok", 120, {
            "delivered_start": 1, "delivered_target": 2, "pending": 1})
        assert device.steps[1][1] == (CanFrame(0x200, data=b"new", timestamp_ns=110),)
        assert device.commands == [CanFrame(0x200, data=b"new", timestamp_ns=110),
                                   CanFrame(0x200, data=b"old", timestamp_ns=120)]
        assert peer.recv(84) == wire(0x201, 0, 5, 9020, b"state")
        assert runtime.endpoint.submitted_frames == 1

        bus.delay_ns = 30
        assert runtime.step(10) == StepResult("ok", 130, {
            "delivered_start": 0, "delivered_target": 1, "pending": 1})
        assert device.steps[2][1] == tuple(device.commands)
        assert peer.recv(84) == wire(0x201, 0, 5, 9030, b"state")
        assert runtime.endpoint.submitted_frames == 2
        # Original command/feedback metadata, immutable enqueue deadlines and
        # actual settled delivery times remain separate public observations.
        assert [(row.frame.data, row.timestamp_ns, row.due_ns, row.delivered_ns)
                for row in deliveries] == [
            (b"new", 110, 110, 110), (b"old", 100, 120, 120),
            (b"state", 120, 120, 120), (b"state", 110, 130, 130)]
        assert [context.t_sim_ns for context, _ in device.steps] == [110, 120, 130]

        runtime.reset(8, 500, worker.QemuTimestampMapper(20000, 500))
        assert bus.delay_ns == 30 and bus.pending == 0 and device.steps == device.commands == []
        assert runtime.endpoint.submitted_frames == 2
        assert runtime.step(1).diagnostics == {
            "delivered_start": 0, "delivered_target": 0, "pending": 1}
        assert runtime.endpoint.submitted_frames == 2
        assert runtime.step(30).diagnostics == {
            "delivered_start": 0, "delivered_target": 1, "pending": 1}
        assert peer.recv(84) == wire(0x201, 0, 5, 20031, b"state")
        assert runtime.endpoint.submitted_frames == 3
        runtime.close()
        assert device.close_calls == 1 and bus.delay_ns == 30 and bus.pending == 1
        assert runtime.endpoint.stream is stream and stream.sock.fileno() >= 0 and peer.fileno() >= 0
    finally:
        runtime.close()


@pytest.mark.parametrize("delay_ns", [True, 1.5, -1, 1 << 64])
def test_invalid_delay_assignment_preserves_actual_pending_commands_before_step_or_io(streams, delay_ns):
    stream, peer = streams
    deliveries = []
    bus = CanBus(delay_ns=5, log=deliveries.append)
    runtime, (device,) = runtime_for(stream, bus=bus)
    try:
        runtime.reset(7, 100, worker.QemuTimestampMapper(9000, 100))
        peer.sendall(wire(0x200, 0, 3, 9000, b"cmd"))
        (can_id, flags, dlc, stamp, payload), = stream.receive()
        runtime.ingest_qemu(CanFrame(can_id, flags, dlc, payload, stamp), 100)
        with pytest.raises((TypeError, ValueError)):
            bus.delay_ns = delay_ns
        assert bus.delay_ns == 5 and bus.pending == 1
        assert runtime.t_sim_ns == bus.t_sim_ns == 100
        assert device.commands == device.steps == deliveries == []
        assert not bus.has_due(104) and bus.has_due(105)
        with pytest.raises(BlockingIOError):
            peer.recv(84)
        runtime.step(5)
        assert device.steps[0][1] == ()
        assert device.commands == [CanFrame(0x200, data=b"cmd", timestamp_ns=105)]
        runtime.step(5)
        assert device.steps[1][1] == tuple(device.commands)
        assert peer.recv(84) == wire(0x201, 0, 5, 9010, b"state")
    finally:
        runtime.close()
    assert device.close_calls == 1 and stream.sock.fileno() >= 0


def test_start_due_budget_exhaustion_freezes_before_device_advance(streams):
    stream, _ = streams
    runtime, (device,) = runtime_for(stream, bus=CanBus(dispatch_budget=1))
    runtime.reset(1, 0, worker.QemuTimestampMapper(1000, 0))
    for can_id in (1, 2):
        runtime.ingest_qemu(CanFrame(can_id, timestamp_ns=1000), 0)
    with pytest.raises(RuntimeError, match="budget exhausted at step start"):
        runtime.step(10)
    assert device.steps == [] and runtime.t_sim_ns == 0
    assert runtime.bus.pending == 1
    with pytest.raises(RuntimeError, match="successful reset"):
        runtime.ingest_qemu(CanFrame(3), 0)
    with pytest.raises(RuntimeError, match="successful reset"):
        runtime.step(10)
    runtime.reset(2, 0, worker.QemuTimestampMapper(2000, 0))
    runtime.step(10)
    assert len(device.steps) == 1
    runtime.close()


def test_future_pending_is_not_mistaken_for_exhausted_start_budget(streams):
    stream, _ = streams
    runtime, (device,) = runtime_for(stream, bus=CanBus(dispatch_budget=1))
    runtime.reset(1, 0, worker.QemuTimestampMapper(1000, 0))
    runtime.ingest_qemu(CanFrame(1, timestamp_ns=1000), 0)
    runtime.ingest_qemu(CanFrame(2, timestamp_ns=1100), 0)
    runtime.step(10)
    assert len(device.steps) == 1 and runtime.t_sim_ns == 10
    assert runtime.bus.pending == 1 and not runtime.bus.has_due(10)
    runtime.close()


def test_single_bus_target_overflow_rejects_before_start_command_or_device(streams):
    stream, _ = streams
    runtime, (device,) = runtime_for(stream)
    try:
        runtime.reset(1, 0, worker.QemuTimestampMapper(1000, 0))
        runtime.ingest_qemu(CanFrame(0x200, data=b"now", timestamp_ns=1000), 0)
        with pytest.raises(ValueError, match="unsigned 64-bit"):
            runtime.step(1 << 64)
        assert runtime.bus.pending == 1 and runtime.bus.t_sim_ns == 0
        assert device.commands == device.steps == [] and runtime.coordinator.failed
    finally:
        runtime.close()


@pytest.mark.parametrize("result", [StepResult("failed", 10),
                                   StepResult("ok", 9), object()])
def test_device_invalid_result_freezes_without_target_settle(streams, result):
    stream, _ = streams
    runtime, (device,) = runtime_for(stream)
    runtime.reset(1, 0, worker.QemuTimestampMapper(1000, 0))
    device.result = result
    with pytest.raises(RuntimeError, match="did not reach target"):
        runtime.step(10)
    assert runtime.t_sim_ns == runtime.bus.t_sim_ns == 0
    assert len(device.steps) == 1 and runtime.bus.pending == 1
    with pytest.raises(RuntimeError, match="successful reset"):
        runtime.step(10)
    device.result = None
    runtime.reset(2, 0, worker.QemuTimestampMapper(2000, 0))
    assert runtime.bus.pending == 0
    runtime.step(10)
    runtime.close()


def test_reset_failure_freezes_and_close_tries_every_device_once(streams):
    stream, _ = streams
    bus = CanBus()
    endpoint = QemuCanEndpoint(bus, stream)
    first, second = Device(bus, "a"), Device(bus, "b")
    runtime = CanRuntime(bus, endpoint, [first, second])
    second.reset_error = ValueError("reset refused")
    with pytest.raises(ValueError, match="reset refused"):
        runtime.reset(1, 0, worker.QemuTimestampMapper(1000, 0))
    with pytest.raises(RuntimeError, match="successful reset"):
        runtime.step(10)
    first.close_error = OSError("first release")
    second.close_error = KeyboardInterrupt("second release")
    with pytest.raises(KeyboardInterrupt) as caught:
        runtime.close()
    assert caught.value is second.close_error
    assert any("first release" in note for note in caught.value.__notes__)
    runtime.close()
    assert first.close_calls == second.close_calls == 1
    assert stream.sock.fileno() >= 0


def test_send_error_after_wire_io_keeps_count_and_freezes_without_replay(streams):
    stream, peer = streams
    original_send = stream.send
    primary = KeyboardInterrupt("send returned no success after wire IO")
    calls = []

    def send(*args):
        calls.append(args)
        original_send(*args)
        if len(calls) == 2:
            raise primary

    stream.send = send
    runtime, (device,) = runtime_for(stream)
    try:
        runtime.reset(1, 0, worker.QemuTimestampMapper(1000, 0))
        runtime.step(10)
        assert runtime.endpoint.submitted_frames == 1
        with pytest.raises(KeyboardInterrupt) as caught:
            runtime.step(10)
        assert caught.value is primary
        assert peer.recv(168) == (wire(0x201, 0, 5, 1010, b"state")
                                  + wire(0x201, 0, 5, 1020, b"state"))
        assert runtime.endpoint.submitted_frames == 1 and len(calls) == 2
        assert runtime.coordinator.failed and runtime.t_sim_ns == 10
        assert runtime.bus.t_sim_ns == 20 and runtime.bus.pending == 0
        assert len(device.steps) == 2
        with pytest.raises(RuntimeError, match="successful reset"):
            runtime.step(10)
        assert len(device.steps) == len(calls) == 2
        with pytest.raises(BlockingIOError):
            peer.recv(84)
        runtime.reset(2, 0, worker.QemuTimestampMapper(2000, 0))
        assert runtime.endpoint.submitted_frames == 1
        runtime.step(10)
        assert peer.recv(84) == wire(0x201, 0, 5, 2010, b"state")
        assert runtime.endpoint.submitted_frames == 2 and len(calls) == 3
    finally:
        runtime.close()
    assert runtime.endpoint.submitted_frames == 2
    assert stream.sock.fileno() >= 0 and peer.fileno() >= 0


def test_submission_is_read_only_and_uses_captured_send_after_normal_return():
    calls = []

    def send(*args):
        calls.append((endpoint.submitted_frames, args))

    stream = SimpleNamespace(send=send)
    endpoint = QemuCanEndpoint(CanBus(), stream)
    assert type(endpoint.submitted_frames) is int and endpoint.submitted_frames == 0
    with pytest.raises(AttributeError):
        endpoint.submitted_frames = 9
    endpoint.bind_mapper(worker.QemuTimestampMapper(1000, 0))
    stream.send = lambda *_: pytest.fail("endpoint replaced its captured send")
    endpoint.receive(CanFrame(0x201, data=b"ok", timestamp_ns=10))
    endpoint.receive(CanFrame(0x202, data=b"yes", timestamp_ns=20))
    assert calls == [(0, (0x201, 0, 2, 1010, b"ok")),
                     (1, (0x202, 0, 3, 1020, b"yes"))]
    assert endpoint.submitted_frames == 2


def test_endpoint_admission_and_invalid_mapper_preserve_existing_binding(streams):
    stream, peer = streams
    bus = CanBus()
    with pytest.raises(TypeError, match="callable send"):
        QemuCanEndpoint(bus, object())
    endpoint = QemuCanEndpoint(bus, stream)
    with pytest.raises(TypeError, match="callable send"):
        SocketCanEndpoint(bus, object())
    with pytest.raises(RuntimeError, match="RESET mapper"):
        endpoint.ingest(CanFrame(1), 0)
    with pytest.raises(RuntimeError, match="RESET mapper"):
        endpoint.receive(CanFrame(1))
    assert endpoint.submitted_frames == 0
    mapper = worker.QemuTimestampMapper(1000, 0)
    endpoint.bind_mapper(mapper)
    with pytest.raises(ValueError):
        endpoint.bind_mapper(SimpleNamespace(map=mapper.map, unmap=mapper.unmap,
                                             qemu_origin_ns=-1))
    assert endpoint.submitted_frames == 0
    endpoint.receive(CanFrame(2, data=b"ok", timestamp_ns=10))
    assert peer.recv(84) == wire(2, 0, 2, 1010, b"ok")
    assert endpoint.submitted_frames == 1
    endpoint.bind_mapper(SimpleNamespace(map=mapper.map, unmap=lambda *_: True,
                                         qemu_origin_ns=1000))
    with pytest.raises(ValueError):
        endpoint.receive(CanFrame(2))
    assert endpoint.submitted_frames == 1
    with pytest.raises(BlockingIOError):
        peer.recv(84)
    with pytest.raises(ValueError):
        endpoint.ingest(CanFrame(1), True)
    assert bus.pending == 0


def test_runtime_owns_unique_valid_devices_and_matching_endpoints(streams):
    stream, _ = streams
    bus = CanBus()
    endpoint = QemuCanEndpoint(bus, stream)
    device = Device(bus)
    with pytest.raises(ValueError, match="single owner"):
        CanParticipant(bus, [device, device])
    with pytest.raises(TypeError, match="reset, step and close"):
        CanParticipant(bus, [object()])
    with pytest.raises(ValueError, match="runtime CAN bus"):
        CanRuntime(CanBus(), endpoint)
    runtime = CanRuntime(bus, endpoint, [device])
    with pytest.raises(RuntimeError, match="successful reset"):
        runtime.ingest_qemu(CanFrame(1), 0)
    runtime.reset(1, 0, worker.QemuTimestampMapper(1000, 0))
    with pytest.raises(RuntimeError, match="no SocketCAN endpoint"):
        runtime.ingest_socketcan(b"", 0)
    runtime.close()
    with pytest.raises(RuntimeError, match="closed"):
        runtime.reset(2, 0, worker.QemuTimestampMapper(1000, 0))
    assert device.close_calls == 1


def test_multibus_admission_is_complete_before_consuming_owned_devices():
    bus = CanBus()
    def forbidden_devices():
        raise AssertionError("device inputs consumed before bus admission")
        yield
    for invalid in ((), (bus, object()), [bus], {bus}, iter((bus,))):
        with pytest.raises(TypeError, match="CanBus"):
            CanParticipant(invalid, forbidden_devices())
    with pytest.raises(ValueError, match="bus must appear once"):
        CanParticipant((bus, bus), forbidden_devices())
    single = CanParticipant(bus)
    assert single.bus is bus and single.buses == (bus,)
    pair = (bus, CanBus())
    participant = CanParticipant(pair)
    assert participant.buses is pair
    with pytest.raises(RuntimeError, match="multiple buses"):
        participant.bus
    single.close()
    participant.close()


def test_multibus_start_then_devices_then_targets_and_aggregate_diagnostics(monkeypatch):
    buses = (CanBus(), CanBus())
    devices = tuple(Device(bus) for bus in buses)
    sources = tuple(SimpleNamespace(receive=lambda _: None) for _ in buses)
    for bus, source in zip(buses, sources):
        bus.attach(source)
    seen = []
    for device in devices:
        def observe_reset(session, reset=device.reset):
            seen.append(tuple((bus.t_sim_ns, bus.pending) for bus in buses))
            reset(session)
        monkeypatch.setattr(device, "reset", observe_reset)
    participant = CanParticipant(buses, devices)
    coordinator = Coordinator({"can": participant})
    try:
        # All bus queues reset before either device reset callback.
        coordinator.reset(Session(1, 7))
        assert seen == [((7, 0), (7, 0))] * 2
        for bus, source in zip(buses, sources):
            bus.send(source, CanFrame(0x100, data=b"now", timestamp_ns=7))
            bus.send(source, CanFrame(0x100, data=b"future", timestamp_ns=12))
        result = coordinator.step(10)["can"]
        assert result == StepResult("ok", 17, {
            "delivered_start": 2, "delivered_target": 4, "pending": 0})
        for device in devices:
            assert [f.data for f in device.steps[0][1]] == [b"now"]
            assert [f.data for f in device.commands] == [b"now", b"future"]
        assert all(bus.t_sim_ns == 17 for bus in buses)
        # Reset callbacks see every borrowed bus at the new origin, queues empty.
        for bus, source in zip(buses, sources):
            bus.send(source, CanFrame(0x123, timestamp_ns=200))
        coordinator.reset(Session(2, 100))
        assert seen == [((7, 0), (7, 0))] * 2 + [((100, 0), (100, 0))] * 2
        for bus, source in zip(buses, sources):
            bus.send(source, CanFrame(0x123, timestamp_ns=200))
    finally:
        coordinator.close()
    assert all(device.close_calls == 1 for device in devices)
    assert all(bus.t_sim_ns == 100 and bus.pending == 1 for bus in buses)


@pytest.mark.parametrize("invalid", ["later_clock", "target_overflow"])
def test_multibus_all_time_admission_precedes_any_dispatch_or_device(invalid):
    buses = (CanBus(), CanBus())
    devices = tuple(Device(bus) for bus in buses)
    coordinator = Coordinator({"can": CanParticipant(buses, devices)})
    try:
        coordinator.reset(Session(1))
        devices[0].emit(CanFrame(0x123))
        if invalid == "later_clock":
            buses[1].step(1)
            error, message, dt = RuntimeError, "step start", 10
        else:
            error, message, dt = ValueError, "unsigned 64-bit", 1 << 64
        with pytest.raises(error, match=message):
            coordinator.step(dt)
        assert buses[0].pending == 1 and buses[0].t_sim_ns == 0
        assert all(device.steps == [] and device.commands == [] for device in devices)
        assert coordinator.failed and coordinator.t_sim_ns == 0
    finally:
        coordinator.close()


@pytest.mark.parametrize("source_index", [0, 1])
def test_multibus_cross_callback_uses_one_ordered_start_pass(source_index):
    buses = (CanBus(), CanBus())
    destination_index = 1 - source_index
    sources = tuple(SimpleNamespace(receive=lambda _: None) for _ in buses)
    for bus, source in zip(buses, sources):
        bus.attach(source)
    device = Device(buses[destination_index])
    bridge = SimpleNamespace(receive=lambda frame: buses[destination_index].send(
        sources[destination_index], frame))
    buses[source_index].attach(bridge)
    coordinator = Coordinator({"can": CanParticipant(buses, (device,))})
    try:
        coordinator.reset(Session(1))
        buses[source_index].send(sources[source_index], CanFrame(0x123, data=b"bridge"))
        if source_index == 0:
            result = coordinator.step(10)["can"]
            assert result.diagnostics == {
                "delivered_start": 2, "delivered_target": 1, "pending": 0}
            assert [f.data for f in device.steps[0][1]] == [b"bridge"]
        else:
            with pytest.raises(RuntimeError, match="budget exhausted at step start"):
                coordinator.step(10)
            assert device.steps == [] and device.commands == []
            assert buses[0].has_due(0) and buses[0].pending == 1
            assert coordinator.failed and coordinator.t_sim_ns == 0
            with pytest.raises(RuntimeError, match="successful reset"):
                coordinator.step(10)
    finally:
        coordinator.close()


def test_multibus_target_backlog_settles_at_next_start_without_repeating_world():
    buses = (CanBus(dispatch_budget=1), CanBus(dispatch_budget=1))
    devices = tuple(Device(bus) for bus in buses)
    sources = tuple(SimpleNamespace(receive=lambda _: None) for _ in buses)
    for bus, source in zip(buses, sources):
        bus.attach(source)
    coordinator = Coordinator({"can": CanParticipant(buses, devices)})
    try:
        coordinator.reset(Session(1))
        for bus, source in zip(buses, sources):
            bus.send(source, CanFrame(0x123, data=b"future", timestamp_ns=10))
        assert coordinator.step(10)["can"].diagnostics == {
            "delivered_start": 0, "delivered_target": 2, "pending": 2}
        assert all(bus.has_due(10) for bus in buses)
        assert all(len(device.steps) == 1 for device in devices)
        assert coordinator.step(10)["can"].diagnostics == {
            "delivered_start": 2, "delivered_target": 2, "pending": 0}
        assert all(len(device.steps) == 2 for device in devices)
    finally:
        coordinator.close()


def test_multibus_later_target_callback_error_preserves_partial_effects_until_reset():
    buses = (CanBus(), CanBus())
    devices = tuple(Device(bus) for bus in buses)
    primary = KeyboardInterrupt("second target callback")
    fail = [True]
    def receive(_):
        if fail[0]:
            raise primary
    sink = SimpleNamespace(receive=receive)
    buses[1].attach(sink)
    coordinator = Coordinator({"can": CanParticipant(buses, devices)})
    try:
        coordinator.reset(Session(1))
        devices[1].emit(CanFrame(0x123, timestamp_ns=100))
        with pytest.raises(KeyboardInterrupt) as caught:
            coordinator.step(10)
        assert caught.value is primary
        assert all(len(device.steps) == 1 for device in devices)
        assert all(bus.t_sim_ns == 10 for bus in buses)
        assert buses[0].pending == 0 and buses[1].pending == 1
        assert coordinator.failed and coordinator.t_sim_ns == 0
        with pytest.raises(RuntimeError, match="successful reset"):
            coordinator.step(10)
        fail[0] = False
        coordinator.reset(Session(2))
        assert all(bus.pending == 0 and device.steps == []
                   for bus, device in zip(buses, devices))
        assert coordinator.step(10)["can"].diagnostics == {
            "delivered_start": 0, "delivered_target": 2, "pending": 0}
    finally:
        coordinator.close()
