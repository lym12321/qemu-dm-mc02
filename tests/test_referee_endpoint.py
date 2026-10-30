"""Borrowed byte transport and referee output; no socket/core/codec replacement."""

from dataclasses import replace
from pathlib import Path
import socket
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from types import SimpleNamespace
from sim.api import NodeDescription, NodeSpec, Session, Topology
from sim.runtime import Simulation
from sim.devices.referee import (
    Buff, GameStatus, Hit, MatchDurations, PowerHeat, RefereeEngine, RefereeState,
    RefereeUartEndpoint, RobotStatus, ScriptEvent, Shot, decode_frame,
)
from sim.transports import StreamTransport


STATE = RefereeState(GameStatus(1, 0, 0, 0x0102030405060708),
                     RobotStatus(3, 2, 100, 200, 3, 100, 80, 7),
                     PowerHeat(40, 10, 20), Buff(0, 0, 0, 0, 0, 0x80))
# Fixed 2026 V1.1.0 payload layouts, previously isolated CRC algorithms, seq wrap.
# Literal bytes were calculated directly with pinned crccheck, without the
# production frame/message encoder. The endpoint is the consumer under test.
WIRE = bytes.fromhex(
    "a50b00fe690100010000080706050403020138e1"
    "a50d00ffe6010203026400c80003006400500007ee23"
    "a50e0000370202000000000000000028000a00140071bd"
    "a5080001b8040200000000000000806ba9"
)
PAYLOADS = (
    (0x0001, "0100000807060504030201"),
    (0x0201, "03026400c80003006400500007"),
    (0x0202, "000000000000000028000a001400"),
    (0x0204, "0000000000000080"),
)


class SendBudget:
    """Delegate actual socket IO; inject only short-write budgets and failures."""

    def __init__(self, sock):
        self.sock = sock
        self.budget = None
        self.error = None
        self.sends = 0
        self.reads = 0
        self.closes = 0

    def send(self, data):
        self.sends += 1
        if self.error is not None:
            raise self.error
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


@pytest.fixture
def pair():
    sender, peer = socket.socketpair()
    sender.setblocking(False)
    peer.setblocking(False)
    try:
        yield SendBudget(sender), peer
    finally:
        sender.close()
        peer.close()


def received(peer):
    output = bytearray()
    while True:
        try:
            part = peer.recv(4096)
        except BlockingIOError:
            return bytes(output)
        assert part, "unexpected peer EOF"
        output.extend(part)


def frames(wire):
    """Split the official complete envelope using its literal length field."""
    result = []
    while wire:
        assert wire[0] == 0xa5
        size = int.from_bytes(wire[1:3], "little") + 9
        assert len(wire) >= size
        result.append(decode_frame(wire[:size]))
        wire = wire[size:]
    return result


def test_socketpair_receives_four_literal_frames_and_wraps_sequence(pair):
    sock, peer = pair
    stream = StreamTransport(sock)
    stream.buffer.extend(b"already-read")
    peer.send(b"unread")
    endpoint = RefereeUartEndpoint(stream, sequence=254)
    assert endpoint.send_state(STATE) == WIRE
    assert endpoint.next_sequence == 2
    assert stream.flush() is True
    assert not stream.tx_pending
    wire = received(peer)
    assert wire == WIRE
    assert len(wire) == 82
    decoded = frames(wire)
    assert [frame.sequence for frame in decoded] == [254, 255, 0, 1]
    assert [(frame.command_id, frame.payload) for frame in decoded] == [
        (command, bytes.fromhex(payload)) for command, payload in PAYLOADS]
    assert sock.reads == 0
    assert stream.buffer == b"already-read"
    stream.read_once()  # RX remains the transport caller's responsibility.
    assert stream.buffer == b"already-readunread"
    with pytest.raises(AttributeError):
        endpoint.next_sequence = 3


class DerivedState(RefereeState):
    pass


class DerivedBuff(Buff):
    pass


