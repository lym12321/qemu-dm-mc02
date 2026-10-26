"""Actual Simulation cuts and retained socket bytes across CAN graph sessions."""

import socket
import struct
from contextlib import closing
from types import SimpleNamespace

import pytest

from sim.api import LinkSpec, NodeDescription, NodeSpec, PortSpec, Session, StepResult, Topology
from sim.fabric.factory import BorrowedCanPort, CanBusFactory
from sim.fabric.frame import CanFrame
from sim.machines.qemu.bridge import CanTransportBridgeFactory
from sim.machines.qemu.endpoints import QemuCanEndpoint
from sim.runtime import Simulation, SimulationState
from sim.transports.can_codec import pack_can_frame, unpack_can_frame
from sim.transports.can_stream import CanStream
from sim.transports.cosim import QemuTimestampMapper
from sim.transports.datagram import DatagramTransport
from sim.transports.socketcan_endpoint import SocketCanEndpoint


def wire(identifier, stamp, data):
    # Existing public fixed84 record layout, independently expressed.
    return struct.pack("<IIB3xQ64s", identifier, 0, len(data), stamp,
                       data.ljust(64, b"\0"))


class Device:
    def __init__(self, context, reply):
        self.connection = context.attach("can", self)
        self.received = []
        self.intervals = []
        self.closed = 0
        self.reply = reply

    def receive(self, frame):
        self.received.append(frame)

    def step(self, ctx):
        self.intervals.append((ctx.t_sim_ns - ctx.dt_ns, ctx.t_sim_ns,
                               tuple(frame.data for frame in self.received)))
        if self.reply:
            self.connection.send(CanFrame(0x201, data=b"state", timestamp_ns=ctx.t_sim_ns))
        return StepResult("ok", ctx.t_sim_ns)

    def close(self):
        self.closed += 1


class DeviceFactory:
    def __init__(self, reply=False):
        self.reply = reply

    def describe(self, spec):
        return NodeDescription((PortSpec("can", "can", min_connections=1),),
                               frozenset({"step"}))

    def create(self, spec, context):
        return Device(context, self.reply)


@pytest.fixture
def streams():
    local, peer = socket.socketpair()
    local.setblocking(False)
    peer.setblocking(False)
    try:
        yield CanStream(local), peer
    finally:
        local.close()
        peer.close()


def graph(stream, *, datagrams=None, session=Session(7, 100),
          qemu_origin=9000, reply=False, options=None, drop=None, on_delivery=None,
          **simulation_options):
    nodes = (NodeSpec("bus", "can", options or {}), NodeSpec("bridge", "bridge"),
             NodeSpec("device", "device"))
    links = [LinkSpec("bridge.qemu", "bus.can"), LinkSpec("device.can", "bus.can")]
    if datagrams is not None:
        links.append(LinkSpec("bridge.socketcan", "bus.can"))
    registry = {"can": CanBusFactory(drop=drop, on_delivery=on_delivery),
                "bridge": CanTransportBridgeFactory(stream, QemuTimestampMapper(
                    qemu_origin, session.t_sim_ns), datagrams),
                "device": DeviceFactory(reply)}
    simulation = Simulation(Topology(nodes, tuple(links)), registry,
        initial_session=session, quantum_ns=10, **simulation_options)
    simulation.start()
    return simulation


def test_wire_ingress_known_due_cut_and_mapped_egress_use_one_simulation(streams):
    stream, peer = streams
    deliveries = []
    with closing(graph(stream, options={"delay_ns": 2}, on_delivery=deliveries.append)) as sim:
        device = sim.components["device"]
        peer.sendall(wire(0x200, 9003, b"command"))
        (identifier, flags, dlc, stamp, payload), = stream.receive()
        sim.schedule_input("bridge.qemu_rx", CanFrame(identifier, flags, dlc, payload, stamp))
        assert device.received == [] and sim.t_sim_ns == 100
        sim.run_until(110)
        assert device.intervals == [(100, 105, ()), (105, 110, (b"command",))]
        assert device.received == [CanFrame(0x200, data=b"command", timestamp_ns=105)]
        receipt, = deliveries
        assert (receipt.timestamp_ns, receipt.due_ns, receipt.delivered_ns) == (103, 105, 105)
        event, = [event for event in sim.events if event.kind == "can.delivery"]
        assert (event.source_time_ns, event.mapped_time_ns, event.delivery_time_ns) == (103, 103, 105)
        assert sim.components["bridge"].endpoint.submitted_frames == 0
        device.connection.send(CanFrame(0x201, data=b"response", timestamp_ns=108))
        sim.run_until(112)
        assert peer.recv(84) == wire(0x201, 9012, b"response")
        assert sim.components["bridge"].endpoint.submitted_frames == 1
    assert device.closed == 1 and stream.sock.fileno() >= 0


