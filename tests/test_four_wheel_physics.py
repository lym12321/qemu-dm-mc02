"""Native contact isolation before the four-wheel public physics consumer.

The uncalibrated fixture is checked against coordinate signs and static weight,
not a no-slip rolling equation or a fitted vehicle performance target.
"""

import math
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

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


def test_robot_manifest_model_is_consumed_by_existing_native_profile(tmp_path):
    from sim.robots.four_wheel import create_four_wheel_plant
    from sim.robots.manifest import load_manifest

    (tmp_path / "firmware.elf").write_bytes(b"path admission fixture")
    path = tmp_path / "robot.yaml"
    path.write_text(f"""schema: 1
robot: {{name: native-profile}}
boards: {{main: {{type: qemu, board: dm-mc02, firmware: firmware.elf}}}}
buses: {{drive: {{type: can}}}}
links: [{{from: main.fdcan1, to: drive}}]
devices:
  fl: {{type: dji-m3508, bus: drive, id: 1, joint: wheel_fl}}
  fr: {{type: dji-m3508, bus: drive, id: 2, joint: wheel_fr}}
  rl: {{type: dji-m3508, bus: drive, id: 3, joint: wheel_rl}}
  rr: {{type: dji-m3508, bus: drive, id: 4, joint: wheel_rr}}
physics: {{backend: mujoco, model: {MODEL}}}
""")
    manifest = load_manifest(path)
    plant = create_four_wheel_plant(manifest.physics.model)
    try:
        assert tuple(device.motor_id for device in plant.devices) == (1, 2, 3, 4)
        for device in manifest.devices:
            state = plant.physics.get_joint_state(device.joint)
            assert math.isfinite(state.position_rad) and math.isfinite(state.velocity_rad_s)
        sample = plant.physics.get_imu("imu")
        assert all(math.isfinite(value) for value in (*sample.gyro_rad_s, *sample.accel_m_s2))
        assert plant.physics.time_s == 0
    finally:
        plant.close()
    assert plant.closed and all(device.closed for device in plant.devices)
    with pytest.raises(RuntimeError, match="closed"):
        plant.physics.get_imu("imu")


@pytest.mark.parametrize("stage", ["imu", "bus", "device"])
def test_profile_acquisition_failure_releases_all_owned_resources(monkeypatch, stage):
    from sim.robots import four_wheel

    native_create = four_wheel.MujocoPhysics.from_xml_path
    owners, closed = [], []
    primary = KeyboardInterrupt("profile admission")
    def create(*args, **kwargs):
        native = native_create(*args, **kwargs)
        owners.append(native)
        close = native.close
        def close_physics():
            closed.append("physics")
            close()
            raise OSError("physics cleanup")
        monkeypatch.setattr(native, "close", close_physics)
        return native
    monkeypatch.setattr(four_wheel.MujocoPhysics, "from_xml_path", create)
    def reject(*args, **kwargs):
        raise primary
    if stage == "imu":
        monkeypatch.setattr(four_wheel, "imu_to_bmi088_units", reject)
    elif stage == "bus":
        monkeypatch.setattr(four_wheel, "CanBus", reject)
    else:
        native_device = four_wheel.M3508Device
        def device(bus, motor_id, **kwargs):
            if motor_id == 2:
                raise primary
            result = native_device(bus, motor_id, **kwargs)
            owners.append(result)
            close = result.close
            def close_device():
                closed.append(motor_id)
                close()
                raise OSError("device cleanup")
            monkeypatch.setattr(result, "close", close_device)
            return result
        monkeypatch.setattr(four_wheel, "M3508Device", device)
    with pytest.raises(KeyboardInterrupt) as caught:
        four_wheel.create_four_wheel_plant(MODEL)
    assert caught.value is primary
    assert closed == ([1, "physics"] if stage == "device" else ["physics"])
    assert "profile physics close failed: physics cleanup" in primary.__notes__
    if stage == "device":
        assert owners[1].closed
        assert "profile device close failed: device cleanup" in primary.__notes__
    with pytest.raises(RuntimeError, match="closed"):
        owners[0].get_imu("imu")


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


# Independent DJI M3508 P19 V1.0 specification, PDF pp7/13:
# reduction ratio3591/187 (SHA256 ca85dbad…797a4de). The C620 V1.01 CAN
# table describes rotor angle/RPM, while ideal lossless gearing is an explicit
# adapter approximation. It is applied once outside the unit-gear XML.
GEAR_RATIO = 3591 / 187


