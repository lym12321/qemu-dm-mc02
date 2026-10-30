"""Adapt one native physics execution owner to the host participant contract."""

import math

from sim.api import Session, StepContext, StepResult


class PhysicsParticipant:
    """Own reset/advance/close; expose the borrowed physics for control and reads.

    Construction transfers ownership only on success. The caller must not step
    or reset the physics directly, or give it to another execution owner.
    Calls require one synchronous Simulation owner.
    Native time and sensor stamps keep their own epoch and precision; the host
    target is an integer session time, not a sensor delivery or CPU barrier.
    """

    def __init__(self, physics):
        for name in ("reset", "validate_step", "step", "close"):
            if not callable(getattr(physics, name, None)):
                raise TypeError(f"physics participant requires {name}")
        self.physics = physics
        self._native_time_s()
        self._session = None
        self._step_id = 0
        self.t_sim_ns = 0
        self.failed = True
        self.closed = False

    def _native_time_s(self):
        value = self.physics.time_s
        if not isinstance(value, float) or not math.isfinite(value) or value < 0:
            raise RuntimeError("physics time_s must be finite nonnegative floating seconds")
        return value

    def reset(self, session: Session) -> None:
        if self.closed:
            raise RuntimeError("physics participant is closed")
        if not isinstance(session, Session):
            raise TypeError("physics reset requires a Session")
        self.failed = True
        self.physics.reset()
        if self._native_time_s() != 0:
            raise RuntimeError("physics reset did not restore native time zero")
        self._session = session
        self._step_id = 0
        self.t_sim_ns = session.t_sim_ns
        self.failed = False

    def step(self, ctx: StepContext) -> StepResult:
        if self.closed or self.failed or self._session is None:
            raise RuntimeError("physics participant requires a successful reset")
        if (not isinstance(ctx, StepContext) or ctx.session_id != self._session.session_id
                or ctx.step_id != self._step_id + 1
                or ctx.t_sim_ns != self.t_sim_ns + ctx.dt_ns):
            raise ValueError("physics step must follow its session and exact target")
        self.failed = True
        self.physics.validate_step(ctx.dt_ns)
        self.physics.step(ctx.dt_ns)
        # Native execution/preflight owns time advancement. Do not round-trip
        # seconds to ns or reject integer ticks whose double projection aliases.
        native_time = self._native_time_s()
        self.t_sim_ns = ctx.t_sim_ns
        self._step_id = ctx.step_id
        self.failed = False
        return StepResult("ok", ctx.t_sim_ns, {"native_time_s": native_time})

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.physics.close()
