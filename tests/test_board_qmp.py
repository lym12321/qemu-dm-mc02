"""Host adapter admission; native GPIO/QMP semantics have separate qtests."""

from collections import Counter
from copy import deepcopy
from pathlib import Path
import socket
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.boards import (
    QemuBoard, drain_paused_can, validate_gpio_pin, wait_paused_can_input,
)
from sim.transports.can_stream import CanStream


def native_state():
    # Literal native schema 1, including intentionally distinct host key input
    # and sampled level. Expected values do not call the production validator.
    return {
        "schema": 1, "timestamp_ns": 123456789, "board": "DM-MC02",
        "vin_mv": 24000,
        "gpio": {
            "A": {"moder": 0x4000, "sampled": 0x80, "odr": 0x80,
                  "output_mask": 0x80},
            "B": {"moder": 0x1e4, "sampled": 0x0f, "odr": 2,
                  "output_mask": 0x12},
            "C": {"moder": 0, "sampled": 0, "odr": 0, "output_mask": 0},
            "D": {"moder": 0, "sampled": 0, "odr": 0, "output_mask": 0},
            "E": {"moder": 0, "sampled": 0, "odr": 0, "output_mask": 0},
            "F": {"moder": 0, "sampled": 0, "odr": 0, "output_mask": 0},
            "G": {"moder": 0, "sampled": 0, "odr": 0, "output_mask": 0},
            "H": {"moder": 0, "sampled": 0, "odr": 0, "output_mask": 0},
        },
        "led": {"pin": "A7", "rgb": 0xffffff, "brightness": 255,
                "source": "gpio-latch"},
        "key": {"pin": "A15", "pressed": False, "level": 0},
        "extension": {"native": True},
    }


class Qmp:
    def __init__(self, value=None):
        self.value = native_state() if value is None else value
        self.commands = []
        self.closed = False

    def command(self, name, arguments=None):
        self.commands.append((name, arguments))
        return self.value

    def close(self):
        self.closed = True


def test_literal_snapshot_and_digital_aliases_preserve_native_fields():
    qmp = Qmp()
    board = QemuBoard(qmp)
    before = deepcopy(qmp.value)
    state = board.snapshot()
    assert state is qmp.value
    assert state == before
    assert qmp.commands == [("qom-get", {
        "path": "/machine", "property": "board-state",
    })]
    assert board.digital("gpio.B0", state) == 1
    assert board.digital("gpio.B4", state) == 0
    assert board.digital("key", state) == 0
    assert board.digital("led", state) == 1
    state["led"].update(rgb=0, brightness=0, source="ws2812-dma")
    assert board.digital("led", state) == 0
    assert len(qmp.commands) == 1
    assert not qmp.closed


@pytest.mark.parametrize("method,args", [
    ("set_gpio", ("I0", 1)), ("set_gpio", ("A16", 1)),
    ("set_gpio", ("A01", 1)), ("set_gpio", ("a1", 1)),
    ("set_gpio", ("A0", True)), ("set_gpio", ("A0", 2)),
    ("set_key", (1,)), ("set_vin", (-1,)),
    ("set_vin", (0x100000000,)), ("set_vin", (True,)),
    ("digital", ("gpio.H16",)), ("digital", ("gpio.B0.extra",)),
    ("digital", ("register.ODR",)), ("digital", (0,)),
])
def test_input_admission_precedes_qmp_command(method, args):
    qmp = Qmp()
    board = QemuBoard(qmp)
    with pytest.raises((TypeError, ValueError)):
        getattr(board, method)(*args)
    assert qmp.commands == []


def test_native_producer_values_and_borrowed_connection():
    qmp = Qmp()
    board = QemuBoard(qmp)
    assert validate_gpio_pin("H15") == "H15"
    board.set_gpio("H15", 1)
    board.set_key(True)
    board.set_vin(0xffffffff)
    board.pause()
    board.resume()
    board.reset()
    assert qmp.commands == [
        ("qom-set", {"path": "/machine", "property": "gpio-input",
                     "value": "H15=1"}),
        ("qom-set", {"path": "/machine", "property": "user-key", "value": True}),
        ("qom-set", {"path": "/machine", "property": "vin-mv", "value": "4294967295"}),
        ("stop", None), ("cont", None), ("system_reset", None),
    ]
    del board
    assert not qmp.closed