def shared_fixture():
    from sim.core import Coordinator
    from sim.devices.dji import M3508Device, M3508JointPlant
    from sim.fabric import CanBus
    from sim.fabric.runtime import CanParticipant

    bus = CanBus()

    class Host:
        def __init__(self):
            self.expected_time = 0
            self.frames = {}
            bus.attach(self, "host")

        def receive(self, frame):
            assert frame.can_id in (0x201, 0x202, 0x203, 0x204)
            assert frame.timestamp_ns == self.expected_time
            assert frame.can_id not in self.frames
            self.frames[frame.can_id] = frame

    host = Host()
    devices = tuple(M3508Device(bus, motor_id) for motor_id in (1, 2, 3, 4))
    plant = physics()
    participant = M3508JointPlant(
        plant, dict(zip(devices, JOINTS)), gear_ratio=GEAR_RATIO)
    coordinator = Coordinator({"can": CanParticipant(bus, (participant,))})
    return plant, bus, host, devices, coordinator


def shared_drive(plant, bus, host, coordinator, payload):
    from sim.fabric import CanFrame

    def advance(command):
        host.frames.clear()
        host.expected_time = coordinator.t_sim_ns + 1_000_000
        bus.send(host, CanFrame(0x200, dlc=8, data=command,
                               timestamp_ns=coordinator.t_sim_ns))
        coordinator.step(1_000_000)
        assert set(host.frames) == {0x201, 0x202, 0x203, 0x204}

    for _ in range(500):
        advance(bytes.fromhex("0000 0000 0000 0000"))
    start = plant.get_body_pose("chassis")
    gyros = []
    for tick in range(500):
        advance(payload)  # Actual C620 refresh, keeping all four devices live.
        if tick >= 250:
            gyros.append(plant.get_imu("imu").gyro_rad_s[2])
    assert plant.time_s == pytest.approx(1.0, abs=1e-13)
    return start, plant.get_body_pose("chassis"), sum(gyros) / len(gyros)


def assert_shared_feedback(plant, host, devices, currents, *, joints=JOINTS):
    for motor, name, current in zip(devices, joints, currents):
        wheel = plant.get_joint_state(name)
        rotor_position = GEAR_RATIO * wheel.position_rad
        rotor_velocity = GEAR_RATIO * wheel.velocity_rad_s
        assert motor.get_rotor_state() == pytest.approx((rotor_position, rotor_velocity))
        frame = host.frames[0x200 + motor.motor_id]
        assert (frame.flags, frame.dlc, len(frame.data)) == (0, 8, 8)
        encoder = int.from_bytes(frame.data[:2], "big")
        rpm = int.from_bytes(frame.data[2:4], "big", signed=True)
        raw_current = int.from_bytes(frame.data[4:6], "big", signed=True)
        assert raw_current == current and frame.data[6:] == b"\x19\x00"
        # Check vendor rotor quantization bins rather than calling the encoder
        # helper or computing its expected floor/round code with copied logic.
        lower = encoder * math.tau / 8192
        wrapped = rotor_position % math.tau
        assert 0 <= encoder <= 8191
        assert lower - 1e-12 <= wrapped < lower + math.tau / 8192 + 1e-12
        assert abs(rotor_velocity - rpm * math.pi / 30) <= math.pi / 60 + 1e-12


@pytest.mark.parametrize("direction", [1, -1])
def test_shared_m3508_equal_current_native_motion_and_rotor_feedback(direction):
    from sim.core import Session

    plant, bus, host, devices, coordinator = shared_fixture()
    try:
        coordinator.reset(Session(1))
        # Literal ±64/16384*20 A with default .02 rotorNm/A and published ratio
        # gives approximately ±.03 wheelNm, independently of the device codec.
        payload = bytes.fromhex("0040 0040 0040 0040" if direction > 0 else
                                "ffc0 ffc0 ffc0 ffc0")
        start, end, gyro_z = shared_drive(plant, bus, host, coordinator, payload)
        assert direction * (end.position_m[0] - start.position_m[0]) > 1e-4
        assert abs(end.position_m[1] - start.position_m[1]) < 1e-4
        assert abs(yaw(end) - yaw(start)) < 1e-3 and abs(gyro_z) < 1e-3
        assert coordinator.t_sim_ns == 1_000_000_000
        assert_shared_feedback(plant, host, devices, (direction * 64,) * 4)
    finally:
        coordinator.close()


