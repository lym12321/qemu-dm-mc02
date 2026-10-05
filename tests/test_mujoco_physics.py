"""Independent mechanics and admission oracles for the native MuJoCo adapter.

These fixtures use Newton/Euler equations, rigid-body site acceleration and
proper acceleration. They do not use the shared rotor helper as an oracle and
do not establish motor calibration, collision fidelity or a CPU time barrier.
"""

from dataclasses import FrozenInstanceError
import math
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
mujoco = pytest.importorskip("mujoco", reason="optional native MuJoCo runtime")
from sim.physics.mujoco import MujocoPhysics
from sim.physics.base import JointState
from sim.physics.mujoco import JointState as LegacyJointState


def rotor_xml(*, site_pos="0 0 0", site_quat="1 0 0 0", ref=0,
              joint_type="hinge", motor_attributes="", extra_body="",
              prefix=""):
    return f"""<mujoco>
      <compiler angle="radian"/>
      <option timestep="0.001" integrator="Euler" gravity="0 0 -9.81"/>
      <worldbody>
        <body name="rotor">
          <joint name="shaft" type="{joint_type}" axis="0 0 1" ref="{ref}"/>
          <inertial pos="0 0 0" mass="1" diaginertia="0.015 0.015 0.02"/>
          <site name="site" pos="{site_pos}" quat="{site_quat}"/>
          <site name="other_site" pos="0 0 0"/>
        </body>
        {extra_body}
      </worldbody>
      <actuator>{prefix}<motor name="drive" joint="shaft" {motor_attributes}/></actuator>
      <sensor><gyro name="gyro" site="site"/>
              <accelerometer name="accel" site="site"/></sensor>
    </mujoco>"""


def rotor(**kwargs):
    model = mujoco.MjModel.from_xml_string(rotor_xml(**kwargs))
    return MujocoPhysics(model, joints={"shaft": "drive"},
                         imus={"imu": ("gyro", "accel")})


def test_shared_joint_state_preserves_existing_reexport():
    assert JointState is LegacyJointState


def test_positive_negative_drive_and_coast_have_independent_euler_solution():
    physics = rotor()
    initial = physics.get_joint_state("shaft")
    assert (initial.position_rad, initial.velocity_rad_s) == (0, 0)
    # Izz=.02, tau=.4 => alpha=20. Euler integrates velocity then position.
    physics.set_joint_effort("shaft", .4)
    for _ in range(10):
        physics.step(1_000_000)
    state = physics.get_joint_state("shaft")
    assert state.velocity_rad_s == pytest.approx(.2, abs=1e-13)
    assert state.position_rad == pytest.approx(.0011, abs=1e-13)
    physics.set_joint_effort("shaft", -.4)
    for _ in range(5):
        physics.step(1_000_000)
    state = physics.get_joint_state("shaft")
    assert state.velocity_rad_s == pytest.approx(.1, abs=1e-13)
    assert state.position_rad == pytest.approx(.0018, abs=1e-13)
    physics.set_joint_effort("shaft", 0)
    for _ in range(7):
        physics.step(1_000_000)
    state = physics.get_joint_state("shaft")
    assert state.velocity_rad_s == pytest.approx(.1, abs=1e-13)
    assert state.position_rad == pytest.approx(.0025, abs=1e-13)
    assert physics.time_s == pytest.approx(.022, abs=1e-15)
    assert initial.position_rad == 0  # Returned values are snapshots.
    with pytest.raises(FrozenInstanceError):
        state.position_rad = 1


def test_pose_gyro_and_site_acceleration_all_observe_the_integrated_target():
    physics = rotor(site_pos="0.25 0 0")
    physics.set_joint_effort("shaft", .4)
    physics.step(10_000_000)
    # One Euler step: omega=.2, theta=.002. Body quat uses this target theta.
    pose = physics.get_body_pose("rotor")
    assert pose.position_m == pytest.approx((0, 0, 0), abs=1e-14)
    assert pose.quaternion_wxyz == pytest.approx(
        (math.cos(.001), 0, 0, math.sin(.001)), abs=1e-13)
    sample = physics.get_imu("imu")
    assert sample.gyro_rad_s == pytest.approx((0, 0, .2), abs=1e-13)
    # Site-local a = alpha x r + omega x (omega x r) - gravity.
    assert sample.accel_m_s2 == pytest.approx((-.01, 5, 9.81), abs=1e-12)
    with pytest.raises(FrozenInstanceError):
        sample.accel_m_s2 = (0, 0, 0)
    with pytest.raises(FrozenInstanceError):
        pose.position_m = (1, 2, 3)


