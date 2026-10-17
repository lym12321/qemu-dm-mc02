"""Host adapter admission; native GPIO/QMP semantics have separate qtests."""

from collections import Counter
from copy import deepcopy
from pathlib import Path
import socket
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.boards import (
    QemuBoard, drain_paused_can, observe_initial_paused_can, validate_gpio_pin,
    wait_paused_can_input,
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


@pytest.fixture
def initial_can(paused_can):
    stream, peer, qmp, clock = paused_can
    qmp.execution = {"state": "idle", "current-ns": 1000}
    qmp.status = {"running": False, "status": "prelaunch"}
    return stream, peer, qmp, clock


def run_initial_can(stream, qmp, **overrides):
    options = dict(timestamp_ns=1000, controller="dm-mc02.fdcan1", chardev="can1",
                   deadline=1.0, check=lambda: None)
    options.update(overrides)
    return observe_initial_paused_can(qmp, stream, **options)


@pytest.mark.parametrize("status", ["prelaunch", "paused"])
def test_initial_can_returns_stable_native_baseline_without_io(initial_can, status):
    stream, peer, qmp, _ = initial_can
    qmp.status["status"] = status
    qmp.controller.update(tx_dropped=3, tx_short_writes=7, native_extra="retained")
    qmp.rx_controller.update(completed_frames=5, rx_dropped=2, native_extra=42)
    before = deepcopy((qmp.execution, qmp.status, qmp.tx, qmp.rx))
    original_socket = stream.sock
    stream.receive = stream.flush = lambda: pytest.fail("initial baseline must not consume or flush")
    result = run_initial_can(stream, qmp)
    assert result == {
        "tx": {"pending_frames": 0, "pending_bytes": 0, "tx_dropped": 3,
               "tx_short_writes": 7, "native_extra": "retained"},
        "rx": {"completed_frames": 5, "partial_bytes": 0, "rx_dropped": 2,
               "native_extra": 42},
    }
    assert (qmp.execution, qmp.status, qmp.tx, qmp.rx) == before
    assert qmp.tx_queries == qmp.rx_queries == 2
    assert all(name in ("x-query-virtual-execution", "query-status", "qom-get")
               for name, _ in qmp.commands)
    assert stream.sock is original_socket
    assert not stream.buffer and not stream.tx_pending
    with pytest.raises(BlockingIOError):
        peer.recv(84)
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("section,field,value,error", [
    ("execution", "state", "reached", RuntimeError),
    ("execution", "current-ns", 1001, RuntimeError),
    ("execution", "target-ns", 1000, RuntimeError),
    ("status", "running", True, RuntimeError),
    ("status", "status", "shutdown", RuntimeError),
    ("connected", None, False, ConnectionError),
    ("tx", "schema", 2, ValueError),
    ("controller", "pending_bytes", 1, ValueError),
    ("controller", "tx_short_writes", 1 << 64, ValueError),
    ("rx", "timestamp_ns", 999, RuntimeError),
    ("rx_controller", "completed_frames", (1 << 64) - 1, ValueError),
    ("rx_controller", "partial_bytes", 1, RuntimeError),
])
def test_initial_can_refuses_invalid_native_baseline(
        initial_can, section, field, value, error):
    stream, _, qmp, _ = initial_can
    if field is None:
        setattr(qmp, section, value)
    else:
        getattr(qmp, section)[field] = value
    with pytest.raises(error):
        run_initial_can(stream, qmp)
    assert not stream.buffer and not stream.tx_pending
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("pending", ["host-input", "host-output", "native-output"])
def test_initial_can_requires_empty_queues_without_draining(initial_can, pending):
    stream, peer, qmp, _ = initial_can
    if pending == "host-input":
        stream.tx_queue.append(memoryview(CAN_LITERAL[19:]))
    elif pending == "host-output":
        stream.buffer.extend(CAN_LITERAL[:19])
    else:
        qmp.controller.update(pending_frames=1, pending_bytes=84)
    with pytest.raises(RuntimeError, match="empty"):
        run_initial_can(stream, qmp)
    assert [bytes(part) for part in stream.tx_queue] == (
        [CAN_LITERAL[19:]] if pending == "host-input" else [])
    assert bytes(stream.buffer) == (CAN_LITERAL[:19] if pending == "host-output" else b"")
    if pending != "native-output":
        assert qmp.commands == []
    with pytest.raises(BlockingIOError):
        peer.recv(84)
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("peer_state", ["unread-output", "eof"])
def test_initial_can_peeks_original_peer_without_consuming(initial_can, peer_state):
    stream, peer, qmp, _ = initial_can
    if peer_state == "eof":
        peer.close()
    else:
        assert peer.send(CAN_LITERAL[:19]) == 19
    assert qmp.connected is True  # A replacement's QOM status cannot prove this peer.
    with pytest.raises(ConnectionError if peer_state == "eof" else RuntimeError):
        run_initial_can(stream, qmp)
    if peer_state != "eof":
        assert stream.sock.recv(84, socket.MSG_PEEK) == CAN_LITERAL[:19]
    assert not stream.buffer and not stream.tx_pending
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("change", [
    "fence", "tx-count", "rx-count", "rx-drop", "native-output", "host-input", "output", "eof",
])
def test_initial_can_reobserves_full_native_state_and_peer(initial_can, change):
    stream, peer, qmp, _ = initial_can

    def changed(number):
        if number == 1 and change == "fence":
            qmp.execution["state"] = "cancelled"
        if number != 2:
            return
        if change == "tx-count":
            qmp.controller["tx_short_writes"] = 1
        elif change == "rx-count":
            qmp.rx_controller["completed_frames"] = 1
        elif change == "rx-drop":
            qmp.rx_controller["rx_dropped"] = 1
        elif change == "native-output":
            qmp.controller.update(pending_frames=1, pending_bytes=84)
        elif change == "host-input":
            stream.tx_queue.append(memoryview(CAN_LITERAL))
        elif change == "output":
            assert peer.send(CAN_LITERAL[:19]) == 19
        elif change == "eof":
            peer.close()

    # Change TX fields before their second reply; RX/peer changes occur during
    # the second RX reply, after all preceding native observations succeeded.
    qmp.on_tx = changed if change in ("tx-count", "native-output") else lambda _: None
    qmp.on_rx = lambda number: changed(number) if change not in ("tx-count", "native-output") else None
    with pytest.raises(ConnectionError if change == "eof" else RuntimeError):
        run_initial_can(stream, qmp)
    if change == "output":
        assert stream.sock.recv(84, socket.MSG_PEEK) == CAN_LITERAL[:19]
    assert [bytes(part) for part in stream.tx_queue] == ([CAN_LITERAL] if change == "host-input" else [])
    assert not stream.buffer
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("overrides", [
    {"timestamp_ns": True}, {"timestamp_ns": 1 << 63}, {"deadline": float("inf")},
    {"controller": ""}, {"chardev": "can1/other"}, {"check": None},
])
def test_initial_can_pure_admission_precedes_native_io(initial_can, overrides):
    stream, peer, qmp, _ = initial_can
    with pytest.raises((TypeError, ValueError)):
        run_initial_can(stream, qmp, **overrides)
    assert qmp.commands == []
    with pytest.raises(BlockingIOError):
        peer.recv(84)
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("dependency", ["qmp", "check", "slow-qmp"])
def test_initial_can_dependency_failure_preserves_primary_and_borrow(initial_can, dependency):
    stream, _, qmp, clock = initial_can
    failure = KeyboardInterrupt("initial baseline dependency failed")

    def fail():
        raise failure

    if dependency == "qmp":
        qmp.command = lambda *args: fail()
    elif dependency == "slow-qmp":
        qmp.on_rx = lambda _: clock.__setitem__(0, 2.0)
    with pytest.raises(TimeoutError if dependency == "slow-qmp" else KeyboardInterrupt) as caught:
        run_initial_can(stream, qmp, check=fail if dependency == "check" else lambda: None)
    if dependency != "slow-qmp":
        assert caught.value is failure
    assert not stream.buffer and not stream.tx_pending
    assert_can_borrows_open(stream, qmp)


def test_initial_can_does_not_weaken_reached_only_operations(initial_can):
    stream, _, qmp, _ = initial_can
    with pytest.raises(ValueError, match="target-ns"):
        run_paused_can(stream, qmp, lambda _: pytest.fail("unexpected output"))
    with pytest.raises(ValueError, match="target-ns"):
        run_paused_can_input(stream, qmp, completed_frames=0)
    assert not stream.buffer and not stream.tx_pending
    assert_can_borrows_open(stream, qmp)


# This producer supplies complete independent native observations. In particular,
# command_many must never be implemented by calling the scalar test producer.
BATCH_GROUP = (
    ("qom-get", {"path": "/machine", "property": "can-execution-state"}),
)


class BatchPausedCanQmp:
    def __init__(self):
        self.execution = {"state": "reached", "current-ns": 1000,
                          "target-ns": 1000}
        self.status = {"running": False, "status": "paused", "singlestep": False}
        self.transport = {"path": "/chardevs/can1", "connected": True,
                          "native_extra": "retained"}
        self.tx = {"schema": 1, "timestamp_ns": 1000, "controllers": {
            "dm-mc02.fdcan1": {"pending_frames": 0, "pending_bytes": 0,
                              "tx_dropped": 0, "tx_short_writes": 0},
        }}
        self.rx = {"schema": 1, "timestamp_ns": 1000, "controllers": {
            "dm-mc02.fdcan1": {"completed_frames": 0, "partial_bytes": 0,
                              "rx_dropped": 0},
        }}
        self.groups = []
        self.direction = "output"  # Callback label, never selects native fields.
        self.phase = "first"
        self.on_group = lambda direction: None
        self.result = lambda values: values
        self.closed = False

    def command(self, *args, **kwargs):
        pytest.fail("batch producer must not receive a scalar command")

    def command_many(self, commands):
        assert type(commands) is tuple
        assert commands == BATCH_GROUP
        self.groups.append(deepcopy(commands))
        self.on_group(self.direction)
        values = ({"schema": 1, "execution": deepcopy(self.execution),
                   "status": deepcopy(self.status),
                   "fdcan-transports": {"dm-mc02.fdcan1": deepcopy(self.transport),
                                        "extra-controller": {"path": None, "connected": None}},
                   "fdcan-tx-state": deepcopy(self.tx),
                   "fdcan-rx-state": deepcopy(self.rx),
                   "native_extra": {"retained": True}},)
        return self.result(values)

    @property
    def controller(self):
        return self.tx["controllers"]["dm-mc02.fdcan1"]

    @property
    def rx_controller(self):
        return self.rx["controllers"]["dm-mc02.fdcan1"]


@pytest.fixture
def batch_can(paused_can):
    stream, peer, _, clock = paused_can
    return stream, peer, BatchPausedCanQmp(), clock


def batch_operation(direction, stream, qmp, **options):
    qmp.direction = direction
    if direction == "initial":
        return run_initial_can(stream, qmp, **options)
    if direction == "input":
        return run_paused_can_input(stream, qmp, **options)
    return run_paused_can(stream, qmp, lambda frame: pytest.fail("unexpected sink"),
                          **options)


def initial_batch(qmp):
    qmp.direction = "initial"
    qmp.execution = {"state": "idle", "current-ns": 1000}
    qmp.status = {"running": False, "status": "prelaunch", "singlestep": False}


def test_batch_initial_fetches_two_complete_fresh_groups_without_io(batch_can):
    stream, peer, qmp, _ = batch_can
    initial_batch(qmp)
    qmp.controller.update(tx_dropped=3, tx_short_writes=7)
    qmp.rx_controller.update(completed_frames=5, rx_dropped=2)
    stream.receive = stream.flush = lambda: pytest.fail("initial observation did IO")

    def fresh(direction):
        assert direction == "initial"
        qmp.rx_controller["native_extra"] = qmp.phase
        qmp.phase = "second"

    qmp.on_group = fresh
    assert run_initial_can(stream, qmp) == {
        "tx": {"pending_frames": 0, "pending_bytes": 0, "tx_dropped": 3,
               "tx_short_writes": 7},
        "rx": {"completed_frames": 5, "partial_bytes": 0, "rx_dropped": 2,
               "native_extra": "second"},
    }
    assert qmp.groups == [BATCH_GROUP] * 2
    with pytest.raises(BlockingIOError):
        peer.recv(84)
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("fragmented", [False, True])
def test_batch_output_observes_around_real_reads_and_fresh_empty(batch_can, fragmented):
    stream, peer, qmp, _ = batch_can
    seen, events = [], []
    original_receive = stream.receive
    qmp.controller.update(pending_frames=1, pending_bytes=84)

    def publish(direction):
        assert direction == "output"
        events.append("group:" + qmp.phase)
        if qmp.phase == "publish":
            wire = CAN_LITERAL[:19] if fragmented else CAN_LITERAL
            assert peer.send(wire) == len(wire)
            qmp.controller.update(pending_frames=0, pending_bytes=0)
            qmp.phase = "prefix" if fragmented else "frame"
        elif qmp.phase == "suffix":
            assert bytes(stream.buffer) == CAN_LITERAL[:19]
            assert peer.send(CAN_LITERAL[19:]) == 65
            qmp.phase = "frame"

    def receive():
        result = original_receive()
        events.append("read:" + qmp.phase)
        if qmp.phase == "first":
            assert result == [] and not stream.buffer
            qmp.phase = "publish"
        elif qmp.phase == "prefix":
            assert result == [] and bytes(stream.buffer) == CAN_LITERAL[:19]
            qmp.phase = "suffix"
        elif qmp.phase == "frame":
            assert result == [CAN_DECODED] and not stream.buffer
            qmp.phase = "fresh-empty"
        else:
            assert qmp.phase == "fresh-empty" and result == []
            qmp.phase = "final"
        return result

    qmp.on_group, stream.receive = publish, receive
    run_paused_can(stream, qmp, seen.append)
    expected_phases = ["first", "publish"] + (["suffix"] if fragmented else [])
    expected_phases += ["fresh-empty", "final"]
    assert [item.removeprefix("group:") for item in events if item.startswith("group:")] == expected_phases
    assert qmp.groups == [BATCH_GROUP] * len(expected_phases)
    assert seen == [CAN_DECODED] and not stream.buffer
    assert events[-2:] == ["read:fresh-empty", "group:final"]
    assert_can_borrows_open(stream, qmp)


def test_batch_input_flushes_real_queue_then_reobserves_exact_receipt(batch_can):
    stream, peer, qmp, _ = batch_can
    qmp.direction = "input"
    stream.tx_queue.append(memoryview(CAN_LITERAL))
    observed, events = bytearray(), []
    original_flush = stream.flush

    def receipt(direction):
        assert direction == "input"
        events.append("group:" + qmp.phase)
        if qmp.phase == "first":
            assert stream.tx_pending
            with pytest.raises(BlockingIOError):
                peer.recv(84)
            qmp.phase = "await-flush"
        elif qmp.phase == "flushed":
            observed.extend(peer.recv(83))
            assert bytes(observed) == CAN_LITERAL[:83]
            qmp.rx_controller["partial_bytes"] = 83
            qmp.phase = "partial"
        elif qmp.phase == "partial":
            observed.extend(peer.recv(1))
            assert bytes(observed) == CAN_LITERAL
            qmp.rx_controller.update(completed_frames=1, partial_bytes=0,
                                     rx_dropped=1, native_extra="first-receipt")
            qmp.phase = "receipt"
        else:
            assert qmp.phase == "receipt"
            qmp.rx_controller["native_extra"] = "fresh-final"
            qmp.phase = "final"

    def flush():
        events.append("flush:" + qmp.phase)
        original_flush()
        assert not stream.tx_pending
        if qmp.phase == "await-flush":
            qmp.phase = "flushed"

    qmp.on_group, stream.flush = receipt, flush
    assert run_paused_can_input(stream, qmp) == {
        "completed_frames": 1, "partial_bytes": 0, "rx_dropped": 1,
        "native_extra": "fresh-final",
    }
    assert events == ["group:first", "flush:await-flush", "group:flushed",
                      "flush:partial", "group:partial", "group:receipt"]
    assert qmp.groups == [BATCH_GROUP] * 4
    assert bytes(observed) == CAN_LITERAL and not stream.tx_pending
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("direction,section,field,value,error", [
    ("initial", "controller", "tx_dropped", 1, RuntimeError),
    ("initial", "rx_controller", "partial_bytes", 1, RuntimeError),
    ("initial", "transport", "path", "/chardevs/replacement", RuntimeError),
    ("output", "execution", "state", "cancelled", RuntimeError),
    ("output", "status", "running", True, RuntimeError),
    ("output", "transport", "connected", False, ConnectionError),
    ("output", "transport", "path", None, RuntimeError),
    ("output", "controller", "tx_dropped", 1, RuntimeError),
    ("input", "execution", "current-ns", 1001, RuntimeError),
    ("input", "rx_controller", "partial_bytes", 1, RuntimeError),
    ("input", "transport", "path", "/chardevs/replacement", RuntimeError),
])
def test_batch_final_group_changes_are_rejected(
        batch_can, direction, section, field, value, error):
    stream, _, qmp, _ = batch_can
    if direction == "initial":
        initial_batch(qmp)
    elif direction == "input":
        qmp.rx_controller["completed_frames"] = 1

    def change(kind):
        assert kind == direction
        if qmp.phase == "first":
            qmp.phase = "final"
            return
        if field is None:
            setattr(qmp, section, value)
        else:
            getattr(qmp, section)[field] = value

    qmp.on_group = change
    with pytest.raises(error) as caught:
        batch_operation(direction, stream, qmp)
    if section == "transport" and field == "path":
        label = "initial-baseline" if direction == "initial" else direction
        assert str(caught.value) == f"CAN {label} transport identity changed"
    assert qmp.groups == [BATCH_GROUP] * 2
    assert not stream.buffer and not stream.tx_pending
    assert_can_borrows_open(stream, qmp)


class BatchTupleSubclass(tuple):
    pass


@pytest.mark.parametrize("direction,shape,error", [
    ("initial", "list", TypeError), ("output", "short", ValueError),
    ("input", "long", ValueError), ("output", "subclass", TypeError),
    ("input", "none", TypeError),
])
def test_batch_requires_plain_tuple_and_exact_result_arity_before_io(
        batch_can, direction, shape, error):
    stream, peer, qmp, _ = batch_can
    if direction == "initial":
        initial_batch(qmp)
    if direction == "output":
        assert peer.send(CAN_LITERAL) == 84
    elif direction == "input":
        stream.tx_queue.append(memoryview(CAN_LITERAL))
    qmp.result = lambda values: {
        "list": list(values), "short": values[:-1], "long": values + (None,),
        "subclass": BatchTupleSubclass(values), "none": None,
    }[shape]
    with pytest.raises(error, match="tuple|arity"):
        batch_operation(direction, stream, qmp)
    assert qmp.groups == [BATCH_GROUP]
    if direction == "output":
        assert stream.sock.recv(84, socket.MSG_PEEK) == CAN_LITERAL
    elif direction == "input":
        assert [bytes(part) for part in stream.tx_queue] == [CAN_LITERAL]
        with pytest.raises(BlockingIOError):
            peer.recv(84)
    assert not stream.buffer
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("direction,invalid,error", [
    ("output", "object", TypeError),
    ("output", "schema", ValueError),
    ("output", "bool-schema", TypeError),
    ("output", "unsupported-schema", ValueError),
    ("output", "execution", ValueError),
    ("output", "status", ValueError),
    ("output", "fdcan-tx-state", ValueError),
    ("input", "fdcan-rx-state", ValueError),
    ("initial", "fdcan-rx-state", ValueError),
])
def test_batch_composed_schema_and_consumed_sections_reject_before_io(
        batch_can, direction, invalid, error):
    stream, peer, qmp, _ = batch_can
    if direction == "initial":
        initial_batch(qmp)
    elif direction == "output":
        assert peer.send(CAN_LITERAL) == 84
    else:
        stream.tx_queue.append(memoryview(CAN_LITERAL))
    stream.receive = stream.flush = lambda: pytest.fail("invalid aggregate did IO")

    def malformed(values):
        aggregate, = values
        if invalid == "object":
            aggregate = None
        elif invalid == "bool-schema":
            aggregate["schema"] = True
        elif invalid == "unsupported-schema":
            aggregate["schema"] = 2
        else:
            del aggregate[invalid]
        return aggregate,

    qmp.result = malformed
    with pytest.raises(error, match="can-execution-state"):
        batch_operation(direction, stream, qmp)
    assert qmp.groups == [BATCH_GROUP]
    if direction == "output":
        assert stream.sock.recv(84, socket.MSG_PEEK) == CAN_LITERAL
    elif direction == "input":
        assert [bytes(part) for part in stream.tx_queue] == [CAN_LITERAL]
        with pytest.raises(BlockingIOError):
            peer.recv(84)
    assert not stream.buffer
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("invalid,error,match", [
    ("missing-map", ValueError, "fdcan-transports.*missing"),
    ("null-map", TypeError, "fdcan-transports must be an object"),
    ("list-map", TypeError, "fdcan-transports must be an object"),
    ("missing-controller", ValueError, "fdcan1.*missing"),
    ("null-controller", TypeError, "fdcan1 must be an object"),
    ("list-controller", TypeError, "fdcan1 must be an object"),
    ("missing-path", ValueError, "transport.path is missing"),
    ("null-path", RuntimeError, "CAN output transport identity changed"),
    ("nonstring-path", RuntimeError, "CAN output transport identity changed"),
    ("wrong-path", RuntimeError, "CAN output transport identity changed"),
    ("missing-connected", ValueError, "transport.connected is missing"),
    ("null-connected", ConnectionError, "disconnected"),
    ("disconnected", ConnectionError, "disconnected"),
    ("truthy-connected", ConnectionError, "disconnected"),
])
def test_batch_requires_actual_bound_transport_before_can_io(batch_can, invalid, error, match):
    stream, peer, qmp, _ = batch_can
    assert peer.send(CAN_LITERAL) == 84
    stream.receive = stream.flush = lambda: pytest.fail("invalid transport did IO")

    def malformed(values):
        state, = values
        transports = state["fdcan-transports"]
        transport = transports["dm-mc02.fdcan1"]
        if invalid == "missing-map":
            del state["fdcan-transports"]
        elif invalid.endswith("-map"):
            state["fdcan-transports"] = None if invalid == "null-map" else []
        elif invalid == "missing-controller":
            del transports["dm-mc02.fdcan1"]
        elif invalid.endswith("-controller"):
            transports["dm-mc02.fdcan1"] = None if invalid == "null-controller" else []
        elif invalid.startswith("missing-"):
            del transport[invalid.removeprefix("missing-")]
        elif invalid.endswith("-path"):
            transport["path"] = {"null-path": None, "nonstring-path": 7,
                                 "wrong-path": "/chardevs/another-connected-peer"}[invalid]
            assert transport["connected"] is True
        else:
            transport["connected"] = {"null-connected": None, "disconnected": False,
                                      "truthy-connected": 1}[invalid]
        return values

    qmp.result = malformed
    with pytest.raises(error, match=match):
        run_paused_can(stream, qmp, lambda _: pytest.fail("unexpected sink"))
    assert qmp.groups == [BATCH_GROUP]
    assert stream.sock.recv(84, socket.MSG_PEEK) == CAN_LITERAL
    assert not stream.buffer and not stream.tx_pending
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("direction", ["output", "input"])
def test_batch_validates_only_consumed_direction_and_tolerates_extra_fields(batch_can, direction):
    stream, _, qmp, _ = batch_can
    if direction == "input":
        qmp.rx_controller["completed_frames"] = 1
    setattr(qmp, "rx" if direction == "output" else "tx", None)
    batch_operation(direction, stream, qmp)
    assert qmp.groups == [BATCH_GROUP] * 2
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("first,error,match", [
    ("execution", RuntimeError, "not reached"),
    ("status", RuntimeError, "paused"),
    ("identity", RuntimeError, "transport identity changed"),
    ("connection", ConnectionError, "disconnected"),
    ("shape", TypeError, "tuple"),
    ("schema", ValueError, "schema"),
])
def test_batch_errors_keep_input_order_and_shape_precedes_values(batch_can, first, error, match):
    stream, peer, qmp, _ = batch_can
    assert peer.send(CAN_LITERAL) == 84
    qmp.tx["schema"] = 2
    qmp.transport["connected"] = False
    if first != "connection":
        qmp.transport["path"] = "/chardevs/other-connected-peer"
    if first in ("execution", "status", "shape", "schema"):
        qmp.status["running"] = True
    if first in ("execution", "shape", "schema"):
        qmp.execution["state"] = "cancelled"
    if first == "shape":
        qmp.result = list
    elif first == "schema":
        qmp.result = lambda values: ({**values[0], "schema": 2},)
    with pytest.raises(error, match=match):
        run_paused_can(stream, qmp, lambda frame: pytest.fail("unexpected sink"))
    assert qmp.groups == [BATCH_GROUP]
    assert stream.sock.recv(84, socket.MSG_PEEK) == CAN_LITERAL
    assert not stream.buffer


def test_batch_initial_validates_tx_before_rx_in_the_same_group(batch_can):
    stream, _, qmp, _ = batch_can
    initial_batch(qmp)
    qmp.tx["schema"] = 2
    qmp.rx["timestamp_ns"] = 999
    stream.receive = stream.flush = lambda: pytest.fail("invalid initial group did IO")
    with pytest.raises(ValueError, match="schema"):
        run_initial_can(stream, qmp)
    assert qmp.groups == [BATCH_GROUP]
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("direction", ["initial", "output", "input"])
def test_batch_failure_preserves_original_exception_without_scalar_fallback(batch_can, direction):
    stream, _, qmp, clock = batch_can
    failure = RuntimeError("native batch failure")
    if direction == "initial":
        initial_batch(qmp)

    def fail(kind):
        assert kind == direction
        clock[0] = 2.0
        raise failure

    qmp.on_group = fail
    with pytest.raises(RuntimeError) as caught:
        batch_operation(direction, stream, qmp)
    assert caught.value is failure
    assert qmp.groups == [BATCH_GROUP]
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("direction,phase,timeout", [
    ("output", "pre", False), ("output", "post", False),
    ("input", "pre", True), ("input", "post", True),
    ("initial", "post", False), ("initial", "post", True),
])
def test_batch_guards_stop_before_receive_sink_or_flush(batch_can, direction, phase, timeout):
    stream, peer, qmp, clock = batch_can
    failure = RuntimeError("liveness check failed")
    if direction == "initial":
        initial_batch(qmp)
    elif direction == "output":
        assert peer.send(CAN_LITERAL) == 84
    else:
        stream.tx_queue.append(memoryview(CAN_LITERAL))
    stream.receive = stream.flush = lambda: pytest.fail("guard allowed transport IO")
    if phase == "post":
        # A post-call guard failure precedes even outer result admission.
        qmp.result = list
    if timeout and phase == "pre":
        clock[0] = 2.0

    def returned(kind):
        assert kind == direction
        qmp.phase = "returned"
        if timeout:
            clock[0] = 2.0

    def check():
        if not timeout and ((phase == "pre" and qmp.phase == "first") or
                            (phase == "post" and qmp.phase == "returned")):
            raise failure

    qmp.on_group = returned
    with pytest.raises(TimeoutError if timeout else RuntimeError) as caught:
        batch_operation(direction, stream, qmp, check=check)
    if not timeout:
        assert caught.value is failure
    assert qmp.groups == ([] if phase == "pre" else [BATCH_GROUP])
    if direction == "output":
        assert stream.sock.recv(84, socket.MSG_PEEK) == CAN_LITERAL
    elif direction == "input":
        assert [bytes(part) for part in stream.tx_queue] == [CAN_LITERAL]
        with pytest.raises(BlockingIOError):
            peer.recv(84)
    assert not stream.buffer
    assert_can_borrows_open(stream, qmp)


@pytest.mark.parametrize("direction,failure_kind", [
    ("output", "exception"), ("input", "exception"),
    ("output", "identity"), ("input", "identity"),
])
def test_batch_later_failure_retains_completed_sink_or_flush_effects(batch_can, direction, failure_kind):
    stream, peer, qmp, _ = batch_can
    qmp.direction = direction
    failure, seen = RuntimeError("later observation failed"), []

    def fail_after_effect(kind):
        assert kind == direction
        if qmp.phase == "effect":
            if failure_kind == "exception":
                raise failure
            qmp.transport["path"] = "/chardevs/replacement"

    qmp.on_group = fail_after_effect
    if direction == "output":
        assert peer.send(CAN_LITERAL + CAN_LITERAL[:19]) == 103

        def sink(frame):
            seen.append(frame)
            qmp.phase = "effect"

        with pytest.raises(RuntimeError) as caught:
            run_paused_can(stream, qmp, sink)
        assert seen == [CAN_DECODED]
        assert bytes(stream.buffer) == CAN_LITERAL[:19]
    else:
        stream.tx_queue.append(memoryview(CAN_LITERAL))
        original_flush = stream.flush

        def flush():
            original_flush()
            qmp.phase = "effect"

        stream.flush = flush
        with pytest.raises(RuntimeError) as caught:
            run_paused_can_input(stream, qmp)
        assert not stream.tx_pending
        assert peer.recv(84) == CAN_LITERAL
        assert not stream.buffer
    if failure_kind == "exception":
        assert caught.value is failure
    else:
        assert str(caught.value) == f"CAN {direction} transport identity changed"
    assert qmp.groups == [BATCH_GROUP] * 2
    assert_can_borrows_open(stream, qmp)


def test_batch_optional_none_keeps_scalar_path_and_noncallable_rejects_before_io(paused_can):
    stream, peer, qmp, _ = paused_can
    qmp.command_many = None
    run_paused_can(stream, qmp, lambda frame: pytest.fail("unexpected sink"))
    assert qmp.tx_queries == 2
    qmp.commands.clear()
    qmp.command_many = True
    assert peer.send(CAN_LITERAL) == 84
    with pytest.raises(TypeError, match="callable"):
        run_paused_can(stream, qmp, lambda frame: pytest.fail("unexpected sink"))
    assert qmp.commands == []
    assert stream.sock.recv(84, socket.MSG_PEEK) == CAN_LITERAL
    assert_can_borrows_open(stream, qmp)
