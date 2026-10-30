"""Canonical integer virtual-time and completed-step data contracts."""

from dataclasses import dataclass, field


def check_time(t_sim_ns: int, previous_ns: int | None, *, strict=True) -> None:
    if type(t_sim_ns) is not int or t_sim_ns < 0:
        raise ValueError("simulation time must be a nonnegative integer")
    if previous_ns is not None and (t_sim_ns <= previous_ns if strict
                                    else t_sim_ns < previous_ns):
        raise ValueError("simulation time must increase strictly" if strict
                         else "simulation time must not decrease")


@dataclass(frozen=True)
class Session:
    session_id: int
    t_sim_ns: int = 0

    def __post_init__(self):
        if type(self.session_id) is not int or self.session_id <= 0:
            raise ValueError("session_id must be a positive integer")
        check_time(self.t_sim_ns, None)
        if self.t_sim_ns > 0xFFFFFFFFFFFFFFFF:
            raise ValueError("session time must fit unsigned 64-bit nanoseconds")


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
