"""One native physics advance owner for M3508 devices bound to shared joints.

The ratio3591/187 is from DJI M3508 P19 manual V1.0, p7/p13/p19, also used by
the fixed reference firmware. Lossless conversion is a model approximation,
not a C620 wire field or a calibrated gearbox. The XML actuators stay unit gear.
Construction borrows inputs for admission; successful construction transfers
exclusive execution ownership of the physics and devices. The bus is borrowed.
"""

from collections.abc import Mapping
import math

from sim.core import Session, StepContext, StepResult
from sim.physics.base import JointPhysics, JointState

from .device import M3508Device


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


class M3508JointPlant:
    def __init__(self, physics: JointPhysics,
                 bindings: Mapping[M3508Device, str], *, gear_ratio=3591 / 187):
        if not isinstance(bindings, Mapping) or not bindings:
            raise ValueError("joint plant requires nonempty device-to-joint bindings")
        gear_ratio = _finite(gear_ratio, "gear_ratio")
        if gear_ratio <= 0:
            raise ValueError("gear_ratio must be positive")
        items = tuple(bindings.items())
        if any(type(device) is not M3508Device for device, _ in items):
            raise TypeError("joint bindings require protocol-only M3508Device")
        if any(not isinstance(name, str) or not name for _, name in items):
            raise ValueError("joint names must be nonempty strings")
        if len({name for _, name in items}) != len(items):
            raise ValueError("each joint must have one M3508 owner")
        bus = items[0][0].bus
        if any(device.bus is not bus for device, _ in items):
            raise ValueError("joint devices must share one CAN bus")
        if len({device.motor_id for device, _ in items}) != len(items):
            raise ValueError("joint devices require unique motor IDs")
        if any(device.closed for device, _ in items):
            raise ValueError("joint devices must be open")
        for method in ("reset", "step", "set_joint_effort", "get_joint_state",
                       "validate_step", "validate_steps", "validate_joint_effort", "close"):
            if not callable(getattr(physics, method, None)):
                raise TypeError(f"joint physics requires {method}")
        self.physics = physics
        self.gear_ratio = gear_ratio
        self._bindings = items
        # Getters have no solver/time/control side effects. Validate every
        # binding and maximal effort before ownership and runtime projection.
        for device, name in items:
            maximum = _finite(20 * device.torque_per_amp * gear_ratio,
                              "maximum joint effort")
            physics.validate_joint_effort(name, maximum)
            physics.validate_joint_effort(name, -maximum)
            self._rotor_sample(name)
        self.devices = tuple(device for device, _ in items)
        self.t_sim_ns = 0
        self._session_id = None
        self._step_id = 0
        self.failed = True
        self.closed = False

    def _rotor_sample(self, name):
        state = self.physics.get_joint_state(name)
        if not isinstance(state, JointState):
            raise TypeError("joint physics must return JointState")
        position = _finite(state.position_rad, "joint position")
        velocity = _finite(state.velocity_rad_s, "joint velocity")
        return (_finite(position * self.gear_ratio, "rotor position"),
                _finite(velocity * self.gear_ratio, "rotor velocity"))

    def reset(self, session: Session):
        if self.closed:
            raise RuntimeError("joint plant is closed")
        if not isinstance(session, Session) or session.t_sim_ns > 0xFFFFFFFFFFFFFFFF:
            raise ValueError("joint reset requires a Session with unsigned64 time")
        self.failed = True
        self.physics.reset()
        samples = tuple(self._rotor_sample(name) for _, name in self._bindings)
        for device, (position, velocity) in zip(self.devices, samples):
            device.reset(session, position_rad=position, velocity_rad_s=velocity)
        self._session_id = session.session_id
        self.t_sim_ns = session.t_sim_ns
        self._step_id = 0
        self.failed = False

    def step(self, ctx: StepContext) -> StepResult:
        if self.closed or self.failed or self._session_id is None:
            raise RuntimeError("joint plant requires a successful reset")
        if (not isinstance(ctx, StepContext) or ctx.session_id != self._session_id
                or ctx.step_id != self._step_id + 1
                or ctx.t_sim_ns != self.t_sim_ns + ctx.dt_ns
                or ctx.t_sim_ns > 0xFFFFFFFFFFFFFFFF
                or ctx.dt_ns > 0x7FFFFFFFFFFFFFFF):
            raise ValueError("joint step must follow its session and exact target")
        self.failed = True
        plans = tuple(device.prepare_step(ctx) for device in self.devices)
        boundaries = sorted({self.t_sim_ns, ctx.t_sim_ns,
                             *(plan.active_until_ns for plan in plans)})
        # The whole operation must fit the backend's single-step admission,
        # including cumulative native target range. Check every actual expiry
        # interval too; a valid total does not imply native-tick aligned splits.
        self.physics.validate_step(ctx.dt_ns)
        self.physics.validate_steps(tuple(target - start
            for start, target in zip(boundaries, boundaries[1:])))
        efforts = tuple(_finite(plan.effort_nm * self.gear_ratio, "joint effort")
                        for plan in plans)
        for (_, name), effort in zip(self._bindings, efforts):
            self.physics.validate_joint_effort(name, effort)
            self.physics.validate_joint_effort(name, 0)
        for start, target in zip(boundaries, boundaries[1:]):
            for (_, name), plan, effort in zip(self._bindings, plans, efforts):
                self.physics.set_joint_effort(
                    name, effort if start < plan.active_until_ns else 0.0)
            self.physics.step(target - start)
        # Collect and validate all target observations before any feedback
        # commit. Solver/sample/emit failures retain partial physical effects;
        # this owner stays frozen and never repeats a prepared step.
        samples = tuple(self._rotor_sample(name) for _, name in self._bindings)
        for device, plan, (position, velocity) in zip(self.devices, plans, samples):
            device.complete_step(plan, position, velocity)
        self.t_sim_ns = ctx.t_sim_ns
        self._step_id = ctx.step_id
        self.failed = False
        return StepResult("ok", self.t_sim_ns,
                          {"physics_segments": len(boundaries) - 1})

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.failed = True
        primary = None
        for device in reversed(self.devices):
            try:
                device.close()
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    primary.add_note(f"M3508 {device.motor_id} close failed: {exc}")
        try:
            self.physics.close()
        except BaseException as exc:
            if primary is None:
                primary = exc
            else:
                primary.add_note(f"joint physics close failed: {exc}")
        if primary is not None:
            raise primary
