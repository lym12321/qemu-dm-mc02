"""Existing fixed84 FDCAN codec over the shared borrowed byte stream."""

import select
import socket
import time

from .can_codec import CanFrame, WIRE_SIZE as CAN_WIRE_SIZE, pack_wire_frame, unpack_wire_frame
from .cosim import MAX_PENDING_FRAMES
from .stream import FixedRecordStream


class CanStream(FixedRecordStream):
    """FDCAN codec over the shared bounded fixed-record transport."""

    def __init__(self, sock: socket.socket):
        super().__init__(
            sock, record_size=CAN_WIRE_SIZE,
            max_pending=MAX_PENDING_FRAMES, queue_name="FDCAN",
            endpoint_name="QEMU FDCAN", clock=time, poller=select)

    def receive(self) -> list[tuple[int, int, int, int, bytes]]:
        frames = []
        for body in self.receive_records():
            frame = unpack_wire_frame(body)
            frames.append((frame.can_id, frame.flags, frame.dlc,
                           frame.timestamp_ns, frame.data))
        return frames

    def send(self, can_id: int, flags: int, dlc: int, timestamp_ns: int,
             data: bytes) -> None:
        """Send one fixed-width frame to the QEMU FDCAN chardev."""
        self._check_tx_capacity()
        payload = pack_wire_frame(CanFrame(
            can_id, flags, dlc, data, timestamp_ns))
        self._enqueue(payload)