@pytest.mark.parametrize("path,value", [
    (("schema",), True), (("schema",), 2), (("timestamp_ns",), -1),
    (("timestamp_ns",), 0.0), (("vin_mv",), 0x100000000),
    (("gpio", "B", "moder"), 0x100000000),
    (("gpio", "B", "sampled"), True),
    (("gpio", "B", "odr"), -1),
    (("gpio", "B", "output_mask"), 0x10000),
    (("led", "rgb"), 0x1000000), (("led", "brightness"), 256),
    (("led", "source"), "electrical"), (("led", "pin"), "I0"),
    (("key", "pin"), "H16"), (("key", "pressed"), 1),
    (("key", "level"), True), (("key", "level"), 2),
])
def test_malformed_native_values_are_rejected(path, value):
    state = native_state()
    container = state
    for part in path[:-1]:
        container = container[part]
    container[path[-1]] = value
    with pytest.raises((TypeError, ValueError)):
        QemuBoard(Qmp(state)).snapshot()


def test_missing_native_field_and_supplied_snapshot_are_validated():
    qmp = Qmp()
    board = QemuBoard(qmp)
    state = native_state()
    del state["gpio"]["B"]["sampled"]
    with pytest.raises(ValueError, match="gpio.B.sampled"):
        board.digital("gpio.B0", state)
    assert qmp.commands == []


@pytest.mark.parametrize("port", ["H", "I"])
def test_native_gpio_geometry_requires_all_eight_banks(port):
    state = native_state()
    if port == "H":
        del state["gpio"][port]
    else:
        state["gpio"][port] = {}
    with pytest.raises(ValueError, match="exactly banks A through H"):
        QemuBoard(Qmp(state)).snapshot()


def test_qmp_errors_propagate_without_transport_cleanup():
    failure = RuntimeError("native QMP rejection")

    class FailedQmp(Qmp):
        def command(self, name, arguments=None):
            raise failure

    qmp = FailedQmp()
    board = QemuBoard(qmp)
    with pytest.raises(RuntimeError) as caught:
        board.snapshot()
    assert caught.value is failure
    with pytest.raises(RuntimeError) as caught:
        board.set_key(False)
    assert caught.value is failure
    assert not qmp.closed
    with pytest.raises(TypeError, match="callable"):
        QemuBoard(object())


# Independent fixed <IIB3xQ64s record: ID0x200, DLC8, timestamp1000ns.
CAN_LITERAL = bytes.fromhex(
    "00020000 00000000 08000000 e803000000000000 1122334455667788"
) + bytes(56)
CAN_DECODED = (0x200, 0, 8, 1000, bytes.fromhex("1122334455667788"))


class PausedCanQmp:
    """Script native replies; socket bytes are always parsed by CanStream."""

    def __init__(self):
        self.execution = {"state": "reached", "current-ns": 1000,
                          "target-ns": 1000}
        self.status = {"running": False, "status": "paused"}
        self.connected = True
        self.tx = {"schema": 1, "timestamp_ns": 1000, "controllers": {
            "dm-mc02.fdcan1": {"pending_frames": 0, "pending_bytes": 0,
                              "tx_dropped": 0, "tx_short_writes": 0},
        }}
        self.commands = []
        self.tx_queries = 0
        self.on_tx = lambda number: None
        self.rx = {"schema": 1, "timestamp_ns": 1000, "controllers": {
            "dm-mc02.fdcan1": {"completed_frames": 0, "partial_bytes": 0,
                              "rx_dropped": 0},
        }}
        self.rx_queries = 0
        self.on_rx = lambda number: None
        self.closed = False

    def command(self, name, arguments=None):
        self.commands.append((name, arguments))
        if name == "x-query-virtual-execution":
            return deepcopy(self.execution)
        if name == "query-status":
            return deepcopy(self.status)
        if arguments == {"path": "/chardevs/can1", "property": "connected"}:
            assert name == "qom-get"
            return self.connected
        if arguments == {"path": "/machine", "property": "fdcan-rx-state"}:
            assert name == "qom-get"
            self.rx_queries += 1
            self.on_rx(self.rx_queries)
            return deepcopy(self.rx)
        assert (name, arguments) == ("qom-get", {
            "path": "/machine", "property": "fdcan-tx-state",
        })
        self.tx_queries += 1
        self.on_tx(self.tx_queries)
        return deepcopy(self.tx)

    @property
    def controller(self):
        return self.tx["controllers"]["dm-mc02.fdcan1"]

    @property
    def rx_controller(self):
        return self.rx["controllers"]["dm-mc02.fdcan1"]


