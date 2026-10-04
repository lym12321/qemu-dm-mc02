"""Protocol-neutral socket framing, bounded writes, and host acquisition."""

from .datagram import DatagramTransport, send_datagram
from .socketcan import SocketCANError, open_socketcan
from .stream import FixedRecordStream, LengthPrefixedStream, StreamTransport
from .unix import connect_unix

__all__ = ["DatagramTransport", "FixedRecordStream", "LengthPrefixedStream",
           "SocketCANError", "StreamTransport", "connect_unix",
           "open_socketcan", "send_datagram"]
