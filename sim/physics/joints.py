"""One native advance owner for heterogeneous joint-space driver plans.

This is the shared execution algorithm, moved from the M3508 joint plant.
Physics and drivers remain borrowed; their resource owners close them.
"""

import math

from sim.core import Session, StepContext, StepResult

from .base import JointPhysics, JointState
from .binding import JointDriveBinding, JointDrivePlan


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


class JointExecution:
    """Prepare all, advance one world over union expiries, then complete all.

    Admission precedes reset/control. A failed solver, getter or completion can
    leave partial effects; this owner freezes and never replays those effects.
    Session integer time remains separate from the backend's native epoch.
    """

    def __init__(self, physics: JointPhysics, bindings: tuple[JointDriveBinding, ...]):
        items = tuple(bindings)
        if not items or any(not isinstance(item, JointDriveBinding) for item in items):
            raise ValueError("joint execution requires nonempty JointDriveBinding values")
        if len({item.joint for item in items}) != len(items):
            raise ValueError("each joint must have one driver")
        if len({id(item.driver) for item in items}) != len(items):
            raise ValueError("each driver must have one joint binding")
        for method in ("reset", "step", "set_joint_effort", "get_joint_state",
                       "validate_step", "validate_steps", "validate_joint_effort", "close"):
            if not callable(getattr(physics, method, None)):
                raise TypeError(f"joint physics requires {method}")
        maxima = []
        for binding in items:
            driver = binding.driver
            for method in ("validate_joint_state", "reset", "prepare_step", "complete_step"):
                if not callable(getattr(driver, method, None)):
                    raise TypeError(f"joint driver requires {method}")
            maximum = _finite(driver.max_abs_effort_nm, "maximum joint effort")
            if maximum < 0:
                raise ValueError("maximum joint effort must be nonnegative")
            maxima.append(maximum)
        self.physics = physics
        self._bindings = items
        self._maxima = tuple(maxima)
        for binding, maximum in zip(items, maxima):
            for effort in (maximum, -maximum, 0.0):
                physics.validate_joint_effort(binding.joint, effort)
        self._samples()
        self.t_sim_ns = 0
        self._session_id = None
        self._step_id = 0
        self.failed = True
        self.closed = False

    def _samples(self):
        samples = []
        for binding in self._bindings:
            state = self.physics.get_joint_state(binding.joint)
            if not isinstance(state, JointState):
                raise TypeError("joint physics must return JointState")
            _finite(state.position_rad, "joint position")
            _finite(state.velocity_rad_s, "joint velocity")
            binding.driver.validate_joint_state(state)
            samples.append(state)
        return tuple(samples)

    def reset(self, session: Session):
        if self.closed:
            raise RuntimeError("joint execution is closed")
        if not isinstance(session, Session) or session.t_sim_ns > 0xFFFFFFFFFFFFFFFF:
            raise ValueError("joint reset requires a Session with unsigned64 time")
        self.failed = True
        self.physics.reset()
        samples = self._samples()
        for binding, state in zip(self._bindings, samples):
            binding.driver.reset(session, state)
        self._session_id = session.session_id
        self.t_sim_ns = session.t_sim_ns
        self._step_id = 0
        self.failed = False

    def step(self, ctx: StepContext) -> StepResult:
        if self.closed or self.failed or self._session_id is None:
            raise RuntimeError("joint execution requires a successful reset")
        if (not isinstance(ctx, StepContext) or ctx.session_id != self._session_id
                or ctx.step_id != self._step_id + 1
                or ctx.t_sim_ns != self.t_sim_ns + ctx.dt_ns
                or ctx.t_sim_ns > 0xFFFFFFFFFFFFFFFF
                or ctx.dt_ns > 0x7FFFFFFFFFFFFFFF):
            raise ValueError("joint step must follow its session and exact target")
        self.failed = True
        start_samples = self._samples()
        plans = tuple(binding.driver.prepare_step(ctx, state)
                      for binding, state in zip(self._bindings, start_samples))
        for plan, maximum in zip(plans, self._maxima):
            if not isinstance(plan, JointDrivePlan) or plan.context != ctx:
                raise ValueError("joint driver must prepare a plan for the exact step")
            JointDrivePlan.__post_init__(plan)
            if abs(plan.effort_nm) > maximum:
                raise ValueError("joint plan exceeds admitted maximum joint effort")
        boundaries = sorted({self.t_sim_ns, ctx.t_sim_ns,
                             *(plan.active_until_ns for plan in plans)})
        self.physics.validate_step(ctx.dt_ns)
        self.physics.validate_steps(tuple(target - start
            for start, target in zip(boundaries, boundaries[1:])))
        for binding, plan in zip(self._bindings, plans):
            self.physics.validate_joint_effort(binding.joint, plan.effort_nm)
            self.physics.validate_joint_effort(binding.joint, 0.0)
        for start, target in zip(boundaries, boundaries[1:]):
            for binding, plan in zip(self._bindings, plans):
                self.physics.set_joint_effort(binding.joint,
                    plan.effort_nm if start < plan.active_until_ns else 0.0)
            self.physics.step(target - start)
        # Every native and adapter-specific sample check precedes feedback.
        samples = self._samples()
        for binding, plan, state in zip(self._bindings, plans, samples):
            result = binding.driver.complete_step(plan, state)
            if (not isinstance(result, StepResult) or result.status != "ok"
                    or type(result.reached_time_ns) is not int
                    or result.reached_time_ns != ctx.t_sim_ns):
                raise RuntimeError("joint driver completion must reach the exact target")
        self.t_sim_ns = ctx.t_sim_ns
        self._step_id = ctx.step_id
        self.failed = False
        return StepResult("ok", self.t_sim_ns,
                          {"physics_segments": len(boundaries) - 1})

    def close(self):
        self.closed = True
        self.failed = True
