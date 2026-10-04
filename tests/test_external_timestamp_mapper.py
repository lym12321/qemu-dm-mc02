"""Contract tests for external clocks and QEMU FDCAN timestamps.

These tests intentionally keep the worker import local to this test module.
The FDCAN test describes the boundary between the QEMU clock epoch and the
worker virtual-time epoch.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import socket
import struct
import sys
import threading
import time
from types import SimpleNamespace

import pytest


WORKER = Path(__file__).resolve().parents[1] / "tools" / \
    "dm_mc02_sim_worker.py"
SPEC = importlib.util.spec_from_file_location(
    "dm_mc02_sim_worker_external_timestamp_test", WORKER)
assert SPEC is not None and SPEC.loader is not None
worker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = worker
SPEC.loader.exec_module(worker)


def test_reset_reanchors_a_new_external_clock_epoch():
    mapper = worker.ExternalTimestampMapper(1_000)

    assert mapper.map(50_000, 1_100) == 1_100
    assert mapper.map(51_000, 1_200) == 2_000

    # A simulator reset can move its clock backwards.  The worker must keep
    # advancing until a forward sample from the new epoch is available.
    assert mapper.map(10, 1_300) == 2_001
    assert mapper.map(20, 1_400) == 2_002
    assert mapper.map(120, 1_500) == 2_102
    assert mapper.map(130, 1_600) == 2_112


def test_external_timestamp_rollback_never_moves_worker_time_backwards():
    mapper = worker.ExternalTimestampMapper(0)
    values = [
        mapper.map(1_000, 100),
        mapper.map(2_000, 200),
        mapper.map(1_500, 300),
        mapper.map(1_501, 400),
        mapper.map(1_600, 500),
    ]

    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_repeated_external_timestamp_uses_fallback_progress():
    mapper = worker.ExternalTimestampMapper(0)

    assert mapper.map(123, 10) == 10
    assert mapper.map(123, 20) == 20
    assert mapper.map(123, 30) == 30
    assert mapper.map(123, 40) == 40


def test_forward_external_clock_recovers_without_old_fallback_rate():
    mapper = worker.ExternalTimestampMapper(0)

    assert mapper.map(10_000, 10_000) == 10_000
    assert mapper.map(10_000, 20_000) == 20_000
    assert mapper.map(10_000, 30_000) == 30_000
    assert mapper.map(10_100, 40_000) == 40_000
    assert mapper.map(10_200, 50_000) == 40_100
    assert mapper.map(10_300, 60_000) == 40_200


class _Backend:
    def __init__(self):
        self.events = []

    def set_motor(self, index, value):
        self.events.append(("set", index, value))

    def reset_motor(self, index):
        self.events.append(("reset", index))

    def set_motor_enabled(self, index, enabled):
        self.events.append(("enabled", index, enabled))

    def set_motor_dm(self, index, command):
        self.events.append(("dm", index, command))

    def motor_feedback(self, index):
        return (0.0, 0.0, 0.0)

    def step(self, dt):
        self.events.append(("step", dt))
        return (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)


def test_qemu_fdcan_timestamp_is_submitted_in_worker_virtual_epoch():
    """A QEMU timestamp must be translated before coordinator submission.

    QEMU's timestamp is in its virtual-clock epoch.  After RESET, the worker
    owns a separate virtual-time epoch, so only the delta from the QEMU reset
    timestamp is meaningful to the worker.  Feeding the raw timestamp into
    ``StepCoordinator`` leaves a command scheduled far in the future.
    """
    adapter = worker.DmMotorBusAdapter(1, motor_protocol="float")
    backend = _Backend()
    coordinator = worker.StepCoordinator(backend, adapter)

    qemu_epoch_ns = 10_000_000_000
    worker_epoch_ns = 0
    qemu_frame_timestamp_ns = qemu_epoch_ns + 25
    expected_worker_timestamp_ns = (
        worker_epoch_ns + qemu_frame_timestamp_ns - qemu_epoch_ns)

    mapper = worker.QemuTimestampMapper(qemu_epoch_ns, worker_epoch_ns)
    mapped_timestamp_ns = mapper.map(qemu_frame_timestamp_ns,
                                     worker_epoch_ns)
    assert mapped_timestamp_ns == expected_worker_timestamp_ns

    # Simulate the worker's FDCAN receive path. The coordinator receives the
    # mapped worker timestamp, never QEMU's raw timestamp.
    coordinator.submit_can(
        0x200, 0, b"\x00\x00\xc0?", mapped_timestamp_ns,
        worker_epoch_ns)
    coordinator.advance(expected_worker_timestamp_ns, 0.001)

    assert backend.events[0] == ("set", 0, 1.5)


def _recv_exact(sock, length):
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ConnectionError("worker socket closed before complete frame")
        data.extend(chunk)
    return bytes(data)


def _recv_v2_wire(sock):
    prefix = _recv_exact(sock, 4)
    length, = struct.unpack("<I", prefix)
    return prefix + _recv_exact(sock, length)


def _step_reply(step, kind, status=0):
    protocol = worker.step_protocol
    payload = (struct.pack("<IIII", status, 0, 0, 0)
               if kind == protocol.STEP_ACK else
               struct.pack("<IIIII", status, 3, 0, 0, 0))
    return protocol.build_step_frame(
        kind, step.header.step_id, step.header.t_sim_ns,
        dt_ns=step.header.dt_ns, flags=step.header.session_id, payload=payload)


@pytest.fixture
def retry_worker_peer(monkeypatch):
    """Run the real worker; socketpairs isolate only host transport setup."""
    cosim_worker, cosim_peer = socket.socketpair()
    can_worker, can_peer = socket.socketpair()
    host_worker, host_peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    peers = (cosim_peer, can_peer, host_peer)
    worker_sockets = (cosim_worker, can_worker, host_worker)
    for sock in peers:
        sock.settimeout(2.0)
    state = SimpleNamespace(
        cosim=cosim_peer, can=can_peer, host=host_peer,
        steps=[], resets=[], closes=[], outcome=[], qemu_origin=10_000_000_000)
    engine = worker.NullEngine(1)
    original_step, original_reset = engine.step, engine.reset

    def step(dt):
        state.steps.append(dt)
        return original_step(dt)

    def reset():
        state.resets.append(True)
        original_reset()

    monkeypatch.setattr(engine, "step", step)
    monkeypatch.setattr(engine, "reset", reset)
    monkeypatch.setattr(engine, "close", lambda: state.closes.append(True),
                        raising=False)
    monkeypatch.setattr(worker, "create_engine", lambda args: engine)
    connections = {"cosim": cosim_worker, "can": can_worker}
    monkeypatch.setattr(worker, "connect_unix",
                        lambda path, timeout: connections[path])
    monkeypatch.setattr(worker, "open_socketcan", lambda interface: host_worker)
    monkeypatch.setattr(sys, "argv", [
        "dm_mc02_sim_worker.py", "--cosim", "cosim", "--fdcan", "can",
        "--socketcan", "test", "--protocol", "v2", "--engine", "null",
        "--motor-count", "1", "--rate", "1000", "--frames", "1",
        "--wait-step-done",
    ])
    args = worker.parse_args()

    def run_worker():
        try:
            state.outcome.append(worker.run(args))
        except Exception as exc:
            state.outcome.append(exc)

    state.thread = threading.Thread(target=run_worker, daemon=True)
    state.thread.start()
    protocol = worker.step_protocol
    try:
        reset_frame = protocol.decode_step_frame(_recv_v2_wire(cosim_peer))
        assert reset_frame.header.kind == protocol.RESET
        cosim_peer.sendall(protocol.build_step_frame(
            protocol.RESET_ACK, 0, 0, flags=reset_frame.header.session_id,
            payload=struct.pack("<IIQII", 0, protocol.CAPABILITY_MASK,
                                state.qemu_origin, 0, 0)))
        state.step_wire = _recv_v2_wire(cosim_peer)
        state.step = protocol.decode_step_frame(state.step_wire)
        assert (state.step.header.kind, state.step.header.step_id,
                state.step.header.t_sim_ns, state.step.header.dt_ns) == \
            (protocol.STEP, 1, 1_000_000, 1_000_000)
        yield state
    finally:
        for sock in peers:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        state.thread.join(timeout=3.0)
        closed_by_worker = [sock.fileno() == -1 for sock in worker_sockets]
        for sock in worker_sockets:
            sock.close()
        assert not state.thread.is_alive(), "worker did not stop on disconnect"
        assert closed_by_worker == [True, True, True]
        assert state.closes == [True]


def _assert_can_forwarded_before_response(peer, marker):
    """Literal Linux/QEMU records are the oracle, not production codecs."""
    fd_payload = bytes([marker]) * 12
    peer.can.sendall(struct.pack(
        "<IIB3xQ64s", 0x1234567, 13, 9, peer.qemu_origin + 500_000,
        fd_payload))
    classic_payload = bytes([marker, 0x20, 0x30, 0x40])
    peer.host.send(struct.pack("=IB3x8s", 0x321, 4, classic_payload))
    # ACK/DONE remains withheld throughout these reads. A next-iteration-only
    # forwarding implementation cannot satisfy this gate.
    peer.host.settimeout(0.15)
    peer.can.settimeout(0.15)
    assert peer.host.recv(72) == struct.pack(
        "=IBBBB64s", 0x81234567, 12, 5, 0, 0, fd_payload)
    assert _recv_exact(peer.can, 84) == struct.pack(
        "<IIB3xQ64s", 0x321, 0, 4, peer.qemu_origin + 1_000_000,
        classic_payload)
    assert peer.steps == [0.001]


def test_worker_queue_full_replays_once_per_retry_and_services_can(
        retry_worker_peer):
    peer = retry_worker_peer
    protocol = worker.step_protocol
    for _ in range(2):
        peer.cosim.sendall(_step_reply(
            peer.step, protocol.STEP_ACK, protocol.STATUS_QUEUE_FULL))
        assert _recv_v2_wire(peer.cosim) == peer.step_wire
        assert peer.steps == [0.001]
    _assert_can_forwarded_before_response(peer, 1)
    peer.cosim.sendall(_step_reply(peer.step, protocol.STEP_ACK))
    _assert_can_forwarded_before_response(peer, 2)
    peer.cosim.sendall(_step_reply(peer.step, protocol.STEP_DONE))
    peer.thread.join(timeout=3.0)
    assert not peer.thread.is_alive()
    assert peer.outcome == [0]
    assert peer.steps == [0.001]


@pytest.mark.parametrize("termination", ["disconnect", "reset", "repeated_reset"])
def test_worker_queue_full_interrupted_without_restepping_old_input(
        retry_worker_peer, termination):
    peer = retry_worker_peer
    protocol = worker.step_protocol
    peer.cosim.sendall(_step_reply(
        peer.step, protocol.STEP_ACK, protocol.STATUS_QUEUE_FULL))
    assert _recv_v2_wire(peer.cosim) == peer.step_wire
    assert peer.steps == [0.001]
    if termination == "disconnect":
        peer.cosim.shutdown(socket.SHUT_RDWR)
        peer.thread.join(timeout=3.0)
        assert not peer.thread.is_alive()
        assert len(peer.outcome) == 1
        assert isinstance(peer.outcome[0], ConnectionError)
        assert "co-sim socket closed" in str(peer.outcome[0])
        assert peer.steps == [0.001]
    else:
        session = 1 if peer.step.header.session_id != 1 else 2
        new_origin = 20_000_000_000
        peer.cosim.sendall(protocol.build_step_frame(
            protocol.RESET, 0, new_origin, flags=session))
        # Keep RESET_ACK withheld beyond the old 250 ms ACK wait. RESET
        # itself must cancel old STEP replay, even across socket reads.
        peer.cosim.settimeout(0.35)
        with pytest.raises(socket.timeout):
            peer.cosim.recv(1)
        assert peer.steps == [0.001] and peer.resets == []
        peer.cosim.settimeout(2.0)
        if termination == "repeated_reset":
            # A second reset can share the same virtual timestamp while
            # replacing the session. Only the latest ACK may restart plant.
            session += 1
            peer.cosim.sendall(protocol.build_step_frame(
                protocol.RESET, 0, new_origin, flags=session))
        peer.cosim.sendall(protocol.build_step_frame(
            protocol.RESET_ACK, 0, new_origin, flags=session,
            payload=struct.pack("<IIQII", 0, protocol.CAPABILITY_MASK,
                                new_origin, 0, 0)))
        new_step = protocol.decode_step_frame(_recv_v2_wire(peer.cosim))
        assert (new_step.header.step_id, new_step.header.t_sim_ns,
                new_step.header.dt_ns, new_step.header.session_id) == \
            (1, new_origin + 1_000_000, 1_000_000, session)
        peer.cosim.sendall(_step_reply(new_step, protocol.STEP_ACK) +
                           _step_reply(new_step, protocol.STEP_DONE))
        peer.thread.join(timeout=3.0)
        assert not peer.thread.is_alive()
        assert peer.outcome == [0]
        assert peer.resets == [True]
        assert peer.steps == [0.001, 0.001]


def test_worker_persistent_queue_full_has_one_real_admission_deadline(
        retry_worker_peer):
    peer = retry_worker_peer
    protocol = worker.step_protocol
    queue_full = _step_reply(
        peer.step, protocol.STEP_ACK, protocol.STATUS_QUEUE_FULL)
    started = time.monotonic()
    replays = 0
    peer.cosim.settimeout(0.3)
    while time.monotonic() - started < 3.25:
        try:
            peer.cosim.sendall(queue_full)
            replay = _recv_v2_wire(peer.cosim)
        except ConnectionError:
            break
        assert replay == peer.step_wire
        replays += 1
    elapsed = time.monotonic() - started
    peer.thread.join(timeout=0.5)
    assert not peer.thread.is_alive(), "QUEUE_FULL kept extending ACK admission"
    assert 1.7 <= elapsed < 3.0
    assert replays > 3
    assert peer.steps == [0.001]
    assert len(peer.outcome) == 1
    assert isinstance(peer.outcome[0], TimeoutError)
    diagnostic = str(peer.outcome[0])
    assert "STEP_ACK admission timed out" in diagnostic
    assert "step=1" in diagnostic and "last_status=1" in diagnostic
    assert "queue_depth=0" in diagnostic


@pytest.mark.parametrize(
    "qemu_origin, worker_origin, worker_time, wire_time", [
        (10_000_000_000, 0, 1_000_000, 10_001_000_000),
        (10_000_000_000, 10_000_000_000, 10_001_000_000, 10_001_000_000),
        (20_000_000_000, 20_000_000_000, 20_001_000_000, 20_001_000_000),
    ], ids=["initial-v2", "v1-shared", "reset-shared"])
def test_feedback_wire_epoch_preserves_motor_state_time(
        monkeypatch, qemu_origin, worker_origin, worker_time, wire_time):
    adapter = worker.DmMotorBusAdapter(1, motor_protocol="dm-mit")
    mapper = worker.QemuTimestampMapper(qemu_origin, worker_origin)
    states = []
    encode_feedback = adapter.encode_feedback

    def record_state(state):
        states.append(state)
        return encode_feedback(state)

    monkeypatch.setattr(adapter, "encode_feedback", record_state)
    sender, receiver = socket.socketpair()
    with sender, receiver:
        receiver.settimeout(2.0)
        worker.send_dm_feedback(worker.CanStream(sender), worker.NullEngine(1),
                                [0], worker_time, adapter, mapper)
        feedback = struct.unpack("<IIB3xQ64s", _recv_exact(receiver, 84))

    assert len(states) == 1 and states[0].timestamp_ns == worker_time
    assert feedback[:4] == (0x11, 0, 8, wire_time)
    assert feedback[4] == bytes.fromhex("01 80 00 80 08 00 19 19") + bytes(56)


def test_worker_v2_feedback_returns_to_qemu_clock_epoch(monkeypatch):
    cosim_worker, cosim_peer = socket.socketpair()
    can_worker, can_peer = socket.socketpair()
    cosim_peer.settimeout(2.0)
    can_peer.settimeout(2.0)
    connections = {"cosim": cosim_worker, "can": can_worker}
    monkeypatch.setattr(worker, "connect_unix",
                        lambda path, timeout: connections[path])
    monkeypatch.setattr(sys, "argv", [
        "dm_mc02_sim_worker.py", "--cosim", "cosim", "--fdcan", "can",
        "--protocol", "v2", "--engine", "null", "--motor-count", "1",
        "--motor-protocol", "dm-mit", "--rate", "1000", "--frames", "1",
    ])
    args = worker.parse_args()
    outcome = []

    def run_worker():
        try:
            outcome.append(worker.run(args))
        except Exception as exc:
            outcome.append(exc)

    thread = threading.Thread(target=run_worker, daemon=True)
    thread.start()
    protocol = worker.step_protocol
    try:
        reset_length, = struct.unpack("<I", _recv_exact(cosim_peer, 4))
        reset = protocol.decode_step_frame(
            struct.pack("<I", reset_length) +
            _recv_exact(cosim_peer, reset_length))
        assert reset.header.kind == protocol.RESET
        # Queue a literal DM disable before releasing the RESET_ACK barrier.
        can_peer.sendall(struct.pack(
            "<IIB3xQ64s", 1, 0, 8, 10_000_000_000,
            bytes.fromhex("ff ff ff ff ff ff ff fd")))
        cosim_peer.sendall(protocol.build_step_frame(
            protocol.RESET_ACK, 0, 0, flags=reset.header.session_id,
            payload=struct.pack("<IIQII", 0, protocol.CAPABILITY_MASK,
                                10_000_000_000, 0, 0)))
        step_length, = struct.unpack("<I", _recv_exact(cosim_peer, 4))
        step = protocol.decode_step_frame(
            struct.pack("<I", step_length) +
            _recv_exact(cosim_peer, step_length))
        assert (step.header.kind, step.header.t_sim_ns,
                step.header.dt_ns) == (protocol.STEP, 1_000_000, 1_000_000)
        cosim_peer.sendall(protocol.build_step_frame(
            protocol.STEP_ACK, 1, 1_000_000, dt_ns=1_000_000,
            flags=reset.header.session_id,
            payload=struct.pack("<IIII", 0, 0, 0, 0)))
        feedback = struct.unpack("<IIB3xQ64s", _recv_exact(can_peer, 84))
        thread.join(timeout=3.0)
        assert not thread.is_alive()
        assert outcome == [0]
        assert feedback[:4] == (0x11, 0, 8, 10_001_000_000)
        assert feedback[4] == bytes.fromhex("01 80 00 80 08 00 19 19") + \
            bytes(56)
    finally:
        for sock in (cosim_peer, can_peer, cosim_worker, can_worker):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        thread.join(timeout=3.0)
        assert not thread.is_alive(), "worker thread did not stop on disconnect"