@pytest.fixture
def paused_can(monkeypatch):
    receiver, peer = socket.socketpair()
    receiver.setblocking(False)
    peer.setblocking(False)
    stream, qmp, clock = CanStream(receiver), PausedCanQmp(), [0.0]
    monkeypatch.setattr("sim.boards.qemu.time.monotonic", lambda: clock[0])
    try:
        yield stream, peer, qmp, clock
    finally:
        receiver.close()
        peer.close()


def run_paused_can(stream, qmp, sink, **overrides):
    options = dict(target_ns=1000, controller="dm-mc02.fdcan1", chardev="can1",
                   tx_dropped=0, deadline=1.0, check=lambda: None)
    options.update(overrides)
    return drain_paused_can(qmp, stream, sink, **options)


def assert_can_borrows_open(stream, qmp):
    assert stream.sock.fileno() >= 0
    assert not qmp.closed


def test_paused_can_rechecks_empty_after_producer_zero(paused_can):
    stream, peer, qmp, _ = paused_can
    seen = []
    qmp.controller.update(pending_frames=1, pending_bytes=84)

    def publish_after_first_empty(number):
        if number == 2:
            # First receive got EAGAIN with pending1. This zero observation
            # publishes bytes before its reply: that old empty is now stale.
            assert peer.send(CAN_LITERAL) == 84
            qmp.controller.update(pending_frames=0, pending_bytes=0)

    qmp.on_tx = publish_after_first_empty
    assert run_paused_can(stream, qmp, seen.append) is None
    assert seen == [CAN_DECODED]
    assert qmp.tx_queries >= 4  # Pending, zero+record, fresh empty, final state.
    assert not stream.buffer
    assert_can_borrows_open(stream, qmp)


def test_paused_can_keeps_fragment_until_actual_suffix(paused_can):
    stream, peer, qmp, _ = paused_can
    seen = []

    def fragmented(number):
        if number == 1:
            assert peer.send(CAN_LITERAL[:19]) == 19
        elif number == 2:
            assert bytes(stream.buffer) == CAN_LITERAL[:19]
            assert peer.send(CAN_LITERAL[19:]) == 65

    qmp.on_tx = fragmented
    run_paused_can(stream, qmp, seen.append)
    assert seen == [CAN_DECODED]
    assert not stream.buffer
    assert_can_borrows_open(stream, qmp)