def test_old_epoch_falls_back_to_current_cut_and_same_time_inputs_stay_ordered(streams):
    stream, _ = streams
    with closing(graph(stream)) as sim:
        for payload in (b"first", b"second"):
            sim.schedule_input("bridge.qemu_rx", CanFrame(0x200, data=payload, timestamp_ns=8999))
        sim.run_until(100)
        assert sim.components["device"].received == [
            CanFrame(0x200, data=b"first", timestamp_ns=100),
            CanFrame(0x200, data=b"second", timestamp_ns=100)]
        assert sim.t_sim_ns == 100 and sim.step_id == 0


def test_native_datagram_ingress_is_purely_decoded_then_both_directions_keep_bytes(streams):
    stream, peer = streams
    local, other = socket.socketpair(type=socket.SOCK_DGRAM)
    local.setblocking(False)
    other.setblocking(False)
    transport = DatagramTransport(local)
    tx = SimpleNamespace(send=lambda frame: transport.send(pack_can_frame(frame)))
    try:
        with closing(graph(stream, datagrams=tx)) as sim:
            raw = struct.pack("=IB3x8s", 0x123, 8, b"abcdefgh")
            other.send(raw)
            sim.schedule_input("bridge.socketcan_rx", unpack_can_frame(local.recv(72), sim.t_sim_ns))
            sim.run_until(100)
            assert peer.recv(84) == wire(0x123, 9000, b"abcdefgh")
            with pytest.raises(BlockingIOError):
                other.recv(72)
            sim.schedule_input("bridge.qemu_rx", CanFrame(0x321, data=b"back", timestamp_ns=9003))
            sim.run_until(103)
            assert other.recv(72) == struct.pack("=IB3x8s", 0x321, 4, b"back\0\0\0\0")
            with pytest.raises(ValueError, match="16-byte classic"):
                unpack_can_frame(b"malformed", sim.t_sim_ns)
            assert sim.state == SimulationState.PAUSED
            sim.run_until(110)
        assert local.fileno() >= 0 and stream.sock.fileno() >= 0
    finally:
        local.close()
        other.close()


class ShortSocket:
    """Inject short writes over an actual socket; leave the stream queue real."""

    def __init__(self, sock):
        self.sock = sock
        self.limit = 7

    def send(self, data):
        return self.sock.send(data[:self.limit])

    def recv(self, size):
        return self.sock.recv(size)


def test_new_session_rebuild_keeps_exact_partial_rx_and_tx_suffixes(streams):
    original, peer = streams
    short = ShortSocket(original.sock)
    stream = CanStream(short)
    first = graph(stream, reply=True)
    old_device = first.components["device"]
    old_bridge = first.components["bridge"]
    first.step(1)
    old = wire(0x201, 9001, b"state")
    assert bytes(stream.tx_queue[0]) == old[7:]
    incoming = wire(0x200, 9005, b"old")
    peer.sendall(incoming[:23])
    assert stream.receive() == [] and bytes(stream.buffer) == incoming[:23]
    suffix = stream.tx_queue[0]
    first.close()
    assert old_device.closed == 1 and stream.tx_queue[0] is suffix

    with closing(graph(stream, session=Session(8, 200), qemu_origin=18000, reply=True)) as second:
        assert second.components["device"] is not old_device
        assert second.components["bridge"] is not old_bridge
        assert old_bridge.endpoint.submitted_frames == 1
        assert second.components["bridge"].endpoint.submitted_frames == 0
        assert stream.tx_queue[0] is suffix and bytes(stream.buffer) == incoming[:23]
        peer.sendall(incoming[23:])
        (identifier, flags, dlc, stamp, payload), = stream.receive()
        second.schedule_input("bridge.qemu_rx", CanFrame(identifier, flags, dlc, payload, stamp))
        second.step(1)
        assert second.components["device"].received == [
            CanFrame(0x200, data=b"old", timestamp_ns=200)]
        short.limit = 4096
        assert stream.flush()
        assert peer.recv(168) == old + wire(0x201, 18001, b"state")
        assert second.components["bridge"].endpoint.submitted_frames == 1
    assert stream.sock.sock.fileno() >= 0


