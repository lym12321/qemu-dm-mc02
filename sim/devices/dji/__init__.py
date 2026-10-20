"""DJI C620/M3508 fixed CAN protocol and simple virtual device."""

from .device import DrivePlan, M3508Device
from .protocol import decode_current_command, encode_feedback

__all__ = ["M3508", "M3508Device", "DrivePlan", "M3508JointPlant",
           "decode_current_command", "encode_feedback"]


def __getattr__(name):
    # Importing the protocol-only device must not acquire a physics dependency.
    if name == "M3508":
        from .m3508 import M3508
        return M3508
    if name == "M3508JointPlant":
        from .joints import M3508JointPlant
        return M3508JointPlant
    raise AttributeError(name)
