"""Persist actual Arena observation batches, without owning runtime or replay.

One message retains the post-read native snapshot and the entire ordered UART
batch, including an empty batch. Nested UART times are polling samples; only
the outer snapshot time bounds their read. Clocks/epochs are explicitly named
by the caller and are never converted to shared world time.
"""

import base64
from dataclasses import dataclass
import json

from sim.boards import validate_snapshot
from sim.boards.channels import ChannelEvent
from sim.boards.launch import UARTS
from sim.trace import TraceEvent, TraceWriter
from sim.trace.storage import MAX_CHANNELS, MAX_PAYLOAD_BYTES

from .runtime import ArenaBoardResult, ArenaObservation, ArenaResult


ENDPOINT = "observation"
EVENT_TYPE = "arena.observation/1"
COMPLETION_TYPE = "arena.completion/1"
_JSON = json.JSONEncoder(ensure_ascii=True, allow_nan=False, separators=(",", ":"))


@dataclass(frozen=True)
class ArenaTraceCompletion:
    """One member's decoded original RESET origin and final capture boundary."""

    name: str
    clock: str
    reset_qemu_ns: int
    end_qemu_ns: int
    observation_batches: int


def _uint(value, name: str, maximum=0xFFFFFFFFFFFFFFFF) -> int:
    if type(value) is not int:
        raise TypeError(f"Arena trace {name} must be an integer")
    if not 0 <= value <= maximum:
        raise ValueError(f"Arena trace {name} is out of range")
    return value


def _clocks(value) -> dict[str, str]:
    if type(value) is not dict:
        raise TypeError("Arena trace clocks must be a plain member-to-clock dictionary")
    if not 1 <= len(value) <= MAX_CHANNELS:
        raise ValueError("Arena trace clocks require 1..256 members")
    for member, clock in value.items():
        TraceEvent(0, member, ENDPOINT, EVENT_TYPE, b"", clock)
    if len(set(value.values())) != len(value):
        raise ValueError("Arena trace members require distinct clock/epoch labels")
    return dict(value)


def _same_json(left, right) -> bool:
    """Reject Python-only values which the stdlib would silently normalize."""
    if type(left) is not type(right):
        return False
    if type(right) is dict:
        return (all(type(key) is str for key in left) and left.keys() == right.keys()
                and all(_same_json(left[key], right[key]) for key in right))
    if type(right) is list:
        return (len(left) == len(right) and
                all(_same_json(a, b) for a, b in zip(left, right)))
    return left == right


def _payload(document) -> bytes:
    # Native JSON owns escaping, cycles and number semantics. Bound its output
    # before accumulating a full payload; no event history or second parser.
    data = bytearray()
    for piece in _JSON.iterencode(document):
        raw = piece.encode("ascii")
        if len(raw) > MAX_PAYLOAD_BYTES - len(data):
            raise ValueError("Arena observation payload exceeds 65536 bytes")
        data.extend(raw)
    payload = bytes(data)
    if not _same_json(document, json.loads(payload)):
        raise TypeError("Arena observation requires lossless plain JSON values")
    return payload


def _validate(item: ArenaObservation) -> None:
    if type(item) is not ArenaObservation:
        raise TypeError("Arena trace requires an ArenaObservation")
    state = validate_snapshot(item.snapshot)
    if type(item.events) is not list:
        raise TypeError("Arena observation events must be a plain list")
    if len(item.events) > len(UARTS):
        raise ValueError("Arena observation has too many UART events")
    endpoints = set()
    for event in item.events:
        if type(event) is not ChannelEvent:
            raise TypeError("Arena observation requires ChannelEvent values")
        if type(event.endpoint) is not str or event.endpoint not in UARTS:
            raise ValueError("Arena observation requires a known UART endpoint")
        if event.endpoint in endpoints:
            raise ValueError("Arena observation has a duplicate UART endpoint")
        endpoints.add(event.endpoint)
        if type(event.timestamp_ns) is not int:
            raise TypeError("Arena UART polling timestamp must be an integer")
        if not 0 <= event.timestamp_ns <= state["timestamp_ns"]:
            raise ValueError("Arena UART polling timestamp exceeds the post-read bound")
        if type(event.data) is not bytes:
            raise TypeError("Arena UART data must be bytes")
        if not 1 <= len(event.data) <= 4096:
            raise ValueError("Arena UART data must contain 1..4096 bytes")


