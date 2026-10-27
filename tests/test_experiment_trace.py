"""Generic experiment records, checked through the independent MCAP codec."""

from dataclasses import dataclass
from io import BytesIO
import json
from pathlib import Path
import sys

import pytest
from mcap.records import Channel, Message
from mcap.stream_reader import StreamReader

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.api import Event, ImuSample, ImuSignalSample, TimeQuality
from sim.fabric import CanFrame
from sim.physics.base import JointState
from sim.trace import TraceEvent, TraceWriter, iter_events
from sim.trace.experiment import ExperimentTrace, iter_experiment_records


class Output:
    def __init__(self):
        self.data = bytearray()
        self.writes = self.flushes = 0
        self.failure = None
        self.closed = False

    def write(self, data):
        self.writes += 1
        if self.failure == "write":
            raise OSError("injected output error")
        if self.failure == "short":
            self.data.extend(data[:-1])
            return len(data) - 1
        self.data.extend(data)
        return len(data)

    def flush(self):
        self.flushes += 1
        if self.failure == "flush":
            raise OSError("injected flush error")

    def close(self):
        self.closed = True


class Input:
    def __init__(self, data):
        self.data = BytesIO(data)
        self.requests = []
        self.closed = False

    def read(self, size):
        assert 0 <= size < 70000, "reader performed an unbounded read"
        self.requests.append(size)
        return self.data.read(size)


def event(sequence=0, **changes):
    values = dict(session_id=7, sequence=sequence, source_node="mcu",
                  source_port="uart1", kind="uart", payload=b"\x00\xff",
                  source_time_ns=3, mapped_time_ns=5, delivery_time_ns=11,
                  quality=TimeQuality.UPPER_BOUND)
    values.update(changes)
    return Event(**values)


def encoded_trace():
    output = BytesIO()
    writer = ExperimentTrace(output, {"duration_ns": 11, "assertions": ()})
    writer.cut(0)
    writer.event(event())
    writer.cut(11)
    writer.complete({"end_ns": 11})
    return output.getvalue()


def rewrite(data, edit):
    """Alter semantic records and regenerate valid MCAP CRC through its owner."""
    records = list(iter_events(BytesIO(data)))
    documents = [json.loads(item.payload) for item in records]
    edit(documents)
    output = BytesIO()
    writer = TraceWriter(output)
    for index, value in enumerate(documents):
        original = records[min(index, len(records) - 1)]
        payload = json.dumps(value, ensure_ascii=True, allow_nan=False,
                             separators=(",", ":")).encode("ascii")
        writer.append(TraceEvent(original.timestamp_ns, original.source,
            original.endpoint, original.event_type, payload, original.clock))
    writer.finish()
    return output.getvalue()


def test_roundtrip_preserves_all_event_fields_and_only_admitted_public_records():
    output = Output()
    config = {"duration_ns": 100, "ready": {"endpoint": "mcu.uart1", "text": b"R\x00"},
              "assertions": ({"before": "1ns", "digital_equals": {"endpoint": "mcu.key", "value": 0}},)}
    writer = ExperimentTrace(output, config)
    writer.cut(0)
    sample = ImuSample((1.25, -2, 3.5), (0, 0, 9.80665))
    payload = {"frame": CanFrame(0x201, data=b"\x01\xff", timestamp_ns=3),
               "imu": sample, "signal": ImuSignalSample(sample, None, 5),
               "joint": JointState(.5, -3), "values": (None, True, -7, b"", "é")}
    observed = event(payload=payload)
    writer.event(observed)
    writer.cut(11)
    writer.cut(11)
    writer.cut(100)
    writer.complete({"end_ns": 100, "success": False})
    source = Input(output.data)
    records = list(iter_experiment_records(source))
    assert [(item.kind, item.data) for item in records] == [
        ("config", config), ("cut", 0), ("event", observed), ("cut", 11),
        ("cut", 11), ("cut", 100), ("completion", {"end_ns": 100, "success": False})]
    decoded = records[2].data
    assert (decoded.session_id, decoded.sequence, decoded.source_node, decoded.source_port,
            decoded.kind, decoded.source_time_ns, decoded.mapped_time_ns,
            decoded.delivery_time_ns, decoded.quality) == (
                7, 0, "mcu", "uart1", "uart", 3, 5, 11, TimeQuality.UPPER_BOUND)
    assert type(decoded.payload["frame"]) is CanFrame
    assert type(decoded.payload["imu"]) is ImuSample
    assert type(decoded.payload["signal"]) is ImuSignalSample
    assert type(decoded.payload["joint"]) is JointState
    assert not source.closed and source.requests and not output.closed
    with pytest.raises(TypeError):
        records[0].data["duration_ns"] = 1

    native = list(StreamReader(BytesIO(output.data), validate_crcs=True).records)
    channel, = [item for item in native if isinstance(item, Channel)]
    assert (channel.topic, channel.metadata) == ("experiment", {
        "source": "simulation", "event_type": "sim/experiment/1", "clock": "simulation"})
    messages = [item for item in native if isinstance(item, Message)]
    raw = json.loads(messages[2].data)
    assert raw["data"]["payload"]["value"]["frame"] == {"type": "CanFrame", "value": {
        "can_id": 0x201, "flags": 0, "dlc": 2,
        "data": {"type": "bytes", "value": "Af8="}, "timestamp_ns": 3}}


