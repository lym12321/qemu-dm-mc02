"""Bounded schema 1 Lab admission; this module starts no simulation resources.

Paths are relative to the YAML document, times are integer nanoseconds, and
UART text is UTF-8 bytes. Stimuli at the duration are allowed as final sends;
assertions must observe their condition strictly before their deadline.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import yaml
from yaml.events import AliasEvent

from sim.boards import validate_gpio_pin
from sim.fabric import CanFrame
from sim.fabric.frame import CAN_EFF_MASK, CAN_SFF_MASK, WIRE_EXTENDED, WIRE_FD

MAX_FILE_BYTES = 1 << 20
MAX_ITEMS = 1024
MAX_TEXT_BYTES = 4096
MAX_TIME_NS = 3600 * 1_000_000_000
_UARTS = frozenset(("uart1", "uart2", "uart3", "uart5", "uart7", "uart10"))
_CANS = frozenset(("can1", "can2", "can3"))


class ManifestError(ValueError):
    """The complete Lab configuration was rejected before runtime admission."""


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


class _LabLoader(yaml.SafeLoader):
    """Use PyYAML safe constructors with bounded composition and unique keys."""

    def __init__(self, stream):
        super().__init__(stream)
        self._depth = 0
        self._nodes = 0

    def compose_node(self, parent, index):
        if self.check_event(AliasEvent) or self.peek_event().anchor is not None:
            raise ManifestError("YAML anchors and aliases are not supported")
        self._nodes += 1
        if self._nodes > 40000:
            raise ManifestError("YAML node limit exceeded")
        self._depth += 1
        try:
            if self._depth > 16:
                raise ManifestError("YAML nesting limit exceeded")
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1


def _unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if type(key) is not str:
            raise ManifestError("YAML mapping keys must be strings")
        if key in result:
            raise ManifestError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


_LabLoader.add_constructor("tag:yaml.org,2002:map", _unique_mapping)


def _fields(value, allowed, required, label: str) -> dict:
    if type(value) is not dict:
        raise ManifestError(f"{label} must be a mapping")
    unknown = set(value) - set(allowed)
    missing = set(required) - set(value)
    if unknown:
        raise ManifestError(f"{label} has unknown fields: {', '.join(sorted(unknown))}")
    if missing:
        raise ManifestError(f"{label} is missing fields: {', '.join(sorted(missing))}")
    return value


def _integer(value, maximum: int, label: str) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ManifestError(f"{label} must be an integer in 0..{maximum}")
    return value


def _text(value, label: str, *, empty: bool = False,
          maximum: int = MAX_TEXT_BYTES) -> bytes:
    if type(value) is not str:
        raise ManifestError(f"{label} must be a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as error:
        raise ManifestError(f"{label} must be valid UTF-8") from error
    if len(encoded) > maximum or (not empty and not encoded):
        raise ManifestError(f"{label} must contain {'0' if empty else '1'}..{maximum} UTF-8 bytes")
    return encoded


def _time(value, label: str, *, zero: bool = True) -> int:
    if type(value) is not str:
        raise ManifestError(f"{label} must be an integer ns/us/ms/s string")
    match = re.fullmatch(r"([0-9]{1,13})(ns|us|ms|s)", value)
    if match is None:
        raise ManifestError(f"{label} must be an integer ns/us/ms/s string")
    number = int(match[1]) * {"ns": 1, "us": 1000, "ms": 1_000_000,
                             "s": 1_000_000_000}[match[2]]
    if number > MAX_TIME_NS or (not zero and number == 0):
        raise ManifestError(f"{label} must be {'nonnegative' if zero else 'positive'} and at most 3600s")
    return number


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


def _assertion(value, index: int) -> Assertion:
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
    if kind == "uart_contains":
        endpoint = _endpoint(condition["endpoint"], _UARTS, label)
        expected = _text(condition["text"], label + ".text")
    elif kind == "digital_equals":
        endpoint = _digital_endpoint(condition["endpoint"], label, led=True)
        expected = _integer(condition["value"], 1, label + ".value")
    else:
        endpoint = _endpoint(condition["endpoint"], _CANS, label)
        expected = _can({key: val for key, val in condition.items() if key != "endpoint"},
                        label + ".can_seen", match=True)
    return Assertion(before_ns, endpoint, kind, expected)


def _items(value, label: str) -> list:
    if type(value) is not list or len(value) > MAX_ITEMS:
        raise ManifestError(f"{label} must be a list with at most {MAX_ITEMS} entries")
    return value


def _readable_file(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise ManifestError(f"{label} must be a regular file")
        with resolved.open("rb"):
            pass
    except (OSError, ValueError, RuntimeError) as error:
        if isinstance(error, ManifestError):
            raise
        raise ManifestError(f"{label} cannot be opened: {error}") from error
    return resolved


def load_manifest(path: str | Path) -> LabManifest:
    """Read one UTF-8 YAML file and admit schema 1 without runtime side effects."""
    if not isinstance(path, (str, Path)):
        raise ManifestError("manifest path must be a string or Path")
    manifest_path = _readable_file(Path(path), "manifest")
    try:
        with manifest_path.open("rb") as source:
            raw = source.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ManifestError("manifest exceeds 1 MiB")
        # _LabLoader inherits only safe_load's SafeLoader constructors.
        document = yaml.load(raw.decode("utf-8"), Loader=_LabLoader)
    except (OSError, UnicodeError, yaml.YAMLError, ValueError, TypeError, KeyError) as error:
        if isinstance(error, ManifestError):
            raise
        raise ManifestError(f"cannot parse manifest: {error}") from error
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
    assertions = tuple(_assertion(entry, index) for index, entry in enumerate(
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
