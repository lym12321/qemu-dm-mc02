"""Driver-neutral admission and failure semantics without a native solver."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from sim.api import Session, StepContext, StepResult
from sim.devices.dji import M3508Device, M3508JointDriver
from sim.fabric import CanBus, CanFrame, VirtualCanDevice
from sim.physics.base import JointState
from sim.physics.binding import JointDriveBinding, JointDrivePlan
from sim.physics.joints import JointExecution


class Driver:
    max_abs_effort_nm = 1

    def __init__(self, name, calls, expiry=10, effort=.5):
        self.name, self.calls = name, calls
        self.expiry, self.effort = expiry, effort

    def validate_joint_state(self, state):
        self.calls.append(("sample", self.name, state))

    def reset(self, session, state):
        self.calls.append(("reset-driver", self.name))

    def prepare_step(self, ctx, state):
        self.calls.append(("prepare", self.name, state))
        self.pending = JointDrivePlan(ctx, self.expiry, self.effort)
        return self.pending

    def complete_step(self, plan, state):
        assert plan is self.pending
        self.calls.append(("complete", self.name, state))
        return StepResult("ok", plan.context.t_sim_ns)


def setup():
    calls = []
    physics = SimpleNamespace(
        reset=lambda: calls.append(("reset-physics",)),
        step=lambda dt: calls.append(("step", dt)),
        get_joint_state=lambda _: JointState(0, 0),
        set_joint_effort=lambda name, value: calls.append(("effort", name, value)),
        validate_step=lambda _: None, validate_steps=lambda _: None,
        validate_joint_effort=lambda *_: None,
        close=lambda: calls.append(("close-physics",)))
    drivers = (Driver("a", calls, 3), Driver("b", calls, 7, -.4))
    bindings = tuple(JointDriveBinding(name, driver)
                     for name, driver in zip(("a", "b"), drivers))
    return physics, drivers, bindings, calls


def test_all_prepare_and_target_admission_precede_control_and_completion():
    physics, _, bindings, calls = setup()
    execution = JointExecution(physics, bindings)
    execution.reset(Session(1))
    calls.clear()
    assert execution.step(StepContext(1, 1, 10, 10)).diagnostics == {"physics_segments": 3}
    assert [call for call in calls if call[0] == "step"] == [("step", 3), ("step", 4), ("step", 3)]
    phases = [call[0] for call in calls]
    assert phases[:4] == ["sample", "sample", "prepare", "prepare"]
    assert phases[-4:] == ["sample", "sample", "complete", "complete"]
    assert [call for call in calls if call[0] == "effort"] == [
        ("effort", "a", .5), ("effort", "b", -.4),
        ("effort", "a", 0), ("effort", "b", -.4),
        ("effort", "a", 0), ("effort", "b", 0)]
    execution.close()
    assert ("close-physics",) not in calls


@pytest.mark.parametrize("duplicate", ["joint", "driver"])
def test_duplicate_claims_reject_before_native_admission(duplicate):
    physics, _, bindings, calls = setup()
    second = (replace(bindings[1], joint="a") if duplicate == "joint"
              else replace(bindings[1], driver=bindings[0].driver))
    with pytest.raises(ValueError, match="one"):
        JointExecution(physics, (bindings[0], second))
    assert calls == []


@pytest.mark.parametrize("result", [None, StepResult("failed", 10),
                                    StepResult("ok", 9), StepResult("ok", True)])
def test_invalid_driver_completion_retains_partial_effects_and_freezes(result, monkeypatch):
    physics, drivers, bindings, calls = setup()
    execution = JointExecution(physics, bindings)
    execution.reset(Session(1))
    monkeypatch.setattr(drivers[0], "complete_step", lambda *_: result)
    calls.clear()
    ctx = StepContext(1, 1, 10, 10)
    with pytest.raises(RuntimeError, match="exact target"):
        execution.step(ctx)
    assert execution.failed and execution.t_sim_ns == 0
    assert [call for call in calls if call[0] == "step"] == [("step", 3), ("step", 4), ("step", 3)]
    assert not any(call[0] == "complete" for call in calls)
    with pytest.raises(RuntimeError, match="successful reset"):
        execution.step(ctx)
    execution.reset(Session(2))
    assert not execution.failed


@pytest.mark.parametrize("kind", ["wrong-context", "above-maximum"])
def test_invalid_plan_freezes_before_any_control(kind, monkeypatch):
    physics, drivers, bindings, calls = setup()
    execution = JointExecution(physics, bindings)
    execution.reset(Session(1))
    ctx = StepContext(1, 1, 10, 10)
    plan = JointDrivePlan(replace(ctx, session_id=2) if kind == "wrong-context" else ctx,
                          10, 2 if kind == "above-maximum" else 1)
    monkeypatch.setattr(drivers[1], "prepare_step", lambda *_: plan)
    calls.clear()
    with pytest.raises(ValueError, match="exact step|maximum joint effort"):
        execution.step(ctx)
    assert execution.failed and not any(call[0] in {"step", "effort", "complete"} for call in calls)


@pytest.mark.parametrize("expiry,effort", [(-1, 0), (11, 0), (True, 0), (10, True), (10, float("nan"))])
def test_plan_rejects_invalid_interval_or_effort(expiry, effort):
    with pytest.raises(ValueError):
        JointDrivePlan(StepContext(1, 1, 10, 10), expiry, effort)


def test_m3508_joint_adapter_preserves_original_plan_identity_and_per_adapter_gearing():
    bus = CanBus()
    devices = (M3508Device(bus, 1), M3508Device(bus, 2))
    try:
        drivers = tuple(M3508JointDriver(device, gear_ratio=ratio)
                        for device, ratio in zip(devices, (2, 3)))
        assert [driver.max_abs_effort_nm for driver in drivers] == pytest.approx([.8, 1.2])
        for driver in drivers:
            driver.reset(Session(1), JointState(.25, .5))
        assert [device.get_rotor_state() for device in devices] == [(.5, 1), (.75, 1.5)]
        plan = drivers[0].prepare_step(StepContext(1, 1, 10, 10), JointState(.25, .5))
        with pytest.raises(ValueError, match="original prepared plan"):
            drivers[0].complete_step(replace(plan), JointState(1, 2))
        drivers[0].complete_step(plan, JointState(1, 2))
        assert devices[0].get_rotor_state() == (2, 4)
        with pytest.raises(ValueError, match="original prepared plan"):
            drivers[0].complete_step(plan, JointState(1, 2))
    finally:
        for device in devices:
            device.close()
        bus.close()


def test_all_adapter_conversions_are_admitted_before_any_feedback(monkeypatch):
    physics, _, _, calls = setup()
    bus = CanBus()
    devices = (M3508Device(bus, 1), M3508Device(bus, 2))
    bindings = tuple(JointDriveBinding(name, M3508JointDriver(device, gear_ratio=2))
                     for name, device in zip(("a", "b"), devices))
    try:
        execution = JointExecution(physics, bindings)
        execution.reset(Session(1))
        # Finite native state overflows only in the second adapter's conversion.
        monkeypatch.setattr(physics, "get_joint_state", lambda name:
            JointState(1e308 if name == "b" and any(c[0] == "step" for c in calls) else 0, 0))
        with pytest.raises(ValueError, match="rotor position"):
            execution.step(StepContext(1, 1, 10, 10))
        assert execution.failed and execution.t_sim_ns == 0
        assert [device.t_sim_ns for device in devices] == [0, 0]
        assert bus.pending == 0 and ("step", 10) in calls
    finally:
        for device in devices:
            device.close()
        bus.close()


def test_zero_torque_m3508_still_preserves_its_command_expiry_split():
    physics, _, _, calls = setup()
    bus = CanBus()
    host = VirtualCanDevice(bus, "command")
    device = M3508Device(bus, 1, torque_per_amp=0, command_timeout_ns=3)
    try:
        execution = JointExecution(physics, (
            JointDriveBinding("a", M3508JointDriver(device, gear_ratio=2)),))
        execution.reset(Session(1))
        host.emit(CanFrame(0x200, data=bytes.fromhex("1000 0000 0000 0000")))
        bus.step(0)
        calls.clear()
        execution.step(StepContext(1, 1, 10, 10))
        assert [call for call in calls if call[0] == "step"] == [("step", 3), ("step", 7)]
        assert [call for call in calls if call[0] == "effort"] == [
            ("effort", "a", 0), ("effort", "a", 0)]
        assert device.current_raw == 0
    finally:
        device.close()
        bus.close()