def test_shared_m3508_differential_native_imu_and_new_session_reset_repeat():
    from sim.core import Session

    plant, bus, host, devices, coordinator = shared_fixture()
    payload = bytes.fromhex("0015 006b 0015 006b")  # Left21/right107 ≈.01/.05 Nm.
    try:
        coordinator.reset(Session(1))
        initial = (plant.get_body_pose("chassis"), plant.get_imu("imu"),
                   tuple(plant.get_joint_state(name) for name in JOINTS))
        start, end, gyro_z = first = shared_drive(plant, bus, host, coordinator, payload)
        assert yaw(end) - yaw(start) > 1e-4 and gyro_z > 1e-4
        left = sum(plant.get_joint_state(name).velocity_rad_s for name in
                   ("wheel_fl", "wheel_rl"))
        right = sum(plant.get_joint_state(name).velocity_rad_s for name in
                    ("wheel_fr", "wheel_rr"))
        assert right > left + 1e-3
        assert_shared_feedback(plant, host, devices, (21, 107, 21, 107))
        first_joints = tuple(plant.get_joint_state(name) for name in JOINTS)
        first_imu = plant.get_imu("imu")
        first_wire = tuple(host.frames[ident].data for ident in (0x201, 0x202, 0x203, 0x204))
        # A nonzero worker epoch does not turn native plant time into wire time.
        coordinator.reset(Session(2, 7_000_000_000))
        assert plant.time_s == 0 and coordinator.t_sim_ns == 7_000_000_000
        assert (plant.get_body_pose("chassis"), plant.get_imu("imu"),
                tuple(plant.get_joint_state(name) for name in JOINTS)) == initial
        assert shared_drive(plant, bus, host, coordinator, payload) == first
        assert coordinator.t_sim_ns == 8_000_000_000
        assert tuple(plant.get_joint_state(name) for name in JOINTS) == first_joints
        assert plant.get_imu("imu") == first_imu
        assert tuple(host.frames[ident].data for ident in (0x201, 0x202, 0x203, 0x204)) == first_wire
        assert_shared_feedback(plant, host, devices, (21, 107, 21, 107))
    finally:
        coordinator.close()


@pytest.fixture
def fixed_runtime():
    from sim.robots.four_wheel import FourWheelRuntime, create_four_wheel_plant

    sys.path.insert(0, str(ROOT / "tools"))
    import dm_mc02_sim_worker as worker

    left, right = socket.socketpair()
    left.setblocking(False)
    right.setblocking(False)
    plant = runtime = None
    try:
        stream, peer = worker.CanStream(left), worker.CanStream(right)
        plant = create_four_wheel_plant(MODEL)
        runtime = FourWheelRuntime(plant, stream)
        yield plant, runtime, stream, peer, worker.QemuTimestampMapper
    finally:
        try:
            if runtime is not None:
                runtime.close()
            elif plant is not None:
                plant.close()
        finally:
            left.close()
            right.close()


