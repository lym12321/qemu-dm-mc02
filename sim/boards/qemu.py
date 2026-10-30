"""Borrow native QMP for DM-MC02 board inputs and read-only observations.

Snapshots contain QEMU virtual time, without advancing it or establishing a
CPU/plant time barrier. GPIO samples retain the model's digital approximation
for output, alternate-function and analog modes; they are not physical lines.
LED source ``ws2812-dma`` observes a DMA waveform, while ``gpio-latch`` is an
approximation of the GPIO latch and does not establish electrical LED drive.
"""

from __future__ import annotations

import math
import re
import socket
import time

from sim.transports.can_stream import CanStream
from sim.gpio import validate_gpio_pin


def _integer(value, name: str, maximum: int | None = None) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    if value < 0 or (maximum is not None and value > maximum):
        raise ValueError(f"{name} is out of range")


def _object(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def _field(value: dict, name: str, path: str):
    if name not in value:
        raise ValueError(f"{path}.{name} is missing")
    return value[name]


def validate_snapshot(value) -> dict:
    """Validate schema 1 in place; preserve the native object and extra fields."""
    state = _object(value, "board-state")
    schema = _field(state, "schema", "board-state")
    _integer(schema, "board-state.schema", 1)
    if schema != 1:
        raise ValueError("unsupported board-state schema")
    _integer(_field(state, "timestamp_ns", "board-state"), "timestamp_ns")
    _integer(_field(state, "vin_mv", "board-state"), "vin_mv", 0xffffffff)
    board = _field(state, "board", "board-state")
    if not isinstance(board, str) or not board:
        raise ValueError("board-state.board must be a nonempty string")
    gpio = _object(_field(state, "gpio", "board-state"), "gpio")
    if set(gpio) != set("ABCDEFGH"):
        raise ValueError("board-state.gpio must contain exactly banks A through H")
    for port, raw_bank in gpio.items():
        bank = _object(raw_bank, f"gpio.{port}")
        for field, maximum in (("moder", 0xffffffff), ("sampled", 0xffff),
                               ("odr", 0xffff), ("output_mask", 0xffff)):
            _integer(_field(bank, field, f"gpio.{port}"),
                     f"gpio.{port}.{field}", maximum)
    for name in ("led", "key"):
        alias = _object(_field(state, name, "board-state"), name)
        validate_gpio_pin(_field(alias, "pin", name))
    led = state["led"]
    _integer(_field(led, "rgb", "led"), "led.rgb", 0xffffff)
    _integer(_field(led, "brightness", "led"), "led.brightness", 0xff)
    if _field(led, "source", "led") not in ("ws2812-dma", "gpio-latch"):
        raise ValueError("unsupported led.source")
    key = state["key"]
    if type(_field(key, "pressed", "key")) is not bool:
        raise TypeError("key.pressed must be a boolean")
    _integer(_field(key, "level", "key"), "key.level", 1)
    return state


def _digital_pin(endpoint: str) -> str | None:
    if not isinstance(endpoint, str):
        raise TypeError("digital endpoint must be a string")
    if endpoint.startswith("gpio."):
        return validate_gpio_pin(endpoint[5:])
    if endpoint not in ("key", "led"):
        raise ValueError("digital endpoint must be gpio.PIN, key or led")
    return None


def digital_level(endpoint: str, snapshot: dict) -> int:
    """Project an admitted native snapshot without QMP or hardware state."""
    pin = _digital_pin(endpoint)
    state = validate_snapshot(snapshot)
    if pin is not None:
        return (state["gpio"][pin[0]]["sampled"] >> int(pin[1:])) & 1
    if endpoint == "key":
        return state["key"]["level"]
    return int(state["led"]["rgb"] != 0)


def _paused_can_io(qmp, stream, *, target_ns, controller, chardev,
                   deadline, check, direction, initial=False):
    command = getattr(qmp, "command", None)
    if not callable(command) or not callable(check):
        raise TypeError("QMP command and check must be callable")
    command_many = getattr(qmp, "command_many", None)
    if command_many is not None and not callable(command_many):
        raise TypeError("QMP command_many must be callable or None")
    _integer(target_ns, "target_ns", 0x7fffffffffffffff)
    for name, value in (("controller", controller), ("chardev", chardev)):
        if (not isinstance(value, str) or
                re.fullmatch(r"[A-Za-z0-9_.-]+", value) is None):
            raise ValueError(f"{name} must be a nonempty native identity")
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise ValueError("deadline must be finite monotonic seconds")
    if (type(stream) is not CanStream or
            type(stream.sock) is not socket.socket or
            stream.sock.family != socket.AF_UNIX or
            stream.sock.type != socket.SOCK_STREAM or stream.sock.fileno() < 0 or
            stream.sock.getblocking()):
        raise ValueError("require canonical CanStream on a nonblocking AF_UNIX stream")

    def guard():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"paused CAN {direction} deadline expired")
        check()
        if time.monotonic() >= deadline:
            raise TimeoutError(f"paused CAN {direction} deadline expired")

    def query(name, arguments=None):
        guard()
        value = command(name, arguments)
        guard()
        return value

    def observe():
        connection = {"path": f"/chardevs/{chardev}", "property": "connected"}
        tx_arguments = {"path": "/machine", "property": "fdcan-tx-state"}
        rx_arguments = {"path": "/machine", "property": "fdcan-rx-state"}
        if command_many is None:
            # Keep immediate validation: a bad scalar reply must stop later IO.
            _can_execution_state(query("x-query-virtual-execution"), target_ns,
                                 direction, initial)
            _can_paused_status(query("query-status"), direction, initial)
            _can_connection_state(query("qom-get", connection), direction)
            if initial:
                tx = _can_tx_state(query("qom-get", tx_arguments), controller,
                                   target_ns, direction)
                rx = _can_rx_state(query("qom-get", rx_arguments), controller,
                                   target_ns, direction)
                return tx, rx
            if direction == "output":
                return (_can_tx_state(query("qom-get", tx_arguments), controller,
                                      target_ns, direction),)
            return (_can_rx_state(query("qom-get", rx_arguments), controller,
                                  target_ns, direction),)

        requests = (("qom-get", {"path": "/machine",
                                 "property": "can-execution-state"}),)
        guard()
        values = command_many(requests)
        guard()
        if type(values) is not tuple:
            raise TypeError("QMP command_many result must be a plain tuple")
        if len(values) != len(requests):
            raise ValueError("QMP command_many result has wrong arity")
        state = _object(values[0], "can-execution-state")
        schema = _field(state, "schema", "can-execution-state")
        _integer(schema, "can-execution-state.schema", 1)
        if schema != 1:
            raise ValueError("unsupported can-execution-state schema")
        _can_execution_state(_field(state, "execution", "can-execution-state"),
                             target_ns, direction, initial)
        _can_paused_status(_field(state, "status", "can-execution-state"),
                           direction, initial)
        transports = _object(_field(state, "fdcan-transports", "can-execution-state"),
                             "fdcan-transports")
        transport = _object(_field(transports, controller, "fdcan-transports"),
                            f"fdcan-transports.{controller}")
        if _field(transport, "path", "transport") != f"/chardevs/{chardev}":
            raise RuntimeError(f"CAN {direction} transport identity changed")
        _can_connection_state(_field(transport, "connected", "transport"), direction)
        if initial:
            tx = _can_tx_state(_field(state, "fdcan-tx-state", "can-execution-state"),
                               controller, target_ns, direction)
            rx = _can_rx_state(_field(state, "fdcan-rx-state", "can-execution-state"),
                               controller, target_ns, direction)
            return tx, rx
        if direction == "output":
            return (_can_tx_state(_field(state, "fdcan-tx-state", "can-execution-state"),
                                  controller, target_ns, direction),)
        return (_can_rx_state(_field(state, "fdcan-rx-state", "can-execution-state"),
                              controller, target_ns, direction),)

    return guard, observe


