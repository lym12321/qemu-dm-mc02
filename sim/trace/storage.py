"""Small opaque events in physical MCAP order with explicit source clocks.

This profile uses the upstream MCAP codec and CRC implementation. Streams are
borrowed blocking binary objects; this module neither closes them nor owns a
clock, transport or replay scheduler. Iterator results are provisional until
exhaustion validates the footer, trailing magic and EOF. MCAP CRC zero means
absent: only checksums present in the container can be checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from mcap.data_stream import RecordBuilder
from mcap.opcode import Opcode
from mcap.records import Channel, DataEnd, Footer, Header, Message
from mcap.stream_reader import StreamReader
from mcap.writer import CompressionType, IndexType, Writer

PROFILE = "robot-sim/trace/1"
ENCODING = "application/octet-stream"
MAX_LABEL_BYTES = 255
MAX_PAYLOAD_BYTES = 65536
MAX_CHANNELS = 256
# Message header is 22 bytes; even a maximum-label Channel body is only 1105.
MAX_RECORD_BYTES = MAX_PAYLOAD_BYTES + 22
_OPCODES = {Opcode.HEADER, Opcode.CHANNEL, Opcode.MESSAGE,
            Opcode.DATA_END, Opcode.FOOTER}


def _label(value: str, name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"trace {name} must be a string")
    if (not value or len(value) > MAX_LABEL_BYTES or "\0" in value or
            len(value.encode("utf-8")) > MAX_LABEL_BYTES):
        raise ValueError(f"trace {name} must be nonempty, NUL-free and <=255 UTF-8 bytes")


@dataclass(frozen=True)
class TraceEvent:
    """Opaque bytes at one source timestamp, in a caller-named clock/epoch.

    Clock labels distinguish independent clocks and resets. Neither numeric time
    nor append order establishes synchronization with another source.
    """

    timestamp_ns: int
    source: str
    endpoint: str
    event_type: str
    payload: bytes
    clock: str

    def __post_init__(self) -> None:
        if type(self.timestamp_ns) is not int:
            raise TypeError("trace timestamp_ns must be an integer")
        if not 0 <= self.timestamp_ns <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError("trace timestamp_ns must fit uint64")
        for name in ("source", "endpoint", "event_type", "clock"):
            _label(getattr(self, name), name)
        if type(self.payload) is not bytes:
            raise TypeError("trace payload must be bytes")
        if len(self.payload) > MAX_PAYLOAD_BYTES:
            raise ValueError("trace payload must be <=65536 bytes")


class _Output:
    """Check upstream's unchecked write counts without taking stream ownership."""

    def __init__(self, stream):
        self.write_bytes = getattr(stream, "write", None)
        self.flush = getattr(stream, "flush", None)
        if not callable(self.write_bytes) or not callable(self.flush):
            raise TypeError("trace output requires callable write/flush")
        self.position = 0

    def write(self, data: bytes) -> int:
        if data:
            count = self.write_bytes(data)
            if type(count) is not int or count != len(data):
                raise OSError("short trace write; output is incomplete")
            self.position += count
        return len(data)

    def tell(self) -> int:
        return self.position


class TraceWriter:
    """Append small events; finish explicitly, then close the caller's stream.

    Admission errors leave the writer usable. An attempted MCAP write or flush
    failure freezes it: partial external effects cannot be retried or rolled back.
    Successful finish is idempotent. At most 256 channel identities and 2**32
    events fit one container; rotate files explicitly before exhausting a limit.
    No event history, chunk index or summary is retained.
    """

    def __init__(self, output):
        self._output = _Output(output)
        self._writer = Writer(
            self._output, compression=CompressionType.NONE,
            use_chunking=False, index_types=IndexType.NONE,
            repeat_channels=False, repeat_schemas=False, use_statistics=False,
            use_summary_offsets=False, enable_crcs=False, enable_data_crcs=True)
        self._channels: dict[tuple[str, str, str, str], int] = {}
        self._sequence = 0
        self._failed = False
        self._finished = False
        self._writer.start(profile=PROFILE)

    def _active(self) -> None:
        if self._failed:
            raise RuntimeError("trace writer failed; output cannot be retried")
        if self._finished:
            raise RuntimeError("trace writer is finished")

    def append(self, event: TraceEvent) -> None:
        self._active()
        if type(event) is not TraceEvent:
            raise TypeError("trace append requires a TraceEvent")
        identity = (event.source, event.endpoint, event.event_type, event.clock)
        channel_id = self._channels.get(identity)
        if channel_id is None and len(self._channels) >= MAX_CHANNELS:
            raise BufferError("trace channel capacity exhausted")
        if self._sequence > 0xFFFFFFFF:
            raise OverflowError("trace sequence exhausted")
        try:
            if channel_id is None:
                channel_id = self._writer.register_channel(
                    topic=event.endpoint, message_encoding=ENCODING, schema_id=0,
                    metadata={"source": event.source, "event_type": event.event_type,
                              "clock": event.clock})
            self._writer.add_message(
                channel_id=channel_id, log_time=event.timestamp_ns,
                publish_time=event.timestamp_ns, sequence=self._sequence,
                data=event.payload)
        except BaseException:
            self._failed = True
            raise
        self._channels[identity] = channel_id
        self._sequence += 1

    def finish(self) -> None:
        if self._finished:
            return
        self._active()
        try:
            self._writer.finish()
            self._output.flush()
        except BaseException:
            self._failed = True
            raise
        self._finished = True


