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
from sim.machines.qemu.endpoints import QemuCanEndpoint
from sim.transports.socketcan_endpoint import SocketCanEndpoint
from sim.fabric.frame import WIRE_FD, WIRE_BRS
from sim.fabric.runtime import BorrowedCanDispatcher, CanParticipant

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


@pytest.mark.parametrize("flags,dlc,data", [
    (0, 8, bytes(range(8))),
    (WIRE_FD | WIRE_BRS, 15, bytes(range(64))),
])
def test_fixed84_classic_and_fd_ingress_and_reset_epoch_egress(
        streams, flags, dlc, data):
    stream, peer = streams
    bus = CanBus()
    endpoint = QemuCanEndpoint(bus, stream)
    device = Device(bus)
    endpoint.bind_mapper(worker.QemuTimestampMapper(9000, 100))
    bus.reset(100)
    peer.sendall(wire(0x310, flags, dlc, 9005, data))
    (can_id, received_flags, received_dlc, timestamp, payload), = stream.receive()
    endpoint.ingest(CanFrame(can_id, received_flags, received_dlc,
                             payload, timestamp), 100)
    assert bus.t_sim_ns == 100
    assert device.commands == []
    bus.step(105)
    assert device.commands == [CanFrame(0x310, flags, dlc, data, 105)]
    device.emit(CanFrame(0x201, data=b"state", timestamp_ns=105))
    bus.step(105)
    assert endpoint.submitted_frames == 1
    assert peer.recv(84) == wire(0x201, 0, 5, 9005, b"state")
    bus.close()
    assert stream.sock.fileno() >= 0 and peer.fileno() >= 0


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
        qemu.bind_mapper(worker.QemuTimestampMapper(1000, 20))
        bus.reset(20)
        raw = struct.pack("=IB3x8s", 0x123, 8, b"abcdefgh")
        right.send(raw)
        native.ingest(left.recv(72), 20)
        assert bus.t_sim_ns == 20 and bus.pending == 1
        bus.step(20)
        assert peer.recv(84) == wire(0x123, 0, 8, 1000, b"abcdefgh")
        payload = bytes(range(64))
        qemu.ingest(CanFrame(0x321, WIRE_FD | WIRE_BRS, 15,
                             payload, 1001), 20)
        bus.step(21)
        assert right.recv(72) == struct.pack("=IBBBB64s", 0x321, 64,
                                             0x05, 0, 0, payload)
        bus.close()
        assert left.fileno() >= 0 and stream.sock.fileno() >= 0
    finally:
        left.close()
        right.close()


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
    bus = CanBus()
    endpoint = QemuCanEndpoint(bus, stream)
    source = Device(bus)
    try:
        endpoint.bind_mapper(worker.QemuTimestampMapper(1000, 0))
        bus.reset(0)
        source.emit(CanFrame(0x201, data=b"state", timestamp_ns=10))
        bus.step(10)
        assert endpoint.submitted_frames == 1
        source.emit(CanFrame(0x201, data=b"state", timestamp_ns=20))
        with pytest.raises(KeyboardInterrupt) as caught:
            bus.step(20)
        assert caught.value is primary
        assert peer.recv(168) == (wire(0x201, 0, 5, 1010, b"state")
                                  + wire(0x201, 0, 5, 1020, b"state"))
        assert endpoint.submitted_frames == 1 and len(calls) == 2
        assert bus.t_sim_ns == 20 and bus.pending == 0
        with pytest.raises(KeyboardInterrupt) as retained:
            bus.step(20)
        assert retained.value is primary and len(calls) == 2
        with pytest.raises(BlockingIOError):
            peer.recv(84)
        endpoint.bind_mapper(worker.QemuTimestampMapper(2000, 0))
        bus.reset(0)
        assert endpoint.submitted_frames == 1
        source.emit(CanFrame(0x201, data=b"state", timestamp_ns=10))
        bus.step(10)
        assert peer.recv(84) == wire(0x201, 0, 5, 2010, b"state")
        assert endpoint.submitted_frames == 2 and len(calls) == 3
    finally:
        bus.close()
    assert endpoint.submitted_frames == 2
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


