"""Four outgoing payloads from RoboMaster 2026 V1.1.0 (20251217).

Official pp9-10/14-17 define layouts. Buff uses the published 8-byte packed
struct, corroborated by the existing firmware; its final byte is offset7.
The offset6 in the official table conflicts with that struct and total size.
Fields are wire values; HP policy, timing and sequence ownership stay external.
"""

from dataclasses import dataclass, fields
import struct

from .protocol import _uint, encode_frame


@dataclass(frozen=True)
class GameStatus:
    game_type: int
    game_progress: int
    stage_remain_time: int  # Seconds.
    sync_timestamp: int  # Explicit UNIX timestamp; never inferred from plant time.


@dataclass(frozen=True)
class RobotStatus:
    robot_id: int
    robot_level: int
    current_hp: int
    maximum_hp: int
    cooling_per_s: int
    heat_limit: int
    chassis_power_limit: int
    power_outputs: int  # gimbal/chassis/shooter in bits0/1/2.


@dataclass(frozen=True)
class PowerHeat:
    buffer_energy_j: int
    heat_17mm: int
    heat_42mm: int


@dataclass(frozen=True)
class Buff:
    recovery_percent: int
    cooling_per_s: int
    defence_percent: int
    vulnerability_percent: int
    attack_percent: int
    remaining_energy: int  # Explicit protocol bitmap, not a computed percentage.


def _values(message, limits):
    return tuple(_uint(getattr(message, field.name), maximum, field.name)
                 for field, maximum in zip(fields(message), limits, strict=True))


def _payload(message):
    if type(message) is GameStatus:
        kind, phase, remaining, timestamp = _values(message, (5, 5, 0xffff, 0xffffffffffffffff))
        if kind == 0:
            raise ValueError("game_type must be in 1..5")
        command, payload = 0x0001, struct.pack("<BHQ", kind | (phase << 4), remaining, timestamp)
    elif type(message) is RobotStatus:
        values = _values(message, (0xff, 0xff, 0xffff, 0xffff, 0xffff, 0xffff, 0xffff, 7))
        command, payload = 0x0201, struct.pack("<BBHHHHHB", *values)
    elif type(message) is PowerHeat:
        values = _values(message, (0xffff, 0xffff, 0xffff))
        command, payload = 0x0202, struct.pack("<8xHHH", *values)
    elif type(message) is Buff:
        values = _values(message, (0xff, 0xffff, 0xff, 0xff, 0xffff, 0xff))
        command, payload = 0x0204, struct.pack("<BHBBHB", *values)
    else:
        raise TypeError("a supported referee message is required")
    return command, payload


def validate_message(message: GameStatus | RobotStatus | PowerHeat | Buff) -> None:
    """Admit supported wire fields without importing a checksum implementation."""
    _payload(message)


def encode_message(message: GameStatus | RobotStatus | PowerHeat | Buff,
                   sequence: int = 0) -> bytes:
    """Validate and encode one supported payload using the shared envelope."""
    command, payload = _payload(message)
    return encode_frame(command, payload, sequence)
