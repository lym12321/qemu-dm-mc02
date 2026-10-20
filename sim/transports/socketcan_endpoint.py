"""Borrowed SocketCAN endpoint; CAN fabric retains delivery scheduling."""
from typing import Protocol
from sim.fabric.can import CanBus, check_can_time
from sim.fabric.frame import CanFrame
from sim.transports.can_codec import unpack_can_frame


class CanDatagramQueue(Protocol):
    def send(self, frame: CanFrame) -> None: ...


class SocketCanEndpoint:
    """Translate native CAN datagrams while borrowing the existing TX queue."""

    def __init__(self, bus: CanBus, tx: CanDatagramQueue,
                 name: str = "socketcan"):
        if not isinstance(bus, CanBus):
            raise TypeError("SocketCAN endpoint requires a CanBus")
        send = getattr(tx, "send", None)
        if not callable(send):
            raise TypeError("SocketCAN TX queue requires callable send")
        self.bus = bus
        self._send = send
        bus.attach(self, name)

    def ingest(self, raw: bytes, current_time_ns: int) -> None:
        check_can_time(current_time_ns)
        self.bus.send(self, unpack_can_frame(raw, current_time_ns))

    def receive(self, frame: CanFrame) -> None:
        self._send(frame)
