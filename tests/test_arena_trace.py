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


def completion_result():
    from sim.arena.runtime import ArenaBoardResult, ArenaResult
    # Result order deliberately differs from capture/mapping order.
    return ArenaResult("fixture", 0, 120, 2, (
        ArenaBoardResult("west", "b_", "omitted", None, 120, 100, 1),
        ArenaBoardResult("east", "a_", "observed", 25, 30, 10, 2)), ())


def completion_capture(output=None):
    output = BytesIO() if output is None else output
    writer = TraceWriter(output)
    sink = ArenaTrace(writer, clocks=CLOCKS)
    for item in (observation(timestamp=20, events=[]),
                 observation(name="west", timestamp=120, events=[]),
                 observation(timestamp=30, events=[])):
        sink(item)
    return output, writer, sink


def test_completion_native_metadata_uses_actual_result_order_origin_and_completed_state():
    from dataclasses import FrozenInstanceError
    from sim.arena.trace import ArenaTraceCompletion, decode_completion
    output, writer, sink = completion_capture()
    result = completion_result()
    before = deepcopy(result)
    sink.complete(result)
    assert result == before
    stopped = output.getvalue()
    with pytest.raises(RuntimeError):
        sink(observation(timestamp=30, events=[]))
    with pytest.raises(RuntimeError):
        sink.complete(result)
    assert output.getvalue() == stopped and not output.closed
    writer.finish()
    native = list(StreamReader(BytesIO(output.getvalue()), validate_crcs=True).records)
    channels = {record.id: record for record in native if isinstance(record, Channel)}
    messages = [record for record in native if isinstance(record, Message)]
    assert [(channels[item.channel_id].metadata, item.sequence, item.log_time,
             item.publish_time, item.data) for item in messages[-2:]] == [
        ({"source": "west", "event_type": "arena.completion/1", "clock": CLOCKS["west"]},
         3, 120, 120, b'{"reset_qemu_ns":100,"observation_batches":1}'),
        ({"source": "east", "event_type": "arena.completion/1", "clock": CLOCKS["east"]},
         4, 30, 30, b'{"reset_qemu_ns":10,"observation_batches":2}'),
    ]
    recorded = list(iter_events(BytesIO(output.getvalue())))
    complete = [decode_completion(event, clocks=CLOCKS) for event in recorded[-2:]]
    assert complete == [ArenaTraceCompletion("west", CLOCKS["west"], 100, 120, 1),
                        ArenaTraceCompletion("east", CLOCKS["east"], 10, 30, 2)]
    with pytest.raises(FrozenInstanceError):
        complete[0].reset_qemu_ns = 120


@pytest.mark.parametrize("invalid", [
    "result-type", "board-type", "board-list", "duplicate", "unknown-member",
    "count", "count-zero", "count-overflow", "reset-after-first", "negative-reset",
    "last-end", "bool-end", "end-overflow",
])
def test_completion_admits_all_result_boards_before_first_append_and_freezes(invalid):
    result = completion_result()
    west, east = result.boards
    if invalid == "result-type":
        result = object()
    elif invalid == "board-type":
        result = replace(result, boards=(west, object()))
    elif invalid == "board-list":
        result = replace(result, boards=list(result.boards))
    elif invalid == "duplicate":
        result = replace(result, boards=(west, west))
    else:
        changes = {"unknown-member": {"name": "absent"}, "count": {"observation_batches": 1},
                   "count-zero": {"observation_batches": 0},
                   "count-overflow": {"observation_batches": 2**32},
                   "reset-after-first": {"reset_qemu_ns": 21},
                   "negative-reset": {"reset_qemu_ns": -1},
                   "last-end": {"end_qemu_ns": 31}, "bool-end": {"end_qemu_ns": True},
                   "end-overflow": {"end_qemu_ns": 2**64}}[invalid]
        result = replace(result, boards=(west, replace(east, **changes)))
    output, writer, sink = completion_capture()
    before = output.getvalue()
    with pytest.raises((TypeError, ValueError)):
        sink.complete(result)
    assert output.getvalue() == before
    for action in (lambda: sink.complete(completion_result()),
                   lambda: sink(observation(timestamp=30, events=[]))):
        with pytest.raises(RuntimeError):
            action()
    assert output.getvalue() == before
    writer.finish()
    assert len(list(iter_events(BytesIO(output.getvalue())))) == 3


