"""Protocol-neutral session and synchronous participant contract."""

from dataclasses import dataclass, field
from typing import Protocol

from .clock import check_time


@dataclass(frozen=True)
class Session:
    session_id: int
    t_sim_ns: int = 0

    def __post_init__(self):
        if type(self.session_id) is not int or self.session_id <= 0:
            raise ValueError("session_id must be a positive integer")
        check_time(self.t_sim_ns, None)


@dataclass(frozen=True)
class StepContext:
    session_id: int
    step_id: int
    t_sim_ns: int
    dt_ns: int

    def __post_init__(self):
        Session(self.session_id, self.t_sim_ns)
        if type(self.step_id) is not int or self.step_id <= 0:
            raise ValueError("step_id must be a positive integer")
        if type(self.dt_ns) is not int or not 0 < self.dt_ns <= self.t_sim_ns:
            raise ValueError("dt_ns must be positive and fit the target time")


@dataclass(frozen=True)
class StepResult:
    status: str
    reached_time_ns: int
    diagnostics: dict[str, object] = field(default_factory=dict)


class SimParticipant(Protocol):
    def reset(self, session: Session) -> None: ...
    def step(self, ctx: StepContext) -> StepResult: ...
    def close(self) -> None: ...
