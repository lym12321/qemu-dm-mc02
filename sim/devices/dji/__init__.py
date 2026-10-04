"""DJI C620/M3508 fixed CAN protocol and simple virtual device."""

from .m3508 import M3508
from .protocol import decode_current_command, encode_feedback

__all__ = ["M3508", "decode_current_command", "encode_feedback"]