def validate_can_execution_snapshot(value, transports):
    """Admit one native aggregate without assuming its CPU has reached a cut.

    This is a fresh observation, never a receipt cache. Paused drain/receipt
    consumers still validate their own before/after IO closure observations.
    ``transports`` maps actual controller identities to chardev names.
    """
    state = _object(value, 'can-execution-state')
    if type(state.get('schema')) is not int or state['schema'] != 1:
        raise ValueError('unsupported can-execution-state schema')
    execution = _object(_field(state, 'execution', 'can-execution-state'), 'execution')
    status = _object(_field(state, 'status', 'can-execution-state'), 'status')
    observed = _object(_field(state, 'fdcan-transports', 'can-execution-state'),
                       'fdcan-transports')
    for controller, chardev in transports.items():
        transport = _object(_field(observed, controller, 'fdcan-transports'), controller)
        if _field(transport, 'path', controller) != f'/chardevs/{chardev}':
            raise RuntimeError('CAN execution transport identity changed')
        _can_connection_state(_field(transport, 'connected', controller), 'execution')
    return execution, status


def _can_execution_state(value, target_ns, direction, initial):
    execution = _object(value, "execution")
    for field in (("current-ns",) if initial else ("current-ns", "target-ns")):
        value = _field(execution, field, "execution")
        _integer(value, f"execution.{field}", 0x7fffffffffffffff)
        if value != target_ns:
            raise RuntimeError(f"CAN {direction} execution time or target changed")
    if initial:
        if (_field(execution, "state", "execution") != "idle" or
                "target-ns" in execution):
            raise RuntimeError("CAN initial baseline requires idle execution without a target")
    elif _field(execution, "state", "execution") != "reached":
        raise RuntimeError(f"CAN {direction} execution target is not reached")


