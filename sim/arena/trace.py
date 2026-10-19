"""Persist actual Arena observation batches, without owning runtime or replay.

One message retains the post-read native snapshot and the entire ordered UART
batch, including an empty batch. Nested UART times are polling samples; only
the outer snapshot time bounds their read. Clocks/epochs are explicitly named
by the caller and are never converted to shared world time.
"""

import base64
import json

from sim.boards import validate_snapshot
from sim.boards.channels import ChannelEvent
from sim.boards.launch import UARTS
from sim.trace import TraceEvent, TraceWriter
from sim.trace.storage import MAX_CHANNELS, MAX_PAYLOAD_BYTES

from .runtime import ArenaObservation


ENDPOINT = "observation"
EVENT_TYPE = "arena.observation/1"
_JSON = json.JSONEncoder(ensure_ascii=True, allow_nan=False, separators=(",", ":"))


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
    """

    def __init__(self, writer: TraceWriter, *, clocks: dict[str, str]):
        if type(writer) is not TraceWriter:
            raise TypeError("Arena trace requires a TraceWriter")
        self._clocks = _clocks(clocks)
        self._writer = writer
        self._failed = False

    def __call__(self, item: ArenaObservation) -> None:
        if self._failed:
            raise RuntimeError("Arena trace failed; observations cannot be retried")
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