def test_paused_can_streams_more_than_one_native_read(paused_can):
    stream, peer, qmp, _ = paused_can
    wire = bytearray()
    expected = []
    for index in range(60):
        record = bytearray(CAN_LITERAL)
        record[20] = index
        wire.extend(record)
        expected.append((0x200, 0, 8, 1000,
                         bytes([index]) + CAN_DECODED[4][1:]))
    assert len(wire) > 4096
    assert peer.send(wire) == len(wire)
    seen, epochs = [], []

    def sink(frame):
        seen.append(frame)
        epochs.append(qmp.tx_queries)

    run_paused_can(stream, qmp, sink)
    assert seen == expected
    assert len(set(epochs)) >= 2
    assert max(Counter(epochs).values()) <= 49  # One4096 read plus <=83 tail.
    assert all(epoch > 0 for epoch in epochs)
    assert not stream.buffer
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("failure", ["eof", "malformed", "stalled-tail"])
def test_paused_can_transport_failures_preserve_borrowed_state(paused_can, failure):
    stream, peer, qmp, clock = paused_can
    seen = []
    if failure == "eof":
        peer.close()
        expected = ConnectionError
    elif failure == "malformed":
        bad = bytearray(CAN_LITERAL)
        bad[9] = 1  # Canonical wire reserved-byte admission, independent literal.
        assert peer.send(bad + CAN_LITERAL) == 168
        expected = ValueError
    else:
        assert peer.send(CAN_LITERAL[:19]) == 19
        qmp.on_tx = lambda number: clock.__setitem__(0, 2.0) if number == 2 else None
        expected = TimeoutError
    with pytest.raises(expected):
        run_paused_can(stream, qmp, seen.append)
    assert not seen
    if failure == "malformed":
        assert bytes(stream.buffer) == CAN_LITERAL
    elif failure == "stalled-tail":
        assert bytes(stream.buffer) == CAN_LITERAL[:19]
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("section,path,value,error", [
    ("execution", ("state",), "cancelled", RuntimeError),
    ("execution", ("current-ns",), 1001, RuntimeError),
    ("execution", ("target-ns",), 1001, RuntimeError),
    ("status", ("running",), True, RuntimeError),
    ("connected", (), 1, ConnectionError),
    ("tx", ("schema",), 2, ValueError),
    ("tx", ("timestamp_ns",), 999, RuntimeError),
    ("controller", ("tx_dropped",), 1, RuntimeError),
    ("controller", ("tx_short_writes",), 1 << 64, ValueError),
    ("controller", ("pending_frames",), 17, ValueError),
    ("controller", ("pending_bytes",), 1, ValueError),
])
def test_paused_can_rejects_changed_native_observations_before_read(
        paused_can, section, path, value, error):
    stream, peer, qmp, _ = paused_can
    if path:
        getattr(qmp, section)[path[0]] = value
    else:
        setattr(qmp, section, value)
    assert peer.send(CAN_LITERAL) == 84
    with pytest.raises(error):
        run_paused_can(stream, qmp, lambda frame: pytest.fail("unexpected sink"))
    assert stream.sock.recv(84, socket.MSG_PEEK) == CAN_LITERAL
    assert not stream.buffer
    assert_can_borrows_open(stream, qmp)


def test_paused_can_rejects_impossible_positive_pending_counts(paused_can):
    stream, _, qmp, clock = paused_can
    # Two active records each retain at least one byte, independently of
    # their exact offsets. A phase limit prevents a bad admission hanging.
    qmp.controller.update(pending_frames=2, pending_bytes=1)
    qmp.on_tx = lambda number: clock.__setitem__(0, 2.0) if number == 2 else None
    with pytest.raises(ValueError, match="pending"):
        run_paused_can(stream, qmp, lambda frame: pytest.fail("unexpected sink"))
    assert qmp.tx_queries == 1
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("late_change", ["reset", "producer"])
def test_paused_can_validates_final_observation_after_fresh_empty(
        paused_can, late_change):
    stream, _, qmp, _ = paused_can

    def change(number):
        if late_change == "reset" and number == 1:
            qmp.execution["state"] = "cancelled"
        elif late_change == "producer" and number == 2:
            qmp.controller.update(pending_frames=1, pending_bytes=84)

    qmp.on_tx = change
    with pytest.raises(RuntimeError):
        run_paused_can(stream, qmp, lambda frame: pytest.fail("unexpected sink"))
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("dependency", [
    "qmp", "check", "sink", "slow-qmp", "slow-sink",
])
def test_paused_can_dependency_failures_propagate_without_cleanup(
        paused_can, dependency):
    stream, peer, qmp, clock = paused_can
    failure = RuntimeError(f"{dependency} failed")
    seen = []

    def fail():
        raise failure

    def sink(frame):
        seen.append(frame)
        if dependency == "sink":
            raise failure
        clock[0] = 2.0

    if dependency == "qmp":
        qmp.command = lambda *args: fail()
    elif dependency == "slow-qmp":
        qmp.on_tx = lambda number: clock.__setitem__(0, 2.0)
    if dependency in ("sink", "slow-sink"):
        assert peer.send(CAN_LITERAL + CAN_LITERAL[:19]) == 103
    with pytest.raises(TimeoutError if dependency.startswith("slow-") else RuntimeError) as caught:
        run_paused_can(stream, qmp, sink,
                       check=fail if dependency == "check" else lambda: None)
    if not dependency.startswith("slow-"):
        assert caught.value is failure
    if dependency in ("sink", "slow-sink"):
        assert seen == [CAN_DECODED]
        assert bytes(stream.buffer) == CAN_LITERAL[:19]
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("overrides", [
    {"target_ns": True}, {"tx_dropped": -1},
    {"deadline": float("inf")}, {"deadline": float("nan")},
    {"controller": ""}, {"chardev": "can1/other"}, {"check": None},
])
def test_paused_can_admission_precedes_native_io(paused_can, overrides):
    stream, peer, qmp, _ = paused_can
    assert peer.send(CAN_LITERAL) == 84
    with pytest.raises((TypeError, ValueError)):
        run_paused_can(stream, qmp, lambda frame: None, **overrides)
    assert qmp.commands == []
    assert stream.sock.recv(84, socket.MSG_PEEK) == CAN_LITERAL
    assert_can_borrows_open(stream, qmp)