@pytest.mark.parametrize("state", [
    None, DerivedState(STATE.game, STATE.robot, STATE.power_heat, STATE.buff),
    replace(STATE, game=STATE.buff),
    replace(STATE, buff=DerivedBuff(0, 0, 0, 0, 0, 0)),
    replace(STATE, game=replace(STATE.game, stage_remain_time=65536)),
    replace(STATE, robot=replace(STATE.robot, current_hp=True)),
    replace(STATE, power_heat=replace(STATE.power_heat, heat_42mm=-1)),
    replace(STATE, buff=replace(STATE.buff, remaining_energy=256)),
])
def test_all_snapshot_admission_precedes_io_even_with_writable_old_queue(pair, state):
    sock, peer = pair
    sock.budget = 0
    stream = StreamTransport(sock, max_pending=1)
    stream.send(b"older")
    sock.budget = None
    endpoint = RefereeUartEndpoint(stream, sequence=254)
    sends = sock.sends
    with pytest.raises((TypeError, ValueError)):
        endpoint.send_state(state)
    assert sock.sends == sends
    assert endpoint.next_sequence == 254
    assert [bytes(view) for view in stream.tx_queue] == [b"older"]
    assert received(peer) == b""
    assert endpoint.send_state(STATE) == WIRE  # Pure admission failure is retryable.
    assert received(peer) == b"older" + WIRE


@pytest.mark.parametrize("sequence", [True, -1, 256, 1.0])
def test_sequence_admission_before_io_in_constructor_and_reset(pair, sequence):
    sock, peer = pair
    stream = StreamTransport(sock)
    with pytest.raises((TypeError, ValueError)):
        RefereeUartEndpoint(stream, sequence=sequence)
    endpoint = RefereeUartEndpoint(stream, sequence=254)
    with pytest.raises((TypeError, ValueError)):
        endpoint.reset(sequence)
    assert endpoint.next_sequence == 254
    assert sock.sends == sock.reads == sock.closes == 0
    assert received(peer) == b""


def test_borrowed_sender_needs_only_complete_batch_admission():
    batches = []
    sender = SimpleNamespace(send=batches.append)
    endpoint = RefereeUartEndpoint(sender, sequence=254)
    assert endpoint.send_state(STATE) == WIRE
    assert batches == [WIRE] and endpoint.next_sequence == 2
    assert not hasattr(endpoint, "flush")
    endpoint.close()
    assert batches == [WIRE]
    for invalid in (object(), SimpleNamespace(send=None)):
        with pytest.raises(TypeError):
            RefereeUartEndpoint(invalid)


def test_capacity_one_admits_whole_batch_and_refusal_does_not_commit_new_batch(pair):
    sock, peer = pair
    sock.budget = 0
    stream = StreamTransport(sock, max_pending=1)
    endpoint = RefereeUartEndpoint(stream, sequence=254)
    assert endpoint.send_state(STATE) == WIRE
    assert endpoint.next_sequence == 2
    assert [bytes(view) for view in stream.tx_queue] == [WIRE]
    sock.budget = 5
    with pytest.raises(BufferError):
        endpoint.send_state(STATE)
    assert endpoint.next_sequence == 2
    assert [bytes(view) for view in stream.tx_queue] == [WIRE[5:]]
    assert received(peer) == WIRE[:5]  # Capacity check may progress only old bytes.
    sock.budget = None
    assert stream.flush() is True
    assert received(peer) == WIRE[5:]
    new = endpoint.send_state(STATE)
    assert endpoint.next_sequence == 6
    assert received(peer) == new
    assert [frame.sequence for frame in frames(new)] == [2, 3, 4, 5]


def test_logical_reset_preserves_exact_partial_suffix_and_already_queued_batch(pair):
    sock, peer = pair
    sock.budget = 5
    stream = StreamTransport(sock, max_pending=2)
    endpoint = RefereeUartEndpoint(stream, sequence=254)
    assert endpoint.send_state(STATE) == WIRE
    assert received(peer) == WIRE[:5]
    second = endpoint.send_state(STATE)
    before = [bytes(view) for view in stream.tx_queue]
    assert before == [WIRE[5:], second]
    sends = sock.sends
    endpoint.reset(sequence=254)
    assert endpoint.next_sequence == 254
    assert sock.sends == sends
    assert [bytes(view) for view in stream.tx_queue] == before
    assert stream.flush() is False
    sock.budget = None
    assert stream.flush() is True
    assert received(peer) == WIRE[5:] + second
    assert endpoint.next_sequence == 254
    assert endpoint.send_state(STATE) == WIRE
    assert received(peer) == WIRE


