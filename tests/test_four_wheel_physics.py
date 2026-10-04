"""Native contact isolation before the four-wheel public physics consumer.

The uncalibrated fixture is checked against coordinate signs and static weight,
not a no-slip rolling equation or a fitted vehicle performance target.
"""

import math
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
mujoco = pytest.importorskip("mujoco", reason="optional native MuJoCo runtime")
import numpy as np
from sim.physics.mujoco import MujocoPhysics

MODEL = ROOT / "models" / "four_wheel.xml"
CORNERS = ("fl", "fr", "rl", "rr")
JOINTS = {f"wheel_{corner}": f"drive_{corner}" for corner in CORNERS}
IMUS = {"imu": ("imu_gyro", "imu_accel")}


def test_native_named_geometry_actuation_and_world_axes():
    model = mujoco.MjModel.from_xml_path(str(MODEL))
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    assert model.nu == 4 and model.nv == 10 and model.nq == 11
    assert model.body_mass.sum() == pytest.approx(.6, abs=1e-15)
    assert model.joint("chassis_free").type == mujoco.mjtJoint.mjJNT_FREE
    for corner, xy in zip(CORNERS, ((.07, .06), (.07, -.06), (-.07, .06), (-.07, -.06))):
        joint = model.joint("wheel_" + corner).id
        actuator = model.actuator("drive_" + corner).id
        body = model.body("wheel_" + corner).id
        assert model.jnt_type[joint] == mujoco.mjtJoint.mjJNT_HINGE
        # The retained upstream wheel-body rotation maps local +Z to world +Y.
        assert data.xaxis[joint] == pytest.approx((0, 1, 0), abs=1e-14)
        assert data.xpos[body][:2] == pytest.approx(xy, abs=1e-14)
        assert model.actuator_trntype[actuator] == mujoco.mjtTrn.mjTRN_JOINT
        assert model.actuator_trnid[actuator, 0] == joint
        assert model.actuator_gear[model.actuator_outadr[actuator]] == pytest.approx((1, 0, 0, 0, 0, 0))
        assert not model.actuator_ctrllimited[model.actuator_ctrladr[actuator]]
        assert not model.actuator_forcelimited[actuator]
    gyro, accel = model.sensor("imu_gyro").id, model.sensor("imu_accel").id
    assert model.sensor_type[gyro] == mujoco.mjtSensor.mjSENS_GYRO
    assert model.sensor_type[accel] == mujoco.mjtSensor.mjSENS_ACCELEROMETER
    assert model.sensor_objtype[gyro] == model.sensor_objtype[accel] == mujoco.mjtObj.mjOBJ_SITE
    assert model.sensor_objid[gyro] == model.sensor_objid[accel] == model.site("imu_site").id


def test_native_four_wheel_floor_contacts_support_static_weight():
    model = mujoco.MjModel.from_xml_path(str(MODEL))
    data = mujoco.MjData(model)
    floor = model.geom("floor").id
    wheel_ids = {model.geom("wheel_" + corner).id for corner in CORNERS}
    totals = []
    for tick in range(500):
        mujoco.mj_step(model, data)
        mujoco.mj_forward(model, data)
        if tick < 400:
            continue
        supported = set()
        total = 0.0
        for index in range(data.ncon):
            contact = data.contact[index]
            pair = {int(contact.geom1), int(contact.geom2)}
            assert floor in pair and bool(pair & wheel_ids)
            force = np.zeros(6)
            mujoco.mj_contactForce(model, data, index, force)
            assert force[0] > 0
            total += force[0]
            supported.update(pair & wheel_ids)
        assert supported == wheel_ids
        totals.append(total)
    # At rest, the native ground reactions support the literal .6kg toy mass.
    assert sum(totals) / len(totals) == pytest.approx(.6 * 9.80665, rel=.01)
    assert data.time == pytest.approx(.5, abs=1e-14)
    assert not any(warning.number for warning in data.warning)


def physics():
    return MujocoPhysics.from_xml_path(MODEL, joints=JOINTS, imus=IMUS)


def yaw(pose):
    w, x, y, z = pose.quaternion_wxyz
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


def drive(plant, *, left, right):
    # One owner advances the shared four-wheel plant once per 1ms, not per wheel.
    for _ in range(500):
        plant.step(1_000_000)
    start = plant.get_body_pose("chassis")
    for corner in CORNERS:
        plant.set_joint_effort("wheel_" + corner, left if corner.endswith("l") else right)
    steady_gyro = []
    for tick in range(500):
        plant.step(1_000_000)
        if tick >= 250:
            steady_gyro.append(plant.get_imu("imu").gyro_rad_s[2])
    return start, plant.get_body_pose("chassis"), sum(steady_gyro) / len(steady_gyro)


@pytest.mark.parametrize("direction", [1, -1])
def test_public_equal_torque_moves_forward_or_reverse_without_spurious_yaw(direction):
    plant = physics()
    start, end, gyro_z = drive(plant, left=direction * .03, right=direction * .03)
    # omega(+Y) x radius(bottom=-Z) points -X: positive traction drives +X.
    assert direction * (end.position_m[0] - start.position_m[0]) > 1e-4
    assert abs(end.position_m[1] - start.position_m[1]) < 1e-4
    assert abs(yaw(end) - yaw(start)) < 1e-3
    assert abs(gyro_z) < 1e-3
    for name in JOINTS:
        assert direction * plant.get_joint_state(name).velocity_rad_s > 1e-3
    assert plant.time_s == pytest.approx(1.0, abs=1e-13)


def test_public_right_faster_differential_has_positive_yaw_and_native_gyro():
    plant = physics()
    start, end, gyro_z = drive(plant, left=.01, right=.05)
    left_speed = sum(plant.get_joint_state(name).velocity_rad_s
                     for name in ("wheel_fl", "wheel_rl")) / 2
    right_speed = sum(plant.get_joint_state(name).velocity_rad_s
                      for name in ("wheel_fr", "wheel_rr")) / 2
    assert right_speed > left_speed + 1e-3
    # Right wheels are y<0. Their stronger +X traction contributes +Z yaw.
    # No rigid no-slip speed/radius equality is assumed for skid steering.
    assert yaw(end) - yaw(start) > 1e-4
    assert gyro_z > 1e-4


def test_public_reset_restores_native_initial_state_and_repeats_deterministically():
    plant = physics()
    initial_pose = plant.get_body_pose("chassis")
    initial_joints = tuple(plant.get_joint_state(name) for name in JOINTS)
    initial_imu = plant.get_imu("imu")
    first = drive(plant, left=.01, right=.05)
    first_joints = tuple(plant.get_joint_state(name) for name in JOINTS)
    first_imu = plant.get_imu("imu")
    plant.reset()
    assert plant.time_s == 0
    assert plant.get_body_pose("chassis") == initial_pose
    assert tuple(plant.get_joint_state(name) for name in JOINTS) == initial_joints
    assert plant.get_imu("imu") == initial_imu
    assert drive(plant, left=.01, right=.05) == first
    assert tuple(plant.get_joint_state(name) for name in JOINTS) == first_joints
    assert plant.get_imu("imu") == first_imu