def test_paused_can_requires_nonblocking_plain_stream(paused_can):
    stream, _, qmp, _ = paused_can
    stream.sock.setblocking(True)
    with pytest.raises(ValueError, match="nonblocking AF_UNIX"):
        run_paused_can(stream, qmp, lambda frame: None)
    assert qmp.commands == []
    assert_can_borrows_open(stream, qmp)

    receiver, peer = socket.socketpair(type=socket.SOCK_DGRAM)
    with receiver, peer:
        receiver.setblocking(False)
        datagram = CanStream(receiver)
        with pytest.raises(ValueError, match="nonblocking AF_UNIX"):
            run_paused_can(datagram, qmp, lambda frame: None)
        assert qmp.commands == []
        assert_can_borrows_open(datagram, qmp)


def run_paused_can_input(stream, qmp, **overrides):
    options = dict(target_ns=1000, controller="dm-mc02.fdcan1", chardev="can1",
                   completed_frames=1, deadline=1.0, check=lambda: None)
    options.update(overrides)
    return wait_paused_can_input(qmp, stream, **options)


def test_paused_can_input_waits_for_receipt_after_queued_socket_write(paused_can):
    stream, peer, qmp, _ = paused_can
    # Borrow the existing bounded TX queue; no substitute writer or socket.
    stream.tx_queue.append(memoryview(CAN_LITERAL))
    observed = bytearray()

    def dispatch(number):
        if number == 1:
            assert stream.tx_pending
            with pytest.raises(BlockingIOError):
                peer.recv(84)
        elif number == 2:
            # Kernel acceptance is already complete, while the native consumer
            # has only seen a partial record and must not report completion.
            assert not stream.tx_pending
            observed.extend(peer.recv(83))
            assert bytes(observed) == CAN_LITERAL[:83]
            qmp.rx_controller["partial_bytes"] = 83
        elif number == 3:
            observed.extend(peer.recv(1))
            assert bytes(observed) == CAN_LITERAL
            # Dispatch completion includes controller rejection/FIFO drops.
            qmp.rx_controller.update(completed_frames=1, partial_bytes=0,
                                     rx_dropped=1)
        elif number == 4:
            # This aggregate also includes non-chardev ingress. Its growth
            # remains diagnostic during the fresh final receipt observation.
            qmp.rx_controller["rx_dropped"] = 2

    qmp.on_rx = dispatch
    assert run_paused_can_input(stream, qmp) == {
        "completed_frames": 1, "partial_bytes": 0, "rx_dropped": 2,
    }
    assert qmp.rx_queries == 4  # Initial, partial, complete, fresh final.
    assert bytes(observed) == CAN_LITERAL
    assert not stream.tx_pending and not stream.buffer
    assert_can_borrows_open(stream, qmp)


def test_paused_can_input_accepts_absolute_goal_below_exhaustion(paused_can):
    stream, _, qmp, _ = paused_can
    goal = (1 << 64) - 2
    qmp.rx_controller.update(completed_frames=goal, rx_dropped=(1 << 64) - 1,
                             extension="retained")
    result = run_paused_can_input(stream, qmp, completed_frames=goal)
    assert result == {"completed_frames": goal, "partial_bytes": 0,
                      "rx_dropped": (1 << 64) - 1, "extension": "retained"}
    assert qmp.rx_queries == 2
    assert_can_borrows_open(stream, qmp)