def test_rotated_site_reports_gravity_and_gyro_in_its_local_frame():
    physics = rotor(site_quat="0.7071067811865476 0.7071067811865476 0 0")
    # Rx90^-1 transforms world proper-gravity +Z onto local +Y.
    assert physics.get_imu("imu").accel_m_s2 == pytest.approx((0, 9.81, 0), abs=1e-12)
    physics.set_joint_effort("shaft", .4)
    physics.step(1_000_000)
    assert physics.get_imu("imu").gyro_rad_s == pytest.approx((0, .02, 0), abs=1e-12)


def test_free_fall_has_zero_proper_acceleration_and_reset_restores_initial_pose(tmp_path):
    xml = """<mujoco><option integrator="Euler" gravity="0 0 -9.81"/>
      <worldbody><body name="falling" pos="1 2 3"><freejoint/>
        <inertial pos="0 0 0" mass="1" diaginertia="0.02 0.02 0.02"/>
        <site name="imu"/>
      </body></worldbody>
      <sensor><gyro name="gyro" site="imu"/>
        <accelerometer name="accel" site="imu"/></sensor></mujoco>"""
    path = tmp_path / "fall.xml"
    path.write_text(xml)
    physics = MujocoPhysics.from_xml_path(path, joints={}, imus={"imu": ("gyro", "accel")})
    assert physics.get_body_pose("falling").position_m == (1, 2, 3)
    assert physics.get_imu("imu").accel_m_s2 == pytest.approx((0, 0, 0), abs=1e-12)
    physics.step(10_000_000)
    assert physics.get_body_pose("falling").position_m == pytest.approx((1, 2, 2.999019), abs=1e-12)
    assert physics.get_imu("imu").accel_m_s2 == pytest.approx((0, 0, 0), abs=1e-12)
    physics.reset()
    assert physics.time_s == 0
    assert physics.get_body_pose("falling").position_m == (1, 2, 3)
    assert physics.get_imu("imu").accel_m_s2 == pytest.approx((0, 0, 0), abs=1e-12)


def test_reset_uses_native_qpos0_and_clears_previous_effort():
    physics = rotor(ref=.25)
    assert physics.get_joint_state("shaft").position_rad == .25
    physics.set_joint_effort("shaft", .4)
    physics.step(1_000_000)
    physics.reset()
    state = physics.get_joint_state("shaft")
    assert state.position_rad == .25 and state.velocity_rad_s == 0
    assert physics.time_s == 0
    physics.step(1_000_000)
    assert physics.get_joint_state("shaft") == state


def test_constructor_reuses_compiled_model_and_getters_do_not_forward(monkeypatch):
    model = mujoco.MjModel.from_xml_string(rotor_xml())
    calls = []
    native = mujoco.mj_forward
    def forward(bound_model, data):
        calls.append((bound_model, data.time))
        return native(bound_model, data)
    monkeypatch.setattr(mujoco, "mj_forward", forward)
    physics = MujocoPhysics(model, joints={"shaft": "drive"}, imus={"imu": ("gyro", "accel")})
    assert calls == [(model, 0)]
    physics.get_joint_state("shaft")
    physics.get_body_pose("rotor")
    physics.get_imu("imu")
    assert calls == [(model, 0)] and physics.time_s == 0
    physics.step(1_000_000)
    assert len(calls) == 2 and calls[-1][0] is model
    assert calls[-1][1] == pytest.approx(.001)
    physics.reset()
    assert calls[-1] == (model, 0) and physics.time_s == 0


def test_float_dt_is_preserved_without_nanosecond_round_trip():
    physics = rotor()
    dt = .000001000125  # Contains a real .125ns part, not an integer-ns step.
    physics.step_seconds(dt)
    assert physics.time_s == dt


