"""Virtual delivery contract and retained Linux/fixed84 codec boundaries.

Ordering/delay/drop are deterministic host semantics, not physical CAN timing.
Literal record fields use Linux include/uapi/linux/can.h and the existing
fixed84 public chardev format; no hardware validation is claimed here.
"""

import importlib.util
from pathlib import Path
import sys

import pytest

from sim.fabric import CanBus, CanFrame, VirtualCanDevice
from sim.transports import can_codec as codec


class Sink:
    def __init__(self, name, order=None):
        self.name = name
        self.frames = []
        self.order = order

    # Endpoint registration must use identity, including equal/unhashable objects.
    def __eq__(self, other):
        return isinstance(other, Sink)

    def receive(self, frame):
        self.frames.append(frame)
        if self.order is not None:
            self.order.append((self.name, frame.can_id, frame.timestamp_ns))


def test_due_order_stable_ties_and_attachment_order():
    bus = CanBus(delay_ns=3)
    order = []
    source, first, second = (Sink(name, order)
                             for name in ("source", "first", "second"))
    for sink in (source, first, second):
        bus.attach(sink, sink.name)
    for can_id, timestamp in ((0x103, 7), (0x101, 2), (0x102, 2)):
        bus.send(source, CanFrame(can_id, timestamp_ns=timestamp))
    assert bus.pending == 3
    assert not bus.has_due(4)
    assert bus.has_due(5)
    assert bus.step(4) == 0
    assert bus.step(5) == 2
    assert order == [("first", 0x101, 5), ("second", 0x101, 5),
                     ("first", 0x102, 5), ("second", 0x102, 5)]
    assert bus.step(10) == 1
    assert order[-2:] == [("first", 0x103, 10), ("second", 0x103, 10)]
    assert source.frames == []
    assert bus.pending == 0
    assert not bus.has_due(10)


def test_delay_changes_only_subsequent_due_times_and_preserves_tie_order():
    events = []
    bus = CanBus(delay_ns=20, log=events.append)
    bus.delay_ns = 10
    source, sink = Sink("source"), Sink("sink")
    # Policy assignment alone must not freeze wiring or start delivery.
    bus.attach(source)
    bus.attach(sink)
    old = CanFrame(1, timestamp_ns=5)
    bus.send(source, old)  # due 15, sequence 0
    assert bus.step(4) == 0
    bus.delay_ns = 0
    assert (bus.delay_ns, bus.pending, bus.t_sim_ns) == (0, 1, 4)
    assert events == sink.frames == []
    overtaking = CanFrame(2, timestamp_ns=6)
    tied = CanFrame(3, timestamp_ns=15)
    bus.send(source, overtaking)
    bus.send(source, tied)
    bus.delay_ns = 20
    increased = CanFrame(4, timestamp_ns=0)
    bus.send(source, increased)
    assert not bus.has_due(5)
    assert bus.step(6) == 1
    assert sink.frames == [CanFrame(2, timestamp_ns=6)]
    assert bus.step(14) == 0
    assert bus.step(15) == 2
    assert [frame.can_id for frame in sink.frames] == [2, 1, 3]
    assert bus.pending == 1
    assert not bus.has_due(19)
    assert bus.step(20) == 1
    assert [(event.frame.can_id, event.sequence, event.timestamp_ns,
             event.due_ns, event.delivered_ns) for event in events] == [
        (2, 1, 6, 6, 6), (1, 0, 5, 15, 15),
        (3, 2, 15, 15, 15), (4, 3, 0, 20, 20)]
    assert events[1].frame is old
    assert bus.pending == 0


@pytest.mark.parametrize("value", [-1, True, 1.5, 1 << 64])
def test_invalid_delay_assignment_preserves_policy_and_pending_admission(value):
    events = []
    bus = CanBus(delay_ns=7, log=events.append)
    source, sink = Sink("source"), Sink("sink")
    bus.attach(source)
    bus.attach(sink)
    bus.step(3)
    original = CanFrame(1, timestamp_ns=4)
    bus.send(source, original)
    with pytest.raises(ValueError):
        bus.delay_ns = value
    assert (bus.delay_ns, bus.pending, bus.t_sim_ns) == (7, 1, 3)
    assert events == source.frames == sink.frames == []
    assert not bus.has_due(10)
    bus.send(source, CanFrame(2, timestamp_ns=4))
    assert bus.step(11) == 2
    assert [(event.sequence, event.due_ns) for event in events] == [(0, 11), (1, 11)]
    assert events[0].frame is original
    assert [frame.can_id for frame in sink.frames] == [1, 2]