def test_dispatcher_admits_one_bus_and_exposes_its_known_deadlines():
    bus = CanBus(delay_ns=5, dispatch_budget=2)
    for invalid in (None, (bus,), [bus]):
        with pytest.raises(TypeError, match="one CanBus"):
            BorrowedCanDispatcher(invalid)
    source = Device(bus, "source")
    sink = Device(bus, "sink")
    dispatcher = BorrowedCanDispatcher(bus)
    with pytest.raises(TypeError, match="Session"):
        dispatcher.reset(100)
    dispatcher.reset(Session(7, 100))
    source.emit(CanFrame(0x123, data=b"old", timestamp_ns=101))
    bus.delay_ns = 9
    source.emit(CanFrame(0x123, data=b"new", timestamp_ns=102))
    assert dispatcher.bus is bus and dispatcher.dispatch_budget == 2
    assert dispatcher.next_due_ns == dispatcher.next_due_ns == 106
    assert bus.t_sim_ns == 100 and bus.pending == 2 and sink.commands == []
    assert dispatcher.dispatch(105, 1) == 0
    assert dispatcher.next_due_ns == 106
    assert dispatcher.dispatch(106, 1) == 1
    assert sink.commands == [CanFrame(0x123, data=b"old", timestamp_ns=106)]
    assert dispatcher.next_due_ns == 111 and source.steps == sink.steps == []


def test_dispatcher_forwards_tighter_budget_including_callback_work_and_zero():
    bus = CanBus(dispatch_budget=2)
    source = Device(bus)
    received = []

    def receive(frame):
        received.append(frame)
        if frame.data == b"first":
            source.emit(CanFrame(0x123, data=b"callback", timestamp_ns=1))

    bus.attach(SimpleNamespace(receive=receive))
    dispatcher = BorrowedCanDispatcher(bus)
    dispatcher.reset(Session(1))
    source.emit(CanFrame(0x123, data=b"first"))
    source.emit(CanFrame(0x123, data=b"second"))
    assert dispatcher.dispatch(1, 1) == 1
    assert [frame.data for frame in received] == [b"first"]
    assert dispatcher.dispatch(2, 0) == 0
    assert bus.t_sim_ns == 2 and bus.pending == 2 and dispatcher.next_due_ns == 0
    assert dispatcher.dispatch(2, 99) == 2
    assert [(frame.data, frame.timestamp_ns) for frame in received] == [
        (b"first", 1), (b"second", 2), (b"callback", 2)]
    assert dispatcher.next_due_ns is None and source.steps == []


def test_dispatcher_retains_bus_failure_without_replay_until_logical_reset():
    bus = CanBus()
    source = Device(bus)
    primary = KeyboardInterrupt("consumer failed after observing frame")
    received = []
    fail = [True]

    def receive(frame):
        received.append(frame.data)
        if fail[0]:
            raise primary

    bus.attach(SimpleNamespace(receive=receive))
    dispatcher = BorrowedCanDispatcher(bus)
    source.emit(CanFrame(0x123, data=b"effect"))
    source.emit(CanFrame(0x123, data=b"queued"))
    for operation in (lambda: dispatcher.dispatch(10, 2),
                      lambda: dispatcher.next_due_ns,
                      lambda: dispatcher.dispatch(10, 2)):
        with pytest.raises(KeyboardInterrupt) as caught:
            operation()
        assert caught.value is primary
    assert received == [b"effect"] and bus.pending == 1 and bus.t_sim_ns == 10
    fail[0] = False
    dispatcher.reset(Session(2, 100))
    assert dispatcher.next_due_ns is None and bus.t_sim_ns == 100
    source.emit(CanFrame(0x123, data=b"fresh", timestamp_ns=100))
    assert dispatcher.dispatch(100, 1) == 1
    assert received == [b"effect", b"fresh"]