def test_config_is_segmented_and_channel_count_does_not_follow_nodes_or_protocols():
    config = {"assertions": tuple({"before": "1ms", "uart_contains": {
        "endpoint": f"mcu{index}.uart1", "text": "marker" * 10}}
        for index in range(1024)), "capabilities": ("sample", "input")}
    output = BytesIO()
    writer = ExperimentTrace(output, config)
    writer.cut(0)
    for index in range(300):
        writer.event(event(index, source_node=f"node{index}", source_port=f"port{index}",
                           kind=f"protocol{index}", source_time_ns=None,
                           mapped_time_ns=index, delivery_time_ns=index))
        writer.cut(index)
    writer.complete({"end_ns": 299})
    wire = list(iter_events(BytesIO(output.getvalue())))
    config_records = [json.loads(item.payload) for item in wire
                      if json.loads(item.payload)["kind"] == "config"]
    assert len(config_records) > 1
    assert all(len(item.payload) <= 65536 for item in wire)
    assert [item["data"]["index"] for item in config_records] == list(range(len(config_records)))
    decoded = list(iter_experiment_records(Input(output.getvalue())))
    assert decoded[0].data == config and len(decoded[0].data["assertions"]) == 1024
    native = list(StreamReader(BytesIO(output.getvalue()), validate_crcs=True).records)
    assert len([item for item in native if isinstance(item, Channel)]) == 1


def test_empty_repeated_cuts_are_physical_records_without_fabricated_events():
    output = BytesIO()
    writer = ExperimentTrace(output, {})
    for cut in (0, 0, 9, 9, 10):
        writer.cut(cut)
    writer.complete({})
    assert [(item.kind, item.data) for item in iter_experiment_records(BytesIO(output.getvalue()))] == [
        ("config", {}), ("cut", 0), ("cut", 0), ("cut", 9), ("cut", 9), ("cut", 10),
        ("completion", {})]


def test_reader_accepts_independent_literal_schema_records():
    output = BytesIO()
    writer = TraceWriter(output)
    # Fixed JSON/base64 literals are an independent producer of the schema,
    # without calling ExperimentTrace or any of its value encoders.
    payloads = (
        (0, b'{"format":"sim/experiment/1","kind":"config","ordinal":0,"data":'
            b'{"index":0,"count":1,"size":29,"data":"eyJ0eXBlIjoibWFwcGluZyIsInZhbHVlIjp7fX0="}}'),
        (2, b'{"format":"sim/experiment/1","kind":"cut","ordinal":1,"data":2}'),
        (2, b'{"format":"sim/experiment/1","kind":"completion","ordinal":2,"data":'
            b'{"events":0,"cuts":1,"records":3,"session_id":null,"last_time_ns":2,'
            b'"summary":{"type":"mapping","value":{}}}}'))
    for stamp, payload in payloads:
        writer.append(TraceEvent(stamp, "simulation", "experiment", "sim/experiment/1",
                                 payload, "simulation"))
    writer.finish()
    assert [(item.kind, item.data) for item in iter_experiment_records(Input(output.getvalue()))] == [
        ("config", {}), ("cut", 2), ("completion", {})]


@dataclass(frozen=True)
class UnknownRecord:
    value: int


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {1: "bad-key"},
                                    [1, 2], bytearray(b"x"), UnknownRecord(1),
                                    JointState("bad", 1), ImuSample((0, 1), (0, 0, 0)),
                                    CanFrame(True), 1 << 256])
def test_invalid_config_is_rejected_before_any_output(value):
    output = Output()
    with pytest.raises((TypeError, ValueError)):
        ExperimentTrace(output, {"value": value})
    assert output.writes == output.flushes == 0 and not output.closed


