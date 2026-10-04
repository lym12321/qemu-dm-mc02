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
