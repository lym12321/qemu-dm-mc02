"""Independent MCAP interoperability and real CAN/UART storage consumers."""

from dataclasses import FrozenInstanceError
from io import BytesIO
from pathlib import Path
import socket
import struct
import sys
import zlib

import pytest
from mcap.data_stream import RecordBuilder
from mcap.exceptions import McapError
from mcap.records import Channel, Chunk, DataEnd, Footer, Header, Message
from mcap.stream_reader import StreamReader
from mcap.writer import CompressionType, IndexType, Writer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.boards.channels import ChannelEvent, LabChannels
from sim.fabric import CanBus, CanFrame
from sim.trace import TraceEvent, TraceWriter, iter_events
from sim.transports import StreamTransport
from sim.transports.can_codec import pack_wire_frame


PROFILE = "robot-sim/trace/1"
MAGIC = b"\x89MCAP0\r\n"
DECODE_ERRORS = (ValueError, McapError, struct.error)


def event(**changes):
    fields = dict(timestamp_ns=23, source="east", endpoint="fdcan1",
                  event_type="can.tx", payload=b"\x00\xffA",
                  clock="qemu:east:reset0")
    fields.update(changes)
    return TraceEvent(**fields)


def channel(identifier=1, **changes):
    fields = dict(id=identifier, schema_id=0, topic="fdcan1",
                  message_encoding="application/octet-stream",
                  metadata={"source": "east", "event_type": "can.tx",
                            "clock": "qemu:east:reset0"})
    fields.update(changes)
    return Channel(**fields)


def message(**changes):
    fields = dict(channel_id=1, sequence=0, log_time=23, publish_time=23,
                  data=b"\x00\xffA")
    fields.update(changes)
    return Message(**fields)


def encoded(record):
    """Use the public upstream encoder, independently of sim.trace."""
    if isinstance(record, bytes):
        return record
    builder = RecordBuilder()
    record.write(builder)
    return builder.end()


def native_container(records, *, crc=True, footer=None):
    data = MAGIC + b"".join(encoded(record) for record in records)
    return (data + encoded(DataEnd(zlib.crc32(data) if crc else 0)) +
            encoded(footer or Footer(0, 0, 0)) + MAGIC)


def native_trace(events, *, crc=True):
    output = BytesIO()
    writer = Writer(output, compression=CompressionType.NONE,
                    use_chunking=False, index_types=IndexType.NONE,
                    repeat_channels=False, repeat_schemas=False,
                    use_statistics=False, use_summary_offsets=False,
                    enable_crcs=False, enable_data_crcs=crc)
    writer.start(profile=PROFILE, library="independent/native-mcap-1.5.0")
    identities = {}
    for sequence, item in enumerate(events):
        identity = item.source, item.endpoint, item.event_type, item.clock
        if identity not in identities:
            identities[identity] = writer.register_channel(
                topic=item.endpoint, message_encoding="application/octet-stream",
                schema_id=0, metadata={"source": item.source,
                                       "event_type": item.event_type,
                                       "clock": item.clock})
        writer.add_message(channel_id=identities[identity], sequence=sequence,
                           log_time=item.timestamp_ns,
                           publish_time=item.timestamp_ns, data=item.payload)
    writer.finish()
    return output.getvalue()