def test_max_delay_is_valid_but_overflowing_send_is_atomic():
    events = []
    bus = CanBus(log=events.append)
    source, sink = Sink("source"), Sink("sink")
    bus.attach(source)
    bus.attach(sink)
    bus.delay_ns = (1 << 64) - 1
    assert bus.delay_ns == 18446744073709551615
    with pytest.raises(ValueError, match="64-bit"):
        bus.send(source, CanFrame(1, timestamp_ns=1))
    assert (bus.pending, bus.t_sim_ns) == (0, 0)
    assert events == sink.frames == []
    bus.send(source, CanFrame(2))
    bus.delay_ns = 0
    bus.send(source, CanFrame(3, timestamp_ns=1))
    assert bus.step(1) == 1
    assert bus.pending == 1
    assert bus.step(18446744073709551615) == 1
    assert [(event.frame.can_id, event.sequence, event.due_ns)
            for event in events] == [(3, 1, 1), (2, 0, 18446744073709551615)]


def test_destination_drop_log_and_late_metadata_preserve_original():
    events, decisions = [], []
    source, keep, discard = (Sink(name) for name in ("source", "keep", "discard"))

    def drop(src, dst, frame):
        decisions.append((src, dst, frame))
        return dst is discard

    bus = CanBus(delay_ns=2, drop=drop, log=events.append)
    for sink in (source, keep, discard):
        bus.attach(sink, sink.name)
    bus.step(20)
    original = CanFrame(0x201, data=b"late", timestamp_ns=5)
    bus.send(source, original)
    assert bus.step(20) == 1
    assert bus.t_sim_ns == 20
    assert keep.frames == [CanFrame(0x201, data=b"late", timestamp_ns=20)]
    assert discard.frames == []
    assert decisions[0][0] is source
    assert decisions[0][1] is keep
    assert decisions[1][1] is discard
    assert all(item[2] is original for item in decisions)
    assert [(event.source, event.destination, event.sequence,
             event.timestamp_ns, event.due_ns, event.delivered_ns,
             event.late, event.dropped) for event in events] == [
        ("source", "keep", 0, 5, 7, 20, True, False),
        ("source", "discard", 0, 5, 7, 20, True, True)]
    assert all(event.frame is original for event in events)
    with pytest.raises(ValueError, match="must not decrease"):
        bus.step(19)
    assert bus.t_sim_ns == 20


def test_loopback_is_explicit():
    source = Sink("source")
    bus = CanBus(loopback=True)
    bus.attach(source)
    original = CanFrame(1, data=b"x")
    bus.send(source, original)
    assert bus.step(0) == 1
    assert source.frames == [original]


class Echo(VirtualCanDevice):
    def __init__(self, bus, name):
        self.seen = []
        super().__init__(bus, name)

    def receive(self, frame):
        self.seen.append(frame.can_id)
        self.emit(CanFrame(frame.can_id + 1, timestamp_ns=frame.timestamp_ns))


def test_recursive_emits_share_bounded_dispatch_budget():
    bus = CanBus(max_pending=1, dispatch_budget=3)
    first, second = Echo(bus, "first"), Echo(bus, "second")
    first.emit(CanFrame(1, timestamp_ns=5))
    assert bus.step(5) == 3
    assert first.seen == [2]
    assert second.seen == [1, 3]
    assert bus.pending == 1
    # A second settle at the same target is valid and bounded independently.
    assert bus.step(5) == 3
    assert first.seen == [2, 4, 6]
    assert second.seen == [1, 3, 5]
    assert bus.pending == 1


def test_capacity_rejection_preserves_existing_pending_frame():
    bus = CanBus(max_pending=1)
    source, sink = Sink("source"), Sink("sink")
    bus.attach(source)
    bus.attach(sink)
    first = CanFrame(1)
    bus.send(source, first)
    with pytest.raises(BufferError, match="capacity exhausted"):
        bus.send(source, CanFrame(2))
    assert bus.pending == 1
    assert bus.step(0) == 1
    assert sink.frames == [first]


def test_reset_clears_pending_clock_sequence_and_retains_wiring_and_delay():
    events = []
    bus = CanBus(log=events.append)
    source, sink = Sink("source"), Sink("sink")
    bus.attach(source, source.name)
    bus.attach(sink, sink.name)
    bus.delay_ns = 3
    bus.send(source, CanFrame(1, timestamp_ns=100))
    bus.step(20)
    with pytest.raises(ValueError):
        bus.reset(-1)
    assert bus.pending == 1
    assert bus.t_sim_ns == 20
    bus.reset(5)
    assert bus.pending == 0
    assert bus.t_sim_ns == 5
    assert bus.delay_ns == 3
    with pytest.raises(RuntimeError, match="before the first send or step"):
        bus.attach(Sink("late"))
    bus.send(source, CanFrame(2, timestamp_ns=6))
    assert bus.step(8) == 0
    assert bus.step(9) == 1
    assert [frame.can_id for frame in sink.frames] == [2]
    assert events[0].sequence == 0
    assert events[0].due_ns == 9


@pytest.mark.parametrize("kwargs", [
    {"delay_ns": -1}, {"delay_ns": 1.5}, {"delay_ns": True},
    {"delay_ns": 1 << 64}, {"loopback": 1}, {"drop": []}, {"log": 3},
    {"max_pending": 0}, {"max_pending": False}, {"dispatch_budget": 0},
    {"dispatch_budget": 1.5},
])
def test_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        CanBus(**kwargs)


