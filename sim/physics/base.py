"""Named SI observations and the shared native joint execution boundary."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class JointState:
    position_rad: float
    velocity_rad_s: float


@dataclass(frozen=True)
class BodyPose:
    position_m: tuple[float, float, float]
    quaternion_wxyz: tuple[float, float, float, float]


@dataclass(frozen=True)
class ImuSample:
    gyro_rad_s: tuple[float, float, float]
    accel_m_s2: tuple[float, float, float]


@dataclass(frozen=True)
class CameraFrame:
    """Latest native RGB8 image; timestamp is the sensor producer's time."""

    timestamp_ns: int
    width: int
    height: int
    step: int
    frame_id: str
    data: bytes


@dataclass(frozen=True)
class LidarScan:
    """Latest native single-row scan; preserve infinity misses and NaN vertical step."""

    timestamp_ns: int
    frame_id: str
    angle_min_rad: float
    angle_max_rad: float
    angle_step_rad: float
    range_min_m: float
    range_max_m: float
    vertical_angle_min_rad: float
    vertical_angle_max_rad: float
    vertical_angle_step_rad: float
    ranges_m: tuple[float, ...]
    intensities: tuple[float, ...]


class JointPhysics(Protocol):
    """Exclusive execution owner with pure, current-state admission checks.

    Validators reject unsupported input without changing commands, time or
    solver state. They do not reserve a transaction or guarantee solver success.
    A consumer must also validate each interval of a split operation.
    """

    def reset(self) -> None: ...
    def step(self, dt_ns: int) -> None: ...
    def validate_step(self, dt_ns: int) -> None: ...
    def validate_steps(self, intervals_ns: tuple[int, ...]) -> None: ...
    def validate_joint_effort(self, name: str, value: float) -> None: ...
    def set_joint_effort(self, name: str, value: float) -> None: ...
    def get_joint_state(self, name: str) -> JointState: ...
    def close(self) -> None: ...
