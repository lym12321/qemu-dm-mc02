"""RoboMaster referee protocol components, independent of boards and worlds."""

from .protocol import Frame, decode_frame, encode_frame

__all__ = ["Frame", "decode_frame", "encode_frame"]
