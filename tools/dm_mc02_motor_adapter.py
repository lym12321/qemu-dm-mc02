"""Host-side DM-MC02 CAN to plant semantic adapter.

The adapter owns the wire mapping and produces protocol-neutral commands.  It
does not know whether the backend is NullEngine, MuJoCo, Gazebo, or ROS2.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import struct
from typing import Any, Protocol


CAN_FLAG_RTR = 1 << 1
CAN_FLAG_FD = 1 << 2

DM_RESET_COMMAND = b"\xff\xff\xff\xff\xff\xff\xff\xfb"
DM_ENABLE_COMMAND = b"\xff\xff\xff\xff\xff\xff\xff\xfc"
DM_DISABLE_COMMAND = b"\xff\xff\xff\xff\xff\xff\xff\xfd"
DM_DEFAULT_SLAVE_ID = 1
DM_DEFAULT_FEEDBACK_ID = 0x11
DM_DEFAULT_P_MAX = 12.5
DM_DEFAULT_V_MAX = 30.0
DM_DEFAULT_T_MAX = 10.0


@dataclass(frozen=True)
class MotorCommand:
    """A protocol-neutral command delivered to a plant backend."""

    mode: str
    index: int
    value: float = 0.0
    position: float = 0.0
    velocity: float = 0.0
    kp: float = 0.0
    kd: float = 0.0
    torque: float = 0.0
    timestamp_ns: int = 0


@dataclass(frozen=True)
class MotorState:
    index: int
    position: float
    velocity: float
    effort: float
    timestamp_ns: int = 0


class MotorBackend(Protocol):
    """Minimal backend contract shared by Null/MuJoCo/ROS2 engines."""

    def step(self, dt: float) -> tuple[Iterable[float], Iterable[float]]: ...

    def set_motor(self, index: int, value: float) -> None: ...

    def reset_motor(self, index: int) -> None: ...

    def set_motor_enabled(self, index: int, enabled: bool) -> None: ...

    def set_motor_dm(self, index: int, command: dict) -> None: ...

    def motor_feedback(self, index: int) -> tuple[float, float, float]: ...


def _decode_linear(raw: int, bits: int, minimum: float,
                   maximum: float) -> float:
    return raw * (maximum - minimum) / ((1 << bits) - 1) + minimum


def _encode_linear(value: float, bits: int, minimum: float,
                   maximum: float) -> int:
    value = min(max(value, minimum), maximum)
    return int(round((value - minimum) * ((1 << bits) - 1) /
                     (maximum - minimum)))


class DmMotorBusAdapter:
    """Map DM MIT and legacy float CAN commands into backend operations."""

    def __init__(self, motor_count: int, motor_protocol: str = "auto",
                 motor_can_base: int = 0x200,
                 dm_slave_id: int = DM_DEFAULT_SLAVE_ID,
                 dm_feedback_id: int = DM_DEFAULT_FEEDBACK_ID,
                 dm_motor_map: dict[int, tuple[int, int]] | None = None,
                 p_max: float = DM_DEFAULT_P_MAX,
                 v_max: float = DM_DEFAULT_V_MAX,
                 t_max: float = DM_DEFAULT_T_MAX):
        self.motor_count = motor_count
        self.motor_protocol = motor_protocol
        self.motor_can_base = motor_can_base
        self.dm_slave_id = dm_slave_id
        self.dm_feedback_id = dm_feedback_id
        self.dm_motor_map = dm_motor_map or {}
        self.p_max = p_max
        self.v_max = v_max
        self.t_max = t_max

    @classmethod
    def from_namespace(cls, args: Any) -> "DmMotorBusAdapter":
        return cls(
            args.motor_count, args.motor_protocol, args.motor_can_base,
            args.dm_slave_id, args.dm_feedback_id,
            getattr(args, "dm_motor_map", {}), args.dm_p_max,
            args.dm_v_max, args.dm_t_max)

    def motor_ids(self, index: int) -> tuple[int, int]:
        if index in self.dm_motor_map:
            return self.dm_motor_map[index]
        return (self.dm_slave_id + index, self.dm_feedback_id + index)

    def motor_index(self, can_id: int) -> int | None:
        for index, (slave_id, _) in self.dm_motor_map.items():
            if can_id == slave_id:
                return index
        index = can_id - self.dm_slave_id
        return index if 0 <= index < self.motor_count else None

    def _decode_mit(self, data: bytes) -> dict | None:
        if len(data) != 8:
            return None
        p_raw = (data[0] << 8) | data[1]
        v_raw = (data[2] << 4) | (data[3] >> 4)
        kp_raw = ((data[3] & 0x0f) << 8) | data[4]
        kd_raw = (data[5] << 4) | (data[6] >> 4)
        t_raw = ((data[6] & 0x0f) << 8) | data[7]
        return {
            "position": _decode_linear(p_raw, 16, -self.p_max, self.p_max),
            "speed": _decode_linear(v_raw, 12, -self.v_max, self.v_max),
            "kp": _decode_linear(kp_raw, 12, 0.0, 500.0),
            "kd": _decode_linear(kd_raw, 12, 0.0, 5.0),
            "torque": _decode_linear(t_raw, 12, -self.t_max, self.t_max),
        }

    def decode(self, can_id: int, flags: int, data: bytes,
               timestamp_ns: int = 0) -> MotorCommand | None:
        if flags & (CAN_FLAG_RTR | CAN_FLAG_FD):
            return None
        if self.motor_protocol in ("auto", "dm-mit"):
            index = self.motor_index(can_id)
            if index is not None and len(data) == 8:
                if data == DM_RESET_COMMAND:
                    return MotorCommand("dm-reset", index,
                                        timestamp_ns=timestamp_ns)
                if data == DM_ENABLE_COMMAND:
                    return MotorCommand("dm-enable", index,
                                        timestamp_ns=timestamp_ns)
                if data == DM_DISABLE_COMMAND:
                    return MotorCommand("dm-disable", index,
                                        timestamp_ns=timestamp_ns)
                command = self._decode_mit(data)
                if command is not None:
                    return MotorCommand(
                        "dm-control", index,
                        position=command["position"],
                        velocity=command["speed"], kp=command["kp"],
                        kd=command["kd"], torque=command["torque"],
                        timestamp_ns=timestamp_ns)
        if self.motor_protocol in ("auto", "float"):
            index = can_id - self.motor_can_base
            if 0 <= index < self.motor_count and len(data) >= 4:
                value = struct.unpack("<f", data[:4])[0]
                if math.isfinite(value):
                    return MotorCommand("float", index, value=value,
                                        timestamp_ns=timestamp_ns)
        return None

    @staticmethod
    def apply(backend: MotorBackend, command: MotorCommand) -> None:
        if command.mode == "float":
            backend.set_motor(command.index, command.value)
        elif command.mode == "dm-reset":
            backend.reset_motor(command.index)
        elif command.mode == "dm-enable":
            backend.set_motor_enabled(command.index, True)
        elif command.mode == "dm-disable":
            backend.set_motor_enabled(command.index, False)
        elif command.mode == "dm-control":
            backend.set_motor_dm(command.index, {
                "position": command.position,
                "speed": command.velocity,
                "kp": command.kp,
                "kd": command.kd,
                "torque": command.torque,
            })

    @staticmethod
    def state(backend: MotorBackend, index: int,
              timestamp_ns: int = 0) -> MotorState:
        position, velocity, effort = backend.motor_feedback(index)
        return MotorState(index, position, velocity, effort, timestamp_ns)

    def encode_feedback(self, state: MotorState) -> tuple[int, bytes]:
        control_id, feedback_id = self.motor_ids(state.index)
        p_raw = _encode_linear(state.position, 16, -self.p_max, self.p_max)
        v_raw = _encode_linear(state.velocity, 12, -self.v_max, self.v_max)
        t_raw = _encode_linear(state.effort, 12, -self.t_max, self.t_max)
        payload = bytes((
            control_id & 0x0f,
            p_raw >> 8, p_raw & 0xff,
            v_raw >> 4,
            ((v_raw & 0x0f) << 4) | (t_raw >> 8),
            t_raw & 0xff, 25, 25,
        ))
        return feedback_id, payload
