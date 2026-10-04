"""C620 CAN subset from DJI's publisher V1.01 manual, PDF pp14–17/31–33.

Canonical PDF SHA256 f422c7a604bc5003fde4ddd3bc1329501a39bb03120020b5ac95587f0a70f71b
(cover 2017.09 V1.0). Command values +/-16384 represent +/-20 A. Signed
feedback is corroborated by RoboRTS 8cffd936b503eef819fa05265cf0162710c9cb3b;
feedback current's 20/16384 A scale is the trobot consumer convention. No
enable/disable opcode is added. This module contains no physical plant.
"""

import struct

from sim.fabric.frame import CanFrame


def validate_motor_id(motor_id: int) -> None:
    if type(motor_id) is not int or not 1 <= motor_id <= 8:
        raise ValueError("M3508 motor_id must be an integer from 1 to 8")


def decode_current_command(frame: CanFrame, motor_id: int) -> int | None:
    """Return this ID's raw current, None for another group, or reject the frame.

    A relevant group must have all four slots in the documented range. Rejecting
    the whole command avoids partially accepting a malformed group broadcast.
    """
    validate_motor_id(motor_id)
    group = 0x200 if motor_id <= 4 else 0x1FF
    if frame.can_id != group:
        return None
    if frame.flags != 0 or frame.dlc != 8 or len(frame.data) != 8:
        raise ValueError("C620 commands require standard DATA DLC8 with eight bytes")
    slots = struct.unpack(">hhhh", frame.data)
    if any(not -16384 <= value <= 16384 for value in slots):
        raise ValueError("C620 current command must be -16384..16384")
    return slots[(motor_id - 1) % 4]


def encode_feedback(motor_id: int, *, encoder: int, speed_rpm: int,
                    current_raw: int, temperature_c: int = 25,
                    timestamp_ns: int = 0) -> CanFrame:
    """Encode explicit rotor wire fields; Null byte is zero by model convention."""
    validate_motor_id(motor_id)
    if type(timestamp_ns) is not int or not 0 <= timestamp_ns <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("timestamp_ns must be an unsigned64 integer")
    for name, value, minimum, maximum in (
            ("encoder", encoder, 0, 8191),
            ("speed_rpm", speed_rpm, -32768, 32767),
            ("current_raw", current_raw, -32768, 32767),
            ("temperature_c", temperature_c, 0, 255)):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    payload = struct.pack(">HhhBB", encoder, speed_rpm, current_raw,
                          temperature_c, 0)
    return CanFrame(0x200 + motor_id, data=payload, timestamp_ns=timestamp_ns)
