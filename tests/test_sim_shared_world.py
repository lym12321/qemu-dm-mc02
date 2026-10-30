"""Socketpair isolation of one actual core/M3508 owner and two board exchanges.

Only native physics and independent board response producers are isolated.
Literal protocol bytes and control/expiry expectations are independent of the
runtime helpers. Referee consumers use real engines and byte transport. This
does not run QEMU or prove firmware readiness, consumption or CPU timing.
"""

from contextlib import ExitStack
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
import socket
import struct
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sim.core import Session, StepContext
from sim.devices.dji import M3508Device, M3508JointPlant
from sim.devices.referee import (
    Buff, GameStatus, Hit, MatchDurations, PowerHeat, RefereeEngine, RefereeState,
    RefereeUartEndpoint, RobotStatus, ScriptEvent, Shot, decode_frame,
)
from sim.fabric import CanBus, CanFrame
from sim.machines.qemu.endpoints import QemuCanEndpoint
from sim.physics import ImuSample, JointState
from sim.robots.shared_world import (
    BoardBinding, BoardExecution, RefereeBinding, SharedWorldRuntime,
)
from sim.transports import StreamTransport
from sim.transports.can_stream import CanStream
from sim.transports.cosim import StepFramedSocket, V2SessionReset
from sim.transports import step_codec as codec


def wire(kind, step, timestamp, dt=0, session=71, payload=b""):
    # Existing public DMC2 magic/version and 36-byte header, literal layouts.
    body = struct.pack("<IHHIQQQI", 0x32434D44, 2, kind, len(payload),
                       step, timestamp, dt, session) + payload
    return struct.pack("<I", len(body)) + body


def can_wire(timestamp, command):
    return struct.pack("<IIB3xQ64s", 0x200, 0, 8, timestamp,
                       bytes.fromhex(command).ljust(64, b"\0"))


class NativeStub:
    def __init__(self):
        self.resets = 0
        self.steps = []
        self.efforts = []
        self.closed = 0
        self.samples = {
            "left": ImuSample((0, 0, .1), (0, 0, 0)),
            "right": ImuSample((0, 0, -.1), (0, 0, 0)),
        }
        self.reads = []

    def reset(self):
        self.resets += 1

    def step(self, dt):
        self.steps.append(dt)

    def validate_step(self, dt):
        pass

    def validate_steps(self, intervals):
        pass

    def validate_joint_effort(self, name, effort):
        pass

    def set_joint_effort(self, name, effort):
        self.efforts.append((name, effort))

    def get_joint_state(self, name):
        return JointState(0, 0)

    def get_imu(self, name):
        self.reads.append((name, tuple(self.steps)))
        return self.samples[name]

    def close(self):
        self.closed += 1


class CountingCanStream(CanStream):
    def __init__(self, sock, native):
        super().__init__(sock)
        self.native = native
        self.reads = []

    def receive(self):
        self.reads.append(self.native.resets)
        return super().receive()


class Boards:
    """Independent response producers; never execute a core/device/world tick."""

    def __init__(self, peers, native):
        self.peers = tuple(StepFramedSocket(peer) for peer in peers)
        self.native = native
        self.origins = [1000, 2000]
        self.frames = [[], []]
        self.on_frame = None
        self.events = []
        self.failure = None

    def __call__(self):
        if self.failure is not None:
            raise self.failure
        for index, peer in enumerate(self.peers):
            for frame in peer.receive():
                self.frames[index].append(frame)
                self.events.append((index, frame.header.kind, self.native.resets,
                                    tuple(self.native.steps)))
                if self.on_frame is not None and self.on_frame(index, frame):
                    continue
                if frame.header.kind == codec.RESET:
                    payload = struct.pack("<IIQII", 0, 3, self.origins[index], 0, 0)
                    kind = 5  # RESET_ACK
                elif frame.header.kind == codec.STEP:
                    payload = bytes(16)
                    kind = 3  # STEP_ACK
                else:
                    raise AssertionError("fixture received an unsupported request")
                peer.send(wire(kind, frame.header.step_id, frame.header.t_sim_ns,
                               frame.header.dt_ns, frame.header.session_id, payload))


@pytest.fixture
def world_resources(request):
    with ExitStack() as stack:
        native = NativeStub()
        buses = (CanBus(), CanBus())
        # Only the focused feedback consumers request the fixed four-per-bus
        # profile; all existing users retain their original devices and joints.
        count = getattr(request, "param", 1)
        devices = tuple(M3508Device(bus, motor_id)
                        for bus in buses for motor_id in range(1, count + 1))
        joints = ("j1", "j2") if count == 1 else tuple(f"j{i}" for i in range(1, 9))
        plant = M3508JointPlant(native, dict(zip(devices, joints)), gear_ratio=2)
        # A successful runtime owns the plant. Only a rejected construction
        # leaves this fixture responsible for its still-open resource.
        stack.callback(lambda: None if plant.closed else plant.close())
        streams, can_peers, cosims, cosim_peers = [], [], [], []
        for _ in range(2):
            for stream_class, output, peers in (
                    (CountingCanStream, streams, can_peers),
                    (StepFramedSocket, cosims, cosim_peers)):
                left, right = socket.socketpair()
                for sock in (left, right):
                    sock.setblocking(False)
                    stack.callback(sock.close)
                output.append(stream_class(left, native) if stream_class is CountingCanStream
                              else stream_class(left))
                peers.append(right)
        endpoints = tuple(QemuCanEndpoint(bus, stream)
                          for bus, stream in zip(buses, streams))
        bindings = tuple(BoardBinding(name, endpoint, cosim, name)
                         for name, endpoint, cosim in zip(("left", "right"), endpoints, cosims))
        boards = Boards(cosim_peers, native)
        observations = []
        yield plant, boards, native, bindings, can_peers, observations, stack


@pytest.fixture
def setup(world_resources):
    plant, boards, native, bindings, peers, observations, stack = world_resources
    runtime = SharedWorldRuntime(
        plant, bindings, check_running=boards,
        on_response=lambda *response: observations.append(response))
    stack.callback(runtime.close)
    yield runtime, boards, native, bindings, peers, observations


def start(runtime):
    return runtime.start(Session(7, 500), (71, 72), deadline=time.monotonic() + 1)


def test_shared_world_transfers_real_participants_and_projects_one_clock(setup):
    from sim.runtime import Simulation
    runtime, _, native, _, _, _ = setup
    assert runtime._simulation is None
    start(runtime)
    simulation = runtime._simulation
    assert isinstance(simulation, Simulation)
    assert simulation.session == Session(7, 500)
    assert all(factory.pending is None for factory in runtime._participant_factories.values())
    assert tuple(simulation.components) == ('boards', 'can_0', 'can_1', 'plant')
    runtime.step(10_000_000, deadline=time.monotonic() + 1)
    assert runtime.t_sim_ns == simulation.t_sim_ns == 10_000_500
    assert runtime.step_id == simulation.step_id == 1
    assert native.steps == [10_000_000]
    runtime.close()
    assert simulation.state == 'closed' and native.closed == 1


def test_partial_factory_creation_closes_transferred_and_pending_owners(referee_setup, monkeypatch):
    runtime, _, native, bindings, _, _, referees, _ = referee_setup
    primary = RuntimeError('later referee construction refused')
    def fail(*_args):
        raise primary
    monkeypatch.setattr(runtime._participant_factories['referee_1'], 'create', fail)
    with pytest.raises(RuntimeError) as caught:
        start(runtime)
    assert caught.value is primary
    assert native.closed == 1 and referees[0].engine.closed
    assert not referees[1].engine.closed
    runtime.close()
    assert referees[1].engine.closed and all(item.endpoint.closed for item in referees)
    assert all(item.cosim.sock.fileno() >= 0 and item.endpoint.stream.sock.fileno() >= 0
               for item in bindings)
    runtime.close()
    assert native.closed == 1


def test_binding_and_endpoint_are_fixed_borrowed_queries(setup):
    runtime, _, native, bindings, _, _ = setup
    assert runtime.failed and not runtime.closed
    assert native.resets == 0 and native.steps == []
    with pytest.raises(FrozenInstanceError):
        bindings[0].name = "changed"
    with pytest.raises(AttributeError):
        bindings[0].endpoint.stream = bindings[1].endpoint.stream
    from tools import dm_mc02_sim_worker as worker
    assert worker.CanStream is CanStream


@pytest.mark.parametrize("invalid", ["tuple", "name", "bus", "socket", "represented", "imu"])
def test_full_admission_rejects_before_ownership_or_io(setup, invalid):
    runtime, _, native, bindings, _, _ = setup
    plant = runtime.plant
    original_reads = list(native.reads)
    if invalid == "tuple":
        bindings = list(bindings)
    elif invalid == "name":
        bindings = (bindings[0], replace(bindings[1], name="left"))
    elif invalid == "bus":
        bindings = (bindings[0], replace(bindings[1], endpoint=bindings[0].endpoint))
    elif invalid == "socket":
        bindings = (bindings[0], replace(bindings[1], cosim=StepFramedSocket(bindings[0].cosim.sock)))
    elif invalid == "represented":
        endpoint = QemuCanEndpoint(CanBus(), bindings[1].endpoint.stream)
        bindings = (bindings[0], replace(bindings[1], endpoint=endpoint))
    else:
        native.samples["right"] = ImuSample((0, float("nan"), 0), (0, 0, 0))
    with pytest.raises((TypeError, ValueError)):
        SharedWorldRuntime(plant, bindings)
    assert not plant.closed and native.resets == 0 and native.steps == []
    assert all(item.endpoint.stream.reads == [] for item in runtime.bindings)
    assert all(item.cosim.tx_pending is False for item in runtime.bindings)
    if invalid != "imu":
        assert native.reads == original_reads


def test_all_reset_acks_precede_one_reset_and_queued_can_uses_each_origin(setup):
    runtime, boards, native, bindings, can_peers, observations = setup
    # Pre-reset physical records, one deliberately split record. Existing stale
    # timestamp fallback maps each to the common logical start, never clamps a
    # future command or deletes the suffix at reset.
    commands = ("1000 0000 0000 0000", "f000 0000 0000 0000")
    raw = tuple(can_wire(origin - 1, command)
                for origin, command in zip(boards.origins, commands))
    can_peers[0].send(raw[0])
    can_peers[1].send(raw[1][:19])
    responses = start(runtime)
    assert [response.header.session_id for response in responses] == [71, 72]
    assert [(name, frame.header.kind, payload.virtual_time_ns)
            for name, frame, payload in observations] == [
                ("left", 5, 1000), ("right", 5, 2000)]
    assert boards.events == [(0, 1, 0, ()), (1, 1, 0, ())]
    assert native.resets == 1 and runtime.t_sim_ns == 500 and runtime.step_id == 0
    assert all(item.endpoint.stream.reads == [] for item in bindings)
    runtime.service(deadline=time.monotonic() + 1)
    assert [item.endpoint.bus.pending for item in bindings] == [1, 0]
    assert bytes(bindings[1].endpoint.stream.buffer) == raw[1][:19]
    can_peers[1].send(raw[1][19:])
    acks = runtime.step(10_000_000, deadline=time.monotonic() + 1)
    assert [response.header.session_id for response in acks] == [71, 72]
    assert native.steps == [10_000_000]
    assert native.efforts == [("j1", .2), ("j2", -.2)]
    assert runtime.t_sim_ns == 10_000_500 and runtime.step_id == 1 and not runtime.failed
    assert all(value == 1 for item in bindings for value in item.endpoint.stream.reads)
    for index, peer in enumerate(can_peers):
        # Stub observation is zero. Literal motor feedback retains current and
        # independent board-QEMU epoch rather than the logical500ns origin.
        literal = bytes.fromhex("0000 0000 " + ("1000" if index == 0 else "f000") + " 19 00")
        assert peer.recv(84) == struct.pack("<IIB3xQ64s", 0x201, 0, 8,
                                           boards.origins[index] + 10_000_000,
                                           literal.ljust(64, b"\0"))
        frame = boards.frames[index][-1]
        assert (frame.header.step_id, frame.header.t_sim_ns, frame.header.dt_ns) == (
            1, 10_000_500, 10_000_000)
        # +/-.1rad/s = +/-(18/pi)deg/s; fixed binary32 literal code verifies
        # conversion/published payload without calling runtime's converter.
        assert frame.payload[:8] == bytes(8)
        assert struct.unpack_from("<f", frame.payload, 8)[0] == pytest.approx(
            5.729577951308232 if index == 0 else -5.729577951308232)
        assert frame.payload[12:48] == bytes(36)
        assert struct.unpack_from("<QI", frame.payload, 48) == (10_000_500, 1)


def test_tick_preserves_existing_plant_expiry_union(setup):
    runtime, boards, native, _, peers, _ = setup
    for device, timeout in zip(runtime.plant.devices, (3_000_000, 7_000_000)):
        device.command_timeout_ns = timeout
    start(runtime)
    for peer, origin, literal in zip(peers, boards.origins,
                                    ("1000 0000 0000 0000", "f000 0000 0000 0000")):
        peer.send(can_wire(origin, literal))
    runtime.step(10_000_000, deadline=time.monotonic() + 1)
    assert native.resets == 1 and native.steps == [3_000_000, 4_000_000, 3_000_000]
    assert native.efforts == [("j1", .2), ("j2", -.2), ("j1", 0),
                              ("j2", -.2), ("j1", 0), ("j2", 0)]


@pytest.mark.parametrize("invalid", ["target", "mapped_target", "dt", "sample", "deadline"])
def test_tick_preflight_rejects_before_any_io_or_world_effect(setup, invalid, monkeypatch):
    runtime, boards, native, bindings, peers, _ = setup
    dt = 1
    if invalid == "target":
        runtime.start(Session(7, (1 << 64) - 1), (71, 72), deadline=time.monotonic() + 1)
    else:
        if invalid == "mapped_target":
            boards.origins[1] = (1 << 64) - 1
        start(runtime)
    if invalid == "dt":
        dt = 1 << 63
    if invalid == "sample":
        monkeypatch.setattr(SharedWorldRuntime, "step_id", property(lambda _: (1 << 32) - 1))
    peers[0].send(can_wire(0, "1000 0000 0000 0000"))
    deadline = float("nan") if invalid == "deadline" else time.monotonic() + 1
    with pytest.raises(ValueError):
        runtime.step(dt, deadline=deadline)
    assert runtime.failed and native.resets == 1 and native.steps == []
    assert all(item.endpoint.stream.reads == [] for item in bindings)
    assert all(len(frames) == 1 for frames in boards.frames)
    with pytest.raises(RuntimeError, match="fresh runtime"):
        runtime.step(1, deadline=time.monotonic() + 1)


def test_all_imu_sources_encode_before_any_step_send_and_failure_retains_world(setup):
    runtime, boards, native, _, _, _ = setup
    start(runtime)
    native.samples["right"] = ImuSample((0, 0, 1e100), (0, 0, 0))
    with pytest.raises(ValueError, match="binary32"):
        runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert runtime.failed and native.steps == [1_000_000] and native.resets == 1
    assert all([frame.header.kind for frame in frames] == [1] for frames in boards.frames)
    with pytest.raises(RuntimeError):
        start(runtime)
    with pytest.raises(RuntimeError):
        runtime.step(1, deadline=time.monotonic() + 1)
    assert native.steps == [1_000_000]


@pytest.mark.parametrize("during_start", [True, False])
def test_unsolicited_reset_interrupts_active_wait_without_second_world_reset(setup, during_start):
    runtime, boards, native, _, _, observations = setup
    if not during_start:
        start(runtime)
    def reset_peer(index, frame):
        if index != 1:
            return False
        boards.peers[0 if not during_start else 1].send(wire(
            1, 0, 9000, session=73))
        return True  # Withhold active response; idle RESET must interrupt it.
    boards.on_frame = reset_peer
    with pytest.raises(V2SessionReset) as caught:
        if during_start:
            start(runtime)
        else:
            runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert (caught.value.step_id, caught.value.t_sim_ns, caught.value.session_id) == (0, 9000, 73)
    assert runtime.failed and native.resets == (0 if during_start else 1)
    assert native.steps == ([] if during_start else [1_000_000])
    if not during_start:
        assert any(name == "left" and frame.header.kind == 1 and payload is None
                   for name, frame, payload in observations)
    with pytest.raises(RuntimeError):
        start(runtime)
    with pytest.raises(RuntimeError):
        runtime.step(1, deadline=time.monotonic() + 1)


def test_idle_done_preserves_real_missing_mask_without_consumption_claim(setup):
    runtime, boards, native, _, _, observations = setup
    start(runtime)
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    boards.peers[0].send(wire(6, 1, 1_000_500, 1_000_000, 71,
                             struct.pack("<IIIII", 2, 0, 3, 0, 0)))
    runtime.service(deadline=time.monotonic() + 1)
    name, frame, done = observations[-1]
    assert (name, frame.header.kind, done.status, done.consumed_mask, done.missing_mask) == (
        "left", 6, 2, 0, 3)
    assert not runtime.failed and native.steps == [1_000_000]


