"""Borrow native QMP for DM-MC02 board inputs and read-only observations.

Snapshots contain QEMU virtual time, without advancing it or establishing a
CPU/plant time barrier. GPIO samples retain the model's digital approximation
for output, alternate-function and analog modes; they are not physical lines.
LED source ``ws2812-dma`` observes a DMA waveform, while ``gpio-latch`` is an
approximation of the GPIO latch and does not establish electrical LED drive.
"""

from __future__ import annotations

import re


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