def test_fixed84_profile_can_shared_advance_target_imu_feedback_and_epoch_reset(
        fixed_runtime, monkeypatch):
    from sim.fabric import CanFrame

    plant, runtime, stream, peer, mapper = fixed_runtime
    native = plant.physics
    initial_imu = native.get_imu("imu")
    events = []
    advance, get_imu = native.step, native.get_imu

    def observed_advance(dt_ns):
        events.append(("step", dt_ns))
        advance(dt_ns)

    def observed_imu(name):
        events.append(("imu", native.time_s))
        return get_imu(name)

    monkeypatch.setattr(native, "step", observed_advance)
    monkeypatch.setattr(native, "get_imu", observed_imu)
    first_state = first_wire = None
    for session, worker_origin, qemu_origin in ((1, 7_000_000_000, 4_000_000_000),
                                               (2, 20_000_000_000, 30_000_000_000)):
        runtime.reset(session, worker_origin, mapper(qemu_origin, worker_origin))
        assert native.time_s == 0 and runtime.t_sim_ns == worker_origin
        assert runtime.get_imu() == initial_imu
        events.clear()
        # Existing fixed84 stream/codec is the borrowed wire owner.
        peer.send(0x200, 0, 8, qemu_origin, bytes.fromhex("0015 006b 0015 006b"))
        ident, flags, dlc, timestamp, payload = stream.receive()[0]
        runtime.ingest_qemu(CanFrame(ident, flags, dlc, payload, timestamp), worker_origin)
        assert native.time_s == 0 and runtime.t_sim_ns == worker_origin
        assert all(device.current_raw == 0 for device in plant.devices)
        result = runtime.step(1_000_000)
        sample = runtime.get_imu()
        assert events == [("step", 1_000_000), ("imu", .001)]
        assert sample == get_imu("imu") and sample != initial_imu
        assert all(math.isfinite(value) for value in
                   (*sample.gyro_rad_s, *sample.accel_m_s2))
        assert result.status == "ok" and result.reached_time_ns == worker_origin + 1_000_000
        assert native.time_s == .001 and runtime.t_sim_ns == worker_origin + 1_000_000
        replies = peer.receive()
        assert [record[0] for record in replies] == [0x201, 0x202, 0x203, 0x204]
        assert all(record[3] == qemu_origin + 1_000_000 for record in replies)
        frames = {ident: CanFrame(ident, flags, dlc, payload, timestamp)
                  for ident, flags, dlc, timestamp, payload in replies}
        assert_shared_feedback(native, SimpleNamespace(frames=frames), plant.devices,
                               (21, 107, 21, 107))
        state = (sample, tuple(native.get_joint_state(name) for name in JOINTS))
        wire = tuple(frame.data for frame in frames.values())
        if first_state is None:
            first_state, first_wire = state, wire
        else:
            assert state == first_state and wire == first_wire


def test_runtime_imu_rejects_before_reset_after_step_failure_and_after_close(
        fixed_runtime, monkeypatch):
    plant, runtime, stream, peer, mapper = fixed_runtime
    native = plant.physics
    reads = []
    get_imu, advance = native.get_imu, native.step

    def observed_imu(name):
        reads.append(native.time_s)
        return get_imu(name)

    def failed_advance(dt_ns):
        advance(dt_ns)
        raise RuntimeError("solver boundary failure")

    monkeypatch.setattr(native, "get_imu", observed_imu)
    with pytest.raises(RuntimeError, match="successful runtime reset/step"):
        runtime.get_imu()
    assert reads == []
    runtime.reset(1, 0, mapper(1_000_000_000, 0))
    initial = runtime.get_imu()
    monkeypatch.setattr(native, "step", failed_advance)
    with pytest.raises(RuntimeError, match="solver boundary failure"):
        runtime.step(1_000_000)
    assert native.time_s == .001 and runtime.t_sim_ns == 0
    with pytest.raises(RuntimeError, match="successful runtime reset/step"):
        runtime.get_imu()
    assert reads == [0]
    monkeypatch.setattr(native, "step", advance)
    runtime.reset(2, 9_000_000_000, mapper(11_000_000_000, 9_000_000_000))
    assert runtime.get_imu() == initial and reads == [0, 0]
    runtime.close()
    with pytest.raises(RuntimeError, match="successful runtime reset/step"):
        runtime.get_imu()
    assert reads == [0, 0] and plant.closed and all(d.closed for d in plant.devices)
    # Runtime owns devices, while the caller retains both native connections.
    peer.send(0x123, 0, 4, 0, b"live")
    assert stream.receive() == [(0x123, 0, 4, 0, b"live")]


# MuJoCo3.12 XMLreference #asset-model/#body-attach/#frame specifies native
# namespace/reference copying and frame accumulation. #compiler-conflict
# rejects different authored global options. The original car/license remains
# canonical; these tests consume the fixed scene without a geometry copier.
TWO_CHASSIS_MODEL = ROOT / "models" / "two_four_wheel.xml"


