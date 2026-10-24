"""Joint-space SI plans borrowed by a world's sole execution owner.

Drivers own commands, expiry and feedback. They never receive the native
physics object. Bindings contain live references, not Event payload snapshots.
"""

from dataclasses import dataclass
import math
from typing import Protocol

from sim.core import Session, StepContext, StepResult

from .base import JointState


@dataclass(frozen=True)
class JointDrivePlan:
    """Hold one joint torque until expiry, then zero for the remaining interval."""

    context: StepContext
    active_until_ns: int
    effort_nm: float

    def __post_init__(self):
        if not isinstance(self.context, StepContext):
            raise TypeError("joint plan requires StepContext")
        start = self.context.t_sim_ns - self.context.dt_ns
        if (type(self.active_until_ns) is not int
                or not start <= self.active_until_ns <= self.context.t_sim_ns):
            raise ValueError("joint plan expiry must fit its step interval")
        try:
            finite = type(self.effort_nm) in (int, float) and math.isfinite(self.effort_nm)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError("joint plan effort must be finite")


class JointDriver(Protocol):
    """Borrowed adapter, with no solver, clock or native resource ownership.

    max_abs_effort_nm is finite/nonnegative and admitted before effects.
    validate_joint_state is pure, including adapter-specific unit conversions.
    Completion accepts only the exact original plan and reports the exact target;
    any failure freezes its world until a successful reset, without replay.
    """

    @property
    def max_abs_effort_nm(self) -> float: ...
    def validate_joint_state(self, state: JointState) -> None: ...
    def reset(self, session: Session, initial_state: JointState) -> None: ...
    def prepare_step(self, context: StepContext, start_state: JointState) -> JointDrivePlan: ...
    def complete_step(self, plan: JointDrivePlan, target_state: JointState) -> StepResult: ...


@dataclass(frozen=True)
class JointDriveBinding:
    joint: str
    driver: JointDriver

    def __post_init__(self):
        if not isinstance(self.joint, str) or not self.joint or "\0" in self.joint:
            raise ValueError("joint binding name must be nonempty without NUL")
        if self.driver is None:
            raise ValueError("joint binding requires a borrowed driver")