def test_running_failure_and_cleanup_keep_borrowed_suffixes(setup):
    runtime, boards, native, bindings, _, _ = setup
    start(runtime)
    primary = KeyboardInterrupt("peer exited")
    boards.failure = primary
    bindings[0].cosim.buffer.extend(b"physical tail")
    bindings[0].endpoint.stream.buffer.extend(b"CAN tail")
    with pytest.raises(KeyboardInterrupt) as caught:
        runtime.service(deadline=time.monotonic() + 1)
    assert caught.value is primary and runtime.failed and native.steps == []
    runtime.close()
    runtime.close()
    assert native.closed == 1 and runtime.closed
    assert bytes(bindings[0].cosim.buffer) == b"physical tail"
    assert bytes(bindings[0].endpoint.stream.buffer) == b"CAN tail"
    assert all(item.cosim.sock.fileno() >= 0 and item.endpoint.stream.sock.fileno() >= 0
               for item in bindings)


def test_outer_deadline_freezes_after_late_reset_ack(setup, monkeypatch):
    runtime, boards, native, _, _, _ = setup
    now = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    def late(index, frame):
        if index == 1:
            now[0] += 10
        return False
    boards.on_frame = late
    with pytest.raises(RuntimeError, match="host deadline"):
        runtime.start(Session(7), (71, 72), deadline=now[0] + 1)
    assert runtime.failed and native.resets == 0 and native.steps == []
    # The actual ACK has reached the borrow; no world projection is permitted
    # merely because canonical active wait could return a complete response.
    assert [len(frames) for frames in boards.frames] == [1, 1]


def test_expired_start_deadline_has_no_transport_or_world_effect(setup):
    runtime, boards, native, _, _, _ = setup
    with pytest.raises(RuntimeError, match="host deadline"):
        runtime.start(Session(7), (71, 72), deadline=time.monotonic() - 1)
    assert runtime.failed and native.resets == 0 and native.steps == []
    assert boards.frames == [[], []]


def test_queue_full_replays_exact_step_bytes_without_repeating_world_or_samples(setup):
    runtime, boards, native, _, _, _ = setup
    start(runtime)
    def full_once(index, frame):
        if index == 0 and frame.header.kind == 2 and len(boards.frames[0]) == 2:
            boards.peers[0].send(wire(3, 1, 1_000_500, 1_000_000, 71,
                                    struct.pack("<IIII", 1, 7, 0, 0)))
            return True
        return False
    boards.on_frame = full_once
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert boards.frames[0][1] == boards.frames[0][2]
    assert len(boards.frames[1]) == 2
    assert native.steps == [1_000_000]
    assert native.reads == [("left", ()), ("right", ()),
                            ("left", (1_000_000,)), ("right", (1_000_000,))]


def test_outer_deadline_is_not_retried_by_canonical_step_helper(setup, monkeypatch):
    runtime, boards, native, _, _, _ = setup
    start(runtime)
    now = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    def late(index, frame):
        if frame.header.kind == 2:
            now[0] += 10
        return False
    boards.on_frame = late
    with pytest.raises(RuntimeError, match="host deadline"):
        runtime.step(1_000_000, deadline=now[0] + 1)
    assert runtime.failed and native.steps == [1_000_000]
    assert [len(frames) for frames in boards.frames] == [2, 1]
    boards()
    assert [len(frames) for frames in boards.frames] == [2, 1]


def test_future_can_timestamp_cannot_control_earlier_interval(setup):
    runtime, boards, native, _, peers, _ = setup
    start(runtime)
    peers[0].send(can_wire(boards.origins[0] + 1, "1000 0000 0000 0000"))
    runtime.step(10, deadline=time.monotonic() + 1)
    assert native.steps == [1, 9]
    assert native.efforts == [("j1", 0), ("j2", 0), ("j1", .2), ("j2", 0)]
    runtime.step(10, deadline=time.monotonic() + 1)
    assert native.efforts == [("j1", 0), ("j2", 0)] + [("j1", .2), ("j2", 0)] * 2
    assert native.steps == [1, 9, 10]


def test_scoped_can_delay_keeps_pending_command_and_feedback_due_with_independent_epochs(setup):
    runtime, boards, native, bindings, peers, _ = setup
    buses = tuple(item.endpoint.bus for item in bindings)
    deliveries = ([], [])
    for bus, rows in zip(buses, deliveries):
        bus.log = rows.append
    start(runtime)  # Shared logical 500ns, independent QEMU 1000/2000ns.
    runtime.set_can_delay("left", 2_000_000)
    for peer, origin, command in zip(peers, boards.origins,
                                     ("1000 0000 0000 0000", "f000 0000 0000 0000")):
        peer.send(can_wire(origin, command))
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert [bus.pending for bus in buses] == [2, 0]
    assert [device.current_raw for device in runtime.plant.devices] == [0, -4096]
    with pytest.raises(BlockingIOError):
        peers[0].recv(84)

    # This command is still physical input when policy changes. Ingress later
    # uses zero delay; the older command's 2ms and feedback's 3ms due stay fixed.
    peers[0].send(can_wire(1_001_000, "2000 0000 0000 0000"))
    runtime.set_can_delay("left", 0)
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert [bus.pending for bus in buses] == [1, 0]
    assert [device.current_raw for device in runtime.plant.devices] == [4096, -4096]
    runtime.set_can_delay("left", 3_000_000)
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert [bus.pending for bus in buses] == [1, 0]
    runtime.set_can_delay("left", 0)
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert [bus.pending for bus in buses] == [1, 0]
    runtime.step(2_000_000, deadline=time.monotonic() + 1)
    assert [bus.pending for bus in buses] == [0, 0]
    assert native.resets == 1 and native.steps == [1_000_000] * 4 + [2_000_000]
    assert native.efforts == [("j1", 0), ("j2", -.2), ("j1", .4), ("j2", -.2),
                              ("j1", .2), ("j2", -.2), ("j1", .2), ("j2", -.2),
                              ("j1", .2), ("j2", -.2)]
    assert runtime.t_sim_ns == 6_000_500 and runtime.step_id == 5 and not runtime.failed
    assert [bus.delay_ns for bus in buses] == [0, 0]

    # Fixed official C620 payloads, zero rotor observation and literal epochs;
    # these do not invoke any production wire, time or feedback helper.
    zero, old, new, right = (bytes.fromhex(value) for value in (
        "0000 0000 0000 19 00", "0000 0000 1000 19 00",
        "0000 0000 2000 19 00", "0000 0000 f000 19 00"))
    for peer, expected in zip(peers, (
            ((2_001_000, new), (3_001_000, zero), (4_001_000, old),
             (6_001_000, old), (6_001_000, old)),
            ((1_002_000, right), (2_002_000, right), (3_002_000, right),
             (4_002_000, right), (6_002_000, right)))):
        assert uart_received(peer) == b"".join(
            struct.pack("<IIB3xQ64s", 0x201, 0, 8, stamp, data.ljust(64, b"\0"))
            for stamp, data in expected)
    assert [(row.frame.data, row.sequence, row.timestamp_ns, row.due_ns, row.delivered_ns)
            for row in deliveries[0]] == [
        (bytes.fromhex("2000 0000 0000 0000"), 2, 1_000_500, 1_000_500, 1_000_500),
        (bytes.fromhex("1000 0000 0000 0000"), 0, 500, 2_000_500, 2_000_500),
        (new, 3, 2_000_500, 2_000_500, 2_000_500),
        (zero, 1, 1_000_500, 3_000_500, 3_000_500),
        (old, 5, 4_000_500, 4_000_500, 4_000_500),
        (old, 4, 3_000_500, 6_000_500, 6_000_500),
        (old, 6, 6_000_500, 6_000_500, 6_000_500)]
    assert [(row.sequence, row.timestamp_ns, row.due_ns, row.delivered_ns)
            for row in deliveries[1]] == [
        (0, 500, 500, 500), (1, 1_000_500, 1_000_500, 1_000_500),
        (2, 2_000_500, 2_000_500, 2_000_500), (3, 3_000_500, 3_000_500, 3_000_500),
        (4, 4_000_500, 4_000_500, 4_000_500), (5, 6_000_500, 6_000_500, 6_000_500)]
    runtime.set_can_delay("left", 17)
    runtime.close()
    assert [bus.delay_ns for bus in buses] == [17, 0] and native.closed == 1
    assert all(item.endpoint.stream.sock.fileno() >= 0 and item.cosim.sock.fileno() >= 0
               for item in bindings)


@pytest.mark.parametrize("world_resources", [4], indirect=True)
def test_motor_feedback_scope_recovery_preserves_eight_devices_and_other_destinations(setup):
    runtime, boards, native, bindings, peers, _ = setup
    bus = bindings[0].endpoint.bus
    deliveries, tapped = [], []
    bus.log = deliveries.append
    tap = SimpleNamespace(receive=tapped.append)
    bus.attach(tap, "tap")
    start(runtime)
    assert runtime.set_motor_feedback_enabled("left", 1, False) is None
    # A different actual source using the same arbitration ID still reaches
    # the board; the reachability policy must not parse CAN IDs or payloads.
    bus.send(tap, CanFrame(0x201, 0, 8, bytes.fromhex("0102 0304 0506 0708"), 500))
    for peer, origin, command in zip(peers, boards.origins,
                                     ("1000 2000 3000 4000", "f000 e000 d000 c000")):
        peer.send(can_wire(origin, command))
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert [device.current_raw for device in runtime.plant.devices] == [
        4096, 8192, 12288, 16384, -4096, -8192, -12288, -16384]
    first = tuple(uart_received(peer) for peer in peers)
    assert [(row.source, row.destination, row.frame.can_id) for row in deliveries if row.dropped] == [
        ("m3508-1", "qemu", 0x201)]
    assert [(frame.can_id, frame.data) for frame in tapped] == [
        (0x200, bytes.fromhex("1000 2000 3000 4000")),
        *[(0x201 + i, bytes.fromhex("0000 0000 " + value + " 19 00"))
          for i, value in enumerate(("1000", "2000", "3000", "4000"))]]
    assert runtime.set_motor_feedback_enabled("left", 1, True) is None and bus.drop is None
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    second = tuple(uart_received(peer) for peer in peers)
    for index, currents in enumerate((("1000", "2000", "3000", "4000"),
                                     ("f000", "e000", "d000", "c000"))):
        for raw, stamp, ids in ((first[index], 1_001_000 + index * 1000,
                                range(2 if index == 0 else 1, 5)),
                               (second[index], 2_001_000 + index * 1000, range(1, 5))):
            prefix = (struct.pack("<IIB3xQ64s", 0x201, 0, 8, 1000,
                                  bytes.fromhex("0102 0304 0506 0708").ljust(64, b"\0"))
                      if index == 0 and stamp == 1_001_000 else b"")
            assert raw == prefix + b"".join(struct.pack(
                "<IIB3xQ64s", 0x200 + motor_id, 0, 8, stamp,
                bytes.fromhex("0000 0000 " + currents[motor_id - 1] + " 19 00").ljust(64, b"\0"))
                for motor_id in ids)
    expected_efforts = list(zip((f"j{i}" for i in range(1, 9)),
                               (.2, .4, .6, .8, -.2, -.4, -.6, -.8)))
    assert native.efforts == expected_efforts * 2 and native.steps == [1_000_000] * 2
    assert all(item.endpoint.bus.pending == 0 for item in bindings)
    assert not runtime.failed and runtime.step_id == 2


def test_motor_feedback_changes_only_dispatch_decisions_for_old_pending_frames(setup):
    runtime, _, native, bindings, peers, _ = setup
    bus, deliveries = bindings[0].endpoint.bus, []
    bus.log = deliveries.append
    start(runtime)
    runtime.set_can_delay("left", 2_000_000)
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert bus.pending == 1
    runtime.set_motor_feedback_enabled("left", 1, False)
    runtime.set_can_delay("left", 0)
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert bus.pending == 1
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert bus.pending == 0 and uart_received(peers[0]) == b""
    assert [(row.sequence, row.timestamp_ns, row.due_ns, row.delivered_ns, row.dropped)
            for row in deliveries] == [
        (1, 2_000_500, 2_000_500, 2_000_500, True),
        (0, 1_000_500, 3_000_500, 3_000_500, True),
        (2, 3_000_500, 3_000_500, 3_000_500, True)]
    assert all(row.frame.data == bytes.fromhex("0000 0000 0000 19 00")
               for row in deliveries)
    runtime.set_motor_feedback_enabled("left", 1, True)
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert uart_received(peers[0]) == struct.pack(
        "<IIB3xQ64s", 0x201, 0, 8, 4_001_000,
        bytes.fromhex("0000 0000 0000 19 00").ljust(64, b"\0"))
    assert len(uart_received(peers[1])) == 4 * 84 and native.steps == [1_000_000] * 4
    assert not runtime.failed and runtime.step_id == 4


def test_motor_feedback_composes_old_drop_once_before_block_and_preserves_errors(setup):
    runtime, _, native, bindings, peers, _ = setup
    bus, calls, deliveries, tapped = bindings[0].endpoint.bus, [], [], []
    tap = SimpleNamespace(receive=tapped.append)
    bus.attach(tap, "tap")
    primary = OSError("existing drop failed")
    fail = False

    def original(source, destination, frame):
        calls.append((source, destination, frame))
        if fail:
            raise primary
        return destination is tap

    bus.drop, bus.log = original, deliveries.append
    start(runtime)
    runtime.set_motor_feedback_enabled("left", 1, False)
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    device = runtime.plant.devices[0]
    assert [(source, destination) for source, destination, _ in calls] == [
        (device, bindings[0].endpoint), (device, tap)]
    assert [(row.destination, row.dropped) for row in deliveries] == [("qemu", True), ("tap", True)]
    assert not tapped and uart_received(peers[0]) == b""
    fail = True
    with pytest.raises(OSError) as caught:
        runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert caught.value is primary and len(calls) == 3 and len(deliveries) == 2
    assert native.steps == [1_000_000] * 2 and runtime.failed
    with pytest.raises(RuntimeError):
        runtime.set_motor_feedback_enabled("left", 1, True)
    runtime.close()
    assert bus.drop is original and native.closed == 1


@pytest.mark.parametrize("world_resources", [4], indirect=True)
def test_motor_feedback_pure_admission_and_lazy_last_enable_restore(setup, monkeypatch):
    runtime, boards, native, bindings, peers, _ = setup
    start(runtime)
    bus, other = (item.endpoint.bus for item in bindings)
    original = lambda *args: False
    bus.drop = original
    peers[0].send(can_wire(1000, "1000 0000 0000 0000"))
    before = (runtime.t_sim_ns, runtime.step_id, list(native.steps), list(native.reads),
              [list(stream.endpoint.stream.reads) for stream in bindings],
              [list(frames) for frames in boards.frames])

    def forbidden(*args, **kwargs):
        pytest.fail("feedback policy admission performed IO or advanced the world")

    class Alias(str):
        pass

    class MotorAlias(int):
        pass

    with monkeypatch.context() as admission:
        admission.setattr(runtime, "_check_running", forbidden)
        admission.setattr(time, "monotonic", forbidden)
        for owner, methods in ((native, ("step", "reset", "get_imu", "get_joint_state")),
                               (CanBus, ("send", "step", "reset")),
                               (CanStream, ("receive", "send", "flush")),
                               (StepFramedSocket, ("receive", "send", "flush"))):
            for method in methods:
                admission.setattr(owner, method, forbidden)
        runtime.set_motor_feedback_enabled("left", 1, True)
        assert bus.drop is original  # No wrapper on the default enabled path.
        bus.drop = 3
        with pytest.raises(TypeError):
            runtime.set_motor_feedback_enabled("left", 1, False)
        assert bus.drop == 3 and not runtime.failed
        bus.drop = original
        for name, motor_id, enabled, error in (
                ("missing", 1, False, ValueError), (None, 1, False, TypeError),
                (Alias("left"), 1, False, TypeError), ("left", 8, False, ValueError),
                ("left", True, False, ValueError), ("left", MotorAlias(1), False, ValueError),
                ("left", 0, False, ValueError), ("left", 9, False, ValueError),
                ("left", 1, 0, TypeError), ("left", 1, None, TypeError)):
            with pytest.raises(error):
                runtime.set_motor_feedback_enabled(name, motor_id, enabled)
            assert bus.drop is original and other.drop is None and not runtime.failed
        runtime.set_motor_feedback_enabled("left", 1, False)
        wrapper = bus.drop
        runtime.set_motor_feedback_enabled("left", 1, False)
        runtime.set_motor_feedback_enabled("left", 2, False)
        runtime.set_motor_feedback_enabled("left", 1, True)
        assert bus.drop is wrapper and other.drop is None
        runtime.set_motor_feedback_enabled("left", 2, True)
        assert bus.drop is original
    assert (runtime.t_sim_ns, runtime.step_id, native.steps, native.reads,
            [stream.endpoint.stream.reads for stream in bindings], boards.frames) == before
    assert all(item.endpoint.bus.pending == 0 for item in bindings)
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert runtime.plant.devices[0].current_raw == 4096 and not runtime.failed


