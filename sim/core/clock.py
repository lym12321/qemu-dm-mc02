"""Integer virtual-time admission shared by host consumers."""


def check_time(t_sim_ns: int, previous_ns: int | None, *, strict=True) -> None:
    if type(t_sim_ns) is not int or t_sim_ns < 0:
        raise ValueError("simulation time must be a nonnegative integer")
    if previous_ns is not None and (t_sim_ns <= previous_ns if strict
                                    else t_sim_ns < previous_ns):
        raise ValueError("simulation time must increase strictly" if strict
                         else "simulation time must not decrease")
