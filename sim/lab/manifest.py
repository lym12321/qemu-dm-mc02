"""Pure shared assertion records/parser and native interactive input records.

Finite automated configurations use sim.config.scene. LabManifest remains an
observation-only input for the existing Scenario/replay consumers. No file
loader, execution scheduler or resource ownership lives here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from sim.config.yaml import (ManifestError, fields as _fields,
                             parse_time as _time, text_bytes as _text)

from sim.gpio import validate_gpio_pin
from sim.fabric import CanFrame
from sim.fabric.frame import CAN_EFF_MASK, CAN_SFF_MASK, WIRE_EXTENDED, WIRE_FD

MAX_ITEMS = 1024
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


def _can(value, label: str) -> CanMatch:
    item = _fields(value, ("id", "flags", "dlc", "data_hex"), ("id",), label)
    can_id = _integer(item["id"], CAN_EFF_MASK, label + ".id")
    flags = _integer(item["flags"], 15, label + ".flags") if "flags" in item else None
    dlc = _integer(item["dlc"], 15, label + ".dlc") if "dlc" in item else None
    data = _data_hex(item["data_hex"], label + ".data_hex") if "data_hex" in item else None
    # An omitted match flag is unconstrained, so validate a possible canonical
    # frame rather than silently imposing standard/classic flags on the match.
    checked_flags = flags if flags is not None else 0
    if flags is None:
        if can_id > CAN_SFF_MASK:
            checked_flags |= WIRE_EXTENDED
        if (dlc is not None and dlc > 8) or (data is not None and len(data) > 8):
            checked_flags |= WIRE_FD
    try:
        CanFrame(can_id, checked_flags, dlc, data or b"")
    except (TypeError, ValueError) as error:
        raise ManifestError(f"{label}: {error}") from error
    return CanMatch(can_id, flags, dlc, data)


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
                        label + ".can_seen")
    return Assertion(before_ns, endpoint, kind, expected)
