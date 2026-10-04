"""Literal command payloads from fixed 2026 V1.1.0 official layouts."""

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.devices.referee import decode_frame
from sim.devices.referee.messages import Buff, GameStatus, PowerHeat, RobotStatus, encode_message


# RoboMaster 2026 communication protocol V1.1.0 (20251217), pp9/10/14–17.
# PDF SHA256 c08c66042b3c9129e3a176d343df54cf72538bb11902dde762a504890969d12f.
# Payloads are literal little-endian bytes; no production packer makes an oracle.
GAME = GameStatus(4, 3, 0xabcd, 0xf0e1d2c3b4a59687)
ROBOT = RobotStatus(0x65, 0xe7, 0xffee, 0x0110, 0x2345, 0x6789, 0xabcd, 5)
POWER = PowerHeat(0x1234, 0xabcd, 0xfedc)
BUFF = Buff(0xe1, 0x2345, 0x67, 0x89, 0xabcd, 0xfe)
# Official p16's last offset6 conflicts with p17's packed struct and length8.
# The selected contract uses packed struct plus existing consumer: offset7.
LITERALS = (
    (GAME, 0x0001, "34cdab8796a5b4c3d2e1f0"),
    (ROBOT, 0x0201, "65e7eeff100145238967cdab05"),
    (POWER, 0x0202, "00000000000000003412cdabdcfe"),
    (BUFF, 0x0204, "e145236789cdabfe"),
)


class DerivedGameStatus(GameStatus):
    pass


@pytest.mark.parametrize("message,command,payload_hex", LITERALS)
def test_official_packed_payload_and_command_use_existing_envelope(message, command, payload_hex):
    # decode_frame is the already isolated envelope consumer, not a payload oracle.
    frame = decode_frame(encode_message(message, sequence=0xa5))
    assert frame.command_id == command
    assert frame.sequence == 0xa5
    assert frame.payload == bytes.fromhex(payload_hex)


@pytest.mark.parametrize("message,payload", [
    (GameStatus(1, 0, 0, 0), bytes.fromhex("0100000000000000000000")),
    (GameStatus(5, 5, 0xffff, 0xffffffffffffffff), b"\x55" + b"\xff" * 10),
    (RobotStatus(0, 0, 0, 0, 0, 0, 0, 0), b"\0" * 13),
    (RobotStatus(255, 255, 65535, 65535, 65535, 65535, 65535, 7), b"\xff" * 12 + b"\x07"),
    (PowerHeat(0, 0, 0), b"\0" * 14),
    (PowerHeat(65535, 65535, 65535), b"\0" * 8 + b"\xff" * 6),
    (Buff(0, 0, 0, 0, 0, 0), b"\0" * 8),
    (Buff(255, 65535, 255, 255, 65535, 255), b"\xff" * 8),
])
def test_unsigned_wire_extrema_and_reserved_zero_bytes(message, payload):
    frame = decode_frame(encode_message(message))
    assert frame.sequence == 0
    assert frame.payload == payload


@pytest.mark.parametrize("message,field,value", [
    (GAME, "game_type", 0), (GAME, "game_type", 6),
    (GAME, "game_progress", -1), (GAME, "game_progress", 6),
    (GAME, "stage_remain_time", 65536), (GAME, "sync_timestamp", 1 << 64),
    (ROBOT, "robot_id", 256), (ROBOT, "robot_level", 256),
    (ROBOT, "current_hp", 65536), (ROBOT, "maximum_hp", 65536),
    (ROBOT, "cooling_per_s", 65536), (ROBOT, "heat_limit", 65536),
    (ROBOT, "chassis_power_limit", 65536), (ROBOT, "power_outputs", 8),
    (POWER, "buffer_energy_j", 65536), (POWER, "heat_17mm", 65536), (POWER, "heat_42mm", 65536),
    (BUFF, "recovery_percent", 256), (BUFF, "cooling_per_s", 65536),
    (BUFF, "defence_percent", 256), (BUFF, "vulnerability_percent", 256),
    (BUFF, "attack_percent", 65536), (BUFF, "remaining_energy", 256),
    (ROBOT, "current_hp", -1),
])
def test_each_declared_field_rejects_out_of_range_encoding(message, field, value):
    # Admission belongs to encoding; constructing a value does not invoke policy.
    with pytest.raises(ValueError):
        encode_message(replace(message, **{field: value}))


@pytest.mark.parametrize("message,field,value", [
    (GAME, "game_type", True), (GAME, "sync_timestamp", None),
    (ROBOT, "robot_level", True), (ROBOT, "robot_id", "1"),
    (POWER, "buffer_energy_j", False), (BUFF, "attack_percent", True),
    (BUFF, "cooling_per_s", 1.0),
])
def test_fields_require_exact_integer_types_including_bool_rejection(message, field, value):
    with pytest.raises((ValueError, TypeError)):
        encode_message(replace(message, **{field: value}))


@pytest.mark.parametrize("message", [None, b"opaque", {"game_type": 1},
                                     DerivedGameStatus(1, 0, 0, 0)])
def test_unknown_message_objects_are_rejected(message):
    with pytest.raises(TypeError):
        encode_message(message)


@pytest.mark.parametrize("sequence", [False, 256])
def test_sequence_admission_is_preserved_at_envelope_boundary(sequence):
    with pytest.raises(ValueError):
        encode_message(GAME, sequence=sequence)


@pytest.mark.parametrize("message,field", [(GAME, "game_type"), (ROBOT, "robot_id"),
                                           (POWER, "buffer_energy_j"), (BUFF, "remaining_energy")])
def test_message_values_are_frozen(message, field):
    with pytest.raises(FrozenInstanceError):
        setattr(message, field, 0)