@pytest.mark.parametrize("error", [OSError("controlled send failure"), KeyboardInterrupt()])
def test_send_failure_preserves_primary_and_freezes_without_replay_until_reset(pair, error):
    sock, peer = pair
    stream = StreamTransport(sock, max_pending=1)
    endpoint = RefereeUartEndpoint(stream, sequence=254)
    sock.error = error
    with pytest.raises(type(error)) as raised:
        endpoint.send_state(STATE)
    assert raised.value is error
    assert endpoint.next_sequence == 254
    assert [bytes(view) for view in stream.tx_queue] == [WIRE]
    sends = sock.sends
    with pytest.raises(RuntimeError):
        endpoint.send_state(STATE)
    with pytest.raises((TypeError, ValueError)):
        endpoint.reset(False)
    assert sock.sends == sends
    endpoint.reset(sequence=37)
    assert sock.sends == sends
    sock.error, sock.budget = None, None
    assert stream.flush() is True
    assert received(peer) == WIRE  # Caller drains physical bytes, without replay.
    assert endpoint.next_sequence == 37
    endpoint.send_state(STATE)
    assert [frame.sequence for frame in frames(received(peer))] == [37, 38, 39, 40]
    assert endpoint.next_sequence == 41


def test_close_does_not_flush_or_close_borrowed_stream_and_getter_remains_readable(pair):
    sock, peer = pair
    sock.budget = 0
    stream = StreamTransport(sock)
    endpoint = RefereeUartEndpoint(stream, sequence=254)
    endpoint.send_state(STATE)
    sends = sock.sends
    endpoint.close()
    endpoint.close()
    assert endpoint.next_sequence == 2
    assert sock.sends == sends
    assert sock.closes == 0
    for action in (lambda: endpoint.send_state(STATE), endpoint.reset):
        with pytest.raises(RuntimeError):
            action()
    assert [bytes(view) for view in stream.tx_queue] == [WIRE]
    sock.budget = None
    assert stream.flush() is True  # Caller retains lifetime and drain ownership.
    assert received(peer) == WIRE
    stream.send(b"caller")
    assert received(peer) == b"caller"


def test_completed_engine_snapshot_directly_consumed_over_existing_transport(pair):
    sock, peer = pair
    engine = RefereeEngine(STATE, durations=MatchDurations(3, 4),
                           cooling_42mm_per_s=2,
                           events=(ScriptEvent(500_000_000, Hit(30)),
                                   ScriptEvent(500_000_000, Shot(5, 7))))
    factory = SimpleNamespace(describe=lambda spec: NodeDescription(capabilities={"step"}),
                              create=lambda spec, context: engine)
    coordinator = Simulation(Topology((NodeSpec("referee", "fixture"),)), {"fixture": factory},
                             initial_session=Session(7, 100))
    coordinator.start()
    coordinator.step(1_000_000_000)
    state = engine.get_state()
    endpoint = RefereeUartEndpoint(StreamTransport(sock), sequence=10)
    endpoint.send_state(state)
    decoded = frames(received(peer))
    assert [frame.sequence for frame in decoded] == [10, 11, 12, 13]
    assert [(frame.command_id, frame.payload.hex()) for frame in decoded] == [
        (0x0001, "0100000807060504030201"),
        (0x0201, "03024600c80003006400500007"),  # Explicit HP 100-30.
        (0x0202, "000000000000000028000c001900"),  # 10-3+5, 20-2+7.
        (0x0204, "0000000000000080"),
    ]
    assert engine.get_state() is state
    assert engine.t_sim_ns == 1_000_000_100
    endpoint.close()
    coordinator.close()