@pytest.mark.parametrize("state", ["prestart", "failed", "closed"])
def test_motor_feedback_lifecycle_refusal_preserves_borrowed_policy(setup, state):
    runtime, boards, native, bindings, _, _ = setup
    if state != "prestart":
        start(runtime)
        if state == "failed":
            boards.failure = OSError("board exited")
            with pytest.raises(OSError):
                runtime.service(deadline=time.monotonic() + 1)
        else:
            runtime.close()
    before = (list(native.steps), native.closed, [item.endpoint.bus.drop for item in bindings])
    with pytest.raises(RuntimeError, match="fresh runtime"):
        runtime.set_motor_feedback_enabled("left", 1, False)
    assert (native.steps, native.closed, [item.endpoint.bus.drop for item in bindings]) == before


def test_motor_feedback_callback_reentry_refuses_other_board(referee_setup):
    runtime, _, native, bindings, _, _, _, pairs = referee_setup
    start(runtime)
    calls = []

    def reenter():
        with pytest.raises(RuntimeError, match="fresh runtime"):
            runtime.set_motor_feedback_enabled("right", 1, False)
        calls.append("refused")

    pairs[0][0].on_send = reenter
    assert runtime.publish_referee("left", deadline=time.monotonic() + 1) == REFEREE_INITIAL_WIRE
    assert calls == ["refused"] and uart_received(pairs[0][1]) == REFEREE_INITIAL_WIRE
    assert all(item.endpoint.bus.drop is None for item in bindings)
    assert not runtime.failed and native.steps == []


def test_motor_feedback_external_replacement_refuses_then_freezes_before_io(setup):
    runtime, boards, native, bindings, peers, _ = setup
    start(runtime)
    bus = bindings[0].endpoint.bus
    runtime.set_motor_feedback_enabled("left", 1, False)
    replacement = lambda *args: False
    bus.drop = replacement
    peers[0].send(can_wire(1000, "1000 0000 0000 0000"))
    frames = [list(rows) for rows in boards.frames]
    reads = [list(item.endpoint.stream.reads) for item in bindings]
    for name, enabled in (("left", True), ("right", False)):
        with pytest.raises(RuntimeError, match="policy changed"):
            runtime.set_motor_feedback_enabled(name, 1, enabled)
        assert not runtime.failed and bus.drop is replacement
    with pytest.raises(RuntimeError, match="policy changed"):
        runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert runtime.failed and native.steps == [] and boards.frames == frames
    assert [item.endpoint.stream.reads for item in bindings] == reads
    assert bindings[0].endpoint.stream.sock.recv(84, socket.MSG_PEEK) == can_wire(
        1000, "1000 0000 0000 0000")
    with pytest.raises(RuntimeError, match="policy changed"):
        runtime.close()
    assert runtime.closed and native.closed == 1 and bus.drop is replacement
    assert all(device.closed for device in runtime.plant.devices)
    runtime.close()


@pytest.mark.parametrize("replace_policy", [False, True])
def test_motor_feedback_cleanup_restores_both_policies_after_plant_error(setup, monkeypatch, replace_policy):
    runtime, _, native, bindings, _, _ = setup
    start(runtime)
    original = lambda *args: False
    bindings[0].endpoint.bus.drop = original
    for item in bindings:
        runtime.set_motor_feedback_enabled(item.name, 1, False)
    replacement = lambda *args: True
    if replace_policy:
        bindings[0].endpoint.bus.drop = replacement
    primary, original_close = KeyboardInterrupt("native close failed"), native.close

    def close():
        for item in bindings:
            with pytest.raises(RuntimeError, match="fresh runtime"):
                runtime.set_motor_feedback_enabled(item.name, 1, True)
        original_close()
        raise primary

    monkeypatch.setattr(native, "close", close)
    with pytest.raises(KeyboardInterrupt) as caught:
        runtime.close()
    assert caught.value is primary and native.closed == 1 and runtime.closed
    assert [item.endpoint.bus.drop for item in bindings] == [replacement if replace_policy else original, None]
    if replace_policy:
        assert any("policy cleanup failed" in note and "policy changed" in note
                   for note in caught.value.__notes__)
    assert all(device.closed for device in runtime.plant.devices)
    assert all(item.cosim.sock.fileno() >= 0 and item.endpoint.stream.sock.fileno() >= 0
               for item in bindings)
    runtime.close()
    assert native.closed == 1


def test_can_delay_maximum_is_admitted_then_existing_step_overflow_is_terminal(setup, monkeypatch):
    runtime, boards, native, bindings, peers, _ = setup
    start(runtime)
    original = CanBus.delay_ns
    calls = []

    def setter(bus, value):
        calls.append((bus, value))
        original.fset(bus, value)

    monkeypatch.setattr(CanBus, "delay_ns", property(original.fget, setter))
    assert runtime.set_can_delay("left", (1 << 64) - 1) is None
    assert not runtime.failed and runtime.t_sim_ns == 500 and runtime.step_id == 0
    peers[0].send(can_wire(1000, "1000 0000 0000 0000"))
    with pytest.raises(ValueError, match="unsigned 64-bit"):
        runtime.step(1, deadline=time.monotonic() + 1)
    assert runtime.failed and native.steps == [] and native.resets == 1
    assert all(item.endpoint.stream.reads == [] for item in bindings)
    assert [len(frames) for frames in boards.frames] == [1, 1]
    with pytest.raises(RuntimeError, match="fresh runtime"):
        runtime.set_can_delay("left", 0)
    assert calls == [(bindings[0].endpoint.bus, (1 << 64) - 1)]
    assert [item.endpoint.bus.delay_ns for item in bindings] == [(1 << 64) - 1, 0]


@pytest.mark.parametrize("callback", ["liveness", "idle_response"])
def test_callback_timeout_is_terminal_and_never_replayed(setup, monkeypatch, callback):
    runtime, boards, native, _, _, _ = setup
    start(runtime)
    failure = TimeoutError("callback deadline expired")

    def once(index, frame):
        if index == 0 and frame.header.kind == 2:
            boards.on_frame = None
            if callback == "liveness":
                raise failure
            # The idle peer's valid diagnostics invokes the observation sink
            # during active ACK service; the sink's timeout is also terminal.
            boards.peers[1].send(wire(4, 0, 500, session=72, payload=bytes(40)))
        return False

    def observe(name, frame, decoded):
        if frame.header.kind == 4:
            raise failure

    boards.on_frame = once
    if callback == "idle_response":
        monkeypatch.setattr(runtime, "_on_response", observe)
    with pytest.raises(RuntimeError, match="wait service failed") as caught:
        runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert caught.value.__cause__ is failure
    assert runtime.failed and native.steps == [1_000_000]
    assert [len(frames) for frames in boards.frames] == [2, 1]
    with pytest.raises(RuntimeError, match="fresh runtime"):
        runtime.step(1_000_000, deadline=time.monotonic() + 1)
    boards()
    assert [len(frames) for frames in boards.frames] == [2, 1]


REFEREE_INITIAL = RefereeState(
    GameStatus(1, 0, 0, 0x0102030405060708),
    RobotStatus(3, 2, 100, 200, 3, 100, 80, 7),
    PowerHeat(40, 10, 20), Buff(0, 0, 0, 0, 0, 0x80))
# Fixed 2026 V1.1.0 layouts and pinned crccheck literals from the isolated
# endpoint gate. No production encoder computes the expected bytes.
REFEREE_INITIAL_WIRE = bytes.fromhex(
    "a50b00fe690100010000080706050403020138e1"
    "a50d00ffe6010203026400c80003006400500007ee23"
    "a50e0000370202000000000000000028000a00140071bd"
    "a5080001b8040200000000000000806ba9")
REFEREE_COMPLETED_PAYLOADS = (
    ((0x0001, "0100000807060504030201"),
     (0x0201, "03022800c80003006400500007"),
     (0x0202, "000000000000000028000c001900"),
     (0x0204, "0000000000000080")),
    ((0x0001, "0100000807060504030201"),
     (0x0201, "09025500c80003006400500007"),
     (0x0202, "0000000000000000280007001200"),
     (0x0204, "0000000000000080")),
)
REFEREE_COMPLETED_WIRES = tuple(bytes.fromhex(value) for value in (
    "a50b00fe690100010000080706050403020138e1"
    "a50d00ffe6010203022800c80003006400500007140c"
    "a50e0000370202000000000000000028000c0019009346"
    "a5080001b8040200000000000000806ba9",
    "a50b000a7c01000100000807060504030201ffd9"
    "a50d000bf3010209025500c80003006400500007ebf9"
    "a50e000c94020200000000000000002800070012001500"
    "a508000d1b04020000000000000080d328",
))
# Scoped live Hit10/Shot2,3 follows the static replacement/hit/shot at .5s:
# HP30, heat17=10-3+5+2=14, heat42=20-2+7+3=28. CRC independently
# calculated with pinned crccheck from these official packed payload literals.
REFEREE_QUEUED_LEFT_PAYLOADS = (
    (0x0001, "0100000807060504030201"),
    (0x0201, "03021e00c80003006400500007"),
    (0x0202, "000000000000000028000e001c00"),
    (0x0204, "0000000000000080"),
)
REFEREE_QUEUED_LEFT_WIRE = bytes.fromhex(
    "a50b00fe690100010000080706050403020138e1"
    "a50d00ffe6010203021e00c80003006400500007ac6a"
    "a50e0000370202000000000000000028000e001c005d01"
    "a5080001b8040200000000000000806ba9")


class UartBudget:
    """Real fd/IO; isolate only a write budget, a failure or a late return."""

    def __init__(self, sock):
        self.sock = sock
        self.budget = None
        self.error = None
        self.on_send = None
        self.sends = self.reads = self.closes = 0

    def fileno(self):
        return self.sock.fileno()

    def gettimeout(self):
        return self.sock.gettimeout()

    def send(self, data):
        self.sends += 1
        if self.error is not None:
            raise self.error
        if self.on_send is not None:
            self.on_send()
        if self.budget == 0:
            raise BlockingIOError
        count = len(data) if self.budget is None else min(self.budget, len(data))
        written = self.sock.send(data[:count])
        if self.budget is not None:
            self.budget -= written
        return written

    def recv(self, size):
        self.reads += 1
        return self.sock.recv(size)

    def close(self):
        self.closes += 1
        self.sock.close()


def uart_received(peer):
    result = bytearray()
    while True:
        try:
            part = peer.recv(4096)
        except BlockingIOError:
            return bytes(result)
        assert part, "unexpected referee UART EOF"
        result.extend(part)


def referee_frames(wire):
    """Use official envelope length and existing validated pure decoder."""
    result = []
    while wire:
        assert wire[0] == 0xa5
        size = int.from_bytes(wire[1:3], "little") + 9
        assert len(wire) >= size
        result.append(decode_frame(wire[:size]))
        wire = wire[size:]
    return result


@pytest.fixture
def referee_resources(world_resources):
    *_, stack = world_resources
    initial_right = replace(REFEREE_INITIAL,
                            robot=replace(REFEREE_INITIAL.robot, robot_id=9))
    replacement = replace(REFEREE_INITIAL.robot, current_hp=50)
    engines = (
        RefereeEngine(REFEREE_INITIAL, durations=MatchDurations(3, 4),
                      cooling_42mm_per_s=2,
                      events=(ScriptEvent(500_000_000, Hit(30)),
                              ScriptEvent(500_000_000, replacement),
                              ScriptEvent(500_000_000, Hit(10)),
                              ScriptEvent(500_000_000, Shot(5, 7)))),
        RefereeEngine(initial_right, durations=MatchDurations(3, 4),
                      cooling_42mm_per_s=2,
                      events=(ScriptEvent(500_000_000, Hit(15)),)),
    )
    pairs, referees = [], []
    for name, engine, sequence in zip(("left", "right"), engines, (254, 10)):
        sender, peer = socket.socketpair()
        for sock in (sender, peer):
            sock.setblocking(False)
            stack.callback(sock.close)
        budget = UartBudget(sender)
        endpoint = RefereeUartEndpoint(StreamTransport(budget, max_pending=1),
                                       sequence=sequence)
        stack.callback(lambda endpoint=endpoint: None if endpoint.closed else endpoint.close())
        stack.callback(lambda engine=engine: None if engine.closed else engine.close())
        pairs.append((budget, peer))
        referees.append(RefereeBinding(name, engine, endpoint))
    yield world_resources, tuple(referees), tuple(pairs)


@pytest.fixture
def referee_setup(referee_resources):
    resources, referees, pairs = referee_resources
    plant, boards, native, bindings, peers, observations, stack = resources
    runtime = SharedWorldRuntime(
        plant, bindings, referees=referees, check_running=boards,
        on_response=lambda *response: observations.append(response))
    stack.callback(runtime.close)
    yield runtime, boards, native, bindings, peers, observations, referees, pairs


def test_can_delay_scoped_admission_and_refusal_delegate_once_without_io(referee_setup, monkeypatch):
    runtime, boards, native, bindings, peers, _, referees, pairs = referee_setup
    start(runtime)
    runtime.set_can_delay("left", 20)
    peers[0].send(can_wire(1000, "1000 0000 0000 0000"))
    runtime.service(deadline=time.monotonic() + 1)
    pairs[0][0].budget = 0
    queued = runtime.publish_referee("left", deadline=time.monotonic() + 1)
    states = tuple(item.engine.get_state() for item in referees)
    world = (runtime.t_sim_ns, runtime.step_id, native.resets,
             list(native.steps), list(native.reads), list(native.efforts))
    reads = [list(item.endpoint.stream.reads) for item in bindings]
    frames = [list(items) for items in boards.frames]
    uart_counts = [(sock.sends, sock.reads, sock.closes) for sock, _ in pairs]
    original = CanBus.delay_ns
    calls = []

    def setter(bus, value):
        calls.append((bus, value))
        original.fset(bus, value)

    def forbidden(*args, **options):
        pytest.fail("pure CAN delay admission observed or serviced a runtime resource")

    class Alias(str):
        pass

    with monkeypatch.context() as admission:
        admission.setattr(CanBus, "delay_ns", property(original.fget, setter))
        admission.setattr(runtime, "_check_running", forbidden)
        admission.setattr(time, "monotonic", forbidden)
        for owner, methods in (
                (native, ("reset", "step", "set_joint_effort", "get_joint_state", "get_imu",
                          "validate_step", "validate_steps", "validate_joint_effort")),
                (CanBus, ("send", "step", "reset")),
                (QemuCanEndpoint, ("ingest", "receive", "bind_mapper")),
                (RefereeEngine, ("reset", "step", "get_state", "queue_events")),
                (RefereeUartEndpoint, ("reset", "send_state", "flush")),
                (CanStream, ("receive", "send", "flush")),
                (StepFramedSocket, ("receive", "send", "flush"))):
            for method in methods:
                admission.setattr(owner, method, forbidden)
        assert runtime.set_can_delay("left", 30) is None
        assert calls == [(bindings[0].endpoint.bus, 30)]
        for name, value, error, delegates in (
                ("missing", 7, ValueError, False), (None, 7, TypeError, False),
                (Alias("left"), 7, TypeError, False),
                ("left", True, ValueError, True), ("left", 1.5, ValueError, True),
                ("left", -1, ValueError, True), ("left", 1 << 64, ValueError, True)):
            count = len(calls)
            with pytest.raises(error):
                runtime.set_can_delay(name, value)
            assert len(calls) == count + int(delegates)
            if delegates:
                assert calls[-1] == (bindings[0].endpoint.bus, value)
            assert [item.endpoint.bus.delay_ns for item in bindings] == [30, 0]
            assert [item.endpoint.bus.pending for item in bindings] == [1, 0]
            assert not runtime.failed and not runtime.closed
        assert runtime.set_can_delay("left", 0) is None
        assert calls[-1] == (bindings[0].endpoint.bus, 0)
        assert len(calls) == 6  # Two successes and the four value refusals.
    assert (runtime.t_sim_ns, runtime.step_id, native.resets,
            native.steps, native.reads, native.efforts) == world
    assert [item.endpoint.bus.t_sim_ns for item in bindings] == [500, 500]
    assert [item.endpoint.stream.reads for item in bindings] == reads
    assert boards.frames == frames
    assert all(item.engine.get_state() is state for item, state in zip(referees, states))
    assert [item.endpoint.next_sequence for item in referees] == [2, 10]
    assert [(sock.sends, sock.reads, sock.closes) for sock, _ in pairs] == uart_counts
    assert [bytes(view) for view in referees[0].endpoint.transport.tx_queue] == [queued]
    assert all(uart_received(peer) == b"" for _, peer in pairs)
    # A valid subsequent operation still settles the old command and uses it
    # only for the next interval; refusal did not freeze or discard admission.
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert native.steps == [20, 999_980, 1_000_000]
    assert native.efforts == [("j1", 0), ("j2", 0)] + [("j1", .2), ("j2", 0)] * 2
    assert not runtime.failed and runtime.step_id == 3


def test_can_delay_active_uart_callback_reentry_refuses_other_bus_before_delegation(
        referee_setup, monkeypatch):
    runtime, _, native, bindings, _, _, _, pairs = referee_setup
    start(runtime)
    calls = []

    def forbidden(bus, value):
        pytest.fail("active callback invoked lower CAN delay setter")

    def reenter():
        with pytest.raises(RuntimeError, match="fresh runtime"):
            runtime.set_can_delay("right", 1)
        calls.append("refused")

    monkeypatch.setattr(CanBus, "delay_ns", property(CanBus.delay_ns.fget, forbidden))
    pairs[0][0].on_send = reenter
    assert runtime.publish_referee("left", deadline=time.monotonic() + 1) == REFEREE_INITIAL_WIRE
    assert uart_received(pairs[0][1]) == REFEREE_INITIAL_WIRE and calls == ["refused"]
    assert [item.endpoint.bus.delay_ns for item in bindings] == [0, 0]
    assert not runtime.failed and native.steps == [] and runtime.step_id == 0


