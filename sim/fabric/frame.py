"""Shared validated CAN frame and DLC semantics, independent of host transports."""

from __future__ import annotations

from dataclasses import dataclass

MAX_DATA_LEN = 64

WIRE_EXTENDED = 1 << 0
WIRE_RTR = 1 << 1
WIRE_FD = 1 << 2
WIRE_BRS = 1 << 3
WIRE_FLAGS = WIRE_EXTENDED | WIRE_RTR | WIRE_FD | WIRE_BRS

CAN_SFF_MASK = 0x7FF
CAN_EFF_MASK = 0x1FFFFFFF
_FD_DLC_LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)


@dataclass(frozen=True)
class CanFrame:
    can_id: int
    flags: int = 0
    dlc: int | None = None
    data: bytes = b""
    timestamp_ns: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.can_id, int) or self.can_id < 0:
            raise ValueError("CAN ID must be an unsigned integer")
        if self.flags & ~WIRE_FLAGS:
            raise ValueError("unsupported DM-MC02 CAN flags")
        if self.flags & WIRE_EXTENDED:
            if self.can_id > CAN_EFF_MASK:
                raise ValueError("extended CAN ID must be 0..0x1fffffff")
        elif self.can_id > CAN_SFF_MASK:
            raise ValueError("standard CAN ID must be 0..0x7ff")
        if self.flags & WIRE_FD and self.flags & WIRE_RTR:
            raise ValueError("CAN-FD frames cannot be remote frames")
        payload = bytes(self.data)
        object.__setattr__(self, "data", payload)
        if len(payload) > MAX_DATA_LEN:
            raise ValueError("CAN payload must be at most 64 bytes")
        if self.flags & WIRE_FD:
            if self.dlc is None:
                wanted = len(payload)
                if wanted > 8 and wanted not in _FD_DLC_LENGTHS:
                    raise ValueError("CAN-FD payload length needs a representable DLC")
                dlc = next(i for i, size in enumerate(_FD_DLC_LENGTHS) if size == wanted)
            else:
                dlc = self.dlc
            if not 0 <= dlc <= 15 or _FD_DLC_LENGTHS[dlc] < len(payload):
                raise ValueError("invalid CAN-FD DLC/payload length")
        else:
            dlc = len(payload) if self.dlc is None else self.dlc
            if not 0 <= dlc <= 8 or len(payload) > dlc:
                raise ValueError("classic CAN DLC must be 0..8 and contain the payload")
            if self.flags & WIRE_BRS:
                raise ValueError("BRS is valid only for CAN-FD")
        object.__setattr__(self, "dlc", dlc)
        if not 0 <= self.timestamp_ns <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError("timestamp_ns must be an unsigned 64-bit value")

    @property
    def length(self) -> int:
        return _FD_DLC_LENGTHS[self.dlc] if self.flags & WIRE_FD else self.dlc


def dlc_to_length(dlc: int, fd: bool = True) -> int:
    """Return payload length represented by a CAN DLC."""
    if not 0 <= dlc <= 15:
        raise ValueError("DLC must be 0..15")
    if not fd and dlc > 8:
        raise ValueError("classic CAN DLC must be 0..8")
    return _FD_DLC_LENGTHS[dlc] if fd else dlc


def length_to_dlc(length: int, fd: bool = True) -> int:
    if not 0 <= length <= MAX_DATA_LEN:
        raise ValueError("CAN payload length must be 0..64")
    if not fd and length > 8:
        raise ValueError("classic CAN payload must be at most 8 bytes")
    if not fd:
        return length
    try:
        return _FD_DLC_LENGTHS.index(length)
    except ValueError as exc:
        raise ValueError("CAN-FD length is not representable by a DLC") from exc
