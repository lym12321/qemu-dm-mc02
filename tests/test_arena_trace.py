"""Bounded observation codec, independent JSON and native MCAP consumers."""

from copy import deepcopy
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import sys

import pytest
from mcap.records import Channel, Message
from mcap.stream_reader import StreamReader

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.arena.runtime import ArenaObservation
from sim.arena.trace import ArenaTrace, decode_observation
from sim.boards.channels import ChannelEvent
from sim.trace import TraceEvent, TraceWriter, iter_events
from test_lab_runtime import snapshot


CLOCKS = {"east": "qemu:east:reset7", "west": "qemu:west:reset2"}


def observation(*, name="east", timestamp=23, events=None):
    return ArenaObservation(name, snapshot(timestamp),
                            [ChannelEvent("uart1", 17, b"\x00\xffA")] if events is None else events)


def json_bytes(value):
    """The standard JSON profile, independent of the observation adapter."""
    return json.dumps(value, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")).encode("ascii")


def payload(*, state=None, events=None):
    return {"snapshot": snapshot(23) if state is None else state,
            "events": ([{"endpoint": "uart1", "timestamp_ns": 17,
                         "data_base64": "AP9B"}] if events is None else events)}


def trace_event(*, body=None, **changes):
    fields = dict(timestamp_ns=23, source="east", endpoint="observation",
                  event_type="arena.observation/1", clock=CLOCKS["east"],
                  payload=json_bytes(payload()) if body is None else body)
    fields.update(changes)
    return TraceEvent(**fields)


def test_native_mcap_and_literal_json_preserve_full_batch_without_source_mutation():
    source = observation(events=[ChannelEvent("uart7", 19, b"\x00\xffA"),
                                 ChannelEvent("uart1", 17, b"B\n")])
    source.snapshot["native-extra"] = {"text": "\u6d4b\u8bd5", "values": [None, True, 1.25]}
    before = deepcopy(source)
    output = BytesIO()
    writer = TraceWriter(output)
    sink = ArenaTrace(writer, clocks=CLOCKS)
    sink(source)
    writer.finish()
    assert source == before and not output.closed
    assert not hasattr(sink, "finish") and not hasattr(sink, "close")
    records = list(StreamReader(BytesIO(output.getvalue()), validate_crcs=True).records)
    channels = [record for record in records if isinstance(record, Channel)]
    messages = [record for record in records if isinstance(record, Message)]
    assert len(channels) == len(messages) == 1
    assert channels[0].topic == "observation" and channels[0].schema_id == 0
    assert channels[0].metadata == {"source": "east", "event_type": "arena.observation/1",
                                    "clock": "qemu:east:reset7"}
    assert channels[0].message_encoding == "application/octet-stream"
    expected = {"snapshot": before.snapshot, "events": [
        {"endpoint": "uart7", "timestamp_ns": 19, "data_base64": "AP9B"},
        {"endpoint": "uart1", "timestamp_ns": 17, "data_base64": "Qgo="}]}
    assert messages[0].data == json_bytes(expected)
    assert messages[0].sequence == 0
    assert messages[0].log_time == messages[0].publish_time == 23
    events = list(iter_events(BytesIO(output.getvalue())))  # Exhaust container first.
    decoded = decode_observation(events[0], clocks=CLOCKS)
    assert decoded == before and decoded is not source
    assert decoded.snapshot is not source.snapshot and decoded.events is not source.events


def test_physical_order_explicit_member_epochs_and_empty_batches_need_no_global_clock():
    output = BytesIO()
    writer = TraceWriter(output)
    sink = ArenaTrace(writer, clocks=CLOCKS)
    original = [observation(timestamp=90), observation(name="west", timestamp=1, events=[]),
                observation(timestamp=0, events=[]), observation(timestamp=0, events=[])]
    for item in original:
        sink(item)
    writer.finish()
    recorded = list(iter_events(BytesIO(output.getvalue())))
    assert [(event.source, event.timestamp_ns, event.clock) for event in recorded] == [
        ("east", 90, CLOCKS["east"]), ("west", 1, CLOCKS["west"]),
        ("east", 0, CLOCKS["east"]), ("east", 0, CLOCKS["east"])]
    assert [decode_observation(event, clocks=CLOCKS) for event in recorded] == original
    assert json.loads(recorded[1].payload)["events"] == []


@pytest.mark.parametrize("clocks", [
    None, {}, {"east": "same", "west": "same"}, {"east": ""},
    {"east": "epoch\0bad"}, {1: "epoch"}, {"east": "\u00e9" * 128},
    {f"member{index}": f"clock{index}" for index in range(257)},
])
def test_clock_admission_is_pure_and_shared_by_encoder_and_decoder(clocks):
    output = BytesIO()
    writer = TraceWriter(output)
    before = output.getvalue()
    with pytest.raises((TypeError, ValueError)):
        ArenaTrace(writer, clocks=clocks)
    with pytest.raises((TypeError, ValueError)):
        decode_observation(trace_event(), clocks=clocks)
    assert output.getvalue() == before and not output.closed


def test_exact_public_owner_and_event_types_are_required_before_io():
    class DerivedWriter(TraceWriter):
        pass
    class DerivedObservation(ArenaObservation):
        pass
    class DerivedEvent(TraceEvent):
        pass
    class DerivedDict(dict):
        pass
    output = BytesIO()
    writer = TraceWriter(output)
    for wrong in (object(), DerivedWriter(BytesIO())):
        with pytest.raises(TypeError):
            ArenaTrace(wrong, clocks=CLOCKS)
    with pytest.raises(TypeError):
        ArenaTrace(writer, clocks=DerivedDict(CLOCKS))
    with pytest.raises(TypeError):
        decode_observation(DerivedEvent(**trace_event().__dict__), clocks=CLOCKS)
    with pytest.raises(TypeError):
        ArenaTrace(writer, clocks=CLOCKS)(DerivedObservation("east", snapshot(23), []))


@pytest.mark.parametrize("invalid", [
    "unknown-member", "bad-snapshot", "tuple-batch", "not-channel", "unknown-uart",
    "duplicate-uart", "empty-data", "mutable-data", "long-data", "future-poll",
    "bool-poll", "tuple-json", "integer-key", "nonfinite-json", "subclass-json",
])
def test_entire_batch_admission_has_no_io_and_freezes_after_first_error(invalid):
    class DerivedList(list):
        pass
    item = observation()
    if invalid == "unknown-member":
        item = replace(item, name="absent")
    elif invalid == "bad-snapshot":
        item.snapshot["gpio"]["A"]["sampled"] = -1
    elif invalid == "tuple-batch":
        item = replace(item, events=tuple(item.events))
    elif invalid == "not-channel":
        item.events.append(object())
    elif invalid == "unknown-uart":
        item.events.append(ChannelEvent("can1", 17, b"x"))
    elif invalid == "duplicate-uart":
        item.events.append(ChannelEvent("uart1", 18, b"x"))
    elif invalid == "empty-data":
        item.events.append(ChannelEvent("uart7", 18, b""))
    elif invalid == "mutable-data":
        item.events.append(ChannelEvent("uart7", 18, bytearray(b"x")))
    elif invalid == "long-data":
        item.events.append(ChannelEvent("uart7", 18, b"x" * 4097))
    elif invalid == "future-poll":
        item.events.append(ChannelEvent("uart7", 24, b"x"))
    elif invalid == "bool-poll":
        item.events.append(ChannelEvent("uart7", True, b"x"))
    else:
        item.snapshot["extra"] = {"tuple-json": (1, 2), "integer-key": {1: "x"},
                                  "nonfinite-json": float("nan"),
                                  "subclass-json": DerivedList([1])}[invalid]
    output = BytesIO()
    writer = TraceWriter(output)
    sink = ArenaTrace(writer, clocks=CLOCKS)
    before = output.getvalue()
    with pytest.raises((TypeError, ValueError)):
        sink(item)
    assert output.getvalue() == before
    with pytest.raises(RuntimeError):
        sink(observation())
    assert output.getvalue() == before
    # The adapter failure does not poison its independently borrowed writer.
    writer.append(TraceEvent(0, "other", "x", "x", b"", "separate:epoch"))
    writer.finish()
    assert len(list(iter_events(BytesIO(output.getvalue())))) == 1


def test_uart_and_total_payload_boundaries_are_exact_without_partial_batch_output():
    all_uarts = ("uart1", "uart2", "uart3", "uart5", "uart7", "uart10")
    item = observation(events=[ChannelEvent(name, index, b"\xff" * 4096)
                               for index, name in enumerate(all_uarts)])
    output = BytesIO()
    writer = TraceWriter(output)
    sink = ArenaTrace(writer, clocks=CLOCKS)
    sink(item)
    baseline = {"snapshot": snapshot(23), "events": []}
    baseline["snapshot"]["extra"] = ""
    padding = 65536 - len(json_bytes(baseline))
    boundary = observation(events=[])
    boundary.snapshot["extra"] = "x" * padding
    sink(boundary)
    before = output.getvalue()
    boundary.snapshot["extra"] += "x"
    with pytest.raises(ValueError):
        sink(boundary)
    assert output.getvalue() == before
    writer.finish()
    recorded = list(iter_events(BytesIO(output.getvalue())))
    assert len(recorded) == 2 and len(recorded[1].payload) == 65536
    assert decode_observation(recorded[0], clocks=CLOCKS) == item
    assert len(decode_observation(recorded[1], clocks=CLOCKS).snapshot["extra"]) == padding


@pytest.mark.parametrize("failure", ["io", "interrupt", "sequence"])
def test_append_failure_propagates_primary_and_freezes_without_retry(failure):
    primary = KeyboardInterrupt("trace interrupted") if failure == "interrupt" else OSError("trace failed")
    class FailingOutput(BytesIO):
        fail = False
        writes = 0
        def write(self, data):
            self.writes += 1
            if self.fail:
                raise primary
            return super().write(data)
    output = FailingOutput()
    writer = TraceWriter(output)
    sink = ArenaTrace(writer, clocks=CLOCKS)
    if failure == "sequence":
        # Existing storage fixture boundary, without four billion fake appends.
        writer._sequence = 2**32
    else:
        output.fail = True
    with pytest.raises(OverflowError if failure == "sequence" else type(primary)) as caught:
        sink(observation())
    if failure != "sequence":
        assert caught.value is primary
    stopped = output.getvalue(), output.writes
    output.fail = False
    with pytest.raises(RuntimeError):
        sink(observation())
    assert (output.getvalue(), output.writes) == stopped and not output.closed


@pytest.mark.parametrize("changes", [
    {"source": "absent"}, {"clock": "qemu:east:reset8"}, {"endpoint": "uart1"},
    {"event_type": "arena.observation/2"}, {"timestamp_ns": 24},
])
def test_decoder_rejects_wrong_profile_source_epoch_and_timestamp(changes):
    with pytest.raises(ValueError):
        decode_observation(trace_event(**changes), clocks=CLOCKS)


@pytest.mark.parametrize("invalid", [
    "whitespace", "duplicate-key", "extra-wrapper", "nonfinite", "noncanonical-base64",
    "bad-base64", "unknown-uart", "duplicate-uart", "future-poll", "bad-snapshot",
])
def test_decoder_rejects_noncanonical_or_invalid_complete_payloads(invalid):
    value = payload()
    if invalid == "whitespace":
        body = b" " + json_bytes(value)
    elif invalid == "duplicate-key":
        body = json_bytes(value)[:-1] + b',"events":[]}'
    elif invalid == "extra-wrapper":
        value["extra"] = 0
    elif invalid == "nonfinite":
        value["snapshot"]["extra"] = float("inf")
        body = json.dumps(value, separators=(",", ":")).encode("ascii")
    elif invalid == "noncanonical-base64":
        value["events"][0]["data_base64"] = "AB=="  # Nonzero unused pad bits.
    elif invalid == "bad-base64":
        value["events"][0]["data_base64"] = "%%%="
    elif invalid == "unknown-uart":
        value["events"][0]["endpoint"] = "can1"
    elif invalid == "duplicate-uart":
        value["events"].append(dict(value["events"][0]))
    elif invalid == "future-poll":
        value["events"][0]["timestamp_ns"] = 24
    else:
        value["snapshot"]["gpio"]["A"]["sampled"] = -1
    if invalid not in ("whitespace", "duplicate-key", "nonfinite"):
        body = json_bytes(value)
    before = bytes(body)
    with pytest.raises((TypeError, ValueError)):
        decode_observation(trace_event(body=body), clocks=CLOCKS)
    assert body == before


def test_independent_literal_json_decodes_empty_batch_and_all_byte_values():
    value = payload(events=[{"endpoint": "uart7", "timestamp_ns": 0,
                             "data_base64": "AAECA/7/"}])
    result = decode_observation(trace_event(body=json_bytes(value)), clocks=CLOCKS)
    assert result == observation(events=[ChannelEvent("uart7", 0, b"\0\1\2\3\xfe\xff")])
    empty = decode_observation(trace_event(body=json_bytes(payload(events=[]))), clocks=CLOCKS)
    assert empty.events == [] and empty.snapshot == snapshot(23) and empty.name == "east"