@pytest.mark.parametrize("source", ["script", "scoped_queue", "scenario"])
def test_referee_shared_session_context_and_literal_scoped_uart(referee_setup, monkeypatch, tmp_path, source):
    runtime, boards, native, _, _, _, referees, pairs = referee_setup
    queued = source != "script"
    assert runtime.referees is referees
    sessions, contexts, reset_observations, endpoint_resets, admissions = [], [], [], [], []
    for item in referees:
        original_reset, original_step = item.engine.reset, item.engine.step
        original_endpoint_reset = item.endpoint.reset
        original_queue = item.engine.queue_events

        def reset(session, original=original_reset):
            sessions.append(session)
            reset_observations.append((tuple(len(frames) for frames in boards.frames),
                                       native.resets))
            return original(session)

        def step(context, original=original_step):
            contexts.append(context)
            return original(context)

        def endpoint_reset(sequence, original=original_endpoint_reset, name=item.name):
            endpoint_resets.append((name, sequence, len(sessions), native.resets))
            return original(sequence)

        def queue(events, *, session_id, original=original_queue, name=item.name):
            admissions.append((name, events, session_id))
            return original(events, session_id=session_id)

        monkeypatch.setattr(item.engine, "reset", reset)
        monkeypatch.setattr(item.engine, "step", step)
        monkeypatch.setattr(item.endpoint, "reset", endpoint_reset)
        monkeypatch.setattr(item.engine, "queue_events", queue)
        with pytest.raises(RuntimeError):
            item.engine.get_state()  # Admission neither queries nor resets it.
        with pytest.raises(AttributeError):
            item.engine.closed = True
        with pytest.raises(AttributeError):
            item.endpoint.transport = object()
        with pytest.raises(AttributeError):
            item.endpoint.closed = True
    with pytest.raises(FrozenInstanceError):
        referees[0].name = "changed"
    with pytest.raises(RuntimeError):
        runtime.publish_referee("left", deadline=time.monotonic() + 1)
    session = Session(7, 500)
    runtime.start(session, (71, 72), deadline=time.monotonic() + 1)
    assert sessions == [session, session] and sessions[0] is sessions[1] is session
    assert reset_observations == [((1, 1), 1), ((1, 1), 1)]
    assert endpoint_resets == [("left", 254, 2, 1), ("right", 10, 2, 1)]
    assert [item.endpoint.next_sequence for item in referees] == [254, 10]
    assert all(sock.sends == 0 and uart_received(peer) == b"" for sock, peer in pairs)
    if queued:
        events = (ScriptEvent(500_000_000, Hit(10)), ScriptEvent(500_000_000, Shot(2, 3)))
        if source == "scenario":
            from sim.scenario.manifest import load_manifest
            from test_arena_manifest import referee_configuration, write_arena

            write_arena(tmp_path / "inputs", members={
                "right": {"manifest": "robots/bravo/robot.yaml", "prefix": "bravo/",
                          "physics": "shared-world"},
                "left": {"manifest": "robots/alpha/robot.yaml", "prefix": "alpha/",
                         "physics": "shared-world"}},
                referees={name: referee_configuration() for name in ("right", "left")})
            path = tmp_path / "scenario.yaml"
            path.write_text("""schema: 1
arena: inputs/arena.yaml
duration: 1s
referee_events:
  left:
    - {at: 500ms, hit: {damage_hp: 10}}
    - {at: 500ms, shot: {heat_17mm: 2, heat_42mm: 3}}
""", encoding="utf-8")
            scenario = load_manifest(path)
            assert scenario.duration_ns == 1_000_000_000
            assert tuple(item.member for item in scenario.arena.referees) == ("left", "right")
            assert tuple(item.member for item in scenario.referees) == ("left",)
            assert scenario.referees[0].events == events
            events = scenario.referees[0].events
        before = tuple(item.engine.get_state() for item in referees)
        before_reads = list(native.reads)
        with monkeypatch.context() as admission:
            admission.setattr(runtime, "_check_running", lambda: pytest.fail("queue admission called liveness"))
            assert runtime.queue_referee_events("left", events, session_id=7) is None
        assert admissions == [("left", events, 7)] and admissions[0][1] is events
        assert all(item.engine.get_state() is state for item, state in zip(referees, before))
        assert native.reads == before_reads and native.steps == []
        assert runtime.t_sim_ns == 500 and runtime.step_id == 0 and not runtime.failed
        assert [item.endpoint.next_sequence for item in referees] == [254, 10]
        assert all(sock.sends == sock.reads == 0 and uart_received(peer) == b"" for sock, peer in pairs)
    else:
        assert admissions == []
    runtime.step(1_000_000_000, deadline=time.monotonic() + 1)
    assert contexts == [StepContext(7, 1, 1_000_000_500, 1_000_000_000)] * 2
    assert contexts[0] is contexts[1]
    assert all(item.engine.t_sim_ns == runtime.t_sim_ns for item in referees)
    assert [item.engine.get_state().robot.current_hp for item in referees] == [30 if queued else 40, 85]
    assert all(sock.sends == 0 and uart_received(peer) == b"" for sock, peer in pairs)
    completed = tuple(item.engine.get_state() for item in referees)
    payloads = (REFEREE_QUEUED_LEFT_PAYLOADS if queued else REFEREE_COMPLETED_PAYLOADS[0],
                REFEREE_COMPLETED_PAYLOADS[1])
    wires = (REFEREE_QUEUED_LEFT_WIRE if queued else REFEREE_COMPLETED_WIRES[0],
             REFEREE_COMPLETED_WIRES[1])
    for item, (sock, peer), sequence, literals, literal_wire in zip(
            referees, pairs, (254, 10), payloads, wires):
        item.endpoint.transport.buffer.extend(b"read tail")
        peer.send(b"unread input")
        emitted = runtime.publish_referee(item.name, deadline=time.monotonic() + 1)
        actual = uart_received(peer)
        assert actual == emitted == literal_wire and len(actual) == 82
        decoded = referee_frames(actual)
        assert [frame.sequence for frame in decoded] == [(sequence + i) % 256 for i in range(4)]
        assert [(frame.command_id, frame.payload.hex()) for frame in decoded] == list(literals)
        assert sock.reads == 0 and item.endpoint.transport.buffer == b"read tail"
    assert native.steps == [1_000_000_000] and runtime.step_id == 1
    assert all(item.engine.get_state() is state for item, state in zip(referees, completed))
    runtime.close()
    runtime.close()
    assert native.closed == 1 and all(item.engine.closed and item.endpoint.closed for item in referees)
    for item, (sock, peer) in zip(referees, pairs):
        assert sock.closes == sock.reads == 0 and sock.fileno() >= 0
        item.endpoint.transport.read_once()  # Caller retains the RX/lifetime.
        assert item.endpoint.transport.buffer == b"read tailunread input"
        item.endpoint.transport.send(b"caller")
        assert uart_received(peer) == b"caller"


@pytest.mark.parametrize("invalid", [
    "tuple", "binding_type", "name", "duplicate_name", "engine_type", "endpoint_type",
    "engine", "endpoint", "transport", "socket", "board_socket",
    "closed_engine", "closed_endpoint", "closed_socket", "blocking_socket",
])
def test_referee_admission_is_complete_before_observation_reset_ownership_or_io(
        referee_resources, monkeypatch, invalid):
    resources, referees, pairs = referee_resources
    plant, boards, native, bindings, _, _, _ = resources
    first, second = referees
    if invalid == "tuple":
        referees = list(referees)
    elif invalid == "binding_type":
        referees = (first, object())
    elif invalid in ("name", "duplicate_name"):
        referees = (first, replace(second, name="\0" if invalid == "name" else first.name))
    elif invalid in ("engine_type", "endpoint_type"):
        referees = (first, replace(second, **{invalid.removesuffix("_type"): object()}))
    elif invalid in ("engine", "endpoint"):
        referees = (first, replace(second, **{invalid: getattr(first, invalid)}))
    elif invalid in ("transport", "socket", "board_socket"):
        transport = (first.endpoint.transport if invalid == "transport" else StreamTransport(
            first.endpoint.transport.sock if invalid == "socket" else bindings[1].cosim.sock))
        referees = (first, replace(second, endpoint=RefereeUartEndpoint(transport)))
    elif invalid == "closed_engine":
        second.engine.close()
    elif invalid == "closed_endpoint":
        second.endpoint.close()
    elif invalid == "closed_socket":
        pairs[1][0].sock.close()
    else:
        pairs[1][0].sock.setblocking(True)

    def forbidden(*args, **kwargs):
        pytest.fail("referee admission observed or modified a runtime resource")

    for cls, methods in ((RefereeEngine, ("get_state", "reset", "step")),
                         (RefereeUartEndpoint, ("reset", "send_state", "flush"))):
        for method in methods:
            monkeypatch.setattr(cls, method, forbidden)
    with pytest.raises((TypeError, ValueError)):
        SharedWorldRuntime(plant, bindings, referees=referees, check_running=boards)
    assert not plant.closed and native.resets == native.closed == 0
    assert native.steps == native.reads == [] and boards.frames == [[], []]
    assert all(item.endpoint.stream.reads == [] and not item.cosim.tx_pending for item in bindings)
    assert all(sock.sends == sock.reads == sock.closes == 0 for sock, _ in pairs)


def test_referee_start_keeps_queued_partial_bytes_and_active_service_drains_without_tick(referee_setup):
    runtime, _, native, _, _, _, referees, pairs = referee_setup
    item, (sock, peer) = referees[0], pairs[0]
    sock.budget = 19
    assert item.endpoint.send_state(REFEREE_INITIAL) == REFEREE_INITIAL_WIRE
    assert uart_received(peer) == REFEREE_INITIAL_WIRE[:19]
    suffix = REFEREE_INITIAL_WIRE[19:]
    assert [bytes(view) for view in item.endpoint.transport.tx_queue] == [suffix]
    sends = sock.sends
    sock.budget = None  # Startup must not flush even when physically writable.
    start(runtime)
    assert sock.sends == sends and uart_received(peer) == b""
    assert item.endpoint.next_sequence == 2
    assert [bytes(view) for view in item.endpoint.transport.tx_queue] == [suffix]
    completed = item.engine.get_state()
    runtime.queue_referee_events("left", (ScriptEvent(0, Hit(10)),), session_id=7)
    assert item.engine.get_state() is completed
    assert sock.sends == sends and uart_received(peer) == b""
    assert item.endpoint.next_sequence == 2
    assert [bytes(view) for view in item.endpoint.transport.tx_queue] == [suffix]
    runtime.service(deadline=time.monotonic() + 1)
    assert uart_received(peer) == suffix and not item.endpoint.transport.tx_pending
    assert item.endpoint.next_sequence == 2 and sock.reads == 0
    assert native.resets == 1 and native.steps == []
    assert runtime.t_sim_ns == item.engine.t_sim_ns == 500 and runtime.step_id == 0


def test_referee_capacity_refusal_keeps_sequence_and_exact_suffix_retry_without_restep(referee_setup):
    runtime, _, native, _, _, _, referees, pairs = referee_setup
    start(runtime)
    item, (sock, peer) = referees[0], pairs[0]
    sock.budget = 19
    first = runtime.publish_referee("left", deadline=time.monotonic() + 1)
    assert first == REFEREE_INITIAL_WIRE and uart_received(peer) == first[:19]
    sock.budget = 5
    with pytest.raises(BufferError):
        runtime.publish_referee("left", deadline=time.monotonic() + 1)
    assert not runtime.failed and item.endpoint.next_sequence == 2
    assert uart_received(peer) == first[19:24]
    assert [bytes(view) for view in item.endpoint.transport.tx_queue] == [first[24:]]
    sock.budget = None
    runtime.service(deadline=time.monotonic() + 1)
    assert uart_received(peer) == first[24:]
    second = runtime.publish_referee("left", deadline=time.monotonic() + 1)
    assert uart_received(peer) == second
    assert [frame.sequence for frame in referee_frames(second)] == [2, 3, 4, 5]
    assert [(frame.command_id, frame.payload.hex()) for frame in referee_frames(second)] == [
        (0x0001, "0100000807060504030201"),
        (0x0201, "03026400c80003006400500007"),
        (0x0202, "000000000000000028000a001400"),
        (0x0204, "0000000000000080")]
    assert native.steps == [] and runtime.step_id == 0 and item.engine.t_sim_ns == 500
    assert pairs[1][0].sends == 0 and sock.reads == 0


@pytest.mark.parametrize("name", ["missing", None])
def test_referee_publish_bad_name_freezes_before_uart_or_world_effect(referee_setup, name):
    runtime, _, native, _, _, _, _, pairs = referee_setup
    start(runtime)
    with pytest.raises((TypeError, ValueError)):
        runtime.publish_referee(name, deadline=time.monotonic() + 1)
    assert runtime.failed and native.steps == []
    assert all(sock.sends == sock.reads == 0 for sock, _ in pairs)
    with pytest.raises(RuntimeError):
        runtime.publish_referee("left", deadline=time.monotonic() + 1)


@pytest.mark.parametrize("invalid", ["unknown", "name_type", "name_subclass", "wire_session",
                                    "session_type", "past", "malformed_batch"])
def test_scoped_queue_refusals_preserve_pending_state_and_leave_runtime_usable(
        referee_setup, monkeypatch, invalid):
    runtime, _, native, bindings, _, _, referees, pairs = referee_setup
    start(runtime)
    runtime.step(100_000_000, deadline=time.monotonic() + 1)
    runtime.queue_referee_events("left", (ScriptEvent(200_000_000, Hit(10)),), session_id=7)
    calls = []
    for item in referees:
        original = item.engine.queue_events
        def queue(events, *, session_id, original=original, name=item.name):
            calls.append((name, events, session_id))
            return original(events, session_id=session_id)
        monkeypatch.setattr(item.engine, "queue_events", queue)
    before = tuple(item.engine.get_state() for item in referees)
    world = (runtime.t_sim_ns, runtime.step_id, list(native.steps), list(native.reads))
    reads = [list(item.endpoint.stream.reads) for item in bindings]
    name, session_id = "left", 7
    events = (ScriptEvent(200_000_000, Hit(30)),)
    if invalid == "unknown":
        name = "missing"
    elif invalid == "name_type":
        name = None
    elif invalid == "name_subclass":
        class Alias(str):
            pass
        name = Alias("left")
    elif invalid == "wire_session":
        session_id = 71  # The host session is7; this is a board wire-session ID.
    elif invalid == "session_type":
        session_id = 7.0
    elif invalid == "past":
        events = (ScriptEvent(99_000_000, Hit(30)),)
    else:
        events = (ScriptEvent(200_000_000, Hit(30)), ScriptEvent(200_000_000, Hit(True)))
    with monkeypatch.context() as admission:
        admission.setattr(runtime, "_check_running", lambda: pytest.fail("queue refusal called liveness"))
        with pytest.raises((TypeError, ValueError)):
            runtime.queue_referee_events(name, events, session_id=session_id)
    assert calls == ([] if invalid.startswith("name_") or invalid == "unknown"
                     else [("left", events, session_id)])
    if calls:
        assert calls[0][1] is events
    assert not runtime.failed and not runtime.closed
    assert all(item.engine.get_state() is state for item, state in zip(referees, before))
    assert (runtime.t_sim_ns, runtime.step_id, native.steps, native.reads) == world
    assert [item.endpoint.stream.reads for item in bindings] == reads
    assert [item.endpoint.next_sequence for item in referees] == [254, 10]
    assert all(sock.sends == sock.reads == 0 and uart_received(peer) == b"" for sock, peer in pairs)
    runtime.queue_referee_events("left", (ScriptEvent(200_000_000, Hit(5)),), session_id=7)
    runtime.step(100_000_000, deadline=time.monotonic() + 1)
    assert [item.engine.get_state().robot.current_hp for item in referees] == [85, 100]
    assert native.steps == [100_000_000, 100_000_000] and runtime.step_id == 2


def test_scoped_queue_capacity_refusal_and_completed_step_release_use_lower_engine(referee_setup):
    runtime, _, native, _, _, _, referees, pairs = referee_setup
    start(runtime)
    runtime.queue_referee_events("left", (ScriptEvent(100_000_000, Hit(0)),) * 1023 +
                                (ScriptEvent(200_000_000, Hit(10)),), session_id=7)
    before = tuple(item.engine.get_state() for item in referees)
    with pytest.raises(BufferError):
        runtime.queue_referee_events("left", (ScriptEvent(200_000_000, Hit(30)),), session_id=7)
    assert not runtime.failed and runtime.t_sim_ns == 500 and runtime.step_id == 0
    assert all(item.engine.get_state() is state for item, state in zip(referees, before))
    assert native.steps == [] and [item.endpoint.next_sequence for item in referees] == [254, 10]
    assert all(sock.sends == sock.reads == 0 and uart_received(peer) == b"" for sock, peer in pairs)
    runtime.step(100_000_000, deadline=time.monotonic() + 1)
    runtime.queue_referee_events("left", (ScriptEvent(200_000_000, Hit(0)),) * 1023, session_id=7)
    runtime.step(100_000_000, deadline=time.monotonic() + 1)
    assert [item.engine.get_state().robot.current_hp for item in referees] == [90, 100]
    assert native.steps == [100_000_000, 100_000_000] and not runtime.failed