def test_invalid_steps_efforts_and_names_have_no_native_effect(monkeypatch):
    physics = rotor()
    physics.set_joint_effort("shaft", .4)
    native_calls = []
    native_step = mujoco.mj_step
    native_forward = mujoco.mj_forward
    def step(model, data):
        native_calls.append("step")
        return native_step(model, data)
    def forward(model, data):
        native_calls.append("forward")
        return native_forward(model, data)
    monkeypatch.setattr(mujoco, "mj_step", step)
    monkeypatch.setattr(mujoco, "mj_forward", forward)
    for value in (0, -1, True, 1.0, 1 << 63):
        with pytest.raises(ValueError):
            physics.step(value)
    for value in (0, -1, True, math.nan, math.inf):
        with pytest.raises(ValueError):
            physics.step_seconds(value)
    for value in (True, math.nan, math.inf, "1"):
        with pytest.raises(ValueError):
            physics.set_joint_effort("shaft", value)
    for method, args in ((physics.set_joint_effort, ("missing", .4)),
                         (physics.get_joint_state, ("missing",)),
                         (physics.get_body_pose, ("missing",)),
                         (physics.get_imu, ("missing",))):
        with pytest.raises(ValueError):
            method(*args)
    assert native_calls == [] and physics.time_s == 0
    physics.step(1_000_000)
    assert physics.get_joint_state("shaft").velocity_rad_s == pytest.approx(.02, abs=1e-13)
    assert native_calls == ["step", "forward"]


def test_complete_binding_admission_precedes_runtime_projection(monkeypatch):
    model = mujoco.MjModel.from_xml_string(rotor_xml())
    calls = []
    native = mujoco.mj_forward
    def forward(bound_model, data):
        calls.append(bound_model)
        return native(bound_model, data)
    monkeypatch.setattr(mujoco, "mj_forward", forward)
    for joints, imus in (({"shaft": "missing"}, {}),
                         ({"missing": "drive"}, {}),
                         ({"shaft": "drive"}, {"imu": ("gyro", "missing")}),
                         ({"shaft": "drive"}, {"imu": ("gyro", "gyro")}),
                         ({"shaft": "drive"}, {"imu": ["gyro", "accel"]})):
        with pytest.raises(ValueError):
            MujocoPhysics(model, joints=joints, imus=imus)
    assert calls == []
    # A rejected binding did not consume or replace the existing native model.
    physics = MujocoPhysics(model, joints={"shaft": "drive"}, imus={"imu": ("gyro", "accel")})
    assert calls == [model] and physics.time_s == 0


@pytest.mark.parametrize("field,index,value", [
    ("actuator_gear", (0, 0), 2),
    ("actuator_trntype", 0, mujoco.mjtTrn.mjTRN_SITE),
    ("actuator_trnid", (0, 0), -1),
    ("jnt_type", 0, mujoco.mjtJoint.mjJNT_SLIDE),
    ("actuator_ctrllimited", 0, 1),
    ("actuator_forcelimited", 0, 1),
    ("actuator_dyntype", 0, mujoco.mjtDyn.mjDYN_FILTER),
    ("actuator_history", (0, 0), 1),
    ("sensor_objid", 1, 1),
    ("sensor_cutoff", 0, 1),
    ("sensor_history", (0, 0), 1),
    ("sensor_delay", 0, .01),
    ("sensor_interval", (0, 0), .01),
])
def test_unsupported_native_binding_rejected_before_forward(monkeypatch, field, index, value):
    # These are invalid admission inputs, not attempts to run a corrupt model.
    model = mujoco.MjModel.from_xml_string(rotor_xml())
    getattr(model, field)[index] = value
    calls = []
    native = mujoco.mj_forward
    def forward(bound_model, data):
        calls.append(bound_model)
        return native(bound_model, data)
    monkeypatch.setattr(mujoco, "mj_forward", forward)
    with pytest.raises(ValueError):
        MujocoPhysics(model, joints={"shaft": "drive"}, imus={"imu": ("gyro", "accel")})
    assert calls == []


def test_multi_control_prefix_uses_motor_control_address_not_actuator_id():
    other = """<body name="passive"><joint name="other" axis="0 0 1"/>
      <inertial pos="0 0 0" mass="1" diaginertia="0.015 0.015 0.02"/></body>"""
    model = mujoco.MjModel.from_xml_string(rotor_xml(
        extra_body=other, prefix='<pid name="prefix" joint="other" input="pos vel"/>'))
    # MuJoCo 3.12 XML reference: PID has two control inputs before this motor.
    assert model.actuator("drive").id == 1 and model.actuator_ctrladr[1] == 2
    physics = MujocoPhysics(model, joints={"shaft": "drive"})
    physics.set_joint_effort("shaft", .4)
    physics.step(1_000_000)
    assert physics.get_joint_state("shaft").velocity_rad_s == pytest.approx(.02, abs=1e-13)


