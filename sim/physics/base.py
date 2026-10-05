"""Named SI joint boundary shared by native physics backends."""

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
