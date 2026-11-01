"""Pure shared assertion admission and bounded observation matching.

Consumers admit endpoint capabilities and map their actual observation times.
This module owns no board projection, IO, resource lifecycle or simulation clock.
UART observations require a completed upper bound; CAN observations retain the
admitted mapped wire time. Matching precedes expiry and terminal status persists.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from sim.config.yaml import (ManifestError, fields as _fields,
                             parse_time as _time, text_bytes as _text)
from sim.fabric import CanFrame
from sim.fabric.can import check_can_time
from sim.fabric.frame import CAN_EFF_MASK, CAN_SFF_MASK, WIRE_EXTENDED, WIRE_FD


@dataclass(frozen=True)
class CanMatch:
    """CAN ID plus optional exact fields; None leaves a field unconstrained."""

    can_id: int
    flags: int | None = None
    dlc: int | None = None
    data: bytes | None = None


@dataclass(frozen=True)
class Assertion:
    """One uart_contains/digital_equals/can_seen condition and strict deadline."""

    before_ns: int
    endpoint: str
    kind: str
    value: bytes | int | CanMatch


def _integer(value, maximum: int, label: str) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ManifestError(f"{label} must be an integer in 0..{maximum}")
    return value


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


def parse_assertion(value, index: int, *, endpoint_validator) -> Assertion:
    """Admit one condition without reading files or acquiring resources.

    The caller supplies a pure ``endpoint_validator(kind, endpoint)`` and owns
    endpoint capabilities, list quotas and duration limits. This parser owns
    only condition payloads and strict deadlines, with no board defaults.
    ``index`` is the zero-based position used in contextual diagnostics.
    """
    if not callable(endpoint_validator):
        raise TypeError("assertion endpoint_validator must be callable")
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
    endpoint = endpoint_validator(kind, endpoint)
    if kind == "uart_contains":
        expected = _text(condition["text"], label + ".text")
    elif kind == "digital_equals":
        expected = _integer(condition["value"], 1, label + ".value")
    else:
        expected = _can({key: val for key, val in condition.items() if key != "endpoint"},
                        label + ".can_seen")
    return Assertion(before_ns, endpoint, kind, expected)


@dataclass(frozen=True)
class AssertionObservation:
    """Actual bytes/frame and their admitted assertion timestamp."""

    endpoint: str
    timestamp_ns: int
    data: bytes | CanFrame


class AssertionEvaluator:
    """Pure shared strict-before matcher; no input dispatch, IO or clock owner."""

    def __init__(self, assertions: tuple[Assertion, ...], duration_ns: int, origin_ns: int):
        check_can_time(origin_ns)
        check_can_time(duration_ns)
        check_can_time(origin_ns + duration_ns)
        if (type(assertions) is not tuple or any(type(item) is not Assertion for item in assertions)
                or any(item.kind not in ("uart_contains", "digital_equals", "can_seen")
                       for item in assertions)):
            raise ValueError("unsupported typed assertions")
        self.assertions, self.duration_ns = assertions, duration_ns
        self.origin_ns = self._time_ns = origin_ns
        self._status = ["pending"] * len(assertions)
        self._tails = [b""] * len(assertions)
        self._finished = False

    @property
    def status(self) -> tuple[str, ...]:
        return tuple(self._status)

    @property
    def all_passed(self) -> bool:
        return all(status == "pass" for status in self._status)

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(
            f"assertion[{index + 1}] {assertion.kind} {assertion.endpoint} "
            f"did not match before {assertion.before_ns} ns"
            for index, assertion in enumerate(self.assertions)
            if self._status[index] == "fail")

    def _check_clock(self, timestamp_ns: int) -> None:
        check_can_time(timestamp_ns, self._time_ns)

    def _expire(self, timestamp_ns: int) -> None:
        for index, assertion in enumerate(self.assertions):
            if (self._status[index] == "pending" and
                    timestamp_ns - self.origin_ns >= assertion.before_ns):
                self._status[index] = "fail"
                self._tails[index] = b""

    def observe_values(self, digital, events, uart_bound_ns: int, *, expire=True):
        """Consume actual projections; optionally defer expiry until a cut ends.

        Deferred expiry permits streaming same-cut batches without retaining a
        transcript. The consumer must close each completed cut with advance().
        """
        self._check_clock(uart_bound_ns)
        if self._finished:
            raise RuntimeError("Lab experiment is finished")
        for endpoint, value in digital.items():
            if not isinstance(endpoint, str) or type(value) is not int or value not in (0, 1):
                raise ValueError("digital observations require endpoint and integer level")
        for event in events:
            if not isinstance(event, AssertionObservation):
                raise TypeError("assertion observations require AssertionObservation")
            check_can_time(event.timestamp_ns)
            if event.timestamp_ns > uart_bound_ns:
                raise ValueError("observation is later than its completed bound")
            if not isinstance(event.endpoint, str) or not event.endpoint:
                raise ValueError("observation endpoint must be nonempty")
            if not isinstance(event.data, (bytes, CanFrame)):
                raise TypeError("observation data must be bytes or CanFrame")
        elapsed_ns = uart_bound_ns - self.origin_ns
        for index, assertion in enumerate(self.assertions):
            if self._status[index] != "pending":
                continue
            matched = False
            if assertion.kind == "digital_equals":
                matched = (elapsed_ns < assertion.before_ns and
                           digital.get(assertion.endpoint) == assertion.value)
            else:
                for event in events:
                    if event.endpoint != assertion.endpoint:
                        continue
                    if (assertion.kind == "uart_contains" and
                            isinstance(event.data, bytes) and
                            self.origin_ns <= event.timestamp_ns <
                            self.origin_ns + assertion.before_ns):
                        combined = self._tails[index] + event.data
                        matched = assertion.value in combined
                        keep = len(assertion.value) - 1
                        self._tails[index] = combined[-keep:] if keep else b""
                    elif (assertion.kind == "can_seen" and
                          isinstance(event.data, CanFrame) and
                          self.origin_ns <= event.timestamp_ns <
                          self.origin_ns + assertion.before_ns):
                        frame = event.data
                        wanted = assertion.value
                        matched = (
                            frame.can_id == wanted.can_id and
                            (wanted.flags is None or frame.flags == wanted.flags) and
                            (wanted.dlc is None or frame.dlc == wanted.dlc) and
                            (wanted.data is None or frame.data == wanted.data))
                    if matched:
                        break
            if matched:
                self._status[index] = "pass"
                self._tails[index] = b""
        if expire:
            self._expire(uart_bound_ns)
        self._time_ns = uart_bound_ns

    def advance(self, timestamp_ns: int):
        """Expire a completed observation cut, including one without events."""
        self._check_clock(timestamp_ns)
        if self._finished:
            raise RuntimeError("Lab experiment is finished")
        self._expire(timestamp_ns)
        self._time_ns = timestamp_ns

    def finish(self, timestamp_ns: int) -> tuple[str, ...]:
        """Finalize assertion status at or after the configured duration."""
        self._check_clock(timestamp_ns)
        if timestamp_ns < self.origin_ns + self.duration_ns:
            raise ValueError("Lab experiment cannot finish before its duration")
        self._expire(timestamp_ns)
        self._time_ns = timestamp_ns
        self._finished = True
        return self.status