@pytest.mark.parametrize("state", ["prestart", "closed"])
def test_scoped_queue_lifecycle_guard_rejects_before_lower_delegation(referee_setup, monkeypatch, state):
    runtime, _, native, _, _, _, referees, pairs = referee_setup
    if state == "closed":
        start(runtime)
        runtime.close()
    def forbidden(*args, **options):
        pytest.fail("lifecycle refusal invoked lower queue")
    for item in referees:
        monkeypatch.setattr(item.engine, "queue_events", forbidden)
    monkeypatch.setattr(CanBus, "delay_ns", property(CanBus.delay_ns.fget, forbidden))
    with pytest.raises(RuntimeError):
        runtime.queue_referee_events("left", (), session_id=7)
    with pytest.raises(RuntimeError):
        runtime.set_can_delay("left", 1)
    assert native.steps == [] and runtime.step_id == 0
    assert all(sock.sends == sock.reads == 0 for sock, _ in pairs)


@pytest.mark.parametrize("capacity", [False, True])
def test_referee_post_attempt_deadline_freezes_even_after_retryable_capacity(
        referee_setup, monkeypatch, capacity):
    runtime, _, native, _, _, _, referees, pairs = referee_setup
    start(runtime)
    item, (sock, peer) = referees[0], pairs[0]
    if capacity:
        sock.budget = 0
        runtime.publish_referee("left", deadline=time.monotonic() + 1)
    now = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    sock.on_send = lambda: now.__setitem__(0, now[0] + 10)
    with pytest.raises(RuntimeError, match="host deadline"):
        runtime.publish_referee("left", deadline=now[0] + 1)
    assert runtime.failed and native.steps == []
    assert item.endpoint.next_sequence == 2
    if capacity:
        assert uart_received(peer) == b""
        assert [bytes(view) for view in item.endpoint.transport.tx_queue] == [REFEREE_INITIAL_WIRE]
    else:
        assert uart_received(peer) == REFEREE_INITIAL_WIRE
    sends = sock.sends
    with pytest.raises(RuntimeError):
        runtime.publish_referee("left", deadline=now[0] + 1)
    assert sock.sends == sends


@pytest.mark.parametrize("source", ["liveness", "state"])
def test_referee_non_send_buffererror_is_terminal_not_snapshot_capacity(
        referee_setup, monkeypatch, source):
    runtime, boards, native, _, _, _, referees, pairs = referee_setup
    start(runtime)
    primary = BufferError(f"{source} failed")
    if source == "liveness":
        boards.failure = primary
    else:
        def failed_state():
            raise primary
        monkeypatch.setattr(referees[0].engine, "get_state", failed_state)
    with pytest.raises(BufferError) as caught:
        runtime.publish_referee("left", deadline=time.monotonic() + 1)
    assert caught.value is primary and runtime.failed
    assert native.steps == [] and all(sock.sends == 0 for sock, _ in pairs)


def test_later_referee_overflow_retains_first_participant_effects_without_board_step(
        referee_resources):
    resources, referees, pairs = referee_resources
    plant, boards, native, bindings, _, _, stack = resources
    initial = replace(REFEREE_INITIAL, robot=replace(REFEREE_INITIAL.robot, cooling_per_s=0),
                      power_heat=PowerHeat(40, 65535, 0))
    overflow = RefereeEngine(initial, durations=MatchDurations(3, 4),
                            events=(ScriptEvent(500_000_000, Shot(1, 0)),))
    stack.callback(lambda: None if overflow.closed else overflow.close())
    referees = (referees[0], replace(referees[1], engine=overflow))
    runtime = SharedWorldRuntime(plant, bindings, referees=referees, check_running=boards)
    stack.callback(runtime.close)
    start(runtime)
    before = overflow.get_state()
    with pytest.raises(OverflowError) as caught:
        runtime.step(1_000_000_000, deadline=time.monotonic() + 1)
    assert "simulation failed during component step, step 1; completed time remains 500" in caught.value.__notes__
    assert runtime.failed and native.steps == [1_000_000_000]
    assert runtime.step_id == 0 and runtime.t_sim_ns == 500
    assert referees[0].engine.t_sim_ns == 1_000_000_500
    assert referees[0].engine.get_state().robot.current_hp == 40
    assert overflow.get_state() is before and overflow.t_sim_ns == 500
    assert all([frame.header.kind for frame in frames] == [1] for frames in boards.frames)
    assert all(sock.sends == 0 for sock, _ in pairs)
    for action in (lambda: runtime.step(1, deadline=time.monotonic() + 1),
                   lambda: runtime.publish_referee("left", deadline=time.monotonic() + 1),
                   lambda: runtime.queue_referee_events("left", (), session_id=7)):
        with pytest.raises(RuntimeError):
            action()
    assert native.steps == [1_000_000_000]


@pytest.mark.parametrize("operation", ["send", "flush"])
def test_later_referee_uart_failure_preserves_primary_suffix_and_freezes_all(
        referee_setup, operation):
    runtime, _, native, _, _, _, referees, pairs = referee_setup
    start(runtime)
    first = runtime.publish_referee("left", deadline=time.monotonic() + 1)
    assert uart_received(pairs[0][1]) == first
    item, (sock, peer) = referees[1], pairs[1]
    expected_sequence = 10
    if operation == "flush":
        sock.budget = 19
        second = runtime.publish_referee("right", deadline=time.monotonic() + 1)
        assert uart_received(peer) == second[:19]
        suffix, expected_sequence = second[19:], 14
    primary = OSError("second UART failed")
    sock.error = primary
    with pytest.raises(OSError) as caught:
        if operation == "send":
            runtime.publish_referee("right", deadline=time.monotonic() + 1)
        else:
            runtime.service(deadline=time.monotonic() + 1)
    assert caught.value is primary and runtime.failed and native.steps == []
    assert item.endpoint.next_sequence == expected_sequence
    queued = [bytes(view) for view in item.endpoint.transport.tx_queue]
    assert len(queued) == 1
    if operation == "flush":
        assert queued == [suffix]
    else:
        assert [(frame.command_id, frame.payload.hex()) for frame in referee_frames(queued[0])] == [
            (0x0001, "0100000807060504030201"),
            (0x0201, "09026400c80003006400500007"),
            (0x0202, "000000000000000028000a001400"),
            (0x0204, "0000000000000080")]
    sends = [budget.sends for budget, _ in pairs]
    for action in (lambda: runtime.publish_referee("left", deadline=time.monotonic() + 1),
                   lambda: runtime.service(deadline=time.monotonic() + 1),
                   lambda: runtime.step(1, deadline=time.monotonic() + 1)):
        with pytest.raises(RuntimeError):
            action()
    runtime.close()
    assert [budget.sends for budget, _ in pairs] == sends
    assert [bytes(view) for view in item.endpoint.transport.tx_queue] == queued
    assert all(budget.closes == budget.reads == 0 for budget, _ in pairs)


@pytest.mark.parametrize("shared_error", [False, True])
def test_referee_close_reentry_and_errors_release_all_owners_once(
        referee_setup, monkeypatch, shared_error):
    runtime, _, native, _, _, _, referees, pairs = referee_setup
    start(runtime)
    pairs[0][0].budget = 0
    queued = runtime.publish_referee("left", deadline=time.monotonic() + 1)
    events = []
    primary = KeyboardInterrupt("right endpoint close")
    later = (OSError("left endpoint close"), OSError("right engine close"),
             OSError("left engine close"), OSError("native close"))
    later[3].add_note("physics borrowed-storage release failed")

    def forbidden_delay(bus, value):
        pytest.fail("close reentry invoked lower CAN delay setter")

    monkeypatch.setattr(CanBus, "delay_ns", property(CanBus.delay_ns.fget, forbidden_delay))
    for index, item in enumerate(referees):
        original_endpoint, original_engine = item.endpoint.close, item.engine.close

        def forbidden_queue(*args, **options):
            pytest.fail("close reentry invoked lower queue")
        monkeypatch.setattr(item.engine, "queue_events", forbidden_queue)

        def endpoint_close(index=index, original=original_endpoint):
            events.append(("endpoint", index))
            for action in (runtime.close,
                           lambda: runtime.publish_referee("left", deadline=time.monotonic() + 1),
                           lambda: runtime.service(deadline=time.monotonic() + 1),
                           lambda: runtime.queue_referee_events("left", (), session_id=7),
                           lambda: runtime.set_can_delay("right", 1)):
                with pytest.raises(RuntimeError):
                    action()
            original()
            raise primary if index == 1 or shared_error else later[0]

        def engine_close(index=index, original=original_engine):
            events.append(("engine", index))
            original()
            raise primary if shared_error and index == 1 else later[1 if index == 1 else 2]

        monkeypatch.setattr(item.endpoint, "close", endpoint_close)
        monkeypatch.setattr(item.engine, "close", engine_close)
    original_native = native.close

    def native_close():
        events.append(("native", 0))
        original_native()
        raise later[3]

    monkeypatch.setattr(native, "close", native_close)
    sends = [sock.sends for sock, _ in pairs]
    with pytest.raises(KeyboardInterrupt) as caught:
        runtime.close()
    assert caught.value is primary
    assert events == [("endpoint", 1), ("endpoint", 0), ("engine", 1), ("engine", 0), ("native", 0)]
    notes = "\n".join(caught.value.__notes__)
    assert all(str(error) in notes for error in (later[2:] if shared_error else later))
    assert "physics borrowed-storage release failed" in notes
    assert runtime.closed and runtime.failed and native.closed == 1
    assert all(item.endpoint.closed and item.engine.closed for item in referees)
    runtime.close()
    assert len(events) == 5 and [sock.sends for sock, _ in pairs] == sends
    assert [bytes(view) for view in referees[0].endpoint.transport.tx_queue] == [queued]
    assert all(sock.reads == sock.closes == 0 and sock.fileno() >= 0 for sock, _ in pairs)


class NativeCpuQmp:
    """Isolate native execution; all CAN bytes still cross the original socket.

    The receipt producer reads its real peer, retains every byte and increments
    only at literal 84-byte boundaries. It never consults an endpoint, stream
    queue, world algorithm or host submission counter.
    """

    def __init__(self, index, peer, origin, native, events):
        self.index, self.peer, self.current = index, peer, origin
        self.native, self.events = native, events
        self.target = None
        self.state = "idle"
        self.requests, self.receipts = [], []
        self.received = bytearray()
        self.partial = bytearray()
        self.completed = self.rx_dropped = self.tx_dropped = 0
        self.output = {}
        self.eof = False
        self.polls = 0
        self.fault = None
        self.read_limits = []
        self.report_completed = None
        self.on_rx = self.on_run = self.on_reached = None

    def _receive_input(self):
        # Read limits describe physical fragments, including initial paused
        # input. Empty native observations must not consume a fragment budget.
        try:
            pending = self.peer.recv(1, socket.MSG_PEEK)
        except BlockingIOError:
            return
        assert pending, "unexpected native CAN input EOF"
        limit = self.read_limits.pop(0) if self.read_limits else 4096
        if limit == 0:
            self.events.append(("receipt-blocked", self.index))
            return
        try:
            raw = self.peer.recv(limit)
        except BlockingIOError:
            return
        assert raw, "unexpected native CAN input EOF"
        self.received.extend(raw)
        self.partial.extend(raw)
        self.events.append(("receipt-bytes", self.index, bytes(raw)))
        while len(self.partial) >= 84:
            record = bytes(self.partial[:84])
            del self.partial[:84]
            self.receipts.append(record)
            self.completed += 1
            self.events.append(("receipt", self.index, record))

    def command(self, name, arguments=None):
        self.events.append(("qmp", self.index, name, arguments))
        if name == "x-run-to-virtual-time":
            assert set(arguments) == {"target-ns"}
            assert self.state in ("idle", "reached")
            self.target = arguments["target-ns"]
            assert type(self.target) is int and self.target > self.current
            if self.on_run is not None:
                self.on_run()
            self.requests.append(self.target)
            self.polls = 0
            self.state = "running"
            self.events.append(("run", self.index, self.target))
            return {}
        if name == "x-query-virtual-execution":
            self.events.append(("execution", self.index, self.state))
            if self.state == "running":
                self.polls += 1
                if self.fault is not None:
                    value = {"state": "running", "current-ns": self.current,
                             "target-ns": self.target}
                    value.update(self.fault)
                    return value
                if self.polls == 2:
                    self.current, self.state = self.target, "reached"
                    raw = self.output.get(len(self.requests), b"")
                    if raw:
                        assert self.peer.send(raw) == len(raw)
                        self.events.append(("output", self.index, raw))
                    if self.eof:
                        self.peer.shutdown(socket.SHUT_WR)
                    if self.on_reached is not None:
                        self.on_reached()
                    self.events.append(("reached", self.index, self.current))
            result = {"state": self.state, "current-ns": self.current}
            if self.target is not None:
                result["target-ns"] = self.target
            return result
        if name == "query-status":
            return {"running": self.state == "running", "status": (
                "running" if self.state == "running" else
                "prelaunch" if self.state == "idle" else "paused")}
        assert name == "qom-get"
        if arguments == {"path": "/chardevs/can1", "property": "connected"}:
            return True
        assert arguments["path"] == "/machine"
        if arguments["property"] == "fdcan-tx-state":
            controller = {"pending_frames": 0, "pending_bytes": 0,
                          "tx_dropped": self.tx_dropped, "tx_short_writes": 0}
        else:
            assert arguments["property"] == "fdcan-rx-state"
            self._receive_input()
            if self.on_rx is not None:
                self.on_rx()
            completed = (self.completed if self.report_completed is None else
                         self.report_completed(self.completed))
            controller = {"completed_frames": completed,
                          "partial_bytes": len(self.partial), "rx_dropped": self.rx_dropped}
        return {"schema": 1, "timestamp_ns": self.current,
                "controllers": {"dm-mc02.fdcan1": controller}}


class NativeCpuBatchQmp(NativeCpuQmp):
    """Independent batch values; scalar calls only execute/poll the CPU.

    Reuse the real peer receipt producer, never scalar command dispatch, for
    each fresh native observation. These literal groups are the public wire
    requests expected by the board adapter, not a copy of its validation.
    """

    def __init__(self, *args):
        super().__init__(*args)
        self.groups = []
        self.after_batch = lambda direction, values: None

    def command(self, name, arguments=None):
        assert name != "qom-get", "paused CAN observations must use public batch"
        return super().command(name, arguments)

    def command_many(self, commands):
        assert type(commands) is tuple
        assert commands == (
            ("qom-get", {"path": "/machine", "property": "can-execution-state"}),)
        # Every aggregate observes the real peer. Labels only describe the
        # existing CPU/world stages; they cannot select data or receive work.
        self._receive_input()
        if self.on_rx is not None:
            self.on_rx()
        direction = ("initial" if self.target is None else
                     "output" if len(self.requests) > len(self.native.steps) else "input")
        execution = {"state": self.state, "current-ns": self.current}
        if self.target is not None:
            execution["target-ns"] = self.target
        count = self.completed if self.report_completed is None else self.report_completed(self.completed)
        values = ({"schema": 1, "execution": execution,
                   "status": {"running": False, "status": (
                       "prelaunch" if self.state == "idle" else "paused"), "singlestep": False},
                   "fdcan-transports": {"dm-mc02.fdcan1": {
                       "path": "/chardevs/can1", "connected": True}},
                   "fdcan-tx-state": {"schema": 1, "timestamp_ns": self.current, "controllers": {
                "dm-mc02.fdcan1": {"pending_frames": 0, "pending_bytes": 0,
                                  "tx_dropped": self.tx_dropped, "tx_short_writes": 0}}},
                   "fdcan-rx-state": {"schema": 1, "timestamp_ns": self.current, "controllers": {
                "dm-mc02.fdcan1": {"completed_frames": count, "partial_bytes": len(self.partial),
                                  "rx_dropped": self.rx_dropped}}}},)
        self.groups.append((direction, values))
        self.events.append(("batch", self.index, direction))
        self.after_batch(direction, values)
        return values


@pytest.fixture
def exact_world_resources():
    """Keep the legacy shared/referee fixture's types and exported tuple intact."""
    with ExitStack() as stack:
        native, events = NativeStub(), []
        buses = (CanBus(), CanBus())
        devices = tuple(M3508Device(bus, 1) for bus in buses)
        plant = M3508JointPlant(native, dict(zip(devices, ("j1", "j2"))), gear_ratio=2)
        stack.callback(lambda: None if plant.closed else plant.close())
        streams, peers, cosims, cosim_peers = [], [], [], []
        for _ in range(2):
            for cls, output, remote in ((CanStream, streams, peers),
                                        (StepFramedSocket, cosims, cosim_peers)):
                left, right = socket.socketpair()
                for sock in (left, right):
                    sock.setblocking(False)
                    stack.callback(sock.close)
                output.append(cls(left))
                remote.append(right)
        boards = Boards(cosim_peers, native)
        cpus = tuple(NativeCpuQmp(index, peer, origin, native, events)
                     for index, (peer, origin) in enumerate(zip(peers, boards.origins)))
        bindings = tuple(BoardBinding(
            name, QemuCanEndpoint(bus, stream), cosim, name,
            BoardExecution(cpu.command, "dm-mc02.fdcan1", "can1"))
            for name, bus, stream, cosim, cpu in zip(
                ("left", "right"), buses, streams, cosims, cpus))
        yield plant, boards, native, bindings, cpus, events, stack