def test_config_size_and_depth_admission_happen_before_output():
    for value in ("x" * (8 * 1024 * 1024),):
        output = Output()
        with pytest.raises(BufferError, match="config"):
            ExperimentTrace(output, {"value": value})
        assert output.writes == 0
    value = None
    for _ in range(34):
        value = (value,)
    output = Output()
    with pytest.raises(ValueError, match="nesting"):
        ExperimentTrace(output, {"value": value})
    assert output.writes == 0


def test_bad_event_or_completion_admission_performs_no_io_or_sequence_consumption():
    output = Output()
    writer = ExperimentTrace(output, {})
    writer.cut(0)
    before = bytes(output.data), output.writes
    for item in (event(1), event(payload=UnknownRecord(1)), event(payload=b"x" * 65536)):
        with pytest.raises((TypeError, ValueError, BufferError)):
            writer.event(item)
        assert (bytes(output.data), output.writes) == before
    writer.event(event())
    before = bytes(output.data), output.writes
    for action in (lambda: writer.cut(10), lambda: writer.event(event(1, session_id=8)),
                   lambda: writer.complete({})):
        with pytest.raises(ValueError):
            action()
        assert (bytes(output.data), output.writes) == before
    writer.cut(11)
    writer.complete({})
    assert [item.data.sequence for item in iter_experiment_records(BytesIO(output.data))
            if item.kind == "event"] == [0]


@pytest.mark.parametrize("failure", ["write", "short", "flush"])
def test_io_failure_freezes_without_retry_or_closing_borrow(failure):
    output = Output()
    writer = ExperimentTrace(output, {})
    writer.cut(0)
    output.failure = failure
    with pytest.raises(OSError):
        writer.complete({}) if failure == "flush" else writer.event(event())
    stopped = bytes(output.data), output.writes, output.flushes
    output.failure = None
    for action in (lambda: writer.event(event()), lambda: writer.cut(1),
                   lambda: writer.complete({}), writer.finish):
        with pytest.raises(RuntimeError, match="failed"):
            action()
        assert (bytes(output.data), output.writes, output.flushes) == stopped
    assert not output.closed


def test_missing_completion_and_trailing_corruption_are_never_accepted():
    output = BytesIO()
    writer = ExperimentTrace(output, {})
    writer.cut(0)
    writer.finish()
    with pytest.raises(ValueError, match="incomplete experiment"):
        list(iter_experiment_records(BytesIO(output.getvalue())))
    valid = encoded_trace()
    for invalid in (valid[:-1], valid + b"unexpected"):
        iterator = iter_experiment_records(BytesIO(invalid))
        # Completion itself is provisional; full exhaustion must still fail.
        seen = []
        with pytest.raises(Exception):
            while True:
                seen.append(next(iterator).kind)
        assert seen[-1] == "completion"


@pytest.mark.parametrize("change", [
    "ordinal", "field", "format", "kind", "config-size", "config-count", "config-base64",
    "event-sequence", "event-session", "event-time", "event-fields", "payload-tag", "payload-base64",
    "completion-count", "completion-time", "completion-duplicate", "after-completion",
])
def test_reader_rejects_semantic_corruption_even_with_valid_mcap_crc(change):
    def edit(documents):
        if change == "ordinal":
            documents[1]["ordinal"] = True
        elif change == "field":
            documents[1]["extra"] = 1
        elif change == "format":
            documents[1]["format"] = "future"
        elif change == "kind":
            documents[1]["kind"] = "future"
        elif change.startswith("config-"):
            key, value = {"config-size": ("size", 8 * 1024 * 1024 + 1),
                          "config-count": ("count", 2), "config-base64": ("data", "!bad!")}[change]
            documents[0]["data"][key] = value
        elif change.startswith("event-"):
            key, value = {"event-sequence": ("sequence", 1), "event-session": ("session_id", False),
                          "event-time": ("delivery_time_ns", 12), "event-fields": ("unknown", 1)}[change]
            documents[2]["data"][key] = value
        elif change.startswith("payload-"):
            key, value = ("type", "UnknownRecord") if change == "payload-tag" else ("value", "!bad!")
            documents[2]["data"]["payload"][key] = value
        elif change == "completion-count":
            documents[-1]["data"]["events"] = 2
        elif change == "completion-time":
            documents[-1]["data"]["last_time_ns"] = 12
        elif change == "completion-duplicate":
            documents.append(documents[-1].copy())
        else:
            documents.append({"format": "sim/experiment/1", "kind": "cut", "ordinal": 5, "data": 11})
    malformed = rewrite(encoded_trace(), edit)
    # The transport envelope remains independently valid, unlike truncation.
    list(StreamReader(BytesIO(malformed), validate_crcs=True).records)
    with pytest.raises((TypeError, ValueError)):
        list(iter_experiment_records(Input(malformed)))
