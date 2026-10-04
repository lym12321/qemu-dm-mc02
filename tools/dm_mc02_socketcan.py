#!/usr/bin/env python3
"""Optional host-side bridge for the DM-MC02 FDCAN chardev.

The QEMU side uses a deliberately fixed wire format, while Linux SocketCAN
uses variable-size ``struct can_frame``/``struct canfd_frame`` records.  This
module keeps those details isolated and has no QEMU dependency.
"""

from __future__ import annotations

import platform
from pathlib import Path
import select
import socket
import sys
import weakref
from dataclasses import dataclass
from typing import Any

# Direct tools execution and importlib consumers resolve the same package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.transports.socketcan import (
    SocketCANError, open_socketcan as transport_open_socketcan,
    require_socketcan,
)

from sim.transports.can_codec import (
    CanFrame, WIRE_SIZE, WIRE_HEADER_SIZE, MAX_DATA_LEN,
    WIRE_EXTENDED, WIRE_RTR, WIRE_FD, WIRE_BRS, WIRE_FLAGS,
    CAN_SFF_MASK, CAN_EFF_MASK, CAN_EFF_FLAG, CAN_RTR_FLAG, CAN_ERR_FLAG,
    CANFD_BRS, CANFD_ESI, CANFD_FDF,
    _CAN_STRUCT, _CANFD_STRUCT, _WIRE_STRUCT, _FD_DLC_LENGTHS,
    dlc_to_length, length_to_dlc, pack_wire_frame, unpack_wire_frame,
    pack_can_frame, unpack_can_frame,
)


def _require_socketcan() -> None:
    require_socketcan(socket_module=socket, platform_module=platform)


def open_socketcan(interface: str, *, enable_fd: bool = True) -> socket.socket:
    """Open and bind a raw SocketCAN interface, with clear host errors."""
    return transport_open_socketcan(
        interface, enable_fd=enable_fd, socket_module=socket,
        platform_module=platform)


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