def test_paused_can_input_keeps_actual_backpressure_suffix_until_peer_reads(paused_can):
    stream, peer, qmp, _ = paused_can
    stream.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
    for total in range(1, 129):
        stream.send(*CAN_DECODED)
        if stream.tx_pending:
            break
    else:
        pytest.fail("finite CAN prefill did not reach socket backpressure")
    expected = CAN_LITERAL * total
    suffix = b"".join(bytes(part) for part in stream.tx_queue)
    assert suffix and expected.endswith(suffix)
    consumed = bytearray()

    def dispatch(number):
        assert number <= 32, "bounded socketpair dispatch did not complete"
        if number <= 2:
            # First wait-loop flush meets the actual full host socket. Neither
            # its EAGAIN nor QMP polling may discard the retained TX suffix.
            assert b"".join(bytes(part) for part in stream.tx_queue) == suffix
        if number >= 2:
            try:
                consumed.extend(peer.recv(4096))
            except BlockingIOError:
                pass
            assert bytes(consumed) == expected[:len(consumed)]
            complete, partial = divmod(len(consumed), 84)
            qmp.rx_controller.update(completed_frames=complete,
                                     partial_bytes=partial)

    qmp.on_rx = dispatch
    result = run_paused_can_input(stream, qmp, completed_frames=total)
    assert bytes(consumed) == expected
    assert result == {"completed_frames": total, "partial_bytes": 0,
                      "rx_dropped": 0}
    assert qmp.rx_queries >= 4
    assert not stream.tx_pending
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("mismatch", ["queue", "partial"])
def test_paused_can_input_rejects_inconsistent_completed_goal(paused_can, mismatch):
    stream, peer, qmp, _ = paused_can
    if mismatch == "queue":
        stream.tx_queue.append(memoryview(CAN_LITERAL))
    else:
        qmp.rx_controller["partial_bytes"] = 1
    with pytest.raises(RuntimeError):
        run_paused_can_input(stream, qmp, completed_frames=0)
    assert [bytes(part) for part in stream.tx_queue] == (
        [CAN_LITERAL] if mismatch == "queue" else [])
    with pytest.raises(BlockingIOError):
        peer.recv(84)
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("goal", [0, 1])
def test_paused_can_input_original_eof_precedes_flush_or_success(paused_can, goal):
    stream, peer, qmp, _ = paused_can
    stream.tx_queue.append(memoryview(CAN_LITERAL[19:]))
    peer.close()
    assert qmp.connected is True  # A replacement may own native connected=true.
    with pytest.raises(ConnectionError):
        run_paused_can_input(stream, qmp, completed_frames=goal)
    assert [bytes(part) for part in stream.tx_queue] == [CAN_LITERAL[19:]]
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("buffered", [False, True])
def test_paused_can_input_leaves_unread_output_and_tail_intact(paused_can, buffered):
    stream, peer, qmp, _ = paused_can
    stream.tx_queue.append(memoryview(CAN_LITERAL))
    if buffered:
        stream.buffer.extend(CAN_LITERAL[:19])
    assert peer.send(CAN_LITERAL) == 84
    with pytest.raises(RuntimeError):
        run_paused_can_input(stream, qmp)
    assert stream.sock.recv(84, socket.MSG_PEEK) == CAN_LITERAL
    assert bytes(stream.buffer) == (CAN_LITERAL[:19] if buffered else b"")
    assert [bytes(part) for part in stream.tx_queue] == [CAN_LITERAL]
    with pytest.raises(BlockingIOError):
        peer.recv(84)
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("change", ["fence", "count", "partial", "eof", "output"])
def test_paused_can_input_reobserves_native_and_original_peer_before_return(
        paused_can, change):
    stream, peer, qmp, _ = paused_can
    qmp.rx_controller["completed_frames"] = 1

    def changed(number):
        if change == "fence" and number == 1:
            qmp.execution["state"] = "cancelled"
        if number == 2:
            if change == "count":
                qmp.rx_controller["completed_frames"] = 0
            elif change == "partial":
                qmp.rx_controller["partial_bytes"] = 1
            elif change == "eof":
                peer.close()
            elif change == "output":
                assert peer.send(CAN_LITERAL[:19]) == 19

    qmp.on_rx = changed
    with pytest.raises(ConnectionError if change == "eof" else RuntimeError):
        run_paused_can_input(stream, qmp)
    if change == "output":
        assert stream.sock.recv(84, socket.MSG_PEEK) == CAN_LITERAL[:19]
    assert not stream.buffer and not stream.tx_pending
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("section,field,value,error", [
    ("rx", "schema", 2, ValueError),
    ("rx", "timestamp_ns", 999, RuntimeError),
    ("rx_controller", "completed_frames", True, TypeError),
    ("rx_controller", "completed_frames", 2, RuntimeError),
    ("rx_controller", "completed_frames", (1 << 64) - 1, ValueError),
    ("rx_controller", "partial_bytes", 84, ValueError),
    ("rx_controller", "rx_dropped", 1 << 64, ValueError),
])
def test_paused_can_input_invalid_native_state_precedes_flush(
        paused_can, section, field, value, error):
    stream, peer, qmp, _ = paused_can
    stream.tx_queue.append(memoryview(CAN_LITERAL))
    getattr(qmp, section)[field] = value
    with pytest.raises(error):
        run_paused_can_input(stream, qmp)
    assert [bytes(part) for part in stream.tx_queue] == [CAN_LITERAL]
    with pytest.raises(BlockingIOError):
        peer.recv(84)
    assert_can_borrows_open(stream, qmp)