class Output:
    """Borrowed output deliberately lacks tell/seek and automatic buffering."""

    def __init__(self):
        self.data = bytearray()
        self.writes = 0
        self.flushes = 0
        self.closed = False
        self.failure = None

    def write(self, data):
        self.writes += 1
        if self.failure == "raise":
            raise OSError("injected output failure")
        if self.failure == "short":
            self.data.extend(data[:-1])
            return len(data) - 1
        if self.failure == "none":
            self.data.extend(data[:len(data) // 2])
            return None
        self.data.extend(data)
        return len(data)

    def flush(self):
        self.flushes += 1
        if self.failure == "flush":
            raise OSError("injected flush failure")

    def close(self):
        self.closed = True


class Input:
    """Borrowed blocking input exposes only bounded reads and caller close."""

    def __init__(self, data):
        self.data = BytesIO(data)
        self.requests = []
        self.closed = False

    def read(self, size):
        assert 0 <= size < 70000, "unbounded read reached the borrowed input"
        self.requests.append(size)
        return self.data.read(size)

    def close(self):
        self.closed = True


def test_writer_native_reader_literal_fields_and_borrowed_lifetime():
    output = Output()
    writer = TraceWriter(output)
    first = event()
    with pytest.raises(FrozenInstanceError):
        first.timestamp_ns = 99
    writer.append(first)
    writer.append(event(timestamp_ns=0, payload=b"", clock="qemu:east:reset1"))
    writer.finish()
    completed = bytes(output.data), output.writes, output.flushes
    writer.finish()
    assert (bytes(output.data), output.writes, output.flushes) == completed
    with pytest.raises(RuntimeError):
        writer.append(first)
    assert not output.closed and output.flushes >= 1

    records = list(StreamReader(BytesIO(output.data), emit_chunks=True,
                                validate_crcs=True).records)
    assert [type(record) for record in records] == [
        Header, Channel, Message, Channel, Message, DataEnd, Footer]
    assert records[0].profile == "robot-sim/trace/1"
    channels = [record for record in records if isinstance(record, Channel)]
    assert [(item.topic, item.schema_id, item.message_encoding, item.metadata)
            for item in channels] == [
        ("fdcan1", 0, "application/octet-stream",
         {"source": "east", "event_type": "can.tx", "clock": "qemu:east:reset0"}),
        ("fdcan1", 0, "application/octet-stream",
         {"source": "east", "event_type": "can.tx", "clock": "qemu:east:reset1"})]
    messages = [record for record in records if isinstance(record, Message)]
    assert [(item.sequence, item.log_time, item.publish_time, item.data)
            for item in messages] == [(0, 23, 23, b"\x00\xffA"), (1, 0, 0, b"")]
    assert messages[0].channel_id == channels[0].id
    assert messages[1].channel_id == channels[1].id
    assert records[-2].data_section_crc != 0
    assert records[-1] == Footer(0, 0, 0)
    output.close()
    assert output.closed


def test_reader_native_writer_preserves_physical_order_and_independent_epochs():
    expected = [event(timestamp_ns=900),
                event(timestamp_ns=3, source="west", clock="qemu:west:reset0"),
                event(timestamp_ns=0, clock="qemu:east:reset1"),
                event(timestamp_ns=0, payload=b"same-time-next")]
    source = Input(native_trace(expected))
    assert list(iter_events(source)) == expected
    assert not source.closed and source.requests
    source.close()
    assert source.closed


def test_empty_container_and_absent_crc_are_format_valid():
    output = Output()
    writer = TraceWriter(output)
    writer.finish()
    assert list(iter_events(BytesIO(output.data))) == []
    expected = [event()]
    assert list(iter_events(BytesIO(native_trace(expected, crc=False)))) == expected


def test_admitted_size_boundaries_remain_exact_without_host_clock_substitution():
    maximum = event(timestamp_ns=2**64 - 1, source="é" * 127 + "a",
                    payload=bytes(range(256)) * 256)
    output = Output()
    writer = TraceWriter(output)
    writer.append(maximum)
    writer.finish()
    assert list(iter_events(Input(output.data))) == [maximum]


@pytest.mark.parametrize("changes", [
    {"timestamp_ns": True}, {"timestamp_ns": -1}, {"timestamp_ns": 2**64},
    {"source": ""}, {"endpoint": "uart\x00hidden"}, {"event_type": 1},
    {"clock": "é" * 128}, {"payload": bytearray(b"mutable")},
    {"payload": b"x" * 65537},
])
def test_event_admission_has_no_io_and_keeps_writer_usable(changes):
    output = Output()
    writer = TraceWriter(output)
    before = bytes(output.data), output.writes, output.flushes
    with pytest.raises((TypeError, ValueError)):
        writer.append(event(**changes))
    assert (bytes(output.data), output.writes, output.flushes) == before
    writer.append(event())
    writer.finish()
    assert list(iter_events(BytesIO(output.data))) == [event()]


def test_channel_capacity_rejection_does_not_write_or_consume_existing_identity():
    output = Output()
    writer = TraceWriter(output)
    for index in range(256):
        writer.append(event(endpoint=f"port{index}"))
    before = bytes(output.data), output.writes
    with pytest.raises((BufferError, ValueError)):
        writer.append(event(endpoint="overflow"))
    assert (bytes(output.data), output.writes) == before
    writer.append(event(endpoint="port0", payload=b"existing"))
    writer.finish()
    decoded = list(iter_events(BytesIO(output.data)))
    assert len(decoded) == 257 and decoded[-1].payload == b"existing"


def test_uint32_last_sequence_native_encoding_and_exhaustion_without_io():
    output = Output()
    writer = TraceWriter(output)
    # Reach the wire boundary without writing four billion prior messages. This
    # fixture deliberately lacks profile ordinal history; inspect it natively.
    writer._sequence = 2**32 - 1
    writer.append(event())
    before = bytes(output.data), output.writes, output.flushes
    with pytest.raises(OverflowError):
        writer.append(event())
    assert (bytes(output.data), output.writes, output.flushes) == before
    writer.finish()
    messages = [item for item in StreamReader(
        BytesIO(output.data), validate_crcs=True).records if isinstance(item, Message)]
    assert [(item.sequence, item.log_time, item.data) for item in messages] == [
        (0xFFFFFFFF, 23, b"\x00\xffA")]
    assert not output.closed


@pytest.mark.parametrize("failure", ["short", "none", "raise", "flush"])
def test_output_failure_freezes_without_retry_or_closing_borrow(failure):
    output = Output()
    writer = TraceWriter(output)
    writer.append(event())
    before_failure = bytes(output.data)
    output.failure = failure
    with pytest.raises((OSError, RuntimeError)):
        if failure == "flush":
            writer.finish()
        else:
            writer.append(event(payload=b"later"))
    if failure in ("short", "none"):
        assert bytes(output.data).startswith(before_failure)
        assert len(output.data) > len(before_failure)
    stopped = bytes(output.data), output.writes, output.flushes
    output.failure = None
    for action in (lambda: writer.append(event()), writer.finish):
        with pytest.raises(RuntimeError):
            action()
        assert (bytes(output.data), output.writes, output.flushes) == stopped
    assert not output.closed


@pytest.mark.parametrize("records", [
    [Header("other/profile", "native")],
    [Header(PROFILE, "x" * 256)],
    [Header(PROFILE, "native"), Header(PROFILE, "native")],
    [channel()],
    [Header(PROFILE, "native"), channel(identifier=0)],
    [Header(PROFILE, "native"), channel(schema_id=1)],
    [Header(PROFILE, "native"), channel(message_encoding="json")],
    [Header(PROFILE, "native"), channel(metadata={"source": "east"})],
    [Header(PROFILE, "native"), channel(metadata={"source": "east", "event_type": "can.tx",
                                                 "clock": "qemu:east:reset0", "extra": "x"})],
    [Header(PROFILE, "native"), channel(), channel()],
    [Header(PROFILE, "native"), channel(), channel(2)],
    [Header(PROFILE, "native"), message()],
    [Header(PROFILE, "native"), channel(), message(channel_id=2)],
    [Header(PROFILE, "native"), channel(), message(sequence=1)],
    [Header(PROFILE, "native"), channel(), message(publish_time=24)],
    [Header(PROFILE, "native"), channel(), message(data=b"x" * 65537)],
    [Header(PROFILE, "native"), channel(), b"\x05" + struct.pack("<Q", 21) + b"\0" * 21],
    [Header(PROFILE, "native")] + [channel(index + 1, topic=f"p{index}")
                                   for index in range(257)],
    [Header(PROFILE, "native"), b"\x7f" + struct.pack("<Q", 0)],
    [Header(PROFILE, "native"), Chunk("zstd", b"not compressed", 0, 0, 0, 2**40)],
])
def test_reader_rejects_nonprofile_records_with_valid_data_crc(records):
    with pytest.raises(DECODE_ERRORS):
        list(iter_events(BytesIO(native_container(records))))


def test_reader_rejects_padding_and_duplicate_metadata_even_with_valid_crc():
    head = Header(PROFILE, "native")
    padded = encoded(head)
    padded = padded[:1] + struct.pack("<Q", len(padded) - 8) + padded[9:] + b"\0"
    with pytest.raises(DECODE_ERRORS):
        list(iter_events(BytesIO(native_container([padded]))))

    # Independent MCAP channel bytes: repeated map keys must not silently collapse.
    def string(value):
        raw = value.encode("utf-8")
        return struct.pack("<I", len(raw)) + raw

    metadata = b"".join(string(key) + string(value) for key, value in [
        ("source", "east"), ("event_type", "can.tx"),
        ("clock", "qemu:east:reset0"), ("source", "east")])
    body = (struct.pack("<HH", 1, 0) + string("fdcan1") +
            string("application/octet-stream") + struct.pack("<I", len(metadata)) + metadata)
    duplicate = b"\x04" + struct.pack("<Q", len(body)) + body
    with pytest.raises(DECODE_ERRORS):
        list(iter_events(BytesIO(native_container([head, duplicate, message()]))))


def test_reader_rejects_summary_footer_and_bounds_malicious_lengths():
    with pytest.raises(DECODE_ERRORS):
        list(iter_events(BytesIO(native_container(
            [Header(PROFILE, "native")], footer=Footer(1, 0, 0)))))
    huge_outer = Input(MAGIC + b"\x01" + struct.pack("<Q", 2**40))
    with pytest.raises(DECODE_ERRORS):
        list(iter_events(huge_outer))
    assert max(huge_outer.requests) < 70000
    # Tiny outer Header claiming a 4GiB nested string; no huge read reaches owner.
    huge_nested = Input(native_container([
        b"\x01" + struct.pack("<Q", 8) + struct.pack("<II", 2**32 - 1, 0)]))
    with pytest.raises(DECODE_ERRORS):
        list(iter_events(huge_nested))
    assert max(huge_nested.requests) < 70000


def test_nested_read_at_record_end_cannot_borrow_next_envelope_bytes():
    profile = PROFILE.encode("utf-8")
    body = struct.pack("<I", len(profile)) + profile  # Missing library length.
    incomplete = b"\x01" + struct.pack("<Q", len(body)) + body
    source = Input(native_container([incomplete]))
    with pytest.raises(DECODE_ERRORS):
        list(iter_events(source))
    assert source.data.tell() == len(MAGIC) + len(incomplete)
    assert not source.closed


def test_reader_requires_complete_crc_footer_trailer_and_eof_before_success():
    raw = native_trace([event()])
    corrupted = raw.replace(b"\x00\xffA", b"\x00\xffB", 1)
    for invalid in (raw[:-1], raw[:-8], raw + b"extra", raw + raw, corrupted):
        source = Input(invalid)
        with pytest.raises(DECODE_ERRORS):
            list(iter_events(source))
        assert not source.closed
    provisional = iter_events(BytesIO(raw[:-1]))
    assert next(provisional) == event()
    with pytest.raises(DECODE_ERRORS):
        next(provisional)


def test_short_blocking_input_read_is_failure_without_retry_or_close():
    class ShortInput(Input):
        def read(self, size):
            self.requests.append(size)
            return self.data.read(max(0, size - 1))

    source = ShortInput(native_trace([event()]))
    with pytest.raises(DECODE_ERRORS):
        list(iter_events(source))
    assert source.requests == [8] and not source.closed


def test_nonblocking_none_at_eof_is_not_accepted_as_completed_binary_input():
    class NoneEofInput(Input):
        def read(self, size):
            data = super().read(size)
            return None if data == b"" else data

    source = NoneEofInput(native_trace([event()]))
    with pytest.raises(ValueError):
        list(iter_events(source))
    assert not source.closed


def test_actual_can_delivery_log_stores_original_fixed84_wire_and_delivery_epoch():
    output = Output()
    writer = TraceWriter(output)
    received = []
    before_receive = []

    class Endpoint:
        def receive(self, frame):
            received.append(frame)

    source, sink, dropped = Endpoint(), Endpoint(), Endpoint()

    def log(delivery):
        before_receive.append(len(received))
        writer.append(TraceEvent(
            delivery.delivered_ns, delivery.source, delivery.destination,
            "can.drop" if delivery.dropped else "can.deliver",
            pack_wire_frame(delivery.frame), "fabric:run0"))

    bus = CanBus(delay_ns=7, drop=lambda _source, destination, _frame: destination is dropped,
                 log=log)
    bus.attach(source, "east")
    bus.attach(sink, "motor1")
    bus.attach(dropped, "motor2")
    bus.send(source, CanFrame(0x123, data=b"\x00\xffA", timestamp_ns=11))
    assert bus.step(25) == 1
    writer.finish()
    literal = (bytes.fromhex("2301000000000000030000000b00000000000000") +
               b"\x00\xffA" + b"\x00" * 61)
    assert len(literal) == 84
    assert list(iter_events(BytesIO(output.data))) == [
        TraceEvent(25, "east", "motor1", "can.deliver", literal, "fabric:run0"),
        TraceEvent(25, "east", "motor2", "can.drop", literal, "fabric:run0")]
    assert before_receive == [0, 1]
    assert received == [CanFrame(0x123, data=b"\x00\xffA", timestamp_ns=25)]


def test_actual_uart_split_reads_store_opaque_bytes_and_poll_clock_only():
    local, peer = socket.socketpair()
    local.setblocking(False)
    try:
        stream = StreamTransport(local)
        lab = LabChannels(uarts={"uart7": stream})
        output = Output()
        writer = TraceWriter(output)
        peer.sendall(b"R\x00")
        first = lab.service(17)
        assert first == [ChannelEvent("uart7", 17, b"R\x00")]
        assert lab.service(18) == []
        peer.sendall(b"\xffEADY\n")
        second = lab.service(21)
        assert second == [ChannelEvent("uart7", 21, b"\xffEADY\n")]
        for item in first + second:
            writer.append(TraceEvent(item.timestamp_ns, "west", item.endpoint,
                                     "uart.rx", item.data, "qemu:west:reset7:poll"))
        writer.finish()
        assert list(iter_events(BytesIO(output.data))) == [
            TraceEvent(17, "west", "uart7", "uart.rx", b"R\x00", "qemu:west:reset7:poll"),
            TraceEvent(21, "west", "uart7", "uart.rx", b"\xffEADY\n", "qemu:west:reset7:poll")]
        assert local.fileno() >= 0 and not output.closed
    finally:
        local.close()
        peer.close()