def test_two_chassis_native_names_references_and_shared_floor():
    model = mujoco.MjModel.from_xml_path(str(TWO_CHASSIS_MODEL))
    assert (model.nq, model.nv, model.nu, model.njnt, model.nbody,
            model.ngeom, model.nsite, model.nsensor) == (22, 20, 8, 10, 11, 11, 2, 4)
    assert model.body_mass.sum() == pytest.approx(1.2, abs=1e-15)
    plane_ids = np.flatnonzero(model.geom_type == mujoco.mjtGeom.mjGEOM_PLANE)
    assert plane_ids.tolist() == [model.geom("alpha/floor").id]
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    for prefix, offset_y in (("alpha/", 0), ("bravo/", .6)):
        body = model.body(prefix + "chassis").id
        freejoint = model.joint(prefix + "chassis_free").id
        assert model.jnt_type[freejoint] == mujoco.mjtJoint.mjJNT_FREE
        assert model.jnt_bodyid[freejoint] == body
        assert data.xpos[body] == pytest.approx((0, offset_y, .03), abs=1e-15)
        assert data.xquat[body] == pytest.approx((1, 0, 0, 0), abs=1e-15)
        qpos = model.jnt_qposadr[freejoint]
        assert model.qpos0[qpos:qpos + 7] == pytest.approx(
            (0, offset_y, .03, 1, 0, 0, 0), abs=1e-15)
        for corner, xy in zip(CORNERS, ((.07, .06), (.07, -.06),
                                       (-.07, .06), (-.07, -.06))):
            name = prefix + "wheel_" + corner
            joint, geom = model.joint(name).id, model.geom(name).id
            actuator = model.actuator(prefix + "drive_" + corner).id
            assert model.jnt_type[joint] == mujoco.mjtJoint.mjJNT_HINGE
            assert data.xaxis[joint] == pytest.approx((0, 1, 0), abs=1e-14)
            assert data.xpos[model.body(name).id] == pytest.approx(
                (xy[0], offset_y + xy[1], .03), abs=1e-15)
            # Native attachment must retain the canonical wheel defaults.
            assert model.geom_type[geom] == mujoco.mjtGeom.mjGEOM_CYLINDER
            assert model.geom_size[geom][:2] == pytest.approx((.03, .01), abs=1e-15)
            assert model.body_mass[model.body(name).id] == pytest.approx(.05, abs=1e-15)
            assert model.dof_damping[model.jnt_dofadr[joint]] == .03
            assert model.actuator_trntype[actuator] == mujoco.mjtTrn.mjTRN_JOINT
            assert model.actuator_trnid[actuator, 0] == joint
            assert model.actuator_gear[model.actuator_outadr[actuator]] == pytest.approx(
                (1, 0, 0, 0, 0, 0))
            assert not model.actuator_ctrllimited[model.actuator_ctrladr[actuator]]
            assert not model.actuator_forcelimited[actuator]
        gyro, accel = (model.sensor(prefix + name).id
                       for name in ("imu_gyro", "imu_accel"))
        site = model.site(prefix + "imu_site").id
        assert model.sensor_type[gyro] == mujoco.mjtSensor.mjSENS_GYRO
        assert model.sensor_type[accel] == mujoco.mjtSensor.mjSENS_ACCELEROMETER
        assert model.sensor_objtype[gyro] == model.sensor_objtype[accel] == mujoco.mjtObj.mjOBJ_SITE
        assert model.sensor_objid[gyro] == model.sensor_objid[accel] == site
        assert model.site_bodyid[site] == body


@pytest.mark.parametrize("old,new,error", [
    ('prefix="bravo/"', 'prefix="alpha/"', "already exists with prefix alpha/"),
    ('body="chassis"', 'body="missing_chassis"', "body"),
    ('timestep="0.001"', 'timestep="0.002"', "timestep"),
])
def test_two_chassis_native_rejects_bad_attachment_before_runtime(old, new, error):
    # Native asset input stays canonical; only the parent test fixture changes.
    xml = TWO_CHASSIS_MODEL.read_text().replace('file="four_wheel.xml"',
                                              f'file="{MODEL}"')
    assert old in xml
    with pytest.raises(ValueError, match=error):
        mujoco.MjModel.from_xml_string(xml.replace(old, new))