def test_paused_can_input_missing_controller_field_precedes_flush(paused_can):
    stream, _, qmp, _ = paused_can
    del qmp.rx_controller["partial_bytes"]
    stream.tx_queue.append(memoryview(CAN_LITERAL))
    with pytest.raises(ValueError, match="partial_bytes"):
        run_paused_can_input(stream, qmp)
    assert [bytes(part) for part in stream.tx_queue] == [CAN_LITERAL]
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("overrides", [
    {"completed_frames": True}, {"completed_frames": -1},
    {"completed_frames": (1 << 64) - 1}, {"target_ns": True},
    {"deadline": float("inf")}, {"deadline": float("nan")},
    {"controller": ""}, {"chardev": "can1/other"}, {"check": None},
])
def test_paused_can_input_argument_admission_precedes_native_io(paused_can, overrides):
    stream, peer, qmp, _ = paused_can
    stream.tx_queue.append(memoryview(CAN_LITERAL))
    with pytest.raises((TypeError, ValueError)):
        run_paused_can_input(stream, qmp, **overrides)
    assert qmp.commands == []
    assert [bytes(part) for part in stream.tx_queue] == [CAN_LITERAL]
    with pytest.raises(BlockingIOError):
        peer.recv(84)
    assert_can_borrows_open(stream, qmp)


def test_paused_can_input_requires_canonical_nonblocking_stream(paused_can):
    stream, _, qmp, _ = paused_can
    stream.sock.setblocking(True)
    with pytest.raises(ValueError, match="nonblocking AF_UNIX"):
        run_paused_can_input(stream, qmp)
    assert qmp.commands == []
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("dependency", ["qmp", "check", "slow-qmp", "stalled"])
def test_paused_can_input_dependency_and_deadline_failures_preserve_borrow(
        paused_can, dependency):
    stream, peer, qmp, clock = paused_can
    stream.tx_queue.append(memoryview(CAN_LITERAL))
    failure = RuntimeError(f"{dependency} failed")

    def fail():
        raise failure

    if dependency == "qmp":
        qmp.command = lambda *args: fail()
    elif dependency == "slow-qmp":
        qmp.on_rx = lambda number: clock.__setitem__(0, 2.0)
    elif dependency == "stalled":
        # The host write completed, but no consumer receipt ever arrives.
        qmp.on_rx = lambda number: clock.__setitem__(0, 2.0) if number == 2 else None
    timeout = dependency in ("slow-qmp", "stalled")
    with pytest.raises(TimeoutError if timeout else RuntimeError) as caught:
        run_paused_can_input(stream, qmp,
                             check=fail if dependency == "check" else lambda: None)
    if not timeout:
        assert caught.value is failure
    if dependency == "stalled":
        assert not stream.tx_pending
        assert peer.recv(84) == CAN_LITERAL
    else:
        assert [bytes(part) for part in stream.tx_queue] == [CAN_LITERAL]
    assert not stream.buffer
    assert_can_borrows_open(stream, qmp)
