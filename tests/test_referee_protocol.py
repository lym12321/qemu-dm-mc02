"""Independent referee envelope vectors, without payload schemas or CRC copies."""

from dataclasses import FrozenInstanceError
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.devices.referee import Frame, decode_frame, encode_frame
from sim.devices.referee.protocol import MAX_FRAME_BYTES, MAX_PAYLOAD_BYTES


# Literals were independently obtained from the actual read-only trobot
# components/utils/src/crc.cc tables, SHA256
# 6b5651ba4be79e3ab8f27c9755f3bbbad988bc8d31ceb5dd2df9b706ef8e3530.
# The audit cross-checked the game vector against pinned DJI CRC tables.
# Command data here is opaque: these fixtures make no payload-layout claim.
VECTORS = (
    (0x0000, b"", 0x00, "a5000000c300004c83"),
    (0xffff, b"", 0xff, "a50000fff6ffff3509"),
    (0x0001, bytes.fromhex("412c010000000000000000"), 0x00,
     "a50b0000020100412c0100000000000000009357"),
    (0x0201, bytes.fromhex("03026400c8000a006400500007"), 0x01,
     "a50d00018d010203026400c8000a006400500007c4e8"),
)
GAME_WIRE = bytes.fromhex(VECTORS[2][3])


@pytest.mark.parametrize("command,payload,sequence,wire_hex", VECTORS)
def test_independent_literal_frames_in_both_directions(command, payload, sequence, wire_hex):
    wire = bytes.fromhex(wire_hex)
    assert encode_frame(command, payload, sequence) == wire
    assert decode_frame(wire) == Frame(command, payload, sequence)


def test_default_sequence_and_immutable_decoded_frame():
    wire = bytes.fromhex(VECTORS[0][3])
    assert encode_frame(0, b"") == wire
    frame = decode_frame(wire)
    with pytest.raises(FrozenInstanceError):
        frame.sequence = 1


@pytest.mark.parametrize("field,value", [
    ("command_id", True), ("command_id", -1), ("command_id", 0x10000),
    ("command_id", 1.0), ("sequence", False), ("sequence", -1),
    ("sequence", 256), ("sequence", "0"),
])
def test_encode_rejects_noncanonical_integer_fields(field, value):
    fields = dict(command_id=1, payload=b"", sequence=0)
    fields[field] = value
    with pytest.raises(ValueError):
        encode_frame(**fields)


@pytest.mark.parametrize("payload", [None, "bytes", bytearray(b"x"), memoryview(b"x")])
def test_encode_requires_immutable_bytes_payload(payload):
    with pytest.raises(TypeError, match="payload must be bytes"):
        encode_frame(1, payload)


@pytest.mark.parametrize("raw", [None, "frame", bytearray(GAME_WIRE), memoryview(GAME_WIRE)])
def test_decode_requires_exact_bytes(raw):
    with pytest.raises(TypeError, match="frame must be bytes"):
        decode_frame(raw)


@pytest.mark.parametrize("length", [0, 4, 8, len(GAME_WIRE) - 1])
def test_truncated_header_command_payload_or_crc_is_rejected(length):
    with pytest.raises(ValueError):
        decode_frame(GAME_WIRE[:length])


@pytest.mark.parametrize("suffix", [b"\0", bytes.fromhex(VECTORS[0][3])])
def test_trailing_byte_or_second_complete_frame_is_rejected(suffix):
    with pytest.raises(ValueError, match="payload length"):
        decode_frame(GAME_WIRE + suffix)


def test_sof_and_declared_length_are_checked():
    with pytest.raises(ValueError, match="SOF"):
        decode_frame(b"\xa4" + GAME_WIRE[1:])
    with pytest.raises(ValueError, match="payload length"):
        decode_frame(GAME_WIRE[:1] + b"\x00\x0b" + GAME_WIRE[3:])


@pytest.mark.parametrize("offset", [3, 4])
def test_changed_sequence_or_header_crc_is_rejected(offset):
    damaged = bytearray(GAME_WIRE)
    damaged[offset] ^= 1
    with pytest.raises(ValueError, match="CRC8"):
        decode_frame(bytes(damaged))


@pytest.mark.parametrize("offset", [5, 7, -1])
def test_changed_command_payload_or_body_crc_is_rejected(offset):
    damaged = bytearray(GAME_WIRE)
    damaged[offset] ^= 1
    with pytest.raises(ValueError, match="CRC16"):
        decode_frame(bytes(damaged))


def test_unknown_command_and_binary_payload_remain_opaque():
    # Secondary round trip checks opacity; independent literals above prove CRC.
    frame = Frame(0xdeaf, b"\xa5\x00\xff\xa5", 0x80)
    assert decode_frame(encode_frame(frame.command_id, frame.payload, frame.sequence)) == frame


def test_supported_size_is_a_project_profile_and_oversize_input_is_rejected():
    assert (MAX_PAYLOAD_BYTES, MAX_FRAME_BYTES) == (119, 128)
    # Independently calculated with the same pinned trobot CRC tables.
    payload = bytes(range(119))
    wire = bytes.fromhex("a577005af33412") + payload + bytes.fromhex("29bb")
    assert len(wire) == 128
    assert encode_frame(0x1234, payload, 0x5a) == wire
    assert decode_frame(wire) == Frame(0x1234, payload, 0x5a)
    # Correct CRCs do not admit frames beyond this project's supported subset.
    oversized = bytes.fromhex("a578005aac3412") + bytes(range(120)) + bytes.fromhex("397a")
    with pytest.raises(ValueError, match="exceeds 119"):
        encode_frame(0x1234, bytes(range(120)), 0x5a)
    with pytest.raises(ValueError, match="outside the supported profile"):
        decode_frame(oversized)