def _can_paused_status(value, direction, initial):
    status = _object(value, "query-status")
    allowed = ("prelaunch", "paused") if initial else ("paused",)
    if status.get("running") is not False or status.get("status") not in allowed:
        raise RuntimeError(f"QEMU must remain paused during CAN {direction} drain")


def _can_connection_state(value, direction):
    if value is not True:
        raise ConnectionError(f"CAN {direction} chardev is disconnected")


def _can_transfer_state(value, property_name, controller, target_ns, direction):
    state = _object(value, property_name)
    schema = _field(state, "schema", property_name)
    _integer(schema, f"{property_name}.schema", 1)
    if schema != 1:
        raise ValueError(f"unsupported {property_name} schema")
    timestamp = _field(state, "timestamp_ns", property_name)
    _integer(timestamp, f"{property_name}.timestamp_ns", 0x7fffffffffffffff)
    if timestamp != target_ns:
        raise RuntimeError(f"CAN {direction} observation time changed")
    controllers = _object(_field(state, "controllers", property_name), "controllers")
    return _object(_field(controllers, controller, "controllers"), controller)


def _can_tx_state(value, controller, timestamp_ns, direction):
    tx = _can_transfer_state(value, "fdcan-tx-state", controller,
                             timestamp_ns, direction)
    for field, maximum in (("pending_frames", 16), ("pending_bytes", 1344),
                           ("tx_dropped", 0xffffffffffffffff),
                           ("tx_short_writes", 0xffffffffffffffff)):
        _integer(_field(tx, field, controller), f"{controller}.{field}", maximum)
    frames, count = tx["pending_frames"], tx["pending_bytes"]
    if not frames <= count <= frames * 84:
        raise ValueError("inconsistent CAN output pending counts")
    return tx


def _can_rx_state(value, controller, timestamp_ns, direction):
    rx = _can_transfer_state(value, "fdcan-rx-state", controller,
                             timestamp_ns, direction)
    for field, maximum in (("completed_frames", 0xfffffffffffffffe),
                           ("partial_bytes", 83),
                           ("rx_dropped", 0xffffffffffffffff)):
        _integer(_field(rx, field, controller), f"{controller}.{field}", maximum)
    return rx


def _empty_can_output(stream, guard):
    guard()
    if stream.buffer:
        raise RuntimeError("drain CAN output before waiting for input")
    try:
        pending = stream.sock.recv(1, socket.MSG_PEEK)
    except BlockingIOError:
        pass
    else:
        if not pending:
            raise ConnectionError("QEMU FDCAN socket closed")
        raise RuntimeError("drain CAN output before waiting for input")
    guard()


