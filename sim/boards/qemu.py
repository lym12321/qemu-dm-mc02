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


def validate_gpio_pin(pin: str) -> str:
    """Accept the public GPIO spelling A0 through H15 without normalization."""
    if not isinstance(pin, str):
        raise TypeError("GPIO pin must be a string")
    if re.fullmatch(r"[A-H](?:[0-9]|1[0-5])", pin) is None:
        raise ValueError("GPIO pin must be A0 through H15")
    return pin


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


def _snapshot(value) -> dict:
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
    state = _snapshot(snapshot)
    if pin is not None:
        return (state["gpio"][pin[0]]["sampled"] >> int(pin[1:])) & 1
    if endpoint == "key":
        return state["key"]["level"]
    return int(state["led"]["rgb"] != 0)


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
    finite ``time.monotonic()`` deadline. Checks surround dependency calls; they
    cannot preempt a blocking QMP/check/sink. Errors propagate with partial sink
    effects and physical tails retained. The caller must freeze the attempt;
    this function does not recover, roll back, close resources or advance time.
    Success is output consumption, not a CPU/world or equal-time timer barrier.
    """
    command = getattr(qmp, "command", None)
    if not callable(command) or not callable(check) or not callable(sink):
        raise TypeError("QMP command, check and sink must be callable")
    _integer(target_ns, "target_ns", 0x7fffffffffffffff)
    _integer(tx_dropped, "tx_dropped", 0xffffffffffffffff)
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
            raise TimeoutError("paused CAN output deadline expired")
        check()
        if time.monotonic() >= deadline:
            raise TimeoutError("paused CAN output deadline expired")

    def query(name, arguments=None):
        guard()
        value = command(name, arguments)
        guard()
        return value

    def observe():
        execution = _object(query("x-query-virtual-execution"), "execution")
        for field in ("current-ns", "target-ns"):
            value = _field(execution, field, "execution")
            _integer(value, f"execution.{field}", 0x7fffffffffffffff)
            if value != target_ns:
                raise RuntimeError("CAN output execution time or target changed")
        if _field(execution, "state", "execution") != "reached":
            raise RuntimeError("CAN output execution target is not reached")
        status = _object(query("query-status"), "query-status")
        if (status.get("running") is not False or status.get("status") != "paused"):
            raise RuntimeError("QEMU must remain paused during CAN output drain")
        if query("qom-get", {"path": f"/chardevs/{chardev}",
                              "property": "connected"}) is not True:
            raise ConnectionError("CAN output chardev is disconnected")
        state = _object(query("qom-get", {
            "path": "/machine", "property": "fdcan-tx-state",
        }), "fdcan-tx-state")
        schema = _field(state, "schema", "fdcan-tx-state")
        _integer(schema, "fdcan-tx-state.schema", 1)
        if schema != 1:
            raise ValueError("unsupported fdcan-tx-state schema")
        timestamp = _field(state, "timestamp_ns", "fdcan-tx-state")
        _integer(timestamp, "fdcan-tx-state.timestamp_ns", 0x7fffffffffffffff)
        if timestamp != target_ns:
            raise RuntimeError("CAN output observation time changed")
        controllers = _object(_field(state, "controllers", "fdcan-tx-state"),
                              "controllers")
        tx = _object(_field(controllers, controller, "controllers"), controller)
        for field, maximum in (("pending_frames", 16), ("pending_bytes", 1344),
                               ("tx_dropped", 0xffffffffffffffff),
                               ("tx_short_writes", 0xffffffffffffffff)):
            _integer(_field(tx, field, controller), f"{controller}.{field}", maximum)
        frames, count = tx["pending_frames"], tx["pending_bytes"]
        if not frames <= count <= frames * 84:
            raise ValueError("inconsistent CAN output pending counts")
        if tx["tx_dropped"] != tx_dropped:
            raise RuntimeError("CAN output drop count changed")
        return frames == 0

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
        return _snapshot(self._command("qom-get", {
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