def test_two_chassis_native_one_floor_supports_both_static_weights():
    model = mujoco.MjModel.from_xml_path(str(TWO_CHASSIS_MODEL))
    data = mujoco.MjData(model)
    floor = model.geom("alpha/floor").id
    wheels = {prefix: {model.geom(prefix + "wheel_" + corner).id for corner in CORNERS}
              for prefix in ("alpha/", "bravo/")}
    totals = {prefix: [] for prefix in wheels}
    for tick in range(500):
        mujoco.mj_step(model, data)
        mujoco.mj_forward(model, data)
        if tick < 400:
            continue
        supported = {prefix: set() for prefix in wheels}
        reactions = {prefix: 0.0 for prefix in wheels}
        for index in range(data.ncon):
            contact = data.contact[index]
            pair = {int(contact.geom1), int(contact.geom2)}
            assert floor in pair
            owners = [prefix for prefix, ids in wheels.items() if pair & ids]
            assert len(owners) == 1
            prefix = owners[0]
            force = np.zeros(6)
            mujoco.mj_contactForce(model, data, index, force)
            assert force[0] > 0
            reactions[prefix] += force[0]
            supported[prefix].update(pair & wheels[prefix])
        for prefix in wheels:
            assert supported[prefix] == wheels[prefix]
            totals[prefix].append(reactions[prefix])
    for reactions in totals.values():
        assert sum(reactions) / len(reactions) == pytest.approx(.6 * 9.80665, rel=.01)
    assert data.time == pytest.approx(.5, abs=1e-14)
    assert not any(warning.number for warning in data.warning)


def test_two_chassis_native_cross_prefix_collision():
    xml = TWO_CHASSIS_MODEL.read_text().replace('file="four_wheel.xml"',
                                              f'file="{MODEL}"')
    assert 'pos="0 .6 0"' in xml
    # Move only the native spawn frame. Original +/- .07 wheel centers and
    # .03 cylinder radii overlap front-alpha/rear-bravo by .01m at x=.19.
    model = mujoco.MjModel.from_xml_string(xml.replace('pos="0 .6 0"',
                                                    'pos=".19 0 0"'))
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    expected = {frozenset((model.geom("alpha/wheel_f" + side).id,
                           model.geom("bravo/wheel_r" + side).id))
                for side in ("l", "r")}
    cross_pairs = {frozenset((int(contact.geom1), int(contact.geom2)))
                   for contact in data.contact[:data.ncon]
                   if contact.dist < 0}
    assert expected <= cross_pairs
    assert data.time == 0  # Native contact projection needs no warmup/clock.
    assert not any(warning.number for warning in data.warning)


