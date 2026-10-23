"""Borrowed driver registration for one native world's execution owner.

These records contain live adapter references, not Event payload snapshots.
The world owns physics, each driver node owns its driver, and the built
executor exclusively controls reset/advance without owning either resource.
"""

from dataclasses import dataclass
from typing import Protocol

from sim.core import Session, StepContext, StepResult

from .base import JointPhysics


class JointExecution(Protocol):
    def reset(self, session: Session) -> None: ...
    def step(self, context: StepContext) -> StepResult: ...
    def close(self) -> None: ...


class JointDriveBuilder(Protocol):
    def build(self, physics: JointPhysics,
              bindings: tuple["JointDriveBinding", ...]) -> JointExecution: ...


@dataclass(frozen=True)
class JointDriveBinding:
    joint: str
    driver: object
    builder: JointDriveBuilder

    def __post_init__(self):
        if not isinstance(self.joint, str) or not self.joint or "\0" in self.joint:
            raise ValueError("joint binding name must be nonempty without NUL")
        if self.driver is None:
            raise ValueError("joint binding requires a borrowed driver")
        if not callable(getattr(self.builder, "build", None)):
            raise TypeError("joint binding builder requires build")