def test_multi_output_prefix_validates_gear_at_output_address(monkeypatch):
    other = """<body name="passive"><joint name="ball" type="ball"/>
      <inertial pos="0 0 0" mass="1" diaginertia="0.015 0.015 0.02"/></body>"""
    model = mujoco.MjModel.from_xml_string(rotor_xml(
        extra_body=other, prefix='<orientation name="prefix" joint="ball" input="quat"/>',
        motor_attributes='gear="2"'))
    assert model.actuator("drive").id == 1 and model.actuator_outadr[1] == 3
    calls = []
    native = mujoco.mj_forward
    def forward(bound_model, data):
        calls.append(bound_model)
        return native(bound_model, data)
    monkeypatch.setattr(mujoco, "mj_forward", forward)
    with pytest.raises(ValueError):
        MujocoPhysics(model, joints={"shaft": "drive"})
    assert calls == []


def test_native_solver_warning_fails_and_freezes_until_reset(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # Native warning logs belong to this test's temp directory.
    physics = rotor()
    # This value is finite and passes value admission, but native MuJoCo emits
    # BADCTRL and replaces the unstable input. Silent solver recovery is not a
    # successful public physics step.
    physics.set_joint_effort("shaft", 1e308)
    with pytest.raises(RuntimeError):
        physics.step(1_000_000)
    assert math.isfinite(physics.time_s)  # Diagnostic native time stays readable.
    for method, args in ((physics.step, (1_000_000,)),
                         (physics.get_joint_state, ("shaft",)),
                         (physics.get_body_pose, ("rotor",)),
                         (physics.get_imu, ("imu",))):
        with pytest.raises(RuntimeError):
            method(*args)
    physics.reset()
    assert physics.time_s == 0
    assert physics.get_joint_state("shaft").position_rad == 0
    assert physics.get_imu("imu").gyro_rad_s == (0, 0, 0)
    physics.set_joint_effort("shaft", .4)
    physics.step(1_000_000)
    assert physics.get_joint_state("shaft").velocity_rad_s == pytest.approx(.02, abs=1e-13)


def test_negative_actuator_group_is_valid_but_disabled_group_rejects_before_forward(monkeypatch):
    physics = rotor(motor_attributes='group="-1"')
    physics.set_joint_effort("shaft", .4)
    physics.step(1_000_000)
    assert physics.get_joint_state("shaft").velocity_rad_s == pytest.approx(.02, abs=1e-13)
    model = mujoco.MjModel.from_xml_string(rotor_xml(motor_attributes='group="0"'))
    model.opt.disableactuator = 1
    calls = []
    native = mujoco.mj_forward
    def forward(bound_model, data):
        calls.append(bound_model)
        return native(bound_model, data)
    monkeypatch.setattr(mujoco, "mj_forward", forward)
    with pytest.raises(ValueError):
        MujocoPhysics(model, joints={"shaft": "drive"})
    assert calls == []


def test_huge_integrated_target_fails_in_this_step_before_a_later_warning(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    physics = rotor()
    physics.set_joint_effort("shaft", .4)
    # The input and alpha=20 are ordinary finite values. This admitted million-
    # second Euler step creates theta=2e13: native mju_isBad rejects that target.
    # Waiting for the following mj_step's BADQPOS warning would publish bad data.
    with pytest.raises(RuntimeError):
        physics.step(1_000_000_000_000_000)
    with pytest.raises(RuntimeError):
        physics.get_body_pose("rotor")
    physics.reset()
    physics.step(1_000_000)
    assert physics.get_joint_state("shaft").position_rad == 0


def test_bad_static_body_pose_is_rejected_when_observed():
    # A dof-less body has no qpos/qvel/sensor entries to validate. Native pose
    # data can still be invalid and must not escape the public observation.
    model = mujoco.MjModel.from_xml_string(
        '<mujoco><worldbody><body name="static" pos="1 0 0"/></worldbody></mujoco>')
    model.body_pos[1, 0] = math.inf
    physics = MujocoPhysics(model, joints={})
    with pytest.raises(RuntimeError, match="body pose"):
        physics.get_body_pose("static")
    with pytest.raises(RuntimeError, match="reset required"):
        physics.step(1_000_000)