def test_registration_and_send_admission_have_no_partial_effects():
    bus = CanBus(delay_ns=1)
    source = Sink("source")
    with pytest.raises(ValueError, match="callable receive"):
        bus.attach(object())
    with pytest.raises(ValueError):
        bus.step(-1)
    bus.attach(source, "source")
    with pytest.raises(ValueError, match="already attached"):
        bus.attach(source)
    with pytest.raises(ValueError, match="name is already"):
        bus.attach(Sink("other"), "source")
    with pytest.raises(ValueError, match="not attached"):
        bus.send(Sink("equal-but-unattached"), CanFrame(1))
    with pytest.raises(ValueError, match="requires a CanFrame"):
        bus.send(source, b"bad")
    with pytest.raises(ValueError, match="64-bit"):
        bus.send(source, CanFrame(1, timestamp_ns=(1 << 64) - 1))
    assert bus.pending == 0
    # Failed admission has not frozen wiring.
    bus.attach(Sink("sink"))
    bus.send(source, CanFrame(1))
    with pytest.raises(RuntimeError):
        bus.attach(Sink("too-late"))


@pytest.mark.parametrize("failed_callback", ["drop", "log", "receive"])
def test_callback_failure_propagates_and_retains_remaining_queue(failed_callback):
    def fail(*args):
        raise OSError("callback failed")

    bus = CanBus(**({failed_callback: fail} if failed_callback != "receive" else {}))
    source, sink = Sink("source"), Sink("sink")
    if failed_callback == "receive":
        sink.receive = fail
    bus.attach(source)
    bus.attach(sink)
    bus.send(source, CanFrame(1))
    bus.send(source, CanFrame(2))
    with pytest.raises(OSError, match="callback failed"):
        bus.step(0)
    assert bus.pending == 1


@pytest.mark.parametrize("operation", ["step", "reset", "delay"])
def test_callbacks_cannot_step_reset_or_change_delay_mid_delivery(operation):
    events = []
    bus = CanBus(dispatch_budget=1, log=events.append)
    source, sink = Sink("source"), Sink("sink")
    if operation == "delay":
        sink.receive = lambda frame: setattr(bus, "delay_ns", 5)
    else:
        sink.receive = lambda frame: getattr(bus, operation)(0)
    bus.attach(source)
    bus.attach(sink)
    bus.send(source, CanFrame(1))
    bus.send(source, CanFrame(2))
    with pytest.raises(RuntimeError, match="delivery callback"):
        bus.step(0)
    # The current broadcast was already popped; the remaining one is retained.
    assert (bus.delay_ns, bus.pending, bus.t_sim_ns) == (0, 1, 0)
    assert [(event.sequence, event.due_ns) for event in events] == [(0, 0)]
    assert sink.frames == []


def test_tool_importlib_consumers_share_canonical_frame_and_codec_objects():
    path = Path(__file__).resolve().parents[1] / "tools/dm_mc02_socketcan.py"
    spec = importlib.util.spec_from_file_location("socketcan_fabric_compat", path)
    assert spec is not None and spec.loader is not None
    legacy = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = legacy
    try:
        spec.loader.exec_module(legacy)
        assert legacy.CanFrame is CanFrame
        for name in ("pack_wire_frame", "unpack_wire_frame", "pack_can_frame",
                     "unpack_can_frame", "dlc_to_length", "length_to_dlc",
                     "_CAN_STRUCT", "_CANFD_STRUCT", "_WIRE_STRUCT", "_FD_DLC_LENGTHS"):
            assert getattr(legacy, name) is getattr(codec, name)
    finally:
        sys.modules.pop(spec.name, None)


def test_literal_classic_and_fd_records_keep_field_and_padding_bytes():
    classic = CanFrame(0x123, data=b"12345678", timestamp_ns=17)
    wire = bytes.fromhex("23010000 00000000 08 000000 1100000000000000")
    wire += b"12345678" + bytes(56)
    native = bytes.fromhex("23010000 08 000000") + b"12345678"
    assert codec.pack_wire_frame(classic) == wire
    assert codec.unpack_wire_frame(wire) == classic
    assert codec.pack_can_frame(classic) == native
    assert codec.unpack_can_frame(native, 17) == classic

    fd = CanFrame(0x1ABCDE, codec.WIRE_EXTENDED | codec.WIRE_FD | codec.WIRE_BRS,
                  data=bytes(range(12)), timestamp_ns=9)
    wire = bytes.fromhex("debc1a00 0d000000 09 000000 0900000000000000")
    wire += bytes(range(12)) + bytes(52)
    native = bytes.fromhex("debc1a80 0c 05 0000") + bytes(range(12)) + bytes(52)
    assert codec.pack_wire_frame(fd) == wire
    assert codec.unpack_wire_frame(wire) == fd
    assert codec.pack_can_frame(fd) == native
    assert codec.unpack_can_frame(native, 9) == fd
