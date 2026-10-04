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
from sim.core import Session, StepResult
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
    runtime.reset(7, 100, worker.QemuTimestampMapper(9000, 100))
    peer.sendall(wire(0x310, flags, dlc, 9005, data))
    (can_id, received_flags, received_dlc, timestamp, payload), = stream.receive()
    runtime.ingest_qemu(CanFrame(can_id, received_flags, received_dlc,
                                payload, timestamp), 100)
    assert runtime.t_sim_ns == runtime.bus.t_sim_ns == 100
    assert device.commands == []
    runtime.step(5)
    assert device.commands == [CanFrame(0x310, flags, dlc, data, 105)]
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
    runtime.reset(2, 0, worker.QemuTimestampMapper(2000, 0))
    assert bytes(stream.tx_queue[0]) == old[7:]
    runtime.step(1)
    new = wire(0x201, 0, 5, 2001, b"state")
    short.limit = 4096
    assert stream.flush()
    assert peer.recv(168) == old + new
    assert len(device.steps) == 1
    runtime.close()


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
    result = runtime.step(10)
    assert device.steps[0][1] == (CanFrame(0x200, data=b"now"),)
    assert [f.data for f in device.commands] == [b"now", b"future"]
    assert result == StepResult("ok", 10, {
        "delivered_start": 1, "delivered_target": 2, "pending": 0})
    assert peer.recv(84) == wire(0x201, 0, 5, 1010, b"state")
    runtime.step(10)
    assert [f.data for f in device.steps[1][1]] == [b"now", b"future"]
    assert runtime.t_sim_ns == runtime.bus.t_sim_ns == 20
    runtime.close()


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
    mapper = worker.QemuTimestampMapper(1000, 0)
    endpoint.bind_mapper(mapper)
    with pytest.raises(ValueError):
        endpoint.bind_mapper(SimpleNamespace(map=mapper.map, unmap=mapper.unmap,
                                             qemu_origin_ns=-1))
    endpoint.receive(CanFrame(2, data=b"ok", timestamp_ns=10))
    assert peer.recv(84) == wire(2, 0, 2, 1010, b"ok")
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
