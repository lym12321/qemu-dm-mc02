"""Existing fixed84 QEMU and native Linux CAN record codecs.

The record formats carry transport metadata, not CAN electrical arbitration or a
CPU virtual-time barrier. Native SocketCAN does not carry the QEMU timestamp.
"""

from __future__ import annotations

import struct

from sim.fabric.frame import (
    CanFrame, MAX_DATA_LEN, WIRE_EXTENDED, WIRE_RTR, WIRE_FD, WIRE_BRS,
    WIRE_FLAGS, CAN_SFF_MASK, CAN_EFF_MASK, _FD_DLC_LENGTHS,
    dlc_to_length, length_to_dlc,
)

WIRE_SIZE = 84
WIRE_HEADER_SIZE = 20

CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CANFD_BRS = 0x01
CANFD_ESI = 0x02
CANFD_FDF = 0x04

_CAN_STRUCT = struct.Struct("=IB3x8s")
_CANFD_STRUCT = struct.Struct("=IBBBB64s")
_WIRE_STRUCT = struct.Struct("<IIB3xQ64s")


def pack_wire_frame(frame: CanFrame | None = None, *, can_id: int = 0,
                    flags: int = 0, dlc: int | None = None,
                    data: bytes = b"", timestamp_ns: int = 0) -> bytes:
    """Encode one DM-MC02 frame into exactly 84 little-endian bytes."""
    item = frame or CanFrame(can_id, flags, dlc, data, timestamp_ns)
    if frame is not None and any((can_id, flags, dlc, data, timestamp_ns)):
        raise TypeError("pass either frame or frame fields, not both")
    return _WIRE_STRUCT.pack(item.can_id, item.flags, item.dlc, item.timestamp_ns,
                             item.data.ljust(MAX_DATA_LEN, b"\0"))


def unpack_wire_frame(raw: bytes) -> CanFrame:
    if len(raw) != WIRE_SIZE:
        raise ValueError(f"DM-MC02 wire frame must be {WIRE_SIZE} bytes")
    can_id, flags, dlc, timestamp_ns, data = _WIRE_STRUCT.unpack(raw)
    if raw[9:12] != b"\0\0\0":
        raise ValueError("wire frame contains non-zero reserved bytes")
    if flags & ~WIRE_FLAGS:
        raise ValueError("wire frame contains unsupported flags")
    length = dlc_to_length(dlc, bool(flags & WIRE_FD))
    return CanFrame(can_id, flags, dlc, data[:length], timestamp_ns)


def pack_can_frame(frame: CanFrame) -> bytes:
    """Encode a CanFrame as Linux ``can_frame`` or ``canfd_frame`` bytes."""
    can_id = frame.can_id
    if frame.flags & WIRE_EXTENDED:
        can_id |= CAN_EFF_FLAG
    if frame.flags & WIRE_RTR:
        can_id |= CAN_RTR_FLAG
    if frame.flags & WIRE_FD:
        fd_flags = CANFD_FDF | (CANFD_BRS if frame.flags & WIRE_BRS else 0)
        return _CANFD_STRUCT.pack(can_id, frame.length, fd_flags, 0, 0,
                                  frame.data.ljust(MAX_DATA_LEN, b"\0"))
    return _CAN_STRUCT.pack(can_id, frame.dlc, frame.data.ljust(8, b"\0"))


def unpack_can_frame(raw: bytes, timestamp_ns: int = 0) -> CanFrame:
    """Decode one native Linux SocketCAN record (classic or CAN-FD)."""
    if len(raw) == _CAN_STRUCT.size:
        can_id, dlc, data = _CAN_STRUCT.unpack(raw)
        if raw[5:8] != b"\0\0\0":
            raise ValueError("classic CAN record contains non-zero reserved bytes")
        fd = False
        flags = WIRE_RTR if can_id & CAN_RTR_FLAG else 0
    elif len(raw) == _CANFD_STRUCT.size:
        can_id, length, fd_flags, _, _, data = _CANFD_STRUCT.unpack(raw)
        if raw[6:8] != b"\0\0":
            raise ValueError("CAN-FD record contains non-zero reserved bytes")
        if fd_flags & ~(CANFD_BRS | CANFD_ESI | CANFD_FDF):
            raise ValueError("CAN-FD record contains unsupported flags")
        if not (fd_flags & CANFD_FDF):
            raise ValueError("CAN-FD record is missing CANFD_FDF")
        if fd_flags & CANFD_ESI:
            raise ValueError("CAN-FD ESI is not representable on the DM wire")
        fd = True
        dlc = length_to_dlc(length)
        flags = WIRE_FD | (WIRE_RTR if can_id & CAN_RTR_FLAG else 0)
        flags |= WIRE_BRS if fd_flags & CANFD_BRS else 0
    else:
        raise ValueError("SocketCAN record must be 16-byte classic or 72-byte FD")
    if can_id & CAN_ERR_FLAG:
        raise ValueError("CAN error frames are not supported by DM-MC02 wire format")
    if fd and can_id & CAN_RTR_FLAG:
        raise ValueError("CAN-FD frames cannot be remote frames")
    extended = bool(can_id & CAN_EFF_FLAG)
    if not extended and can_id & ~(CAN_SFF_MASK | CAN_RTR_FLAG):
        raise ValueError("standard CAN frame contains bits outside its ID")
    if extended:
        flags |= WIRE_EXTENDED
    identifier = can_id & (CAN_EFF_MASK if extended else CAN_SFF_MASK)
    length = dlc_to_length(dlc, fd)
    return CanFrame(identifier, flags, dlc, data[:length], timestamp_ns)