@pytest.mark.parametrize("captured", [0, 1])
def test_completion_requires_exact_configured_and_observed_member_coverage(captured):
    output = BytesIO()
    writer = TraceWriter(output)
    sink = ArenaTrace(writer, clocks=CLOCKS)
    if captured:
        sink(observation(timestamp=30, events=[]))
    before = output.getvalue()
    with pytest.raises(ValueError):
        sink.complete(completion_result())
    assert output.getvalue() == before


@pytest.mark.parametrize("members", [128, 129])
def test_completion_channel_capacity_is_preflighted_without_restricting_observation_capture(members):
    from sim.arena.runtime import ArenaBoardResult, ArenaResult
    clocks = {f"member{index}": f"qemu:member{index}:epoch0" for index in range(members)}
    output = BytesIO()
    writer = TraceWriter(output)
    sink = ArenaTrace(writer, clocks=clocks)
    for name in clocks:
        sink(observation(name=name, timestamp=0, events=[]))
    result = ArenaResult("fixture", 0, 0, 0, tuple(
        ArenaBoardResult(name, name + "_", "omitted", None, 0, 0, 1) for name in clocks), ())
    before = output.getvalue()
    if members == 129:
        with pytest.raises((ValueError, BufferError)):
            sink.complete(result)
        assert output.getvalue() == before
    else:
        sink.complete(result)
    writer.finish()
    events = list(iter_events(BytesIO(output.getvalue())))
    assert len(events) == (256 if members == 128 else 129)
    assert sum(event.event_type == "arena.completion/1" for event in events) == (128 if members == 128 else 0)