@pytest.fixture
def exact_setup(exact_world_resources):
    plant, boards, native, bindings, cpus, events, stack = exact_world_resources
    def observe(name, frame, decoded):
        events.append(("response", name, frame.header.kind, frame.header.step_id))
    runtime = SharedWorldRuntime(plant, bindings, check_running=boards, on_response=observe)
    stack.callback(runtime.close)
    return runtime, boards, native, bindings, cpus, events


@pytest.fixture(params=(False, True), ids=("nonexact", "exact"))
def multi_cut_setup(request):
    resources = request.getfixturevalue(
        "exact_world_resources" if request.param else "world_resources")
    plant, boards, native, bindings, peers, events, stack = resources
    observations = []
    runtime = SharedWorldRuntime(
        plant, bindings, quantum_ns=400_000, check_running=boards,
        on_response=lambda *response: observations.append(response))
    stack.callback(runtime.close)
    return SimpleNamespace(runtime=runtime, boards=boards, native=native,
                           bindings=bindings, cpus=peers if request.param else (),
                           peers=() if request.param else peers,
                           observations=observations, events=events)


@pytest.mark.parametrize("quantum", [0, -1, True, 1.5, 1 << 64])
def test_quantum_admission_precedes_getters_ownership_and_io(world_resources, quantum):
    plant, boards, native, bindings, _, _, _ = world_resources
    with pytest.raises(ValueError):
        SharedWorldRuntime(plant, bindings, quantum_ns=quantum, check_running=boards)
    assert native.reads == native.steps == [] and native.resets == native.closed == 0
    assert not plant.closed and boards.frames == [[], []]


def test_known_quantum_count_exhaustion_rejects_before_io(world_resources):
    plant, boards, native, bindings, _, _, stack = world_resources
    runtime = SharedWorldRuntime(plant, bindings, quantum_ns=1, check_running=boards)
    stack.callback(runtime.close)
    start(runtime)
    reads = list(native.reads)
    with pytest.raises(ValueError, match="STEP sample ID"):
        runtime.step(1 << 32, deadline=time.monotonic() + 1)
    assert runtime.failed and runtime.t_sim_ns == 500 and runtime.step_id == 0
    assert native.steps == [] and native.reads == reads
    assert all(len(frames) == 1 for frames in boards.frames)


def test_each_internal_cut_exchanges_literal_imu_before_next_cpu(multi_cut_setup):
    fixture = multi_cut_setup
    runtime, boards, native = fixture.runtime, fixture.boards, fixture.native
    start(runtime)
    assert tuple(runtime._simulation.components) == ('boards', 'can_0', 'can_1', 'plant')
    assert runtime._simulation.quantum_ns == 400_000

    def previous_exchange_is_complete(cpu):
        previous = len(cpu.requests)
        if previous:
            cut = (400_500, 800_500, 1_000_500)[previous - 1]
            assert runtime.t_sim_ns == cut and runtime.step_id == previous
            acks = [(name, frame.header.step_id) for name, frame, _ in fixture.observations
                    if frame.header.kind == codec.STEP_ACK]
            assert acks[-2:] == [("left", previous), ("right", previous)]
            expected = int(previous == 3)
            assert all(cpu.completed == expected and not cpu.partial for cpu in fixture.cpus)
            assert all(state['completed_frames'] == expected
                       for state in runtime.can_input_states)

    for cpu in fixture.cpus:
        cpu.on_run = lambda cpu=cpu: previous_exchange_is_complete(cpu)
    responses = runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert native.steps == [400_000, 400_000, 200_000]
    assert runtime.t_sim_ns == 1_000_500 and runtime.step_id == 3
    assert all(response.header.step_id == 3 for response in responses)
    for index, (frames, origin) in enumerate(zip(boards.frames, boards.origins)):
        steps = frames[1:]
        assert [(frame.header.step_id, frame.header.t_sim_ns, frame.header.dt_ns,
                 frame.header.session_id) for frame in steps] == [
            (1, 400_500, 400_000, 71 + index),
            (2, 800_500, 400_000, 71 + index),
            (3, 1_000_500, 200_000, 71 + index)]
        for step, cut, frame in zip((1, 2, 3), (400_500, 800_500, 1_000_500), steps):
            # Independent .1rad/s -> degrees/s binary32 wire literal; no
            # production conversion or codec computes the expected payload.
            z = 5.729577951308232 * (1 if index == 0 else -1)
            assert frame.payload == struct.pack(
                '<12fQI', 0, 0, z, 0, 0, 0, 0, 0, 0, 0, 0, 0, cut, step)
        if fixture.cpus:
            cpu = fixture.cpus[index]
            assert cpu.requests == [origin + 400_000, origin + 800_000, origin + 1_000_000]
            assert cpu.receipts == [struct.pack(
                '<IIB3xQ64s', 0x201, 0, 8, origin + 1_000_000,
                bytes.fromhex('0000 0000 0000 1900').ljust(64, b'\0'))]

    # Re-settling a completed cut cannot send or sample a fourth transaction.
    before = [tuple(frames) for frames in boards.frames], list(native.reads), list(fixture.events)
    runtime._simulation.run_until(runtime.t_sim_ns, deadline_monotonic=time.monotonic() + 1)
    assert ([tuple(frames) for frames in boards.frames], native.reads, fixture.events) == before
    runtime.step(400_000, deadline=time.monotonic() + 1)
    assert native.steps == [400_000, 400_000, 200_000, 400_000]
    assert runtime.step_id == 4 and not runtime.failed


@pytest.mark.parametrize("failure", ["imu", "ack", "observer"])
def test_second_cut_failure_keeps_first_commit_and_never_replays(
        multi_cut_setup, monkeypatch, failure):
    fixture = multi_cut_setup
    runtime, boards, native = fixture.runtime, fixture.boards, fixture.native
    start(runtime)
    primary = OSError('second cut observation failed')
    if failure == 'imu':
        original = native.get_imu
        def invalid(name):
            sample = original(name)
            return ImuSample((0, 0, 1e100), (0, 0, 0)) if (
                name == 'right' and len(native.steps) == 2) else sample
        monkeypatch.setattr(native, 'get_imu', invalid)
    elif failure == 'ack':
        def reject(index, frame):
            if index == 1 and frame.header.kind == codec.STEP and frame.header.step_id == 2:
                boards.peers[index].send(wire(
                    codec.STEP_ACK, 2, frame.header.t_sim_ns, frame.header.dt_ns,
                    frame.header.session_id, struct.pack('<IIII', 2, 0, 0, 0)))
                return True
            return False
        boards.on_frame = reject
    else:
        original = runtime._on_response
        def observe(name, frame, decoded):
            original(name, frame, decoded)
            if name == 'left' and frame.header.kind == codec.STEP_ACK and frame.header.step_id == 2:
                raise primary
        monkeypatch.setattr(runtime, '_on_response', observe)
    with pytest.raises(ValueError if failure == 'imu' else RuntimeError if failure == 'ack'
                       else OSError) as caught:
        runtime.step(1_000_000, deadline=time.monotonic() + 1)
    if failure == 'observer':
        assert caught.value is primary
    assert runtime.failed and runtime._simulation.state == 'failed'
    assert runtime.t_sim_ns == 400_500 and runtime.step_id == 1
    assert native.steps == [400_000, 400_000]
    assert all(response.header.step_id == 1
               for response in runtime._simulation.components['boards'].responses)
    expected_ids = ([1], [1]) if failure == 'imu' else (
        ([1, 2], [1, 2]) if failure == 'ack' else ([1, 2], [1]))
    assert tuple([frame.header.step_id for frame in frames[1:]]
                 for frames in boards.frames) == expected_ids
    if fixture.cpus:
        assert all(len(cpu.requests) == 2 for cpu in fixture.cpus)
    before = [tuple(frames) for frames in boards.frames], list(native.steps)
    for operation in (lambda: runtime.step(1, deadline=time.monotonic() + 1),
                      lambda: runtime.service(deadline=time.monotonic() + 1)):
        with pytest.raises(RuntimeError, match='fresh runtime'):
            operation()
    assert ([tuple(frames) for frames in boards.frames], native.steps) == before
    # Logical cancellation never consumes or deletes borrowed physical tails.
    for binding in fixture.bindings:
        binding.cosim.buffer.extend(b'co-sim tail')
        binding.endpoint.stream.buffer.extend(b'CAN tail')
    runtime.close()
    runtime.close()
    assert native.closed == 1
    assert all(bytes(binding.cosim.buffer) == b'co-sim tail'
               and bytes(binding.endpoint.stream.buffer) == b'CAN tail'
               and binding.cosim.sock.fileno() >= 0
               and binding.endpoint.stream.sock.fileno() >= 0
               for binding in fixture.bindings)


def test_second_cut_changed_delay_is_admitted_before_world_or_cpu(multi_cut_setup):
    fixture = multi_cut_setup
    runtime, boards, native = fixture.runtime, fixture.boards, fixture.native
    start(runtime)
    original = runtime._on_response
    def change_delay(name, frame, decoded):
        original(name, frame, decoded)
        if name == 'right' and frame.header.kind == codec.STEP_ACK and frame.header.step_id == 1:
            fixture.bindings[1].endpoint.bus.delay_ns = (1 << 64) - 1
    runtime._on_response = change_delay
    with pytest.raises(ValueError):
        runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert runtime.failed and runtime.t_sim_ns == 400_500 and runtime.step_id == 1
    assert native.steps == [400_000] and all(len(frames) == 2 for frames in boards.frames)
    assert all(len(cpu.requests) == 1 for cpu in fixture.cpus)


@pytest.mark.parametrize('multi_cut_setup', [True], indirect=True)
def test_second_cut_native_headroom_refuses_before_cpu_release(multi_cut_setup):
    fixture = multi_cut_setup
    for device in fixture.runtime.plant.devices:
        device.feedback_period_ns = 400_000
    fixture.cpus[1].completed = (1 << 64) - 514
    start(fixture.runtime)
    with pytest.raises(ValueError, match='counter headroom'):
        fixture.runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert fixture.runtime.failed and fixture.runtime.t_sim_ns == 400_500
    assert fixture.runtime.step_id == 1 and fixture.native.steps == [400_000]
    assert all(len(cpu.requests) == len(cpu.receipts) == 1 for cpu in fixture.cpus)
    assert all(len(frames) == 2 for frames in fixture.boards.frames)


@pytest.mark.parametrize('multi_cut_setup', [True], indirect=True)
def test_second_cut_receipt_failure_retains_the_first_complete_native_group(multi_cut_setup):
    fixture = multi_cut_setup
    for device in fixture.runtime.plant.devices:
        device.feedback_period_ns = 400_000
    start(fixture.runtime)
    fixture.cpus[1].report_completed = lambda count: count + int(len(fixture.native.steps) == 2)
    with pytest.raises(RuntimeError, match='exceeds goal'):
        fixture.runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert fixture.runtime.failed and fixture.runtime.t_sim_ns == 400_500
    assert fixture.runtime.step_id == 1 and fixture.native.steps == [400_000, 400_000]
    assert all(len(cpu.requests) == len(cpu.receipts) == 2 for cpu in fixture.cpus)
    assert all(response.header.step_id == 1 for response in
               fixture.runtime._simulation.components['boards'].responses)
    assert fixture.runtime.can_input_states == (
        {'completed_frames': 1, 'partial_bytes': 0, 'rx_dropped': 0},) * 2
    assert_exact_frozen(fixture.runtime, fixture.native, fixture.cpus)


@pytest.mark.parametrize('multi_cut_setup', [False], indirect=True)
@pytest.mark.parametrize('stamp', [0, 1000])
def test_nonexact_ack_can_stays_physical_until_next_pre_world_service(multi_cut_setup, stamp):
    fixture = multi_cut_setup
    runtime, boards, native = fixture.runtime, fixture.boards, fixture.native
    start(runtime)
    delivered = []
    fixture.bindings[0].endpoint.bus.log = delivered.append
    runtime.set_can_delay('left', 100)
    def emit_late(index, frame):
        if index == 0 and frame.header.kind == codec.STEP and frame.header.step_id == 1:
            assert runtime.t_sim_ns == 500 and native.steps == [400_000]
            raw = can_wire(stamp, '1000 0000 0000 0000')
            assert fixture.peers[0].send(raw) == len(raw)
        return False
    boards.on_frame = emit_late
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    command = next(row for row in delivered if row.frame.can_id == 0x200)
    assert (command.timestamp_ns, command.due_ns, command.delivered_ns) == (
        (400_500, 400_600, 800_500) if stamp == 0 else (500, 600, 800_500))
    assert command.late
    assert native.steps == [400_000, 400_000, 200_000]
    # Discovery follows cut selection: delivery occurs after that World interval,
    # without reopening an already settled cut for either timestamp form.
    assert native.efforts == [('j1', 0), ('j2', 0), ('j1', 0), ('j2', 0),
                              ('j1', .2), ('j2', 0)]


def test_native_cpu_requests_and_completion_use_simulation_machine_phases(exact_setup, monkeypatch):
    runtime, _, native, _, cpus, _ = exact_setup
    start(runtime)
    simulation = runtime._simulation
    assert tuple(simulation.components) == ('boards', 'can_0', 'can_1', 'plant')
    phases = []
    request, finish = runtime._request_cpus, runtime._finish_cpus
    def requesting(*args):
        phases.append((simulation._phase, len(native.steps)))
        return request(*args)
    def finishing(*args):
        phases.append((simulation._phase, len(native.steps)))
        return finish(*args)
    monkeypatch.setattr(runtime, '_request_cpus', requesting)
    monkeypatch.setattr(runtime, '_finish_cpus', finishing)
    runtime.step(10_000_000, deadline=time.monotonic() + 1)
    assert phases == [('machine request', 0), ('machine completion', 0)]
    assert all(len(cpu.requests) == 1 for cpu in cpus)
    assert native.steps == [10_000_000] and simulation.step_id == 1


def assert_exact_frozen(runtime, native, cpus):
    before = list(native.steps), tuple(tuple(cpu.requests) for cpu in cpus)
    assert runtime.failed
    for action in (lambda: runtime.step(1, deadline=time.monotonic() + 1),
                   lambda: runtime.service(deadline=time.monotonic() + 1)):
        with pytest.raises(RuntimeError):
            action()
    assert (native.steps, tuple(tuple(cpu.requests) for cpu in cpus)) == before


def test_motor_feedback_exact_socket_receipts_keep_missing_selected_and_recover_without_replay(exact_setup):
    runtime, boards, native, bindings, cpus, _ = exact_setup
    for cpu, origin, command in zip(cpus, boards.origins,
                                     ("1000 0000 0000 0000", "f000 0000 0000 0000")):
        cpu.output[1] = can_wire(origin, command)
    start(runtime)
    runtime.set_motor_feedback_enabled("left", 1, False)
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert [cpu.completed for cpu in cpus] == [0, 1]
    assert [state["completed_frames"] for state in runtime.can_input_states] == [0, 1]
    assert [device.current_raw for device in runtime.plant.devices] == [4096, -4096]
    assert cpus[0].receipts == []
    runtime.set_motor_feedback_enabled("left", 1, True)
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert [cpu.requests for cpu in cpus] == [[1_001_000, 2_001_000], [1_002_000, 2_002_000]]
    for cpu, current, stamps in ((cpus[0], "1000", (2_001_000,)),
                                 (cpus[1], "f000", (1_002_000, 2_002_000))):
        assert cpu.receipts == [struct.pack(
            "<IIB3xQ64s", 0x201, 0, 8, stamp,
            bytes.fromhex("0000 0000 " + ("0000" if stamp == 1_002_000 else current) + " 19 00").ljust(64, b"\0"))
            for stamp in stamps]
        assert not cpu.partial
    assert [state["completed_frames"] for state in runtime.can_input_states] == [1, 2]
    assert native.steps == [1_000_000] * 2
    assert native.efforts == [("j1", 0), ("j2", 0), ("j1", .2), ("j2", -.2)]
    assert not runtime.failed and all(item.endpoint.bus.pending == 0 for item in bindings)


