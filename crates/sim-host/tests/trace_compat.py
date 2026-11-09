"""Independent Python MCAP 1.5.0 compatibility consumer, test tool only."""
from io import BytesIO
from pathlib import Path
import sys
from mcap.stream_reader import StreamReader
from mcap.writer import Writer, CompressionType, IndexType
from mcap.records import Header, Channel, Message, DataEnd, Footer

PROFILE = "robot-sim/trace/1"
EVENTS = [(900, "east", "fdcan1", "can.tx", b"\x00\xffA", "qemu:east:reset0"),
          (3, "west", "fdcan1", "can.tx", b"west", "qemu:west:reset0"),
          (0, "east", "fdcan1", "can.tx", b"", "qemu:east:reset1")]
path = Path(sys.argv[2])
if sys.argv[1] == "write":
    with path.open("wb") as output:
        writer = Writer(output, compression=CompressionType.NONE, use_chunking=False,
                        index_types=IndexType.NONE, repeat_channels=False,
                        repeat_schemas=False, use_statistics=False,
                        use_summary_offsets=False, enable_crcs=False, enable_data_crcs=True)
        writer.start(profile=PROFILE, library="independent/python-mcap-1.5.0")
        for sequence, (time, source, endpoint, kind, payload, clock) in enumerate(EVENTS):
            channel = writer.register_channel(endpoint, "application/octet-stream", 0,
                                              {"source": source, "event_type": kind, "clock": clock})
            writer.add_message(channel, time, payload, time, sequence)
        writer.finish()
elif sys.argv[1] == "read":
    records = list(StreamReader(BytesIO(path.read_bytes()), emit_chunks=True, validate_crcs=True).records)
    assert [type(r) for r in records] == [Header, Channel, Message, Channel, Message, Channel, Message, DataEnd, Footer]
    assert records[0].profile == PROFILE
    channels = {r.id: r for r in records if isinstance(r, Channel)}
    actual = []
    for r in records:
        if isinstance(r, Message):
            c = channels[r.channel_id]
            assert c.schema_id == 0 and c.message_encoding == "application/octet-stream"
            assert r.sequence == len(actual) and r.log_time == r.publish_time
            actual.append((r.log_time, c.metadata["source"], c.topic, c.metadata["event_type"], r.data, c.metadata["clock"]))
    assert actual == EVENTS
    assert records[-2].data_section_crc != 0 and records[-1] == Footer(0, 0, 0)
else:
    raise ValueError("unknown compatibility action")
