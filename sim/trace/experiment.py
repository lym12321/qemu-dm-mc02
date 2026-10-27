"""Lossless experiment observations over the existing bounded MCAP profile.

This module owns neither streams nor simulation time. Configuration is opaque
pure metadata; applications own capability and assertion admission. All records
use one MCAP channel, so node/protocol diversity cannot exhaust channel slots.
Iterator values are provisional until completion, MCAP CRC/footer and EOF have
all been checked by fully exhausting the iterator.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass, fields
import json
import math
from types import MappingProxyType

from sim.api import Event, ImuSample, ImuSignalSample
from sim.api.events import check_timestamp
from sim.api.imu import validate_imu_sample
from sim.fabric.frame import CanFrame
from sim.physics.base import JointState

from .storage import MAX_PAYLOAD_BYTES, TraceEvent, TraceWriter, iter_events


FORMAT = "sim/experiment/1"
MAX_CONFIG_BYTES = 8 * 1024 * 1024
CONFIG_SEGMENT_BYTES = 32 * 1024
MAX_VALUE_DEPTH = 32
MAX_INTEGER_BITS = 256
_IDENTITY = ("simulation", "experiment", FORMAT, "simulation")
_RECORD_TYPES = {item.__name__: item for item in
                 (CanFrame, ImuSample, ImuSignalSample, JointState)}


@dataclass(frozen=True)
class ExperimentRecord:
    """A decoded config mapping, Event, cut integer or completion mapping."""

    kind: str
    data: object


def _validate_record(value):
    if type(value) is CanFrame:
        if (any(type(getattr(value, name)) is not int for name in
                ("can_id", "flags", "dlc", "timestamp_ns")) or type(value.data) is not bytes):
            raise ValueError("experiment CanFrame requires exact wire field types")
        CanFrame(**{item.name: getattr(value, item.name) for item in fields(value)})
    elif type(value) is ImuSample:
        validate_imu_sample(value)
    elif type(value) is ImuSignalSample:
        if type(value.sample) is not ImuSample:
            raise ValueError("experiment ImuSignalSample requires an ImuSample")
        validate_imu_sample(value.sample)
        if value.source_time_ns is not None:
            check_timestamp(value.source_time_ns)
        check_timestamp(value.mapped_time_ns)
    elif type(value) is JointState:
        for number in (value.position_rad, value.velocity_rad_s):
            if type(number) not in (int, float):
                raise ValueError("experiment JointState requires finite numbers")
            try:
                finite = math.isfinite(number)
            except OverflowError as exc:
                raise ValueError("experiment JointState requires finite numbers") from exc
            if not finite:
                raise ValueError("experiment JointState requires finite numbers")


def _encode(value, depth=0):
    if depth > MAX_VALUE_DEPTH:
        raise ValueError("experiment value nesting exceeds 32 levels")
    if value is None or type(value) in (bool, str):
        return value
    if type(value) is int:
        if value.bit_length() > MAX_INTEGER_BITS:
            raise ValueError("experiment integer exceeds 256 bits")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("experiment values must be finite")
        return value
    if type(value) is bytes:
        return {"type": "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("experiment mapping keys must be strings")
        return {"type": "mapping", "value": {
            key: _encode(item, depth + 1) for key, item in value.items()}}
    if type(value) is tuple:
        return {"type": "tuple", "value": [_encode(item, depth + 1) for item in value]}
    if type(value) in _RECORD_TYPES.values():
        _validate_record(value)
        return {"type": type(value).__name__, "value": {
            item.name: _encode(getattr(value, item.name), depth + 1)
            for item in fields(value)}}
    raise TypeError(f"unsupported experiment value: {type(value).__name__}")


def _decode(value, depth=0):
    if depth > MAX_VALUE_DEPTH:
        raise ValueError("experiment value nesting exceeds 32 levels")
    if value is None or type(value) in (bool, str, int, float):
        _encode(value, depth)
        return value
    if type(value) is not dict or set(value) != {"type", "value"}:
        raise ValueError("invalid typed experiment value")
    tag, data = value["type"], value["value"]
    if tag == "bytes" and type(data) is str:
        try:
            result = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid experiment base64 bytes") from exc
        if base64.b64encode(result).decode("ascii") != data:
            raise ValueError("noncanonical experiment base64 bytes")
        return result
    if tag == "tuple" and type(data) is list:
        return tuple(_decode(item, depth + 1) for item in data)
    if tag == "mapping" and type(data) is dict:
        return MappingProxyType({key: _decode(item, depth + 1) for key, item in data.items()})
    if type(tag) is str and tag in _RECORD_TYPES and type(data) is dict:
        constructor = _RECORD_TYPES[tag]
        if set(data) != {item.name for item in fields(constructor)}:
            raise ValueError(f"invalid {tag} fields")
        arguments = {key: _decode(item, depth + 1) for key, item in data.items()}
        try:
            result = constructor(**arguments)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"invalid experiment {tag}") from exc
        # Constructors must not silently coerce malformed wire types.
        if _encode(result, depth) != value:
            raise ValueError(f"noncanonical experiment {tag}")
        return result
    raise ValueError("unsupported typed experiment value")


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=True, allow_nan=False,
                          separators=(",", ":")).encode("ascii")
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("invalid experiment JSON value") from exc


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate experiment JSON field")
        result[key] = value
    return result


def _load(payload):
    try:
        result = json.loads(payload, object_pairs_hook=_object,
                            parse_constant=lambda value: (_ for _ in ()).throw(
                                ValueError("nonfinite experiment JSON")))
    except (UnicodeError, TypeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("invalid experiment JSON") from exc
    if _json(result) != payload:
        raise ValueError("noncanonical experiment JSON")
    return result


def _mapping(value, name):
    if not isinstance(value, Mapping):
        raise TypeError(f"experiment {name} must be a mapping")
    return _encode(value)


def _event_value(event):
    if type(event) is not Event:
        raise TypeError("experiment recording requires an Event")
    if (type(event.source_node) is not str or not event.source_node
            or (event.source_port is not None
                and (type(event.source_port) is not str or not event.source_port))):
        raise ValueError("experiment Event requires a source node and optional port")
    _encode(event.session_id)
    _encode(event.sequence)
    return {item.name: (_encode(event.payload) if item.name == "payload"
                        else str(event.quality) if item.name == "quality"
                        else getattr(event, item.name)) for item in fields(Event)}


def _event_from(value):
    if type(value) is not dict or set(value) != {item.name for item in fields(Event)}:
        raise ValueError("invalid experiment Event fields")
    arguments = dict(value)
    arguments["payload"] = _decode(arguments["payload"])
    try:
        event = Event(**arguments)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid experiment Event") from exc
    if _event_value(event) != value:
        raise ValueError("noncanonical experiment Event")
    return event


class ExperimentTrace:
    """Append observations and physical cut markers to a borrowed output.

    Metadata is admitted in full before the first write: <=8MiB typed JSON,
    segmented into 32KiB pieces. Other records must each fit the existing 64KiB
    payload bound. Values have <=32 nested levels and <=256-bit integers.
    One session has consecutive Event sequences starting at zero. Event delivery
    times and cut markers never move backwards; equal/empty cuts are retained.
    Admission errors perform no IO. Any attempted write/finish error freezes
    this owner without retry. Call complete only after execution and cleanup;
    finish alone leaves an explicitly incomplete experiment for diagnostics.
    """

    def __init__(self, output, config: Mapping):
        encoded = _json(_mapping(config, "config"))
        if len(encoded) > MAX_CONFIG_BYTES:
            raise BufferError("experiment config exceeds 8MiB")
        segments = tuple(encoded[offset:offset + CONFIG_SEGMENT_BYTES]
                         for offset in range(0, len(encoded), CONFIG_SEGMENT_BYTES))
        self._ordinal = self._events = self._cuts = 0
        self._session = self._last_cut = None
        self._last_time = 0
        self._failed = self._finished = False
        self._writer = TraceWriter(output)
        for index, segment in enumerate(segments):
            self._append("config", {"index": index, "count": len(segments),
                         "size": len(encoded), "data": base64.b64encode(segment).decode("ascii")}, 0)

    def _active(self):
        if self._failed:
            raise RuntimeError("experiment trace failed; output cannot be retried")
        if self._finished:
            raise RuntimeError("experiment trace is finished")

    def _append(self, kind, data, timestamp):
        self._active()
        payload = _json({"format": FORMAT, "kind": kind,
                         "ordinal": self._ordinal, "data": data})
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise BufferError("experiment record exceeds 65536 bytes")
        item = TraceEvent(timestamp, _IDENTITY[0], _IDENTITY[1],
                          _IDENTITY[2], payload, _IDENTITY[3])
        try:
            self._writer.append(item)
        except BaseException:
            self._failed = True
            raise
        self._ordinal += 1

    def event(self, event: Event):
        self._active()
        value = _event_value(event)
        if (event.sequence != self._events
                or self._session not in (None, event.session_id)
                or event.delivery_time_ns < self._last_time):
            raise ValueError("experiment Events require consecutive sequences, one session and ordered delivery")
        self._append("event", value, event.delivery_time_ns)
        self._events += 1
        self._session = event.session_id
        self._last_time = event.delivery_time_ns

    def cut(self, cut_ns: int):
        self._active()
        check_timestamp(cut_ns)
        if cut_ns < self._last_time:
            raise ValueError("experiment observation cuts moved backwards")
        self._append("cut", cut_ns, cut_ns)
        self._cuts += 1
        self._last_cut = self._last_time = cut_ns

    def complete(self, summary: Mapping):
        self._active()
        value = _mapping(summary, "completion summary")
        if self._last_cut is None or self._last_cut != self._last_time:
            raise ValueError("experiment completion requires its last observation cut")
        self._append("completion", {"events": self._events, "cuts": self._cuts,
                     "records": self._ordinal + 1, "session_id": self._session,
                     "last_time_ns": self._last_time, "summary": value}, self._last_time)
        self.finish()

    def finish(self):
        if self._finished:
            return
        self._active()
        try:
            self._writer.finish()
        except BaseException:
            self._failed = True
            raise
        self._finished = True


def iter_experiment_records(source):
    """Stream physical records; exhaust before trusting completion or results.

    Config bytes are bounded at 8MiB and each message at 64KiB; decoded metadata
    has the same finite value/depth limits. No observation history is retained.
    The caller owns the blocking input.
    A missing completion, any later record or invalid MCAP footer/CRC/EOF raises.
    """
    ordinal = events = cuts = 0
    session = last_cut = None
    last_time = 0
    config = bytearray()
    config_count = config_size = None
    config_index = 0
    configured = completed = False
    for item in iter_events(source):
        if (item.source, item.endpoint, item.event_type, item.clock) != _IDENTITY:
            raise ValueError("unexpected experiment MCAP identity")
        value = _load(item.payload)
        if (type(value) is not dict or set(value) != {"format", "kind", "ordinal", "data"}
                or value["format"] != FORMAT or type(value["ordinal"]) is not int
                or value["ordinal"] != ordinal or completed):
            raise ValueError("invalid experiment record order/schema")
        ordinal += 1
        kind, data = value["kind"], value["data"]
        if not configured:
            if (kind != "config" or item.timestamp_ns != 0 or type(data) is not dict
                    or set(data) != {"index", "count", "size", "data"}
                    or any(type(data[key]) is not int for key in ("index", "count", "size"))
                    or data["index"] != config_index or type(data["data"]) is not str
                    or not 0 < data["size"] <= MAX_CONFIG_BYTES
                    or data["count"] != (data["size"] + CONFIG_SEGMENT_BYTES - 1) // CONFIG_SEGMENT_BYTES):
                raise ValueError("invalid experiment config segment")
            if config_count is None:
                config_count, config_size = data["count"], data["size"]
            if (data["count"], data["size"]) != (config_count, config_size):
                raise ValueError("inconsistent experiment config segments")
            try:
                segment = base64.b64decode(data["data"], validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("invalid experiment config base64") from exc
            expected = min(CONFIG_SEGMENT_BYTES, config_size - len(config))
            if (len(segment) != expected
                    or base64.b64encode(segment).decode("ascii") != data["data"]):
                raise ValueError("invalid experiment config segment size/encoding")
            config.extend(segment)
            config_index += 1
            if config_index == config_count:
                metadata = _decode(_load(bytes(config)))
                if not isinstance(metadata, Mapping):
                    raise ValueError("experiment config must be a mapping")
                config.clear()
                configured = True
                yield ExperimentRecord("config", metadata)
        elif kind == "event":
            event = _event_from(data)
            if (event.sequence != events or session not in (None, event.session_id)
                    or event.delivery_time_ns < last_time or item.timestamp_ns != event.delivery_time_ns):
                raise ValueError("invalid experiment Event sequence/session/delivery")
            events += 1
            session, last_time = event.session_id, event.delivery_time_ns
            yield ExperimentRecord("event", event)
        elif kind == "cut":
            check_timestamp(data)
            if data < last_time or item.timestamp_ns != data:
                raise ValueError("invalid experiment cut order/time")
            cuts += 1
            last_cut = last_time = data
            yield ExperimentRecord("cut", data)
        elif kind == "completion":
            if (type(data) is not dict or set(data) != {
                    "events", "cuts", "records", "session_id", "last_time_ns", "summary"}
                    or any(type(data[key]) is not int for key in ("events", "cuts", "records", "last_time_ns"))
                    or (data["session_id"] is not None and type(data["session_id"]) is not int)
                    or (data["events"], data["cuts"], data["records"], data["session_id"], data["last_time_ns"])
                       != (events, cuts, ordinal, session, last_time)
                    or last_cut is None or last_cut != last_time or item.timestamp_ns != last_time):
                raise ValueError("invalid experiment completion counts/time/session")
            summary = _decode(data["summary"])
            if not isinstance(summary, Mapping):
                raise ValueError("experiment completion summary must be a mapping")
            completed = True
            yield ExperimentRecord("completion", summary)
        else:
            raise ValueError("unsupported experiment record kind")
    if not configured or not completed:
        raise ValueError("incomplete experiment trace")
