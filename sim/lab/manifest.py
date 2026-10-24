"""Bounded schema 1 Lab admission; this module starts no simulation resources.

Paths are relative to the YAML document, times are integer nanoseconds, and
UART text is UTF-8 bytes. Stimuli at the duration are allowed as final sends;
assertions must observe their condition strictly before their deadline.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from sim.config.yaml import (MAX_FILE_BYTES, MAX_TIME_NS, ManifestError, fields as _fields,
                          load_document, parse_time as _time, readable_file as _readable_file,
                          text_bytes as _text)

from sim.gpio import validate_gpio_pin
from sim.fabric import CanFrame
from sim.fabric.frame import CAN_EFF_MASK, CAN_SFF_MASK, WIRE_EXTENDED, WIRE_FD

MAX_ITEMS = 1024
MAX_TEXT_BYTES = 4096
_UARTS = frozenset(("uart1", "uart2", "uart3", "uart5", "uart7", "uart10"))
_CANS = frozenset(("can1", "can2", "can3"))


@dataclass(frozen=True)
class Ready:
    """An explicitly requested application UART marker, without a time guess."""

    endpoint: str
    text: bytes


@dataclass(frozen=True)
class CanMatch:
    """CAN ID plus optional exact fields; None leaves a field unconstrained."""

    can_id: int
    flags: int | None = None
    dlc: int | None = None
    data: bytes | None = None


@dataclass(frozen=True)
class Stimulus:
    """One scheduled uart/key/gpio/adc_raw/adc_voltage/can input."""

    at_ns: int
    endpoint: str
    kind: str
    value: bytes | bool | int | CanFrame


@dataclass(frozen=True)
class Assertion:
    """One uart_contains/digital_equals/can_seen condition and strict deadline."""

    before_ns: int
    endpoint: str
    kind: str
    value: bytes | int | CanMatch


@dataclass(frozen=True)
class LabManifest:
    """Resolved paths and admitted immutable inputs, preserving YAML order."""

    path: Path
    name: str
    board: str
    firmware: Path
    duration_ns: int
    stimulus: tuple[Stimulus, ...]
    assertions: tuple[Assertion, ...]
    ready: Ready | None = None


def _integer(value, maximum: int, label: str) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ManifestError(f"{label} must be an integer in 0..{maximum}")
    return value


def _endpoint(value, allowed, label: str) -> str:
    if type(value) is not str or value not in allowed:
        raise ManifestError(f"{label} has an unsupported endpoint")
    return value


def _digital_endpoint(value, label: str, *, led: bool = False) -> str:
    if type(value) is str and value.startswith("gpio."):
        try:
            validate_gpio_pin(value[5:])
        except (TypeError, ValueError) as error:
            raise ManifestError(f"{label} has an unsupported GPIO endpoint") from error
        return value
    return _endpoint(value, ("key", "led") if led else ("key",), label)


def _data_hex(value, label: str) -> bytes:
    if type(value) is not str or re.fullmatch(r"(?:[0-9a-fA-F]{2}){0,64}", value) is None:
        raise ManifestError(f"{label} must contain contiguous hex pairs for at most 64 bytes")
    return bytes.fromhex(value)


def _can(value, label: str, *, match: bool = False) -> CanFrame | CanMatch:
    item = _fields(value, ("id", "flags", "dlc", "data_hex"),
                   ("id",) if match else ("id", "dlc", "data_hex"), label)
    can_id = _integer(item["id"], CAN_EFF_MASK, label + ".id")
    flags = _integer(item["flags"], 15, label + ".flags") if "flags" in item else None
    dlc = _integer(item["dlc"], 15, label + ".dlc") if "dlc" in item else None
    data = _data_hex(item["data_hex"], label + ".data_hex") if "data_hex" in item else None
    # An omitted match flag is unconstrained, so validate a possible canonical
    # frame rather than silently imposing standard/classic flags on the match.
    checked_flags = flags if flags is not None else 0
    if match and flags is None:
        if can_id > CAN_SFF_MASK:
            checked_flags |= WIRE_EXTENDED
        if (dlc is not None and dlc > 8) or (data is not None and len(data) > 8):
            checked_flags |= WIRE_FD
    try:
        frame = CanFrame(can_id, checked_flags, dlc, data or b"")
    except (TypeError, ValueError) as error:
        raise ManifestError(f"{label}: {error}") from error
    return CanMatch(can_id, flags, dlc, data) if match else frame


def _stimulus(value, index: int) -> Stimulus:
    label = f"stimulus[{index}]"
    item = _fields(value, ("at", "endpoint", "bytes", "pressed", "value",
                           "raw", "voltage_uv", "frame"), ("at", "endpoint"), label)
    at_ns = _time(item["at"], label + ".at")
    endpoint = item["endpoint"]
    if type(endpoint) is not str:
        raise ManifestError(f"{label}.endpoint must be a string")
    if endpoint in _UARTS:
        _fields(item, ("at", "endpoint", "bytes"), ("bytes",), label)
        return Stimulus(at_ns, endpoint, "uart", _text(item["bytes"], label + ".bytes", empty=True))
    if endpoint in _CANS:
        _fields(item, ("at", "endpoint", "frame"), ("frame",), label)
        return Stimulus(at_ns, endpoint, "can", _can(item["frame"], label + ".frame"))
    channel = re.fullmatch(r"adc1\.ch(?:[0-9]|[12][0-9]|3[01])", endpoint)
    if channel:
        _fields(item, ("at", "endpoint", "raw", "voltage_uv"), (), label)
        if ("raw" in item) == ("voltage_uv" in item):
            raise ManifestError(f"{label} requires exactly one of raw or voltage_uv")
        field = "raw" if "raw" in item else "voltage_uv"
        number = _integer(item[field], 65535 if field == "raw" else 3300000,
                          label + "." + field)
        return Stimulus(at_ns, endpoint, "adc_raw" if field == "raw" else "adc_voltage", number)
    endpoint = _digital_endpoint(endpoint, label)
    if endpoint == "key":
        _fields(item, ("at", "endpoint", "pressed"), ("pressed",), label)
        if type(item["pressed"]) is not bool:
            raise ManifestError(f"{label}.pressed must be a boolean")
        return Stimulus(at_ns, endpoint, "key", item["pressed"])
    _fields(item, ("at", "endpoint", "value"), ("value",), label)
    return Stimulus(at_ns, endpoint, "gpio", _integer(item["value"], 1, label + ".value"))


def parse_assertion(value, index: int, *, endpoint_validator=None) -> Assertion:
    """Admit one Lab condition without reading files or importing runtime IO.

    ``index`` is its zero-based position for contextual diagnostics. The caller
    owns list quotas, duration limits and which endpoints its runtime observes.
    A pure ``endpoint_validator(kind, endpoint)`` may replace native Lab endpoint
    admission while retaining the same condition payload and deadline parsing.
    """
    label = f"assertions[{index}]"
    kinds = ("uart_contains", "digital_equals", "can_seen")
    item = _fields(value, ("before", *kinds), ("before",), label)
    selected = [kind for kind in kinds if kind in item]
    if len(selected) != 1:
        raise ManifestError(f"{label} requires exactly one assertion kind")
    before_ns = _time(item["before"], label + ".before", zero=False)
    kind = selected[0]
    fields = ("endpoint", "text") if kind == "uart_contains" else (
        ("endpoint", "value") if kind == "digital_equals" else
        ("endpoint", "id", "flags", "dlc", "data_hex"))
    required = ("endpoint", "id") if kind == "can_seen" else fields
    condition = _fields(item[kind], fields, required, label + "." + kind)
    endpoint = condition["endpoint"]
    if endpoint_validator is not None:
        endpoint = endpoint_validator(kind, endpoint)
    if kind == "uart_contains":
        if endpoint_validator is None:
            endpoint = _endpoint(endpoint, _UARTS, label)
        expected = _text(condition["text"], label + ".text")
    elif kind == "digital_equals":
        if endpoint_validator is None:
            endpoint = _digital_endpoint(endpoint, label, led=True)
        expected = _integer(condition["value"], 1, label + ".value")
    else:
        if endpoint_validator is None:
            endpoint = _endpoint(endpoint, _CANS, label)
        expected = _can({key: val for key, val in condition.items() if key != "endpoint"},
                        label + ".can_seen", match=True)
    return Assertion(before_ns, endpoint, kind, expected)


def _items(value, label: str) -> list:
    if type(value) is not list or len(value) > MAX_ITEMS:
        raise ManifestError(f"{label} must be a list with at most {MAX_ITEMS} entries")
    return value


def load_manifest(path: str | Path) -> LabManifest:
    """Read one UTF-8 YAML file and admit schema 1 without runtime side effects."""
    manifest_path, document = load_document(path)
    item = _fields(document, ("schema", "name", "board", "firmware", "duration",
                              "stimulus", "assertions", "ready"),
                   ("schema", "name", "board", "firmware"), "manifest")
    if _integer(item["schema"], 1, "schema") != 1:
        raise ManifestError("unsupported Lab schema")
    _text(item["name"], "name", maximum=256)
    if not item["name"].strip():
        raise ManifestError("name must not be blank")
    if type(item["board"]) is not str or item["board"] != "dm-mc02":
        raise ManifestError("board must be dm-mc02")
    _text(item["firmware"], "firmware")
    firmware = _readable_file(manifest_path.parent / item["firmware"], "firmware")
    stimuli = tuple(_stimulus(entry, index) for index, entry in enumerate(
        _items(item.get("stimulus", []), "stimulus")))
    assertions = tuple(parse_assertion(entry, index) for index, entry in enumerate(
        _items(item.get("assertions", []), "assertions")))
    latest_ns = max((*(entry.at_ns for entry in stimuli),
                     *(entry.before_ns for entry in assertions)), default=0)
    duration_ns = _time(item["duration"], "duration", zero=False) if "duration" in item else latest_ns
    if not duration_ns or latest_ns > duration_ns:
        raise ManifestError("duration must be positive and include all at/before times")
    ready = None
    if "ready" in item:
        condition = _fields(item["ready"], ("endpoint", "text"), ("endpoint", "text"), "ready")
        ready = Ready(_endpoint(condition["endpoint"], _UARTS, "ready"),
                      _text(condition["text"], "ready.text"))
    return LabManifest(manifest_path, item["name"], item["board"], firmware,
                       duration_ns, stimuli, assertions, ready)