def test_exact_cpu_group_reaches_drains_and_receives_before_the_next_interval(exact_setup):
    runtime, boards, native, bindings, cpus, events = exact_setup
    for cpu, origin, command in zip(cpus, boards.origins,
                                     ("1000 0000 0000 0000", "f000 0000 0000 0000")):
        cpu.output[1] = can_wire(origin + 1, command)
    start(runtime)
    assert native.resets == 1 and all(cpu.requests == [] for cpu in cpus)
    assert all(type(item.endpoint.stream) is CanStream and
               type(item.endpoint.stream.sock) is socket.socket for item in bindings)
    with pytest.raises(FrozenInstanceError):
        bindings[0].execution.chardev = "changed"
    assert runtime.can_input_states == (
        {"completed_frames": 0, "partial_bytes": 0, "rx_dropped": 0},) * 2
    observed = runtime.can_input_states
    observed[0]["completed_frames"] = 999
    assert runtime.can_input_states[0]["completed_frames"] == 0
    original_step = native.step
    def world_step(dt):
        assert all(cpu.state == "reached" for cpu in cpus)
        assert all(item.endpoint.bus.pending == (0 if native.steps else 1) for item in bindings)
        for item in bindings:
            assert not item.endpoint.stream.buffer
            with pytest.raises(BlockingIOError):
                item.endpoint.stream.sock.recv(1, socket.MSG_PEEK)
        events.append(("world", dt))
        original_step(dt)
    native.step = world_step
    events.clear()
    runtime.step(10_000_000, deadline=time.monotonic() + 1)
    runs = [i for i, row in enumerate(events) if row[0] == "run"]
    polls = [i for i, row in enumerate(events) if row[:1] == ("execution",) and row[2] == "running"]
    assert len(runs) == 2 and max(runs) < min(polls)
    assert [cpu.requests for cpu in cpus] == [[10_001_000], [10_002_000]]
    assert native.steps == [10_000_000] and native.efforts == [("j1", 0), ("j2", 0)]
    assert [device.current_raw for device in runtime.plant.devices] == [4096, -4096]
    assert all(item.endpoint.bus.pending == 0 for item in bindings)
    zero = bytes.fromhex("0000 0000 0000 19 00")
    for cpu, origin in zip(cpus, boards.origins):
        assert cpu.receipts == [struct.pack("<IIB3xQ64s", 0x201, 0, 8,
                                            origin + 10_000_000, zero.ljust(64, b"\0"))]
    # A second CPU request sees all prior ACKs and complete actual native input.
    def next_run():
        assert all(len(cpu.receipts) == 1 and not cpu.partial for cpu in cpus)
        assert all(frames[-1].header.kind == codec.STEP and
                   frames[-1].header.step_id == 1 for frames in boards.frames)
        assert {(row[1], row[3]) for row in events
                if row[0] == "response" and row[2] == codec.STEP_ACK} == {
                    ("left", 1), ("right", 1)}
    for cpu in cpus:
        cpu.on_run = next_run
    runtime.step(10_000_000, deadline=time.monotonic() + 1)
    assert native.steps == [10_000_000, 10_000_000]
    assert native.efforts == [("j1", 0), ("j2", 0), ("j1", .2), ("j2", -.2)]
    assert [cpu.requests for cpu in cpus] == [[10_001_000, 20_001_000],
                                            [10_002_000, 20_002_000]]
    assert all(len(cpu.receipts) == 2 and cpu.completed == 2 for cpu in cpus)
    assert [state["completed_frames"] for state in runtime.can_input_states] == [2, 2]
    assert runtime.t_sim_ns == 20_000_500 and runtime.step_id == 2 and not runtime.failed


@pytest.mark.parametrize("invalid", ["mixed", "type", "command", "controller", "chardev",
                                    "stream", "socket", "budget"])
def test_exact_full_admission_precedes_getters_ownership_and_io(exact_world_resources, invalid):
    plant, boards, native, bindings, cpus, events, _ = exact_world_resources
    item = bindings[1]
    if invalid == "mixed":
        item = replace(item, execution=None)
    elif invalid == "type":
        item = replace(item, execution=object())
    elif invalid in ("command", "controller", "chardev"):
        value = {"command": None, "controller": "bad/name", "chardev": ""}[invalid]
        item = replace(item, execution=replace(item.execution, **{invalid: value}))
    elif invalid == "stream":
        stream = CountingCanStream(item.endpoint.stream.sock, native)
        item = replace(item, endpoint=QemuCanEndpoint(item.endpoint.bus, stream, "other"))
    elif invalid == "socket":
        # An otherwise usable fd wrapper fails the exact original-socket premise.
        item.endpoint.stream.sock = UartBudget(item.endpoint.stream.sock)
    else:
        item.endpoint.bus.dispatch_budget = 0
    with pytest.raises((TypeError, ValueError)):
        SharedWorldRuntime(plant, (bindings[0], item), check_running=boards)
    assert native.reads == native.steps == [] and native.resets == native.closed == 0
    assert not plant.closed and boards.frames == [[], []] and events == []


@pytest.mark.parametrize("invalid", ["mapped_target", "delayed_target", "sample", "headroom", "budget_changed"])
def test_exact_all_board_preflight_precedes_any_cpu_or_world_effect(exact_setup, monkeypatch, invalid):
    runtime, boards, native, bindings, cpus, events = exact_setup
    if invalid == "headroom":
        # Two per-bus dispatch phases can each submit dispatch_budget records.
        cpus[1].completed = (1 << 64) - 513
    start(runtime)
    dt = 1_000_000
    if invalid == "mapped_target":
        dt = (1 << 63) - 1500  # Only the later board's origin makes this overflow.
    elif invalid == "delayed_target":
        runtime.set_can_delay("right", (1 << 64) - 1)
    elif invalid == "sample":
        monkeypatch.setattr(SharedWorldRuntime, "step_id", property(lambda _: (1 << 32) - 1))
    elif invalid == "budget_changed":
        bindings[1].endpoint.bus.dispatch_budget += 1
    before = list(events), list(native.reads)
    with pytest.raises((ValueError, RuntimeError)):
        runtime.step(dt, deadline=time.monotonic() + 1)
    assert all(cpu.requests == cpu.receipts == [] for cpu in cpus)
    assert native.steps == [] and native.resets == 1 and native.reads == before[1]
    assert all(len(frames) == 1 for frames in boards.frames)
    assert_exact_frozen(runtime, native, cpus)


def test_exact_large_logical_epoch_keeps_native_cpu_targets_in_the_reset_epoch(exact_setup):
    runtime, _, native, _, cpus, _ = exact_setup
    runtime.start(Session(7, (1 << 63) + 500), (71, 72), deadline=time.monotonic() + 1)
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert runtime.t_sim_ns == (1 << 63) + 1_000_500
    assert [cpu.requests for cpu in cpus] == [[1_001_000], [1_002_000]]
    assert native.steps == [1_000_000] and all(len(cpu.receipts) == 1 for cpu in cpus)
    assert not runtime.failed


@pytest.mark.parametrize("fault", ["cancelled", "overshoot", "backward", "target", "malformed"])
def test_exact_native_execution_fault_freezes_before_world(exact_setup, fault):
    runtime, _, native, _, cpus, _ = exact_setup
    start(runtime)
    cpus[0].fault = {
        "cancelled": {"state": "cancelled"},
        "overshoot": {"current-ns": 1_001_001},
        "backward": {"current-ns": 999},
        "target": {"target-ns": 1_001_001},
        "malformed": {"current-ns": True},
    }[fault]
    with pytest.raises((TypeError, ValueError, RuntimeError)):
        runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert [cpu.requests for cpu in cpus] == [[1_001_000], [1_002_000]]
    assert native.steps == [] and native.resets == 1
    assert_exact_frozen(runtime, native, cpus)


def test_exact_partial_output_eof_keeps_the_original_tail_and_freezes(exact_setup):
    runtime, _, native, bindings, cpus, _ = exact_setup
    start(runtime)
    raw = can_wire(1001, "1000 0000 0000 0000")
    cpus[0].output[1], cpus[0].eof = raw[:19], True
    with pytest.raises(ConnectionError):
        runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert bytes(bindings[0].endpoint.stream.buffer) == raw[:19]
    assert native.steps == [] and bindings[0].endpoint.bus.pending == 0
    assert_exact_frozen(runtime, native, cpus)
    runtime.close()
    assert bytes(bindings[0].endpoint.stream.buffer) == raw[:19]
    assert bindings[0].endpoint.stream.sock.fileno() >= 0


def test_exact_tx_loss_keeps_prior_board_ingress_without_world_replay(exact_setup):
    runtime, _, native, bindings, cpus, _ = exact_setup
    start(runtime)
    raw = can_wire(1001, "1000 0000 0000 0000")
    cpus[0].output[1] = raw
    cpus[1].on_reached = lambda: setattr(cpus[1], "tx_dropped", 1)
    with pytest.raises(RuntimeError, match="drop"):
        runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert native.steps == [] and bindings[0].endpoint.bus.pending == 1
    assert bindings[1].endpoint.bus.pending == 0
    assert_exact_frozen(runtime, native, cpus)


def test_exact_fragmented_native_receipt_wait_does_not_repeat_world_or_imu(exact_setup):
    runtime, _, native, _, cpus, events = exact_setup
    start(runtime)
    cpus[0].read_limits = [19, 0, 0, 65]
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert native.steps == [1_000_000] and native.reads == [
        ("left", ()), ("right", ()), ("left", (1_000_000,)), ("right", (1_000_000,))]
    assert [len(row[2]) for row in events if row[:2] == ("receipt-bytes", 0)] == [19, 65]
    assert len([row for row in events if row == ("receipt-blocked", 0)]) == 2
    assert all(cpu.completed == 1 and len(cpu.receipts) == 1 and not cpu.partial for cpu in cpus)
    assert [cpu.requests for cpu in cpus] == [[1_001_000], [1_002_000]]
    assert not runtime.failed


def test_exact_native_rejected_records_still_complete_transport_receipt(exact_setup):
    runtime, _, native, _, cpus, _ = exact_setup
    start(runtime)
    cpus[0].on_rx = lambda: setattr(cpus[0], "rx_dropped", cpus[0].completed)
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert native.steps == [1_000_000] and cpus[0].completed == cpus[0].rx_dropped == 1
    assert len(cpus[0].receipts) == 1 and not runtime.failed
    assert runtime.can_input_states == (
        {"completed_frames": 1, "partial_bytes": 0, "rx_dropped": 1},
        {"completed_frames": 1, "partial_bytes": 0, "rx_dropped": 0})
    cpus[0].on_rx, cpus[0].rx_dropped = None, 0
    with pytest.raises(RuntimeError, match="backward"):
        runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert native.steps == [1_000_000]
    assert [cpu.requests for cpu in cpus] == [[1_001_000], [1_002_000]]
    assert_exact_frozen(runtime, native, cpus)


def test_exact_native_receipt_beyond_goal_keeps_completed_world_and_freezes(exact_setup):
    runtime, _, native, _, cpus, _ = exact_setup
    start(runtime)
    cpus[1].report_completed = lambda count: count + 1 if count else 0
    with pytest.raises(RuntimeError, match="exceeds goal"):
        runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert native.steps == [1_000_000] and all(len(cpu.receipts) == 1 for cpu in cpus)
    assert runtime.t_sim_ns == 500 and runtime.step_id == 0
    assert [state["completed_frames"] for state in runtime.can_input_states] == [0, 0]
    assert_exact_frozen(runtime, native, cpus)


def test_exact_receipt_reset_cannot_release_the_next_cpu_interval(exact_setup, monkeypatch):
    runtime, _, native, _, cpus, _ = exact_setup
    start(runtime)
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    cpus[1].report_completed = lambda count: 0
    now = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    cpus[1].on_rx = lambda: now.__setitem__(0, now[0] + .3)
    with pytest.raises((RuntimeError, TimeoutError)):
        runtime.step(1_000_000, deadline=now[0] + 1)
    assert native.steps == [1_000_000] and all(len(cpu.receipts) == 1 for cpu in cpus)
    assert [cpu.requests for cpu in cpus] == [[1_001_000], [1_002_000]]
    assert_exact_frozen(runtime, native, cpus)


def test_exact_ack_service_never_ingests_can_outside_the_explicit_drain(exact_setup):
    runtime, boards, native, bindings, cpus, _ = exact_setup
    start(runtime)
    raw = can_wire(1_001_000, "1000 0000 0000 0000")
    def unexpected_output(index, frame):
        if index == 0 and frame.header.kind == codec.STEP:
            assert cpus[0].peer.send(raw) == len(raw)
        return False
    boards.on_frame = unexpected_output
    with pytest.raises(RuntimeError, match="drain CAN output"):
        runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert native.steps == [1_000_000] and bindings[0].endpoint.bus.pending == 0
    assert not bindings[0].endpoint.stream.buffer
    assert bindings[0].endpoint.stream.sock.recv(84, socket.MSG_PEEK) == raw
    assert_exact_frozen(runtime, native, cpus)


@pytest.fixture
def batch_exact_resources(exact_world_resources):
    plant, boards, native, bindings, scalar_cpus, events, stack = exact_world_resources
    cpus = tuple(NativeCpuBatchQmp(cpu.index, cpu.peer, cpu.current, native, events)
                 for cpu in scalar_cpus)
    bindings = tuple(replace(item, execution=replace(
        item.execution, command=cpu.command, command_many=cpu.command_many))
        for item, cpu in zip(bindings, cpus))
    return plant, boards, native, bindings, cpus, events, stack


@pytest.fixture
def batch_exact_setup(batch_exact_resources):
    plant, boards, native, bindings, cpus, events, stack = batch_exact_resources
    runtime = SharedWorldRuntime(plant, bindings, check_running=boards)
    stack.callback(runtime.close)
    return runtime, boards, native, bindings, cpus, events


@pytest.mark.parametrize("fragmented", [False, True])
def test_exact_batch_keeps_real_receipts_and_fresh_groups_before_next_cpu(
        batch_exact_setup, fragmented):
    runtime, boards, native, bindings, cpus, events = batch_exact_setup
    start(runtime)
    if fragmented:
        cpus[0].read_limits = [19, 0, 65]
        cpus[0].on_rx = lambda: setattr(cpus[0], "rx_dropped", cpus[0].completed)
    cpus[0].output[1] = can_wire(1001, "1000 0000 0000 0000")
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert native.steps == [1_000_000] and native.efforts == [("j1", 0), ("j2", 0)]
    assert runtime.can_input_states == (
        {"completed_frames": 1, "partial_bytes": 0, "rx_dropped": int(fragmented)},
        {"completed_frames": 1, "partial_bytes": 0, "rx_dropped": 0})
    zero = bytes.fromhex("0000 0000 0000 19 00")
    for cpu, origin in zip(cpus, boards.origins):
        assert cpu.receipts == [struct.pack("<IIB3xQ64s", 0x201, 0, 8,
                                            origin + 1_000_000, zero.ljust(64, b"\0"))]
        groups = [values for direction, values in cpu.groups if direction == "input"]
        assert groups[-1][0]["fdcan-rx-state"]["controllers"]["dm-mc02.fdcan1"]["completed_frames"] == 1
        assert len(groups) >= 2 and groups[-1] is not groups[-2]
        assert {direction for direction, _ in cpu.groups} == {"initial", "output", "input"}
        cpu.on_run = lambda: all_receipts_before_run()
    def all_receipts_before_run():
        assert all(cpu.completed == 1 and not cpu.partial for cpu in cpus)
        assert all(frames[-1].header.kind == codec.STEP and frames[-1].header.step_id == 1
                   for frames in boards.frames)
    if fragmented:
        assert [len(row[2]) for row in events if row[:2] == ("receipt-bytes", 0)] == [19, 65]
        first_input = next(values for direction, values in cpus[0].groups if direction == "input")
        assert first_input[0]["fdcan-rx-state"]["controllers"]["dm-mc02.fdcan1"] == {
            "completed_frames": 0, "partial_bytes": 19, "rx_dropped": 0}
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert native.steps == [1_000_000, 1_000_000] and all(cpu.completed == 2 for cpu in cpus)
    assert [cpu.requests for cpu in cpus] == [[1_001_000, 2_001_000], [1_002_000, 2_002_000]]
    assert all(row[2] in ("x-run-to-virtual-time", "x-query-virtual-execution", "query-status")
               for row in events if row[0] == "qmp")
    assert not runtime.failed and all(not item.endpoint.stream.buffer for item in bindings)


def test_exact_batch_callable_admission_precedes_getters_ownership_and_io(batch_exact_resources):
    plant, boards, native, bindings, _, events, _ = batch_exact_resources
    bad = replace(bindings[1], execution=replace(bindings[1].execution, command_many=True))
    with pytest.raises(TypeError, match="command_many"):
        SharedWorldRuntime(plant, (bindings[0], bad), check_running=boards)
    assert native.reads == native.steps == [] and native.resets == native.closed == 0
    assert not plant.closed and boards.frames == [[], []] and events == []


@pytest.mark.parametrize("direction", ["output", "input"])
def test_exact_batch_failure_retains_effects_without_scalar_fallback_or_replay(batch_exact_setup, direction):
    runtime, _, native, _, cpus, _ = batch_exact_setup
    start(runtime)
    primary = KeyboardInterrupt("borrowed native batch interrupted")
    def fail(kind, values):
        if (bool(native.steps) == (direction == "input") and
                cpus[1].requests):
            raise primary
    cpus[1].after_batch = fail
    with pytest.raises(KeyboardInterrupt) as caught:
        runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert caught.value is primary
    assert native.steps == ([1_000_000] if direction == "input" else [])
    assert [cpu.requests for cpu in cpus] == [[1_001_000], [1_002_000]]
    assert all(len(cpu.receipts) == (1 if direction == "input" else 0) for cpu in cpus)
    assert_exact_frozen(runtime, native, cpus)