@pytest.mark.parametrize("mutation", ["dispatch", "reset", "close"])
def test_dispatcher_rejects_own_callback_mutation_and_bus_retains_failure(mutation):
    bus = CanBus()
    source = Device(bus)
    dispatcher = BorrowedCanDispatcher(bus)
    operations = {"dispatch": lambda: dispatcher.dispatch(0, 1),
                  "reset": lambda: dispatcher.reset(Session(2)),
                  "close": dispatcher.close}
    bus.attach(SimpleNamespace(receive=lambda _: operations[mutation]()))
    source.emit(CanFrame(0x123))
    with pytest.raises(RuntimeError, match="delivery callback") as caught:
        dispatcher.dispatch(0, 1)
    with pytest.raises(RuntimeError) as retained:
        dispatcher.next_due_ns
    assert retained.value is caught.value and bus.pending == 0
    dispatcher.reset(Session(3, 10))
    assert dispatcher.dispatch(10, 1) == 0
    dispatcher.close()


def test_dispatcher_reset_and_close_preserve_borrowed_devices_and_stream_prefix(streams):
    stream, peer = streams
    bus = CanBus()
    source = Device(bus, "source")
    sink = Device(bus, "sink")
    endpoint = QemuCanEndpoint(bus, stream)
    endpoint.bind_mapper(worker.QemuTimestampMapper(1000, 0))
    dispatcher = BorrowedCanDispatcher(bus)
    source.emit(CanFrame(0x123, data=b"kept"))
    assert bus.step(0) == 1
    assert peer.recv(84) == wire(0x123, 0, 4, 1000, b"kept")
    incoming = wire(0x301, 0, 2, 1001, b"rx")
    peer.sendall(incoming[:19])
    assert stream.receive() == []
    source.emit(CanFrame(0x123, data=b"discard", timestamp_ns=100))
    dispatcher.reset(Session(2, 5))
    assert bus.pending == 0
    assert sink.commands == [CanFrame(0x123, data=b"kept")]
    source.emit(CanFrame(0x123, data=b"tx", timestamp_ns=6))
    dispatcher.close()
    dispatcher.close()
    for operation in (lambda: dispatcher.dispatch(6, 1),
                      lambda: dispatcher.reset(Session(3)),
                      lambda: dispatcher.next_due_ns,
                      lambda: dispatcher.dispatch_budget):
        with pytest.raises(RuntimeError, match="closed"):
            operation()
    assert bus.pending == 1 and bus.t_sim_ns == 5
    assert source.close_calls == sink.close_calls == 0
    assert source.steps == sink.steps == []
    assert bus.step(6) == 1
    assert sink.commands == [CanFrame(0x123, data=b"kept"),
                             CanFrame(0x123, data=b"tx", timestamp_ns=6)]
    assert peer.recv(84) == wire(0x123, 0, 2, 1006, b"tx")
    peer.sendall(incoming[19:])
    assert stream.receive() == [(0x301, 0, 2, 1001, b"rx")]
    assert stream.sock.fileno() >= 0 and peer.fileno() >= 0


def test_participant_owns_unique_valid_devices(streams):
    stream, _ = streams
    bus = CanBus()
    device = Device(bus)
    with pytest.raises(ValueError, match="single owner"):
        CanParticipant(bus, [device, device])
    with pytest.raises(TypeError, match="reset, step and close"):
        CanParticipant(bus, [object()])
    participant = CanParticipant(bus, [device])
    participant.close()
    with pytest.raises(RuntimeError, match="closed"):
        participant.reset(Session(1))
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
        assert coordinator.failed == (invalid == "later_clock") and coordinator.t_sim_ns == 0
        if invalid == "target_overflow":
            coordinator.step(1)
            assert all(len(device.steps) == 1 for device in devices)
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