def test_two_chassis_motor_world_independent_buses_motion_reset_and_expiry(monkeypatch):
    from sim.core import Coordinator, Session
    from sim.devices.dji import M3508Device, M3508JointPlant
    from sim.fabric import CanBus, CanFrame
    from sim.fabric.runtime import CanParticipant

    names = ("alpha", "bravo")
    joint_groups = tuple(tuple(f"{name}/wheel_{corner}" for corner in CORNERS)
                         for name in names)
    native = MujocoPhysics.from_xml_path(
        TWO_CHASSIS_MODEL,
        joints={f"{name}/wheel_{corner}": f"{name}/drive_{corner}"
                for name in names for corner in CORNERS},
        imus={name: (f"{name}/imu_gyro", f"{name}/imu_accel") for name in names})
    calls = {"reset": 0, "step": [], "close": 0}
    reset, advance, close = native.reset, native.step, native.close

    def observed_reset():
        calls["reset"] += 1
        reset()

    def observed_step(dt_ns):
        calls["step"].append(dt_ns)
        advance(dt_ns)

    def observed_close():
        calls["close"] += 1
        close()

    monkeypatch.setattr(native, "reset", observed_reset)
    monkeypatch.setattr(native, "step", observed_step)
    monkeypatch.setattr(native, "close", observed_close)

    class Host:
        def __init__(self, bus):
            self.frames = {}
            self.expected_time = 0
            bus.attach(self, "host")

        def receive(self, frame):
            assert frame.can_id in (0x201, 0x202, 0x203, 0x204)
            assert frame.timestamp_ns == self.expected_time
            assert frame.can_id not in self.frames
            self.frames[frame.can_id] = frame

    buses = (CanBus(), CanBus())
    hosts = tuple(Host(bus) for bus in buses)
    devices, groups = [], []
    world = coordinator = None
    try:
        # Deliberately different short timeouts exercise the existing expiry
        # union. Refreshes below keep both groups live during the motion gate.
        for bus, timeout in zip(buses, (3_000_000, 7_000_000)):
            group = []
            for motor_id in (1, 2, 3, 4):
                device = M3508Device(bus, motor_id, command_timeout_ns=timeout)
                devices.append(device)
                group.append(device)
            groups.append(tuple(group))
        world = M3508JointPlant(native, {
            device: joint for group, joints in zip(groups, joint_groups)
            for device, joint in zip(group, joints)}, gear_ratio=GEAR_RATIO)
        coordinator = Coordinator({"can": CanParticipant(buses, (world,))})

        def snapshot():
            return tuple((native.get_body_pose(name + "/chassis"),
                          native.get_imu(name),
                          tuple(native.get_joint_state(joint) for joint in joints))
                         for name, joints in zip(names, joint_groups))

        def tick(payloads, dt_ns=1_000_000):
            for bus, host, payload in zip(buses, hosts, payloads):
                host.frames.clear()
                host.expected_time = coordinator.t_sim_ns + dt_ns
                bus.send(host, CanFrame(0x200, dlc=8, data=payload,
                                       timestamp_ns=coordinator.t_sim_ns))
            coordinator.step(dt_ns)
            for host in hosts:
                assert set(host.frames) == {0x201, 0x202, 0x203, 0x204}

        coordinator.reset(Session(1))
        initial = snapshot()
        for _ in range(500):
            tick((bytes(8), bytes(8)))
        start = tuple(native.get_body_pose(name + "/chassis") for name in names)
        # Independent C620 wire literals: alpha +64 on all motors, bravo
        # left -21/right -107. No producer encoder supplies expected values.
        payloads = (bytes.fromhex("0040 0040 0040 0040"),
                    bytes.fromhex("ffeb ff95 ffeb ff95"))
        gyros = [[], []]
        for iteration in range(500):
            tick(payloads)
            if iteration >= 250:
                for values, name in zip(gyros, names):
                    values.append(native.get_imu(name).gyro_rad_s[2])
        end = snapshot()
        assert end[0][0].position_m[0] - start[0].position_m[0] > 1e-4
        assert end[1][0].position_m[0] - start[1].position_m[0] < -1e-4
        assert abs(yaw(end[0][0]) - yaw(start[0])) < 1e-3
        assert yaw(end[1][0]) - yaw(start[1]) < -1e-4
        assert abs(sum(gyros[0]) / len(gyros[0])) < 1e-3
        assert sum(gyros[1]) / len(gyros[1]) < -1e-4
        for state, direction in zip(end, (1, -1)):
            assert all(direction * joint.velocity_rad_s > 1e-3 for joint in state[2])
            assert all(math.isfinite(value) for value in
                       (*state[1].gyro_rad_s, *state[1].accel_m_s2))
        for host, group, joints, currents in zip(
                hosts, groups, joint_groups, ((64,) * 4, (-21, -107, -21, -107))):
            assert_shared_feedback(native, host, group, currents, joints=joints)
        assert coordinator.t_sim_ns == 1_000_000_000
        assert native.time_s == pytest.approx(1, abs=1e-13)
        assert calls["reset"] == 1 and calls["step"] == [1_000_000] * 1000

        coordinator.reset(Session(2, 7_000_000_000))
        assert native.time_s == 0 and coordinator.t_sim_ns == 7_000_000_000
        assert snapshot() == initial and all(device.current_raw == 0 for device in devices)
        calls["step"].clear()
        tick(payloads, dt_ns=10_000_000)
        assert calls["reset"] == 2 and calls["step"] == [3_000_000, 4_000_000, 3_000_000]
        assert native.time_s == pytest.approx(.01, abs=1e-15)
        assert coordinator.t_sim_ns == 7_010_000_000
        assert all(device.current_raw == 0 for device in devices)
        for host, group, joints in zip(hosts, groups, joint_groups):
            assert_shared_feedback(native, host, group, (0,) * 4, joints=joints)
    finally:
        if coordinator is not None:
            coordinator.close()
        elif world is not None:
            world.close()
        else:
            for device in reversed(devices):
                device.close()
            native.close()
    assert world.closed and all(device.closed for device in devices)
    assert calls["close"] == 1
    with pytest.raises(RuntimeError, match="closed"):
        native.get_imu("alpha")
    # Borrowed buses remain available after closing their sole device/world owner.
    for bus in buses:
        bus.reset(0)
        assert bus.t_sim_ns == 0
