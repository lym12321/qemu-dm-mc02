"""Borrowed SocketCAN endpoint; CAN fabric retains delivery scheduling."""
from typing import Protocol
from sim.fabric.can import CanPort, check_can_port, check_can_time
from sim.fabric.frame import CanFrame
from sim.transports.can_codec import unpack_can_frame


class CanDatagramQueue(Protocol):
    def send(self, frame: CanFrame) -> None: ...


class SocketCanEndpoint:
    """Translate native CAN datagrams while borrowing the existing TX queue."""

    def __init__(self, bus: CanPort, tx: CanDatagramQueue,
                 name: str = "socketcan"):
        check_can_port(bus)
        send = getattr(tx, "send", None)
        if not callable(send):
            raise TypeError("SocketCAN TX queue requires callable send")
        self.bus = bus
        self._send = send
        bus.attach(self, name)

    def ingest(self, raw: bytes, current_time_ns: int) -> None:
        check_can_time(current_time_ns)
        self.ingest_frame(unpack_can_frame(raw, current_time_ns))

    def ingest_frame(self, frame: CanFrame) -> None:
        """Enqueue a record already decoded during pure input admission."""
        if not isinstance(frame, CanFrame):
            raise TypeError("SocketCAN ingress requires a CanFrame")
        self.bus.send(self, frame)

    def receive(self, frame: CanFrame) -> None:
        self._send(frame)