@pytest.mark.parametrize("interrupt", [False, True])
def test_completion_partial_output_preserves_primary_and_never_retries(interrupt):
    primary = KeyboardInterrupt("completion interrupted") if interrupt else OSError("completion failed")
    class Output(BytesIO):
        armed = False
        writes = 0
        def write(self, data):
            self.writes += 1
            if self.armed and b'{"reset_qemu_ns":10,"observation_batches":2}' in data:
                super().write(data[:len(data) // 2])
                raise primary
            return super().write(data)
    output, writer, sink = completion_capture(Output())
    before = output.getvalue()
    output.armed = True
    with pytest.raises(type(primary)) as caught:
        sink.complete(completion_result())
    assert caught.value is primary
    assert output.getvalue().startswith(before) and len(output.getvalue()) > len(before)
    provisional = iter_events(BytesIO(output.getvalue()))
    first = [next(provisional) for _ in range(4)]
    assert first[-1].event_type == "arena.completion/1" and first[-1].source == "west"
    stopped = output.getvalue(), output.writes
    output.armed = False
    with pytest.raises(RuntimeError):
        sink.complete(completion_result())
    with pytest.raises(RuntimeError):
        sink(observation(timestamp=30, events=[]))
    with pytest.raises(RuntimeError):
        writer.finish()
    assert (output.getvalue(), output.writes) == stopped and not output.closed


@pytest.mark.parametrize("limit", ["channels", "sequence"])
def test_completion_existing_writer_capacity_can_leave_partial_metadata_and_freezes(limit):
    output, writer, sink = completion_capture()
    if limit == "channels":
        for index in range(253):
            writer.append(TraceEvent(0, f"unrelated{index}", "x", "x", b"", "other:epoch"))
    else:
        # Exercise the actual storage ordinal boundary without billions of appends.
        writer._sequence = 2**32 - 1
    with pytest.raises(BufferError if limit == "channels" else OverflowError):
        sink.complete(completion_result())
    stopped = output.getvalue()
    with pytest.raises(RuntimeError):
        sink.complete(completion_result())
    assert output.getvalue() == stopped
    writer.finish()
    if limit == "channels":
        events = list(iter_events(BytesIO(output.getvalue())))
        assert [(event.source, event.event_type) for event in events if event.event_type == "arena.completion/1"] == [
            ("west", "arena.completion/1")]
    else:
        # Deliberately seeded ordinals cannot be read as a contiguous run.
        messages = [record for record in StreamReader(BytesIO(output.getvalue())).records
                    if isinstance(record, Message)]
        assert messages[-1].sequence == 2**32 - 1
        assert messages[-1].data == b'{"reset_qemu_ns":100,"observation_batches":1}'


def test_completion_decoder_independent_literal_uint_boundaries_and_frozen_record():
    from sim.arena.trace import ArenaTraceCompletion, decode_completion
    event = trace_event(event_type="arena.completion/1", timestamp_ns=2**64 - 1,
                        body=b'{"reset_qemu_ns":0,"observation_batches":4294967295}')
    assert decode_completion(event, clocks=CLOCKS) == ArenaTraceCompletion(
        "east", CLOCKS["east"], 0, 2**64 - 1, 2**32 - 1)


@pytest.mark.parametrize("invalid", [
    "source", "clock", "type", "endpoint", "whitespace", "duplicate-key", "missing-field",
    "extra-field", "bool-reset", "future-reset", "count-zero", "count-overflow",
])
def test_completion_decoder_rejects_wrong_profile_noncanonical_and_out_of_range_fields(invalid):
    from sim.arena.trace import decode_completion
    changes, body = {}, b'{"reset_qemu_ns":10,"observation_batches":2}'
    if invalid in ("source", "clock", "type", "endpoint"):
        changes = {"source": {"source": "unknown"}, "clock": {"clock": "different:epoch"},
                   "type": {"event_type": "arena.observation/1"},
                   "endpoint": {"endpoint": "uart1"}}[invalid]
    elif invalid == "whitespace":
        body = b" " + body
    elif invalid == "duplicate-key":
        body = b'{"reset_qemu_ns":10,"observation_batches":2,"observation_batches":2}'
    elif invalid == "missing-field":
        body = b'{"reset_qemu_ns":10}'
    elif invalid == "extra-field":
        body = b'{"reset_qemu_ns":10,"observation_batches":2,"world_time":0}'
    elif invalid == "bool-reset":
        body = b'{"reset_qemu_ns":true,"observation_batches":2}'
    elif invalid == "future-reset":
        body = b'{"reset_qemu_ns":24,"observation_batches":2}'
    elif invalid == "count-zero":
        body = b'{"reset_qemu_ns":10,"observation_batches":0}'
    else:
        body = b'{"reset_qemu_ns":10,"observation_batches":4294967296}'
    changes.setdefault("event_type", "arena.completion/1")
    with pytest.raises((TypeError, ValueError)):
        decode_completion(trace_event(body=body, **changes), clocks=CLOCKS)


@pytest.fixture
def offline_replay_scenario(tmp_path):
    from sim.arena.manifest import load_manifest
    from sim.lab.manifest import Assertion
    from sim.scenario import ScenarioAssertion, ScenarioManifest
    from test_arena_manifest import write_arena

    arena = load_manifest(write_arena(tmp_path))
    # Global assertion order deliberately alternates independently clocked members.
    assertions = (
        ScenarioAssertion("west", Assertion(20, "uart1", "uart_contains", b"hello")),
        ScenarioAssertion("east", Assertion(20, "uart1", "uart_contains", b"hello")),
        ScenarioAssertion("west", Assertion(20, "gpio.A0", "digital_equals", 1)),
        ScenarioAssertion("east", Assertion(20, "uart1", "uart_contains", b"LATE")),
        ScenarioAssertion("east", Assertion(6, "gpio.A0", "digital_equals", 1)),
        ScenarioAssertion("west", Assertion(20, "uart1", "uart_contains", b"missing")),
    )
    return ScenarioManifest(tmp_path / "original-unread.yaml", arena, 100, (), assertions=assertions)


def offline_replay_observation(name, stamp, *, poll=None, text=None, endpoint="uart1", sampled=0):
    import base64
    state = snapshot(stamp)
    state["gpio"]["A"].update(sampled=sampled, odr=1 - sampled, output_mask=1)
    events = [] if text is None else [{"endpoint": endpoint,
        "timestamp_ns": stamp if poll is None else poll,
        "data_base64": base64.b64encode(text).decode("ascii")}]
    # Literal public format, independent of ArenaTrace/decode/replay helpers.
    return TraceEvent(stamp, name, "observation", "arena.observation/1",
                      json_bytes({"snapshot": state, "events": events}), CLOCKS[name])


def offline_replay_completion(name, stamp, reset, count):
    return TraceEvent(stamp, name, "observation", "arena.completion/1",
        json_bytes({"reset_qemu_ns": reset, "observation_batches": count}), CLOCKS[name])


def offline_replay_records():
    return [
        offline_replay_observation("east", 1005, poll=1001, text=b"he", sampled=1),
        offline_replay_observation("west", 10, text=b"hello"),
        offline_replay_observation("east", 1019, poll=1018, text=b"llo", sampled=1),
        offline_replay_observation("west", 30),  # Empty batch expires unmet conditions.
        offline_replay_observation("east", 1020, poll=1019, text=b"LATE", sampled=1),
        offline_replay_observation("east", 1100),
        offline_replay_observation("west", 110),
        offline_replay_completion("west", 110, 10, 3),
        offline_replay_completion("east", 1100, 1000, 4),
    ]


def offline_replay_bytes(records=None):
    output = BytesIO()
    writer = TraceWriter(output)
    for event in offline_replay_records() if records is None else records:
        writer.append(event)
    writer.finish()
    return output.getvalue()


class OfflineReplayInput(BytesIO):
    """Observe the borrowed stream boundary, optionally changing pass two."""

    def __init__(self, data, second=None, *, failure=None):
        super().__init__(data)
        self.second, self.failure = second, failure
        self.rewinds, self.eofs, self.reads = [], [], 0

    def seek(self, offset, whence=0):
        self.rewinds.append((self.tell(), offset, whence))
        if self.failure is not None and self.failure[0] == "seek":
            raise self.failure[1]
        if self.second is not None:
            BytesIO.__init__(self, self.second)
        return super().seek(offset, whence)

    def read(self, size=-1):
        self.reads += 1
        if (self.rewinds and self.failure is not None and self.failure[0] == "read"
                and self.tell() >= len(self.getvalue()) - 8):
            raise self.failure[1]
        value = super().read(size)
        if not value:
            self.eofs.append((len(self.rewinds), self.tell()))
        return value


def test_offline_replay_two_full_passes_original_origins_global_status_and_borrowed_lifetime(
        offline_replay_scenario, monkeypatch):
    from dataclasses import FrozenInstanceError
    from sim.scenario.replay import replay_assertions

    raw = offline_replay_bytes()
    source = OfflineReplayInput(raw)
    def no_config_reads(*args, **kwargs):
        pytest.fail("offline evaluation reopened a configuration or firmware path")
    monkeypatch.setattr(Path, "read_bytes", no_config_reads)
    monkeypatch.setattr(Path, "read_text", no_config_reads)
    result = replay_assertions(source, offline_replay_scenario, clocks=CLOCKS)
    assert result.status == ("pass", "pass", "fail", "fail", "pass", "fail")
    assert not result.passed and len(result.failures) == 3
    for text, member, ordinal in zip(result.failures, ("west", "east", "west"), (3, 4, 6), strict=True):
        assert member in text and f"assertion[{ordinal}]" in text
    assert tuple(item.name for item in result.completions) == tuple(
        item.name for item in offline_replay_scenario.arena.robots)
    assert {item.name: (item.reset_qemu_ns, item.end_qemu_ns, item.observation_batches)
            for item in result.completions} == {"east": (1000, 1100, 4), "west": (10, 110, 3)}
    assert source.rewinds == [(len(raw), 0, 0)]
    assert source.eofs == [(0, len(raw)), (1, len(raw))]
    assert source.tell() == len(raw) and not source.closed
    with pytest.raises(FrozenInstanceError):
        result.status = ()


@pytest.mark.parametrize("invalid", [
    "scenario-type", "arena-type", "members-list", "member-type", "robot-type",
    "board-type", "ready-type", "ready-endpoint", "ready-text", "duplicate-member",
    "referee-type", "referee-member", "duration", "assertion-type", "clocks",
])
def test_offline_replay_typed_admission_precedes_input_reads(offline_replay_scenario, invalid):
    from sim.arena.manifest import ArenaReferee
    from sim.robots.manifest import Ready
    from sim.scenario.replay import replay_assertions

    scenario, clocks = offline_replay_scenario, CLOCKS
    arena = scenario.arena
    first, second = arena.robots
    if invalid == "scenario-type":
        scenario = object()
    elif invalid == "arena-type":
        scenario = replace(scenario, arena=object())
    elif invalid == "members-list":
        scenario = replace(scenario, arena=replace(arena, robots=list(arena.robots)))
    elif invalid in ("member-type", "robot-type", "board-type", "ready-type", "ready-endpoint", "ready-text"):
        if invalid == "member-type":
            first = object()
        elif invalid == "robot-type":
            first = replace(first, robot=object())
        elif invalid == "board-type":
            first = replace(first, robot=replace(first.robot, board=object()))
        else:
            ready = {"ready-type": object(), "ready-endpoint": Ready("other.uart1", b"ready"),
                     "ready-text": Ready("main.uart1", b"")}[invalid]
            first = replace(first, robot=replace(first.robot, ready=ready))
        scenario = replace(scenario, arena=replace(arena, robots=(first, second)))
    elif invalid == "duplicate-member":
        scenario = replace(scenario, arena=replace(arena, robots=(first, replace(second, name=first.name))))
    elif invalid in ("referee-type", "referee-member"):
        entry = object() if invalid == "referee-type" else ArenaReferee("absent", "uart7", None, None, (), 0)
        scenario = replace(scenario, arena=replace(arena, referees=(entry,)))
    elif invalid == "duration":
        scenario = replace(scenario, duration_ns=True)
    elif invalid == "assertion-type":
        scenario = replace(scenario, assertions=(object(),))
    else:
        clocks = {"east": CLOCKS["east"]}
    source = OfflineReplayInput(offline_replay_bytes())
    with pytest.raises((TypeError, ValueError)):
        replay_assertions(source, scenario, clocks=clocks)
    assert source.reads == 0 and source.tell() == 0 and not source.closed


@pytest.mark.parametrize("invalid", [
    "missing-completion", "duplicate-completion", "post-completion-observation", "missing-member-batches",
    "unknown-member", "wrong-epoch", "unknown-type", "wrong-topic", "postread-decrease",
    "poll-before-previous", "first-before-reset", "first-poll-before-reset", "wrong-count",
    "wrong-end", "unopened-uart", "unasserted-short-duration", "unasserted-overflow",
])
def test_offline_replay_validates_whole_member_lifecycle_even_without_assertions(
        offline_replay_scenario, invalid):
    from sim.scenario.replay import replay_assertions

    records = offline_replay_records()
    scenario = replace(offline_replay_scenario, assertions=())
    if invalid == "missing-completion":
        records.pop()
    elif invalid == "duplicate-completion":
        records.append(records[-1])
    elif invalid == "post-completion-observation":
        records.append(offline_replay_observation("east", 1100))
    elif invalid == "missing-member-batches":
        records = [item for item in records if item.source != "west" or item.event_type == "arena.completion/1"]
    elif invalid in ("unknown-member", "wrong-epoch", "unknown-type", "wrong-topic"):
        fields = {"unknown-member": {"source": "absent"}, "wrong-epoch": {"clock": "qemu:east:wrong"},
                  "unknown-type": {"event_type": "unknown/1"}, "wrong-topic": {"endpoint": "uart1"}}[invalid]
        records[0] = replace(records[0], **fields)
    elif invalid == "postread-decrease":
        records[2] = offline_replay_observation("east", 1004)
    elif invalid == "poll-before-previous":
        records[2] = offline_replay_observation("east", 1019, poll=1004, text=b"llo")
    elif invalid == "first-before-reset":
        records[0] = offline_replay_observation("east", 999)
    elif invalid == "first-poll-before-reset":
        records[0] = offline_replay_observation("east", 1005, poll=999, text=b"he")
    elif invalid == "wrong-count":
        records[-1] = offline_replay_completion("east", 1100, 1000, 3)
    elif invalid == "wrong-end":
        records[-1] = offline_replay_completion("east", 1101, 1000, 4)
    elif invalid == "unopened-uart":
        records[0] = offline_replay_observation("east", 1005, text=b"he", endpoint="uart7")
    elif invalid == "unasserted-short-duration":
        scenario = replace(scenario, duration_ns=101)
    else:
        top = 2**64 - 1
        records = [offline_replay_observation("east", top), offline_replay_observation("west", 110),
                   offline_replay_completion("west", 110, 10, 1),
                   offline_replay_completion("east", top, top - 50, 1)]
    source = OfflineReplayInput(offline_replay_bytes(records))
    with pytest.raises((TypeError, ValueError)):
        replay_assertions(source, scenario, clocks=CLOCKS)
    assert not source.closed


def test_offline_replay_empty_assertion_run_still_requires_duration_and_can_pass(offline_replay_scenario):
    from sim.scenario.replay import replay_assertions
    source = OfflineReplayInput(offline_replay_bytes())
    result = replay_assertions(source, replace(offline_replay_scenario, assertions=()), clocks=CLOCKS)
    assert result.status == result.failures == () and result.passed
    assert len(result.completions) == 2 and len(source.eofs) == 2


@pytest.mark.parametrize("invalid", ["nonseekable", "offset", "bool-position"])
def test_offline_replay_requires_caller_owned_input_at_zero(offline_replay_scenario, invalid):
    from sim.scenario.replay import replay_assertions
    source = OfflineReplayInput(offline_replay_bytes())
    if invalid == "nonseekable":
        source.seek = None
    elif invalid == "offset":
        BytesIO.seek(source, 1)
    else:
        source.tell = lambda: False
    with pytest.raises((TypeError, ValueError)):
        replay_assertions(source, offline_replay_scenario, clocks=CLOCKS)
    assert source.reads == 0 and not source.closed


@pytest.mark.parametrize("failure", ["trailer", "crc", "io", "interrupt", "seek"])
def test_offline_replay_late_second_pass_failure_overrides_prior_assertion_failures(
        offline_replay_scenario, failure):
    from sim.scenario.replay import replay_assertions

    raw = offline_replay_bytes()
    changed, primary = raw, None
    if failure == "trailer":
        changed = raw[:-1]
    elif failure == "crc":
        # Alter only an observation's otherwise valid literal bytes, preserving
        # all lengths. The native MCAP DataEnd checksum must catch this late.
        assert raw.count(b'aGVsbG8=') == 1
        changed = raw.replace(b'aGVsbG8=', b'aGVsbG4=')
    else:
        primary = KeyboardInterrupt("offline interrupted") if failure == "interrupt" else OSError("offline failed")
    source = OfflineReplayInput(raw, changed,
        failure=None if primary is None else ("seek" if failure == "seek" else "read", primary))
    result = None
    with pytest.raises(type(primary) if primary is not None else Exception) as caught:
        result = replay_assertions(source, offline_replay_scenario, clocks=CLOCKS)
    assert result is None and not source.closed and len(source.rewinds) == 1
    if primary is not None:
        assert caught.value is primary
    elif failure == "crc":
        from mcap.stream_reader import CRCValidationError
        assert isinstance(caught.value, CRCValidationError)
    else:
        assert isinstance(caught.value, ValueError)


@pytest.mark.parametrize("change", ["observations", "completion"])
def test_offline_replay_revalidates_second_pass_and_evaluates_its_actual_bytes(
        offline_replay_scenario, change):
    from sim.scenario.replay import replay_assertions
    records = offline_replay_records()
    if change == "observations":
        records[0] = offline_replay_observation("east", 1005, poll=1001, text=b"XX", sampled=1)
        records[2] = offline_replay_observation("east", 1019, poll=1018, text=b"YYY", sampled=1)
    else:
        records[-1] = offline_replay_completion("east", 1100, 999, 4)
    source = OfflineReplayInput(offline_replay_bytes(), offline_replay_bytes(records))
    if change == "completion":
        with pytest.raises(ValueError):
            replay_assertions(source, offline_replay_scenario, clocks=CLOCKS)
    else:
        result = replay_assertions(source, offline_replay_scenario, clocks=CLOCKS)
        assert result.status == ("pass", "fail", "fail", "fail", "pass", "fail")
        assert source.eofs == [(0, len(offline_replay_bytes())), (1, len(source.getvalue()))]
    assert len(source.rewinds) == 1 and not source.closed
