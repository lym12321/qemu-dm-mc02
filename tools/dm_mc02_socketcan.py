#!/usr/bin/env python3
"""Optional host-side bridge for the DM-MC02 FDCAN chardev.

The QEMU side uses a deliberately fixed wire format, while Linux SocketCAN
uses variable-size ``struct can_frame``/``struct canfd_frame`` records.  This
module keeps those details isolated and has no QEMU dependency.
"""

from __future__ import annotations

import errno
import platform
import select
import socket
import struct
import weakref
from dataclasses import dataclass
from typing import Any

WIRE_SIZE = 84
WIRE_HEADER_SIZE = 20
MAX_DATA_LEN = 64

WIRE_EXTENDED = 1 << 0
WIRE_RTR = 1 << 1
WIRE_FD = 1 << 2
WIRE_BRS = 1 << 3
WIRE_FLAGS = WIRE_EXTENDED | WIRE_RTR | WIRE_FD | WIRE_BRS

CAN_SFF_MASK = 0x7FF
CAN_EFF_MASK = 0x1FFFFFFF
CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CANFD_BRS = 0x01
CANFD_ESI = 0x02
CANFD_FDF = 0x04

_CAN_STRUCT = struct.Struct("=IB3x8s")
_CANFD_STRUCT = struct.Struct("=IBBBB64s")
_WIRE_STRUCT = struct.Struct("<IIB3xQ64s")
_FD_DLC_LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)


class SocketCANError(RuntimeError):
    """Raised when the host cannot provide the requested SocketCAN feature."""


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


def _require_socketcan() -> None:
    if platform.system() != "Linux":
        raise SocketCANError("SocketCAN is supported only on Linux")
    if not hasattr(socket, "AF_CAN"):
        raise SocketCANError("this Python build has no AF_CAN support")
    if not hasattr(socket, "CAN_RAW"):
        raise SocketCANError("this Python build has no CAN_RAW support")


def open_socketcan(interface: str, *, enable_fd: bool = True) -> socket.socket:
    """Open and bind a raw SocketCAN interface, with clear host errors."""
    _require_socketcan()
    try:
        sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        if enable_fd:
            option = getattr(socket, "CAN_RAW_FD_FRAMES", 5)
            sock.setsockopt(socket.SOL_CAN_RAW, option, 1)
        sock.bind((interface,))
        return sock
    except OSError as exc:
        try:
            sock.close()
        except UnboundLocalError:
            pass
        if exc.errno in (errno.ENODEV, errno.ENXIO):
            raise SocketCANError(f"SocketCAN interface is unavailable: {interface}") from exc
        raise SocketCANError(f"cannot open SocketCAN interface {interface}: {exc}") from exc


def _send_all(sock: Any, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        count = sock.send(view)
        if count <= 0:
            raise ConnectionError("socket closed while sending")
        view = view[count:]


class _BridgeState:
    def __init__(self) -> None:
        self.wire_rx = bytearray()
        self.next_direction = "wire-to-can"


_BRIDGE_STATES: weakref.WeakKeyDictionary[Any, _BridgeState] = \
    weakref.WeakKeyDictionary()
_BRIDGE_FALLBACK_STATES: dict[tuple[int, int], _BridgeState] = {}


def _bridge_state(wire_sock: Any, can_sock: Any) -> _BridgeState:
    """Keep stream and scheduling state without changing socket APIs."""
    try:
        state = _BRIDGE_STATES.get(wire_sock)
        if state is None:
            state = _BridgeState()
            _BRIDGE_STATES[wire_sock] = state
        return state
    except (TypeError, KeyError):
        key = (id(wire_sock), id(can_sock))
        return _BRIDGE_FALLBACK_STATES.setdefault(key, _BridgeState())


def _take_wire_frame(state: _BridgeState) -> CanFrame | None:
    if len(state.wire_rx) < WIRE_SIZE:
        return None
    raw = bytes(state.wire_rx[:WIRE_SIZE])
    del state.wire_rx[:WIRE_SIZE]
    return unpack_wire_frame(raw)


def bridge_once(wire_sock: Any, can_sock: Any, *, direction: str = "both",
                timeout: float | None = 0.0, timestamp_ns: int = 0) -> int:
    """Forward at most one frame, returning 1 or 0.

    ``wire_sock`` is the fixed-frame QEMU stream; ``can_sock`` is a raw
    SocketCAN-like socket.  ``direction`` may be ``wire-to-can``,
    ``can-to-wire`` or ``both``.  Sockets may be socketpair/mock objects,
    which keeps this function straightforward to test without QEMU or root.
    """
    if direction not in {"both", "wire-to-can", "can-to-wire"}:
        raise ValueError("direction must be both, wire-to-can or can-to-wire")
    state = _bridge_state(wire_sock, can_sock)
    wire_allowed = direction != "can-to-wire"
    can_allowed = direction != "wire-to-can"
    buffered = wire_allowed and len(state.wire_rx) >= WIRE_SIZE

    watch = []
    if wire_allowed:
        watch.append(wire_sock)
    if can_allowed:
        watch.append(can_sock)
    poll_timeout = 0.0 if buffered else timeout
    ready = select.select(watch, [], [], poll_timeout)[0]
    if buffered and wire_sock not in ready:
        ready.append(wire_sock)
    if not ready:
        return 0

    if direction == "both" and wire_sock in ready and can_sock in ready:
        selected = state.next_direction
    elif wire_allowed and wire_sock in ready:
        selected = "wire-to-can"
    else:
        selected = "can-to-wire"

    if selected == "wire-to-can":
        if len(state.wire_rx) < WIRE_SIZE:
            part = wire_sock.recv(WIRE_SIZE * 2)
            if not part:
                raise ConnectionError("wire socket closed")
            state.wire_rx.extend(part)
        frame = _take_wire_frame(state)
        if frame is None:
            state.next_direction = "can-to-wire"
            return 0
        _send_all(can_sock, pack_can_frame(frame))
        state.next_direction = "can-to-wire"
        return 1

    frame = unpack_can_frame(can_sock.recv(_CANFD_STRUCT.size), timestamp_ns)
    _send_all(wire_sock, pack_wire_frame(frame))
    state.next_direction = "wire-to-can"
    return 1


__all__ = ["CanFrame", "SocketCANError", "WIRE_EXTENDED", "WIRE_RTR",
           "WIRE_FD", "WIRE_BRS", "WIRE_SIZE", "dlc_to_length",
           "length_to_dlc", "pack_wire_frame", "unpack_wire_frame",
           "pack_can_frame", "unpack_can_frame", "open_socketcan",
           "bridge_once"]
