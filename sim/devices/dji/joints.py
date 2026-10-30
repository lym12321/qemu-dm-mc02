"""C620 joint adapter and owning composition over the shared world executor.

The ideal ratio3591/187 comes from DJI M3508 P19 manual V1.0, p7/p13/p19.
Only this adapter converts rotor torque and observations to joint-space SI.
Efficiency, backlash and rotor inertia remain uncalibrated approximations.
The owned plant closes its devices and physics independently; buses are borrowed.
"""

from collections.abc import Mapping
import math

from sim.api import Session, StepContext, StepResult
from sim.physics.base import JointPhysics, JointState
from sim.physics.binding import JointDriveBinding, JointDrivePlan
from sim.physics.joints import JointExecution

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


class M3508JointDriver:
    """Borrow one protocol device; own gearing and its original pending plan."""

    def __init__(self, device: M3508Device, *, gear_ratio=3591 / 187):
        if type(device) is not M3508Device:
            raise TypeError("joint bindings require protocol-only M3508Device")
        if device.closed:
            raise ValueError("joint devices must be open")
        ratio = _finite(gear_ratio, "gear_ratio")
        if ratio <= 0:
            raise ValueError("gear_ratio must be positive")
        self.max_abs_effort_nm = _finite(20 * device.torque_per_amp * ratio,
                                        "maximum joint effort")
        self.device = device
        self.gear_ratio = ratio
        self._pending = None

    def _rotor_sample(self, state):
        if not isinstance(state, JointState):
            raise TypeError("joint driver requires JointState")
        position = _finite(state.position_rad, "joint position")
        velocity = _finite(state.velocity_rad_s, "joint velocity")
        return (_finite(position * self.gear_ratio, "rotor position"),
                _finite(velocity * self.gear_ratio, "rotor velocity"))

    def validate_joint_state(self, state: JointState):
        self._rotor_sample(state)

    def reset(self, session: Session, initial_state: JointState):
        position, velocity = self._rotor_sample(initial_state)
        self._pending = None
        self.device.reset(session, position_rad=position, velocity_rad_s=velocity)

    def prepare_step(self, context: StepContext, start_state: JointState) -> JointDrivePlan:
        if self._pending is not None:
            raise RuntimeError("M3508 joint driver already has a prepared step")
        self.validate_joint_state(start_state)
        raw = self.device.prepare_step(context)
        plan = JointDrivePlan(context, raw.active_until_ns,
                              _finite(raw.effort_nm * self.gear_ratio, "joint effort"))
        self._pending = plan, raw
        return plan

    def complete_step(self, plan: JointDrivePlan, target_state: JointState) -> StepResult:
        if self._pending is None or plan is not self._pending[0]:
            raise ValueError("M3508 joint completion requires its original prepared plan")
        position, velocity = self._rotor_sample(target_state)
        raw = self._pending[1]
        # A protocol completion may commit before emission fails. Never replay.
        self._pending = None
        return self.device.complete_step(raw, position, velocity)


class M3508JointPlant(JointExecution):
    """Own native physics and M3508 devices after complete joint admission."""

    def __init__(self, physics: JointPhysics,
                 bindings: Mapping[M3508Device, str], *, gear_ratio=3591 / 187):
        if not isinstance(bindings, Mapping) or not bindings:
            raise ValueError("joint plant requires nonempty device-to-joint bindings")
        items = tuple(bindings.items())
        drivers = tuple(M3508JointDriver(device, gear_ratio=gear_ratio) for device, _ in items)
        if len({(id(device.bus), device.motor_id) for device, _ in items}) != len(items):
            raise ValueError("joint devices require unique motor IDs on each CAN bus")
        super().__init__(physics, tuple(JointDriveBinding(name, driver)
                                      for (_, name), driver in zip(items, drivers)))
        self.devices = tuple(device for device, _ in items)

    def close(self):
        if self.closed:
            return
        super().close()
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