def test_bus_capacity_failure_freezes_before_delivery_and_cannot_replay(streams):
    stream, _ = streams
    sim = graph(stream, options={"max_pending": 1})
    device = sim.components["device"]
    for data in (b"queued", b"overflow"):
        sim.schedule_input("bridge.qemu_rx", CanFrame(0x200, data=data, timestamp_ns=9000))
    with pytest.raises(BufferError, match="capacity") as caught:
        sim.run_until(101)
    assert sim.failure is caught.value and sim.state == SimulationState.FAILED
    assert sim.t_sim_ns == 100 and device.received == [] and device.intervals == []
    with pytest.raises(RuntimeError, match="healthy"):
        sim.run_until(101)
    sim.close()
    assert device.closed == 1 and stream.sock.fileno() >= 0


def test_partial_transport_failure_records_no_submission_and_never_replays(streams):
    stream, peer = streams
    failure = OSError("failed after prefix")
    calls = []

    def send(*args):
        calls.append(args)
        stream.sock.send(b"prefix")
        raise failure

    sim = graph(SimpleNamespace(send=send), reply=True)
    bridge = sim.components["bridge"]
    with pytest.raises(OSError) as caught:
        sim.step(1)
    assert caught.value is failure and sim.failure is failure
    assert peer.recv(84) == b"prefix" and bridge.endpoint.submitted_frames == 0
    with pytest.raises(RuntimeError):
        sim.step(1)
    assert len(calls) == 1
    sim.close()
    with pytest.raises(BlockingIOError):
        peer.recv(84)


def test_drop_and_observation_callbacks_keep_canonical_events_and_terminal_failures(streams):
    stream, _ = streams
    decisions = []
    with closing(graph(stream, drop=lambda _source, _dest, _frame: True,
                       on_delivery=decisions.append)) as sim:
        sim.schedule_input("bridge.qemu_rx", CanFrame(0x200, data=b"drop", timestamp_ns=9000))
        sim.run_until(100)
        assert sim.components["device"].received == []
        receipt, = decisions
        event, = [event for event in sim.events if event.kind == "can.delivery"]
        assert receipt.dropped and event.payload["dropped"]

    errors = []
    sim = None

    def observed(_delivery):
        # A caught queue overflow must still stop before destination.receive.
        sim.schedule_input("bridge.qemu_rx", CanFrame(1))
        try:
            sim.schedule_input("bridge.qemu_rx", CanFrame(2))
        except BufferError as error:
            errors.append(error)

    sim = graph(stream, on_delivery=observed, max_pending_inputs=1)
    sim.schedule_input("bridge.qemu_rx", CanFrame(0x200, timestamp_ns=9000))
    with pytest.raises(BufferError) as caught:
        sim.run_until(100)
    assert caught.value is errors[0] and sim.failure is errors[0]
    assert sim.components["device"].received == []
    sim.close()


def test_factory_and_endpoint_admission_have_no_attachment_or_transport_effects():
    calls = []
    port = SimpleNamespace(attach=lambda *_: calls.append("attach"),
                           send=lambda *_: calls.append("send"))
    for endpoint in (QemuCanEndpoint, SocketCanEndpoint):
        with pytest.raises(TypeError, match="callable send"):
            endpoint(port, object())
        with pytest.raises(TypeError, match="attach and send"):
            endpoint(object(), SimpleNamespace(send=lambda *_: None))
    assert calls == []
    for kwargs in ({"drop": False}, {"on_delivery": 1}):
        with pytest.raises(TypeError, match="callable or None"):
            CanBusFactory(**kwargs)
    factory = CanTransportBridgeFactory(SimpleNamespace(send=lambda *_: calls.append("io")),
                                        QemuTimestampMapper(9000, 100))
    spec = NodeSpec("bridge", "bridge")
    description = factory.describe(spec)
    assert [port.name for port in description.ports] == ["qemu", "qemu_rx"]
    with pytest.raises(TypeError, match="CanFrame"):
        factory.validate_input(spec, "qemu_rx", b"wire", connected_ports=())
    with pytest.raises(ValueError, match="internally connected"):
        factory.validate_input(spec, "qemu_rx", CanFrame(1), connected_ports=("qemu_rx",))
    assert calls == []


def test_borrowed_port_rejects_wrong_source_and_duplicate_attachment():
    endpoint = SimpleNamespace(receive=lambda _: None)
    calls = []
    connection = SimpleNamespace(send=lambda frame: calls.append(frame))
    context = SimpleNamespace(attach=lambda port, peer: connection)
    port = BorrowedCanPort(context, "qemu")
    with pytest.raises(ValueError, match="unbound"):
        port.send(endpoint, CanFrame(1))
    port.attach(endpoint)
    with pytest.raises(ValueError, match="already"):
        port.attach(endpoint)
    with pytest.raises(ValueError, match="unbound"):
        port.send(object(), CanFrame(1))
    frame = CanFrame(1, data=b"ok")
    port.send(endpoint, frame)
    assert calls == [frame]