def observe_initial_paused_can(qmp, stream: CanStream, *, timestamp_ns: int,
                               controller: str, chardev: str,
                               deadline: float, check) -> dict:
    """Observe an empty original CAN connection before the first CPU target.

    The owner keeps native execution idle at the supplied RESET origin, without
    prior input, output producers, reset, restore, rail, configuration, purge or
    connection changes. This borrows the same canonical stream, sole reader and
    writer, deadline and QMP bounds as drain_paused_can; it never reads output,
    flushes input, advances time or owns cleanup. Two complete observations must
    agree before returning the native TX/RX controller objects as a baseline.

    This does not establish an input receipt, firmware readiness, timer closure
    or CPU/world barrier. On failure, retain effects/resources and freeze.
    """
    _integer(timestamp_ns, "timestamp_ns", 0x7fffffffffffffff)
    guard, observe_state = _paused_can_io(
        qmp, stream, target_ns=timestamp_ns, controller=controller, chardev=chardev,
        deadline=deadline, check=check, direction="initial-baseline", initial=True)

    def observe():
        if stream.tx_pending or stream.buffer:
            raise RuntimeError("CAN initial baseline requires empty host queues")
        tx, rx = observe_state()
        if tx["pending_frames"] or tx["pending_bytes"] or rx["partial_bytes"]:
            raise RuntimeError("CAN initial baseline requires empty native queues")
        _empty_can_output(stream, guard)
        if stream.tx_pending:
            raise RuntimeError("CAN initial baseline host input changed")
        return {"tx": tx, "rx": rx}

    first = observe()
    final = observe()
    for direction, fields in (("tx", ("tx_dropped", "tx_short_writes")),
                              ("rx", ("completed_frames", "rx_dropped"))):
        if any(first[direction][field] != final[direction][field] for field in fields):
            raise RuntimeError("CAN initial baseline changed during observation")
    guard()
    return final


def drain_paused_can(qmp, stream: CanStream, sink, *, target_ns: int,
                     controller: str, chardev: str, tx_dropped: int,
                     deadline: float, check) -> None:
    """Publish already admitted CAN output to a borrowed per-frame ``sink``.

    Restricted to the original plain nonblocking AF_UNIX connection, sole writer
    and sole reader, without mux or FD passing. The owner must quiesce all
    producers and exclude reset, purge, power, configuration and connection
    changes from the execution fence until return.
    Paused CPUs alone do not establish this premise. ``tx_dropped`` is the
    no-drop baseline established before that producer interval.

    The caller owns QMP command timeouts, liveness ``check()``, and the absolute
    finite ``time.monotonic()`` deadline. Optional ``command_many`` batches each
    fresh independent observation; checks surround the whole group instead of
    each scalar reply. Batch failures propagate without fallback or retry.
    Checks surround dependency calls and cannot preempt a blocking QMP/check/sink.
    Errors propagate with partial sink effects and physical tails retained. The
    caller must freeze the attempt; this function does not recover, roll back,
    close resources or advance time.
    Success is output consumption, not a CPU/world or equal-time timer barrier.
    """
    if not callable(sink):
        raise TypeError("QMP command, check and sink must be callable")
    _integer(tx_dropped, "tx_dropped", 0xffffffffffffffff)
    guard, observe_state = _paused_can_io(
        qmp, stream, target_ns=target_ns, controller=controller, chardev=chardev,
        deadline=deadline, check=check, direction="output")

    def observe():
        tx, = observe_state()
        if tx["tx_dropped"] != tx_dropped:
            raise RuntimeError("CAN output drop count changed")
        return tx["pending_frames"] == 0

    while True:
        empty = observe()
        guard()
        batch = stream.receive()  # Canonical parser: exactly one recv(4096).
        guard()
        for frame in batch:
            guard()
            sink(frame)
            guard()
        # With the canonical parser, [] and an empty tail means this fresh
        # receive got EAGAIN (EOF and malformed records raise). A previous empty
        # read cannot be reused after observing producer zero.
        if empty and not batch and not stream.buffer:
            if not observe():
                raise RuntimeError("CAN output producer changed during drain")
            guard()
            return


