"""Pure RoboMaster referee frame envelope; command payloads remain opaque.

RoboMaster 2026 communication protocol V1.1.0 (20251217), p4 and pp77-82.
Wire: A5, payload length u16 LE, sequence u8, header CRC8, command u16 LE,
payload, CRC16 LE. CRC algorithms belong to pinned MIT crccheck 1.3.1.
The supported profile limits complete frames to 128 bytes. This module does
not implement a stream parser, command schemas, sequence policy or rules.
"""

from dataclasses import dataclass
import struct


MAX_FRAME_BYTES = 128
MAX_PAYLOAD_BYTES = MAX_FRAME_BYTES - 9


def _checksums():
    try:
        from crccheck.crc import Crc8MaximDow, Crc16Mcrf4Xx
    except ModuleNotFoundError as error:
        if error.name not in ("crccheck", "crccheck.crc"):
            raise
        from sim.api import BackendUnavailable
        raise BackendUnavailable("referee protocol requires the referee extra (crccheck==1.3.1)") from error
    return Crc8MaximDow, Crc16Mcrf4Xx


@dataclass(frozen=True)
class Frame:
    command_id: int
    payload: bytes
    sequence: int


def _uint(value, maximum: int, label: str) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{label} must be an integer in 0..{maximum}")
    return value


def encode_frame(command_id: int, payload: bytes, sequence: int = 0) -> bytes:
    """Encode one bounded frame; sequence is explicit and has no side effects."""
    _uint(command_id, 0xffff, "command ID")
    _uint(sequence, 0xff, "sequence")
    if type(payload) is not bytes:
        raise TypeError("referee payload must be bytes")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"referee payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    Crc8MaximDow, Crc16Mcrf4Xx = _checksums()
    header = struct.pack("<BHB", 0xa5, len(payload), sequence)
    header += bytes((Crc8MaximDow.calc(header, initvalue=0xff),))
    body = header + struct.pack("<H", command_id) + payload
    return body + struct.pack("<H", Crc16Mcrf4Xx.calc(body))


def decode_frame(raw: bytes) -> Frame:
    """Validate one complete frame, rejecting truncation and trailing bytes."""
    if type(raw) is not bytes:
        raise TypeError("referee frame must be bytes")
    if not 9 <= len(raw) <= MAX_FRAME_BYTES:
        raise ValueError("referee frame length is outside the supported profile")
    if raw[0] != 0xa5:
        raise ValueError("referee frame SOF must be 0xa5")
    payload_length = struct.unpack_from("<H", raw, 1)[0]
    if payload_length + 9 != len(raw):
        raise ValueError("referee payload length does not match the complete frame")
    Crc8MaximDow, Crc16Mcrf4Xx = _checksums()
    if Crc8MaximDow.calc(raw[:4], initvalue=0xff) != raw[4]:
        raise ValueError("referee header CRC8 mismatch")
    if Crc16Mcrf4Xx.calc(raw[:-2]) != struct.unpack_from("<H", raw, len(raw) - 2)[0]:
        raise ValueError("referee frame CRC16 mismatch")
    return Frame(struct.unpack_from("<H", raw, 5)[0], raw[7:-2], raw[3])