@pytest.mark.parametrize("violation", ["deadline", "liveness"])
def test_exact_post_batch_guard_freezes_after_one_world_step(batch_exact_setup, monkeypatch, violation):
    runtime, boards, native, _, cpus, _ = batch_exact_setup
    start(runtime)
    now, failed = [time.monotonic()], [False]
    primary = OSError("board disappeared after native batch")
    def check():
        boards()
        if failed[0]:
            raise primary
    monkeypatch.setattr(runtime, "_check_running", check)
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    def invalidate(direction, values):
        if native.steps and cpus[0].receipts:
            if violation == "deadline":
                now[0] += 2
            else:
                failed[0] = True
    cpus[0].after_batch = invalidate
    with pytest.raises(RuntimeError if violation == "deadline" else OSError) as caught:
        runtime.step(1_000_000, deadline=now[0] + 1)
    if violation == "liveness":
        assert caught.value is primary
    assert native.steps == [1_000_000] and len(cpus[0].receipts) == 1
    assert [cpu.requests for cpu in cpus] == [[1_001_000], [1_002_000]]
    assert_exact_frozen(runtime, native, cpus)


@pytest.mark.parametrize("mutation", ["count", "identity"])
def test_exact_batch_final_result_is_fresh_and_change_freezes_completed_world(batch_exact_setup, mutation):
    runtime, _, native, _, cpus, _ = batch_exact_setup
    start(runtime)
    inputs = []
    def change_final(direction, values):
        if native.steps and cpus[0].receipts:
            inputs.append(values)
            if len(inputs) == 2:
                if mutation == "count":
                    values[0]["fdcan-rx-state"]["controllers"]["dm-mc02.fdcan1"]["completed_frames"] = 2
                else:
                    values[0]["fdcan-transports"]["dm-mc02.fdcan1"]["path"] = "/chardevs/replacement"
    cpus[0].after_batch = change_final
    with pytest.raises(RuntimeError, match="exceeds goal" if mutation == "count" else "transport identity changed"):
        runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert len(inputs) == 2 and inputs[0] is not inputs[1]
    assert inputs[0][0]["fdcan-rx-state"]["controllers"]["dm-mc02.fdcan1"]["completed_frames"] == 1
    assert inputs[0][0]["fdcan-transports"]["dm-mc02.fdcan1"] == {
        "path": "/chardevs/can1", "connected": True}
    assert native.steps == [1_000_000] and len(cpus[0].receipts) == 1
    assert runtime.can_input_states == ({"completed_frames": 0, "partial_bytes": 0, "rx_dropped": 0},) * 2
    assert_exact_frozen(runtime, native, cpus)


@pytest.fixture(params=(False, True), ids=('nonexact', 'exact'))
def can_deadline_setup(request):
    resources = request.getfixturevalue(
        'exact_world_resources' if request.param else 'world_resources')
    plant, boards, native, bindings, peers, events, stack = resources
    def observe(name, frame, decoded):
        events.append(('response', name, frame.header.kind, frame.header.step_id))
    runtime = SharedWorldRuntime(plant, bindings, check_running=boards, on_response=observe)
    stack.callback(runtime.close)
    return SimpleNamespace(runtime=runtime, boards=boards, native=native,
                           bindings=bindings, cpus=peers if request.param else (),
                           peers=tuple(cpu.peer for cpu in peers) if request.param else peers,
                           events=events)


def admit_fixture_can(f):
    """Decode fixture bytes into the real queue before CPU execution.

    This isolates a pre-existing queue, not native QEMU producer timing. Native
    gates obtain pending due from preceding CPU output plus a bus delay.
    """
    for binding in f.bindings:
        for can_id, flags, dlc, stamp, data in binding.endpoint.stream.receive():
            binding.endpoint.ingest(CanFrame(can_id, flags, dlc, data, stamp), f.runtime.t_sim_ns)


def test_known_can_due_splits_world_and_exchanges_before_next_cpu(can_deadline_setup):
    f = can_deadline_setup
    start(f.runtime)
    deliveries = []
    f.bindings[0].endpoint.bus.log = deliveries.append
    raw = can_wire(f.boards.origins[0] + 1_500_000, '1000 0000 0000 0000')
    assert f.peers[0].send(raw) == 84
    admit_fixture_can(f)
    assert f.bindings[0].endpoint.bus.next_due_ns == 1_500_500
    original_step = f.native.step
    def world_step(dt):
        # Source discovery is not dispatch: the first World interval must use
        # the previous control even though its command is already queued.
        if not f.native.steps:
            assert f.runtime.plant.devices[0].current_raw == 0
        original_step(dt)
    f.native.step = world_step
    def next_run():
        if f.native.steps:
            assert f.runtime.t_sim_ns == 1_500_500 and f.runtime.step_id == 1
            assert all(len(cpu.receipts) == 1 for cpu in f.cpus)
            assert all(frames[-1].header.step_id == 1 for frames in f.boards.frames)
            assert {row[1] for row in f.events if row[:1] == ('response',)
                    and row[2:] == (codec.STEP_ACK, 1)} == {'left', 'right'}
    for cpu in f.cpus:
        cpu.on_run = next_run
    f.runtime.step(4_000_000, deadline=time.monotonic() + 1)
    assert f.native.steps == [1_500_000, 2_500_000]
    assert f.native.efforts == [('j1', 0), ('j2', 0), ('j1', .2), ('j2', 0)]
    command = next(row for row in deliveries if row.frame.can_id == 0x200)
    assert (command.timestamp_ns, command.due_ns, command.delivered_ns, command.late) == (
        1_500_500, 1_500_500, 1_500_500, False)
    for frames in f.boards.frames:
        assert [(frame.header.step_id, frame.header.t_sim_ns, frame.header.dt_ns)
                for frame in frames[1:]] == [(1, 1_500_500, 1_500_000), (2, 4_000_500, 2_500_000)]
    if f.cpus:
        assert [cpu.requests for cpu in f.cpus] == [
            [1_501_000, 4_001_000], [1_502_000, 4_002_000]]
        assert [state['completed_frames'] for state in f.runtime.can_input_states] == [2, 2]
        # The first feedback reflects the old control. This is a transport
        # receipt of actual socket bytes, never firmware consumption evidence.
        assert f.cpus[0].receipts[0][20:28] == bytes.fromhex('0000 0000 0000 19 00')
        assert f.cpus[0].receipts[1][20:28] == bytes.fromhex('0000 0000 1000 19 00')
    assert f.runtime.step_id == 2 and f.runtime.t_sim_ns == 4_000_500


def test_due_cut_keeps_independent_command_expiry_inside_one_world(can_deadline_setup):
    f = can_deadline_setup
    f.runtime.plant.devices[0].command_timeout_ns = 1_000_000
    f.runtime.plant.devices[1].command_timeout_ns = 10_000_000
    start(f.runtime)
    for peer, stamp, command in zip(f.peers,
            (f.boards.origins[0] + 1_500_000, f.boards.origins[1]),
            ('1000 0000 0000 0000', 'f000 0000 0000 0000')):
        assert peer.send(can_wire(stamp, command)) == 84
    admit_fixture_can(f)
    f.runtime.step(4_000_000, deadline=time.monotonic() + 1)
    assert f.native.steps == [1_500_000, 1_000_000, 1_500_000]
    assert f.native.efforts == [('j1', 0), ('j2', -.2), ('j1', .2), ('j2', -.2),
                               ('j1', 0), ('j2', -.2)]
    # Device expiry subdivides physics, not CPU/STEP transactions.
    assert f.runtime.step_id == 2 and all(len(frames) == 3 for frames in f.boards.frames)
    assert f.native.resets == 1
    f.runtime.close()
    f.runtime.close()
    assert f.native.closed == 1


def test_one_bus_settle_budget_cannot_be_spent_again_in_world_step(world_resources):
    plant, boards, native, bindings, peers, _, stack = world_resources
    bindings[0].endpoint.bus.dispatch_budget = 1
    runtime = SharedWorldRuntime(plant, bindings, check_running=boards)
    stack.callback(runtime.close)
    start(runtime)
    raw = can_wire(boards.origins[0] + 1_500_000, '1000 0000 0000 0000')
    assert peers[0].send(raw * 2) == 168
    with pytest.raises(RuntimeError, match='can_0 dispatch budget exhausted'):
        runtime.step(4_000_000, deadline=time.monotonic() + 1)
    assert native.steps == [1_500_000] and native.efforts == [('j1', 0), ('j2', 0)]
    assert runtime.failed and runtime.t_sim_ns == 500 and runtime.step_id == 0
    assert plant.devices[0].current_raw == 4096 and bindings[0].endpoint.bus.pending == 2
    assert all(len(frames) == 1 for frames in boards.frames)
    before = list(native.steps), bindings[0].endpoint.bus.pending
    with pytest.raises(RuntimeError, match='fresh runtime'):
        runtime.step(1, deadline=time.monotonic() + 1)
    assert (native.steps, bindings[0].endpoint.bus.pending) == before


def test_pending_due_is_immutable_and_drop_is_evaluated_at_its_cut(can_deadline_setup):
    f = can_deadline_setup
    start(f.runtime)
    bus = f.bindings[0].endpoint.bus
    deliveries = []
    bus.log = deliveries.append
    f.runtime.set_can_delay('left', 250_000)
    assert f.peers[0].send(can_wire(f.boards.origins[0] + 1_000_000,
                                  '1000 0000 0000 0000')) == 84
    admit_fixture_can(f)
    assert bus.next_due_ns == 1_250_500
    f.runtime.set_can_delay('left', 0)
    assert bus.next_due_ns == 1_250_500
    bus.drop = lambda *_: True
    f.runtime.step(2_000_000, deadline=time.monotonic() + 1)
    command = next(row for row in deliveries if row.frame.can_id == 0x200)
    assert (command.timestamp_ns, command.due_ns, command.delivered_ns, command.dropped) == (
        1_000_500, 1_250_500, 1_250_500, True)
    assert f.native.steps == [1_250_000, 750_000]
    assert all(effort == 0 for _, effort in f.native.efforts)
    bus.drop = None
    f.runtime.step(1, deadline=time.monotonic() + 1)
    assert f.runtime.plant.devices[0].current_raw == 0
    assert sum(row.frame.can_id == 0x200 for row in deliveries) == 1


def test_dynamic_due_cut_sample_exhaustion_freezes_before_second_world(can_deadline_setup, monkeypatch):
    f = can_deadline_setup
    start(f.runtime)
    raw = can_wire(f.boards.origins[0] + 1_500_000, '1000 0000 0000 0000')
    assert f.peers[0].send(raw) == 84
    admit_fixture_can(f)
    # The outer quantum lower bound admits one cut. The actual second cut must
    # still validate wire sample headroom before any next CPU or World effect.
    admit = f.runtime._admit_cut
    def limited(target, step_id):
        return admit(target, step_id if step_id <= 1 else 1 << 32)
    monkeypatch.setattr(f.runtime, '_admit_cut', limited)
    with pytest.raises(ValueError, match='sample ID'):
        f.runtime.step(4_000_000, deadline=time.monotonic() + 1)
    assert f.runtime.t_sim_ns == 1_500_500 and f.runtime.step_id == 1
    assert f.runtime.failed and f.native.steps == [1_500_000]
    assert all(len(cpu.requests) == 1 for cpu in f.cpus)
    assert all(len(frames) == 2 for frames in f.boards.frames)


def test_dispatch_graph_capacity_is_admitted_before_getters_or_reset(world_resources):
    plant, boards, native, bindings, _, _, _ = world_resources
    with pytest.raises(ValueError, match='1..64 nodes'):
        SharedWorldRuntime(plant, (bindings[0],) * 63, check_running=boards)
    assert native.reads == native.steps == [] and native.resets == native.closed == 0
    assert not plant.closed and boards.frames == [[], []]


def test_cross_bus_callback_settles_in_stable_order_with_one_budget_per_bus(world_resources):
    plant, boards, native, bindings, peers, _, stack = world_resources
    buses = tuple(item.endpoint.bus for item in bindings)
    observed = []
    left = SimpleNamespace(receive=lambda frame: observed.append(('left', frame.data)))
    def right_receive(frame):
        observed.append(('right', frame.data))
        if frame.data == b'right':
            buses[0].send(left, CanFrame(0x123, data=b'callback', timestamp_ns=500))
    right = SimpleNamespace(receive=right_receive)
    buses[0].attach(left, 'tap-left')
    buses[1].attach(right, 'tap-right')
    runtime = SharedWorldRuntime(plant, bindings, check_running=boards)
    stack.callback(runtime.close)
    start(runtime)
    # Later owner queues work on the earlier owner after its first visit. The
    # settle loop must drain it before the World interval, without a new step.
    buses[0].send(bindings[0].endpoint, CanFrame(0x123, data=b'left', timestamp_ns=500))
    buses[1].send(bindings[1].endpoint, CanFrame(0x123, data=b'right', timestamp_ns=500))
    deliveries = []
    buses[0].log = deliveries.append
    runtime.step(1, deadline=time.monotonic() + 1)
    assert observed == [('left', b'left'), ('right', b'right')]
    assert [(row.frame.data, row.destination, row.sequence) for row in deliveries] == [
        (b'left', 'm3508-1', 0), (b'left', 'tap-left', 0),
        (b'callback', 'm3508-1', 1), (b'callback', 'qemu', 1)]
    assert all(row.delivered_ns == 500 for row in deliveries)
    assert native.steps == [1] and runtime.step_id == 1


def test_second_due_cut_failure_retains_first_commit_and_borrowed_tails(can_deadline_setup):
    f = can_deadline_setup
    start(f.runtime)
    assert f.peers[0].send(can_wire(f.boards.origins[0] + 1_500_000,
                                  '1000 0000 0000 0000')) == 84
    admit_fixture_can(f)
    primary = OSError('second deadline ACK observer failed')
    observe = f.runtime._on_response
    def fail_second(name, frame, decoded):
        observe(name, frame, decoded)
        if name == 'right' and frame.header.kind == codec.STEP_ACK and frame.header.step_id == 2:
            raise primary
    f.runtime._on_response = fail_second
    with pytest.raises(OSError) as caught:
        f.runtime.step(4_000_000, deadline=time.monotonic() + 1)
    assert caught.value is primary and f.runtime.failed
    assert f.runtime.t_sim_ns == 1_500_500 and f.runtime.step_id == 1
    assert f.native.steps == [1_500_000, 2_500_000]
    assert all(frame.header.step_id == 1 for frame in
               f.runtime._simulation.components['boards'].responses)
    before = list(f.native.steps), [tuple(frames) for frames in f.boards.frames]
    with pytest.raises(RuntimeError, match='fresh runtime'):
        f.runtime.step(1, deadline=time.monotonic() + 1)
    assert (f.native.steps, [tuple(frames) for frames in f.boards.frames]) == before
    for item in f.bindings:
        item.endpoint.stream.buffer.extend(b'CAN tail')
        item.cosim.buffer.extend(b'STEP tail')
    f.runtime.close()
    assert f.native.closed == 1
    assert all(bytes(item.endpoint.stream.buffer) == b'CAN tail'
               and bytes(item.cosim.buffer) == b'STEP tail'
               and item.endpoint.stream.sock.fileno() >= 0
               and item.cosim.sock.fileno() >= 0 for item in f.bindings)


@pytest.mark.parametrize('backtrack', [False, True])
def test_initial_settle_receipt_precedes_first_cpu_without_rebasing(exact_world_resources, backtrack):
    plant, boards, native, bindings, cpus, events, stack = exact_world_resources
    source = SimpleNamespace(receive=lambda _: None)
    bus = bindings[0].endpoint.bus
    bus.attach(source, 'virtual-source')
    runtime = SharedWorldRuntime(plant, bindings, check_running=boards)
    stack.callback(runtime.close)
    start(runtime)
    bus.send(source, CanFrame(0x123, data=b'initial', timestamp_ns=500))
    if backtrack:
        observed = []
        def invalid_drop():
            if cpus[0].receipts:
                observed.append(True)
                cpus[0].rx_dropped = int(len(observed) <= 2)
        cpus[0].on_rx = invalid_drop
        with pytest.raises(RuntimeError, match='initial CAN baseline changed'):
            runtime.step(1_000_000, deadline=time.monotonic() + 1)
        assert runtime.failed and native.steps == [] and cpus[0].completed == 1
        assert runtime.t_sim_ns == 500 and all(cpu.requests == [] for cpu in cpus)
        return
    def before_cpu():
        assert native.steps == [] and runtime.t_sim_ns == 500
        assert cpus[0].completed == 1 and len(cpus[0].receipts) == 1
        assert cpus[0].receipts[0] == struct.pack('<IIB3xQ64s', 0x123, 0, 7, 1000,
                                                  b'initial'.ljust(64, b'\0'))
        assert runtime._can_baselines[0]['submitted_frames'] == 0
        assert runtime._can_baselines[0]['completed_frames'] == 0
    for cpu in cpus:
        cpu.on_run = before_cpu
    runtime.step(1_000_000, deadline=time.monotonic() + 1)
    assert native.steps == [1_000_000] and runtime.step_id == 1
    assert [state['completed_frames'] for state in runtime.can_input_states] == [2, 1]