def wait_paused_can_input(qmp, stream: CanStream, *, target_ns: int,
                          controller: str, chardev: str, completed_frames: int,
                          deadline: float, check, initial: bool = False) -> dict:
    """Flush admitted input and observe its synchronous native processing.

    The caller establishes a pre-send native baseline and the exact number of
    complete records on this sole writer, reserving the absolute goal strictly
    below UINT64_MAX. The original peer, reached/paused target and configuration
    must remain unchanged from before sending; reset, restore, rail, purge,
    reconnect and extra input invalidate that attempt.

    Drain QEMU output first. This operation only peeks at the original socket;
    unread output or a retained output tail is refused without consuming it.
    It borrows the same canonical stream and deadline/check/QMP bounds as
    drain_paused_can, preserving effects and resources on failure. Return the
    final native controller dict, including actual rx_dropped: completed input
    includes rejected records and does not prove FIFO or firmware consumption.
    No time is advanced and no baseline or count is cached across calls.
    ``initial=True`` admits the original idle/prelaunch cut before the first
    CPU request, with the same original connection and empty output premise.
    """
    if type(initial) is not bool:
        raise TypeError("initial must be a boolean")
    _integer(completed_frames, "completed_frames", 0xfffffffffffffffe)
    guard, observe_state = _paused_can_io(
        qmp, stream, target_ns=target_ns, controller=controller, chardev=chardev,
        deadline=deadline, check=check, direction="input", initial=initial)
    if stream.buffer:
        raise RuntimeError("drain CAN output before waiting for input")

    def observe():
        states = observe_state()
        if initial and (states[0]["pending_frames"] or states[0]["pending_bytes"]):
            raise RuntimeError("CAN initial input requires empty native output")
        rx = states[-1]
        if rx["completed_frames"] > completed_frames:
            raise RuntimeError("CAN input completed count exceeds goal")
        _empty_can_output(stream, guard)
        return rx

    while True:
        rx = observe()
        if rx["completed_frames"] == completed_frames:
            if rx["partial_bytes"] or stream.tx_pending:
                raise RuntimeError("CAN input has bytes beyond completed goal")
            final = observe()
            if (final["completed_frames"] != completed_frames or
                    final["partial_bytes"] or stream.tx_pending):
                raise RuntimeError("CAN input changed during final observation")
            guard()
            return final
        guard()
        stream.flush()
        guard()


class QemuBoard:
    """Borrow an already connected synchronous QMP ``command`` interface.

    The caller owns connections, command deadlines and process lifecycle.
    This adapter neither closes QMP nor determines whether an application is
    ready. Input setters use the board's native GPIO/EXTI and power producers.
    """

    def __init__(self, qmp):
        command = getattr(qmp, "command", None)
        if not callable(command):
            raise TypeError("QemuBoard requires a callable QMP command")
        self._command = command

    def snapshot(self) -> dict:
        """Return the validated native board-state object without caching it."""
        return validate_snapshot(self._command("qom-get", {
            "path": "/machine", "property": "board-state",
        }))

    def digital(self, endpoint: str, snapshot: dict | None = None) -> int:
        """Read ``gpio.B0``, ``key`` level or ``led`` RGB nonzero as 0 or 1.

        A supplied snapshot is validated and borrowed without a QMP command.
        The key's sampled level is distinct from its host ``pressed`` input.
        See ``led.source`` to distinguish waveform and latch observations.
        """
        _digital_pin(endpoint)  # Reject before acquiring a snapshot over QMP.
        return digital_level(endpoint, self.snapshot() if snapshot is None else snapshot)

    def set_gpio(self, pin: str, level: int) -> None:
        validate_gpio_pin(pin)
        _integer(level, "GPIO level", 1)
        self._command("qom-set", {
            "path": "/machine", "property": "gpio-input",
            "value": f"{pin}={level}",
        })

    def set_key(self, pressed: bool) -> None:
        if type(pressed) is not bool:
            raise TypeError("key pressed must be a boolean")
        self._command("qom-set", {
            "path": "/machine", "property": "user-key", "value": pressed,
        })

    def set_vin(self, millivolts: int) -> None:
        _integer(millivolts, "VIN millivolts", 0xffffffff)
        self._command("qom-set", {
            "path": "/machine", "property": "vin-mv",
            "value": str(millivolts),
        })

    def pause(self) -> None:
        self._command("stop")

    def resume(self) -> None:
        self._command("cont")

    def reset(self) -> None:
        self._command("system_reset")
