"""Referee snapshot output over the existing borrowed byte transport.

This endpoint has no socket, UART, receive parser, clock or engine ownership.
Reset preserves physical queued bytes, including a partially sent frame.
"""

from .engine import RefereeState
from .messages import Buff, GameStatus, PowerHeat, RobotStatus, encode_message
from .protocol import _uint


class RefereeUartEndpoint:
    """Queue four complete frames as one bounded transport item.

    Capacity refusal admits no new bytes and permits retry. Other IO failures
    may follow partial output, so freeze until explicit logical reset. Success
    means transport admission, not firmware receipt; the caller owns draining.
    """

    def __init__(self, transport, *, sequence: int = 0):
        send = getattr(transport, "send", None)
        if not callable(send):
            raise TypeError("referee UART requires a borrowed send(bytes) endpoint")
        self._next_sequence = _uint(sequence, 255, "sequence")
        self._transport = transport
        self._send = send
        self._failed = False
        self._closed = False

    @property
    def next_sequence(self):
        return self._next_sequence

    @property
    def transport(self):
        """Borrow the caller's byte transport without taking its ownership."""
        return self._transport

    @property
    def closed(self):
        return self._closed

    def _ready(self):
        if self._closed or self._failed:
            raise RuntimeError("referee endpoint is closed or requires reset")

    def send_state(self, state: RefereeState) -> bytes:
        self._ready()
        if type(state) is not RefereeState:
            raise TypeError("referee output requires a RefereeState")
        frames = []
        for offset, (message, expected) in enumerate((
                (state.game, GameStatus), (state.robot, RobotStatus),
                (state.power_heat, PowerHeat), (state.buff, Buff))):
            if type(message) is not expected:
                raise TypeError(f"referee state requires {expected.__name__}")
            frames.append(encode_message(message, (self._next_sequence + offset) % 256))
        wire = b"".join(frames)
        try:
            self._send(wire)
            self._next_sequence = (self._next_sequence + 4) % 256
        except BufferError:
            # Existing transport rejects capacity before enqueueing new bytes.
            raise
        except BaseException:
            self._failed = True
            raise
        return wire

    def reset(self, sequence: int = 0) -> None:
        if self._closed:
            raise RuntimeError("referee endpoint is closed")
        self._next_sequence = _uint(sequence, 255, "sequence")
        self._failed = False

    def close(self) -> None:
        self._closed = True
