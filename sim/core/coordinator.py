"""Synchronous host barrier; participants own their execution mechanisms."""

from collections.abc import Mapping

from .participant import Session, SimParticipant, StepContext, StepResult


class Coordinator:
    """Commit time only after every participant reaches the exact target.

    A failed step cannot undo already executed participants. Further steps
    are refused until reset. This is a host barrier, not a QEMU CPU barrier.
    Calls must return; blocking plugins are not preempted by this component.
    """

    def __init__(self, participants: Mapping[str, SimParticipant]):
        self.participants = dict(participants)
        if not self.participants or any(not name for name in self.participants):
            raise ValueError("at least one named participant is required")
        if len({id(p) for p in self.participants.values()}) != len(self.participants):
            raise ValueError("each participant must have a single owner")
        for p in self.participants.values():
            if any(not callable(getattr(p, name, None))
                   for name in ("reset", "step", "close")):
                raise TypeError("participant requires reset, step and close")
        self.session: Session | None = None
        self.t_sim_ns = 0
        self.step_id = 0
        self.failed = True
        self.closed = False

    def reset(self, session: Session) -> None:
        if self.closed:
            raise RuntimeError("coordinator is closed")
        if not isinstance(session, Session):
            raise TypeError("reset requires a Session")
        self.failed = True
        for name, participant in self.participants.items():
            try:
                participant.reset(session)
            except BaseException as exc:
                exc.add_note(f"participant {name} failed during reset")
                raise
        self.session = session
        self.t_sim_ns = session.t_sim_ns
        self.step_id = 0
        self.failed = False

    def step(self, dt_ns: int) -> dict[str, StepResult]:
        if self.closed or self.failed or self.session is None:
            raise RuntimeError("coordinator requires a successful reset")
        if type(dt_ns) is not int or dt_ns <= 0:
            raise ValueError("dt_ns must be a positive integer")
        ctx = StepContext(self.session.session_id, self.step_id + 1,
                          self.t_sim_ns + dt_ns, dt_ns)
        results = {}
        self.failed = True
        for name, participant in self.participants.items():
            try:
                result = participant.step(ctx)
                if (not isinstance(result, StepResult) or result.status != "ok"
                        or type(result.reached_time_ns) is not int
                        or result.reached_time_ns != ctx.t_sim_ns):
                    raise RuntimeError(f"participant {name} did not reach target: {result!r}")
                results[name] = result
            except BaseException as exc:
                exc.add_note(f"participant {name} failed at step {ctx.step_id}")
                raise
        self.t_sim_ns = ctx.t_sim_ns
        self.step_id = ctx.step_id
        self.failed = False
        return results

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        primary = None
        for name, participant in reversed(tuple(self.participants.items())):
            try:
                participant.close()
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    primary.add_note(f"participant {name} close failed: {type(exc).__name__}: {exc}")
        if primary is not None:
            raise primary