class ArenaTrace:
    """Borrow a writer as an Arena ``on_observation`` callback.

    Supply all intended members with distinct, explicit QEMU epoch labels.
    The mapping is copied; it does not infer or validate manifest topology.
    Any callback failure freezes this adapter: observations already consumed by
    Arena cannot be retried. Complete admission precedes the single append,
    whose IO effects cannot be rolled back. Caller owns finish/files/cleanup.

    Optionally call complete with the actual result returned after successful
    Arena cleanup. Its original RESET origins and final bounds/counts are checked
    against capture before metadata is appended. These checks do not authenticate
    the caller or validate every intermediate timestamp in a forged history.
    """

    def __init__(self, writer: TraceWriter, *, clocks: dict[str, str]):
        if type(writer) is not TraceWriter:
            raise TypeError("Arena trace requires a TraceWriter")
        self._clocks = _clocks(clocks)
        self._writer = writer
        self._failed = False
        self._completed = False
        self._observed: dict[str, tuple[int, int, int]] = {}

    def _active(self) -> None:
        if self._failed:
            raise RuntimeError("Arena trace failed; observations cannot be retried")
        if self._completed:
            raise RuntimeError("Arena trace is complete")

    def __call__(self, item: ArenaObservation) -> None:
        self._active()
        try:
            _validate(item)
            if type(item.name) is not str or item.name not in self._clocks:
                raise ValueError("unknown Arena trace member")
            event = TraceEvent(item.snapshot["timestamp_ns"], item.name, ENDPOINT,
                               EVENT_TYPE, b"", self._clocks[item.name])
            document = {"snapshot": item.snapshot, "events": [
                {"endpoint": entry.endpoint, "timestamp_ns": entry.timestamp_ns,
                 "data_base64": base64.b64encode(entry.data).decode("ascii")}
                for entry in item.events]}
            payload = _payload(document)
            self._writer.append(TraceEvent(event.timestamp_ns, event.source,
                                event.endpoint, event.event_type, payload, event.clock))
            count, first, _ = self._observed.get(item.name, (0, event.timestamp_ns, 0))
            self._observed[item.name] = (count + 1, first, event.timestamp_ns)
        except BaseException:
            self._failed = True
            raise

    def complete(self, result: ArenaResult) -> None:
        """Append final metadata once, using the actual post-cleanup result.

        All result admission precedes the first append. Completion adds one
        channel identity per member, so at most 128 members can fit a lifecycle.
        Existing writer channels/sequence may still exhaust capacity. Any error
        freezes this adapter; partial metadata/IO is retained, never retried.
        This neither finishes the borrowed writer nor closes its output.
        """
        self._active()
        try:
            if type(result) is not ArenaResult:
                raise TypeError("Arena trace completion requires an ArenaResult")
            if (type(result.boards) is not tuple or
                    any(type(board) is not ArenaBoardResult for board in result.boards)):
                raise TypeError("Arena trace completion requires an ArenaBoardResult tuple")
            if any(type(board.name) is not str for board in result.boards):
                raise TypeError("Arena trace completion member names must be strings")
            names = [board.name for board in result.boards]
            if (len(set(names)) != len(names) or set(names) != self._clocks.keys()
                    or self._observed.keys() != self._clocks.keys()):
                raise ValueError("Arena trace completion members differ from configured capture")
            if 2 * len(names) > MAX_CHANNELS:
                raise BufferError("Arena trace lifecycle exceeds channel capacity")
            events = []
            for board in result.boards:
                reset = _uint(board.reset_qemu_ns, "reset_qemu_ns")
                end = _uint(board.end_qemu_ns, "end_qemu_ns")
                count = _uint(board.observation_batches, "observation_batches", 0xFFFFFFFF)
                observed, first, last = self._observed[board.name]
                if not count or count != observed or not reset <= first <= end or last != end:
                    raise ValueError("Arena trace completion differs from captured bounds/count")
                payload = _payload({"reset_qemu_ns": reset, "observation_batches": count})
                events.append(TraceEvent(end, board.name, ENDPOINT, COMPLETION_TYPE,
                                         payload, self._clocks[board.name]))
            for event in events:
                self._writer.append(event)
            self._completed = True
        except BaseException:
            self._failed = True
            raise