class _Input:
    """Bound only MCAP envelopes; upstream still decodes every record body.

    Native StreamReader's outer limit does not bound nested string lengths and
    it silently skips unknown opcodes. Reject them before any body read. Capture
    one bounded record for native re-encoding comparison, never event history.
    A plain proxy also avoids upstream wrapping/closing a borrowed RawIOBase.
    """

    def __init__(self, stream):
        self.read_bytes = getattr(stream, "read", None)
        if not callable(self.read_bytes):
            raise TypeError("trace input requires callable read")
        self.state = "magic"
        self.remaining = 0
        self.opcode = 0
        self.record = bytearray()

    def read(self, size: int) -> bytes:
        if size == 0:
            return b""
        expected = {"magic": 8, "opcode": 1, "length": 8, "trailer": 8}
        if self.state == "body":
            if size < 0 or size > self.remaining:
                raise ValueError("trace field exceeds its record boundary")
        elif self.state not in expected or size != expected[self.state]:
            raise ValueError("invalid trace record framing")
        data = self.read_bytes(size)
        if type(data) is not bytes or len(data) != size:
            raise ValueError("truncated trace; input must provide blocking binary reads")
        if self.state == "magic":
            self.state = "opcode"
        elif self.state == "opcode":
            self.opcode = data[0]
            if self.opcode not in _OPCODES:
                raise ValueError("unsupported trace MCAP record")
            self.record = bytearray(data)
            self.state = "length"
        elif self.state == "length":
            self.remaining = int.from_bytes(data, "little")
            if not 0 < self.remaining <= MAX_RECORD_BYTES:
                raise ValueError("trace MCAP record size exceeds profile bounds")
            self.record.extend(data)
            self.state = "body"
        elif self.state == "body":
            self.record.extend(data)
            self.remaining -= size
            if not self.remaining:
                # Only the outer consumer may admit the next envelope after
                # native parsing has returned. A nested field cannot read it.
                self.state = "record_end"
        else:
            self.state = "done"
        return data


def iter_events(input) -> Iterator[TraceEvent]:
    """Read this profile in physical append order, without owning the input.

    Fully exhaust before accepting a trace. Early yields cannot attest the later
    CRC/footer/EOF; a future replay consumer must validate before external effects.
    IO and native MCAP decoding errors propagate; profile errors raise ValueError.
    """
    stream = _Input(input)
    channels: dict[int, tuple[str, str, str, str]] = {}
    identities: set[tuple[str, str, str, str]] = set()
    header = data_end = footer = False
    sequence = 0
    for record in StreamReader(stream, emit_chunks=True, validate_crcs=True,
                               record_size_limit=MAX_RECORD_BYTES).records:
        # Reuse the native encoder to detect tolerated padding, duplicate keys
        # and inner length inconsistencies without copying MCAP's body parser.
        builder = RecordBuilder()
        record.write(builder)
        if builder.end() != stream.record:
            raise ValueError("noncanonical trace MCAP record")
        if stream.state != "record_end":
            raise ValueError("incomplete trace MCAP record")
        stream.state = "trailer" if stream.opcode == Opcode.FOOTER else "opcode"
        if not header:
            if not isinstance(record, Header) or record.profile != PROFILE:
                raise ValueError("unsupported trace MCAP profile/header")
            if len(record.library.encode("utf-8")) > MAX_LABEL_BYTES:
                raise ValueError("trace MCAP library label exceeds 255 UTF-8 bytes")
            header = True
        elif isinstance(record, Channel) and not data_end:
            if (record.id == 0 or record.id in channels or
                    record.schema_id != 0 or record.message_encoding != ENCODING or
                    set(record.metadata) != {"source", "event_type", "clock"}):
                raise ValueError("invalid trace MCAP channel")
            identity = (record.metadata["source"], record.topic,
                        record.metadata["event_type"], record.metadata["clock"])
            for value, name in zip(identity, ("source", "endpoint", "event_type", "clock")):
                _label(value, name)
            if identity in identities or len(channels) >= MAX_CHANNELS:
                raise ValueError("duplicate trace channel identity or capacity exhausted")
            channels[record.id] = identity
            identities.add(identity)
        elif isinstance(record, Message) and not data_end:
            if (record.channel_id not in channels or record.sequence != sequence or
                    record.publish_time != record.log_time):
                raise ValueError("invalid trace MCAP message channel/sequence/time")
            source, endpoint, event_type, clock = channels[record.channel_id]
            event = TraceEvent(record.log_time, source, endpoint, event_type,
                               record.data, clock)
            sequence += 1
            yield event
        elif isinstance(record, DataEnd) and not data_end:
            data_end = True
        elif isinstance(record, Footer) and data_end:
            if record.summary_start or record.summary_offset_start or record.summary_crc:
                raise ValueError("trace MCAP summaries are unsupported")
            footer = True
        else:
            raise ValueError("invalid trace MCAP record order")
    # StreamReader yields Footer *before* reading the trailing magic. Reaching
    # this point requires resuming it through that check, then checking real EOF.
    if not (header and data_end and footer and stream.state == "done"):
        raise ValueError("incomplete trace MCAP container")
    tail = stream.read_bytes(1)
    if type(tail) is not bytes or tail != b"":
        raise ValueError("trailing data after trace MCAP container")
