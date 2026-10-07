"""Socketpair isolation of one actual core/M3508 owner and two board exchanges.

Only native physics and independent board response producers are isolated.
Literal protocol bytes and control/expiry expectations are independent of the
runtime helpers. This does not run QEMU or prove sensor consumption/CPU timing.
"""

from contextlib import ExitStack
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import socket
import struct
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sim.core import Session
from sim.devices.dji import M3508Device, M3508JointPlant
from sim.fabric import CanBus
from sim.fabric.endpoints import QemuCanEndpoint
from sim.physics import ImuSample, JointState
from sim.robots.shared_world import BoardBinding, SharedWorldRuntime
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
def setup():
    with ExitStack() as stack:
        native = NativeStub()
        buses = (CanBus(), CanBus())
        devices = tuple(M3508Device(bus, 1) for bus in buses)
        plant = M3508JointPlant(native, dict(zip(devices, ("j1", "j2"))), gear_ratio=2)
        stack.callback(plant.close)
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
        runtime = SharedWorldRuntime(
            plant, bindings, check_running=boards,
            on_response=lambda *response: observations.append(response))
        stack.callback(runtime.close)
        yield runtime, boards, native, bindings, can_peers, observations


def start(runtime):
    return runtime.start(Session(7, 500), (71, 72), deadline=time.monotonic() + 1)


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
    assert native.efforts == [("j1", 0), ("j2", 0)]
    runtime.step(10, deadline=time.monotonic() + 1)
    assert native.efforts == [("j1", 0), ("j2", 0), ("j1", .2), ("j2", 0)]
    assert native.steps == [10, 10]


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