def decode_observation(event: TraceEvent, *, clocks: dict[str, str]) -> ArenaObservation:
    """Decode one admitted observation without IO or consumer side effects.

    Fully exhaust the containing ``iter_events`` before accepting its events;
    CRC/footer/EOF failure can follow a yielded event. This function does not
    validate a complete run, RESET origin, cross-batch order or replay schedule.
    """
    clocks = _clocks(clocks)
    if type(event) is not TraceEvent:
        raise TypeError("Arena observation decode requires a TraceEvent")
    if (event.endpoint != ENDPOINT or event.event_type != EVENT_TYPE or
            event.source not in clocks or event.clock != clocks[event.source]):
        raise ValueError("unexpected Arena observation profile, member or clock/epoch")
    document = json.loads(event.payload)
    if type(document) is not dict or set(document) != {"snapshot", "events"}:
        raise ValueError("Arena observation payload requires snapshot and events")
    if _payload(document) != event.payload:
        raise ValueError("Arena observation payload is not compact canonical JSON")
    state = validate_snapshot(document["snapshot"])
    if state["timestamp_ns"] != event.timestamp_ns:
        raise ValueError("Arena observation timestamp differs from post-read snapshot")
    entries = document["events"]
    if type(entries) is not list or len(entries) > len(UARTS):
        raise ValueError("Arena observation events must be a bounded list")
    events = []
    for entry in entries:
        if type(entry) is not dict or set(entry) != {"endpoint", "timestamp_ns", "data_base64"}:
            raise ValueError("invalid Arena observation UART fields")
        encoded = entry["data_base64"]
        if type(encoded) is not str or len(encoded) > 5464:
            raise ValueError("Arena UART data_base64 must be a bounded string")
        data = base64.b64decode(encoded, validate=True)
        if base64.b64encode(data).decode("ascii") != encoded:
            raise ValueError("Arena UART data_base64 is not canonical")
        events.append(ChannelEvent(entry["endpoint"], entry["timestamp_ns"], data))
    item = ArenaObservation(event.source, state, events)
    _validate(item)
    return item


def decode_completion(event: TraceEvent, *, clocks: dict[str, str]) -> ArenaTraceCompletion:
    """Decode pure per-member metadata; full run validation belongs to replay.

    A complete MCAP container and this record alone do not prove that all
    members/batches exist in order or that the caller supplied a genuine result.
    """
    clocks = _clocks(clocks)
    if type(event) is not TraceEvent:
        raise TypeError("Arena completion decode requires a TraceEvent")
    if (event.endpoint != ENDPOINT or event.event_type != COMPLETION_TYPE or
            event.source not in clocks or event.clock != clocks[event.source]):
        raise ValueError("unexpected Arena completion profile, member or clock/epoch")
    document = json.loads(event.payload)
    if (type(document) is not dict or
            set(document) != {"reset_qemu_ns", "observation_batches"}):
        raise ValueError("Arena completion payload requires reset_qemu_ns and observation_batches")
    if _payload(document) != event.payload:
        raise ValueError("Arena completion payload is not compact canonical JSON")
    reset = _uint(document["reset_qemu_ns"], "reset_qemu_ns")
    count = _uint(document["observation_batches"], "observation_batches", 0xFFFFFFFF)
    if not count or reset > event.timestamp_ns:
        raise ValueError("Arena completion has invalid reset/count bounds")
    return ArenaTraceCompletion(event.source, event.clock, reset, event.timestamp_ns, count)
