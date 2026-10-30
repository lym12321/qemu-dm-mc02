"""Native Gazebo admission and independent rigid-rotor mechanics.

Izz=.02 and torque=.4 imply alpha=20 rad/s². The position bound below permits
one fixed integration tick of error around the continuous solution. No project
rotor helper, motor law, wire codec or MCU clock supplies expected values.
"""

from dataclasses import FrozenInstanceError
from pathlib import Path
import math
import shutil
import sys
import threading
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from sim.physics import BodyPose, GazeboPhysics, ImuSample, JointState, PhysicsParticipant
from sim.physics.mujoco import BodyPose as LegacyBodyPose
from sim.physics.mujoco import ImuSample as LegacyImuSample
from sim.physics.mujoco import JointState as LegacyJointState
from sim.api import Session
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tests"))
from simulation_support import simulation_for
from sim.devices.dji import M3508Device, M3508JointPlant
from sim.devices.bmi088 import imu_to_bmi088_units
from sim.fabric import CanBus, CanFrame, VirtualCanDevice

MODELS = Path(__file__).resolve().parents[4] / "models"
ARENA_PATH = MODELS / "gazebo_rmua19.sdf"
ARENA_RESOURCES = MODELS / "vendor/rmua19/resource/models"
PROBE_BINDINGS = {"floor": ("floor_probe", "body"),
                  "obstacle": ("obstacle_probe", "body")}


def test_native_community_field_collision(native):
    world = native.World(str(ARENA_PATH), {}, PROBE_BINDINGS, {}, [str(ARENA_RESOURCES)])
    try:
        assert world.time_ns == 0
        assert world.get_body_pose("floor")[:3] == [-1, 0, 1]
        assert world.get_body_pose("obstacle")[:3] == [0, 0, 1]
        # DART 6.13.2 ContactConstraint.cpp:45-48,397-406 caps penetration
        # correction at .001 m/s. Keep 2mm gate, allow bounded recovery from
        # initial impact; pinned upstream plane is a native large-box proxy.
        world.step(5_000_000_000)
        # Pinned upstream ground z=0 and obstacle_1 top=.4; each probe's
        # half-height=.1. Actual DART contact must support both, not freefall.
        assert world.get_body_pose("floor")[:3] == pytest.approx((-1, 0, .1), abs=.002)
        assert world.get_body_pose("obstacle")[:3] == pytest.approx((0, 0, .5), abs=.002)
        world.step(1_000_000_000)
        assert world.time_ns == 6_000_000_000
        assert world.get_body_pose("floor")[2] == pytest.approx(.1, abs=.002)
        assert world.get_body_pose("obstacle")[2] == pytest.approx(.5, abs=.002)
    finally:
        world.close()


def test_typed_community_world_reset_uses_admitted_dom(native, tmp_path, monkeypatch):
    resources = tmp_path / "models"
    shutil.copytree(ARENA_RESOURCES, resources)
    model_sdf = resources / "rmua19_battlefield/model.sdf"
    model_sdf.write_text(model_sdf.read_text().replace(
        "model://rmua19_battlefield/materials/textures/wood.jpg",
        "materials/textures/wood.jpg"))
    path = tmp_path / "world.sdf"
    shutil.copyfile(ARENA_PATH, path)
    monkeypatch.chdir(tmp_path)
    with GazeboPhysics(path, bodies=PROBE_BINDINGS, resource_paths=(resources,)) as physics:
        initial = {name: physics.get_body_pose(name) for name in PROBE_BINDINGS}
        physics.step(5_000_000_000)
        assert physics.get_body_pose("floor").position_m == pytest.approx((-1, 0, .1), abs=.002)
        assert physics.get_body_pose("obstacle").position_m == pytest.approx((0, 0, .5), abs=.002)
        # Includes are flattened into the retained native DOM. Reset does not
        # re-read XML; external texture bytes remain separate native resources.
        path.unlink()
        model_sdf.unlink()
        physics.reset()
        assert physics.time_ns == 0
        assert {name: physics.get_body_pose(name) for name in PROBE_BINDINGS} == initial
        physics.step(5_000_000_000)
        assert physics.get_body_pose("obstacle").position_m[2] == pytest.approx(.5, abs=.002)


def test_missing_model_and_pbr_texture_rejected(native, tmp_path):
    resources = tmp_path / "models"
    resources.mkdir()
    # Native SDF_PATH/install fallback remains supported. Unique missing URIs
    # isolate rejection even when the caller's environment has the real arena.
    model_name = "missing_" + uuid.uuid4().hex
    path = tmp_path / "missing.sdf"
    path.write_text(ARENA_PATH.read_text().replace("model://rmua19_battlefield",
                                                 "model://" + model_name))
    with pytest.raises(ValueError, match="SDF admission failed"):
        native.World(str(path), {}, PROBE_BINDINGS, {}, [str(resources)])
    model = resources / model_name
    shutil.copytree(ARENA_RESOURCES / "rmua19_battlefield", model)
    sdf = model / "model.sdf"
    sdf.write_text(sdf.read_text().replace("model://rmua19_battlefield",
                                         "model://" + model_name))
    (model / "materials/textures/wood.jpg").unlink()
    with pytest.raises(ValueError, match="material resource unavailable.*wood.jpg"):
        native.World(str(path), {}, PROBE_BINDINGS, {}, [str(resources)])


@pytest.mark.parametrize("directories", ["models", ("",), ("a:b",), (None,)])
def test_resource_directory_types_rejected_before_acquisition(directories):
    with pytest.raises(ValueError, match="resource"):
        GazeboPhysics("unused.sdf", bodies=PROBE_BINDINGS, resource_paths=directories)


def rotor_sdf(*, joint_type="revolute", step="0.001", effort="10",
              static="false", plugin="", second_joint=""):
    return f"""<sdf version="1.10"><world name="rotor_world">
      <gravity>0 0 0</gravity>
      <physics name="physics" type="ignored">
        <max_step_size>{step}</max_step_size><real_time_factor>0</real_time_factor>
      </physics>
      <plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>
      {plugin}
      <model name="rotor"><static>{static}</static>
        <link name="body"><inertial><mass>1</mass>
          <inertia><ixx>.015</ixx><iyy>.015</iyy><izz>.02</izz></inertia>
        </inertial></link>
        <joint name="shaft" type="{joint_type}">
          <parent>world</parent><child>body</child>
          <axis><xyz>0 0 1</xyz><limit><lower>-1e16</lower><upper>1e16</upper>
            <effort>{effort}</effort></limit><dynamics><damping>0</damping></dynamics></axis>
        </joint>
      </model>{second_joint}
    </world></sdf>"""


def imu_sdf(*, pose="0 0 0 0 0 0", rate=0, imu_xml="<imu/>"):
    return rotor_sdf().replace('<gravity>0 0 0</gravity>', '<gravity>0 0 -10</gravity>').replace(
        '</inertial></link>', '</inertial><sensor name="imu" type="imu">'
        f'<pose>{pose}</pose><update_rate>{rate}</update_rate>{imu_xml}</sensor></link>')


@pytest.fixture
def native():
    # This dedicated SDK gate requires the built extension. A missing or broken
    # adapter is a failure here, not an optional-dependency skip.
    import _dm_gazebo_native
    return _dm_gazebo_native


@pytest.fixture
def sdf_path(tmp_path):
    path = tmp_path / "rotor.sdf"
    path.write_text(rotor_sdf())
    return path


def test_joint_state_reexport_preserves_existing_type():
    assert JointState is LegacyJointState
    assert BodyPose is LegacyBodyPose
    assert ImuSample is LegacyImuSample
    state = JointState(.1, .2)
    with pytest.raises(FrozenInstanceError):
        state.position_rad = 0


def test_context_cleanup_preserves_primary_and_reports_close_failure():
    physics = object.__new__(GazeboPhysics)
    cleanup = RuntimeError("close unavailable")

    def fail_close():
        raise cleanup

    physics.close = fail_close
    primary = ValueError("consumer failed")
    assert physics.__exit__(ValueError, primary, None) is None
    assert primary.__notes__ == ["Gazebo close failed: close unavailable"]
    with pytest.raises(RuntimeError) as caught:
        physics.__exit__(None, None, None)
    assert caught.value is cleanup


@pytest.mark.parametrize("bindings", [None, {}, {"shaft": "rotor"},
    {"shaft": ("rotor",)}, {"": ("rotor", "shaft")},
    {"shaft": ("rotor::nested", "shaft")}, {"shaft": ("rotor", "sh\0aft")},
    {"a": ("rotor", "shaft"), "b": ("rotor", "shaft")}])
def test_binding_types_rejected_before_native_acquisition(bindings):
    with pytest.raises(ValueError):
        GazeboPhysics("unused.sdf", joints=bindings)


def test_native_drive_coast_reset_and_closed_owner(native, sdf_path):
    physics = GazeboPhysics(sdf_path, joints={"shaft": ("rotor", "shaft")})
    try:
        assert native.gazebo_version == "8.15.0"
        assert physics.time_ns == 0 and physics.timestep_ns == 1_000_000
        assert physics.get_joint_state("shaft") == JointState(0, 0)
        physics.set_joint_effort("shaft", .4)
        physics.step(100_000_000)
        state = physics.get_joint_state("shaft")
        assert physics.time_ns == 100_000_000
        assert state.velocity_rad_s == pytest.approx(2, abs=1e-9)
        assert state.position_rad == pytest.approx(.1, abs=.0021)
        # Getter is a completed observation; no time or state progression.
        assert physics.get_joint_state("shaft") == state
        assert physics.time_s == .1
        physics.set_joint_effort("shaft", 0)
        physics.step(10_000_000)
        coast = physics.get_joint_state("shaft")
        assert coast.velocity_rad_s == pytest.approx(2, abs=1e-9)
        assert coast.position_rad - state.position_rad == pytest.approx(.02, abs=1e-9)
        physics.set_joint_effort("shaft", -.4)
        physics.step(50_000_000)
        assert physics.get_joint_state("shaft").velocity_rad_s == pytest.approx(1, abs=1e-9)
        physics.reset()
        assert physics.time_ns == 0
        assert physics.get_joint_state("shaft") == JointState(0, 0)
        physics.step(1_000_000)  # old torque must be cancelled by reset
        assert physics.get_joint_state("shaft") == JointState(0, 0)
        physics.set_joint_effort("shaft", -.4)
        physics.step(1_000_000)
        assert physics.get_joint_state("shaft").velocity_rad_s == pytest.approx(-.02, abs=1e-10)
        physics.reset()  # observer remains attached across repeated native resets
        assert physics.time_ns == 0
        assert physics.get_joint_state("shaft") == JointState(0, 0)
    finally:
        physics.close()
    physics.close()
    for action in (lambda: physics.time_ns, lambda: physics.reset(),
                   lambda: physics.step(1_000_000),
                   lambda: physics.validate_step(1_000_000),
                   lambda: physics.validate_steps((1_000_000,)),
                   lambda: physics.get_joint_state("shaft"),
                   lambda: physics.validate_joint_effort("shaft", 0),
                   lambda: physics.set_joint_effort("shaft", 0)):
        with pytest.raises(RuntimeError, match="closed"):
            action()


def test_direct_native_server_observes_completed_tick_and_reset(native, sdf_path):
    # Isolate the C++/native boundary before the Python value-type consumer.
    world = native.World(str(sdf_path), {"shaft": ("rotor", "shaft")})
    try:
        assert world.time_ns == 0
        assert world.get_joint_state("shaft") == (0, 0)
        world.set_joint_effort("shaft", .4)
        world.validate_step(1_000_000)
        world.validate_steps((1_000_000, 2_000_000))
        world.validate_joint_effort("shaft", -.4)
        assert world.time_ns == 0 and world.get_joint_state("shaft") == (0, 0)
        world.step(1_000_000)
        position, velocity = world.get_joint_state("shaft")
        assert world.time_ns == 1_000_000
        assert velocity == pytest.approx(.02, abs=1e-10)
        assert 0 < position <= .00002 + 1e-12
        world.reset()
        assert world.time_ns == 0
        assert world.get_joint_state("shaft") == (0, 0)
    finally:
        world.close()


@pytest.mark.parametrize("kwargs", [dict(joint_type="fixed"), dict(static="true"),
    dict(step="0.0000000001"), dict(effort="0"),
    dict(plugin='<plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands"/>')])
def test_sdf_admission_rejects_unsupported_native_profile(native, tmp_path, kwargs):
    path = tmp_path / "unsupported.sdf"
    path.write_text(rotor_sdf(**kwargs))
    with pytest.raises(ValueError):
        GazeboPhysics(path, joints={"shaft": ("rotor", "shaft")})


def test_missing_joint_and_bad_sdf_rejected_before_server(native, tmp_path, sdf_path):
    with pytest.raises(ValueError, match="revolute"):
        GazeboPhysics(sdf_path, joints={"shaft": ("rotor", "missing")})
    path = tmp_path / "broken.sdf"
    path.write_text("<sdf>")
    with pytest.raises(ValueError, match="SDF admission"):
        GazeboPhysics(path, joints={"shaft": ("rotor", "shaft")})


@pytest.mark.parametrize("bindings", [dict(joints={"shaft": ("rotor", "shaft")}),
                                      dict(bodies={"body": ("rotor", "body")})])
def test_failed_native_initialization_releases_server_before_next_owner(native, tmp_path,
                                                                        sdf_path, bindings):
    path = tmp_path / "missing-engine.sdf"
    # Physics.cc:915-920 forwards this native engine selection. SDF admission
    # succeeds; missing engine leaves completed joint data unavailable at time0.
    path.write_text(rotor_sdf().replace(
        'name="gz::sim::systems::Physics"/>',
        'name="gz::sim::systems::Physics"><engine>'
        '<filename>dm-test-missing-physics-engine</filename></engine></plugin>'))
    with pytest.raises(RuntimeError, match="completed (joint sample unavailable|body pose nonfinite)"):
        GazeboPhysics(path, **bindings)
    with GazeboPhysics(sdf_path, joints={"shaft": ("rotor", "shaft")}) as physics:
        physics.step(1_000_000)
        assert physics.time_ns == 1_000_000


def test_invalid_commands_do_not_advance_or_replace_drive(native, sdf_path):
    with GazeboPhysics(sdf_path, joints={"shaft": ("rotor", "shaft")}) as physics:
        physics.set_joint_effort("shaft", .4)
        for dt in (True, 1.0, 0, -1, 1, 10_001_000_000, 1 << 63):
            for check in (physics.validate_step, physics.step):
                with pytest.raises(ValueError):
                    check(dt)
        for effort in (True, "0", float("nan"), float("inf"), 10**1000, 10.01):
            for check in (physics.validate_joint_effort, physics.set_joint_effort):
                with pytest.raises(ValueError):
                    check("shaft", effort)
        for check in (physics.validate_joint_effort, physics.set_joint_effort):
            with pytest.raises(ValueError):
                check("missing", 0)
        with pytest.raises(ValueError):
            physics.get_joint_state("missing")
        assert physics.time_ns == 0
        assert physics.get_joint_state("shaft") == JointState(0, 0)
        physics.step(1_000_000)
        assert physics.get_joint_state("shaft").velocity_rad_s == pytest.approx(.02, abs=1e-10)


def test_accepted_preflight_does_not_change_time_or_held_effort(native, sdf_path):
    with GazeboPhysics(sdf_path, joints={"shaft": ("rotor", "shaft")}) as physics:
        physics.set_joint_effort("shaft", .4)
        physics.validate_step(10_000_000_000)  # admitted upper tick-count bound
        physics.validate_steps((3_000_000, 7_000_000))
        for intervals in ((), [1_000_000], (True,), (1 << 63,), (1_000_000, 1)):
            with pytest.raises(ValueError):
                physics.validate_steps(intervals)
        physics.validate_joint_effort("shaft", -10)  # inclusive SDF limit
        assert physics.time_ns == 0
        assert physics.get_joint_state("shaft") == JointState(0, 0)
        physics.step(1_000_000)
        assert physics.get_joint_state("shaft").velocity_rad_s == pytest.approx(.02, abs=1e-10)


def test_integer_ns_does_not_round_to_python_timedelta_microseconds(native, tmp_path):
    path = tmp_path / "nanosecond.sdf"
    path.write_text(rotor_sdf(step="0.000001001"))
    with GazeboPhysics(path, joints={"shaft": ("rotor", "shaft")}) as physics:
        assert physics.timestep_ns == 1001
        physics.step(3003)
        assert physics.time_ns == 3003
        physics.reset()
        assert physics.time_ns == 0


def test_native_float_duration_cast_is_the_timestep_oracle(native, tmp_path):
    # Gazebo 8.15 SimulationRunner.cc:173-177 casts double seconds to native
    # duration; .001001 is slightly below the exact decimal in binary64.
    path = tmp_path / "fractional.sdf"
    path.write_text(rotor_sdf(step="0.001001"))
    with GazeboPhysics(path, joints={"shaft": ("rotor", "shaft")}) as physics:
        assert physics.timestep_ns == 1_000_999
        physics.step(2_001_998)
        assert physics.time_ns == 2_001_998


@pytest.mark.parametrize("before, after, message", [
    ('<model name="rotor">', '<model name="rotor"><plugin filename="absent" name="absent"/>', "model plugins"),
    ('</inertial></link>', '</inertial><sensor name="imu" type="imu"/></link>', "link sensors"),
    ('</joint>', '<sensor name="wrench" type="force_torque"/></joint>', "joint sensors"),
    ('</inertial></link>', '</inertial><visual name="v"><geometry><box><size>1 1 1</size></box></geometry><plugin filename="absent" name="absent"/></visual></link>', "visual plugins"),
    ('</physics>', '</physics><physics name="other" type="ignored" default="true"><max_step_size>.002</max_step_size></physics>', "one physics profile"),
    ('</world>', '<actor name="actor"/></world>', "no actors"),
])
def test_nested_command_owners_rejected_before_native_plugin_load(native, tmp_path,
                                                                  before, after, message):
    path = tmp_path / "other-owner.sdf"
    path.write_text(rotor_sdf().replace(before, after))
    with pytest.raises(ValueError, match=message):
        GazeboPhysics(path, joints={"shaft": ("rotor", "shaft")})


def test_all_bound_joint_efforts_are_written_each_native_tick(native, tmp_path):
    xml = rotor_sdf()
    # Independent duplicate body has the same stated inertia, without coupling.
    model = xml[xml.index('<model name="rotor">'):xml.index('</model>') + 8]
    path = tmp_path / "two-rotors.sdf"
    path.write_text(xml.replace('</world>', model.replace('name="rotor"', 'name="other"') + '</world>'))
    with GazeboPhysics(path, joints={"a": ("rotor", "shaft"),
                                    "b": ("other", "shaft")}) as physics:
        physics.set_joint_effort("a", .4)
        physics.set_joint_effort("b", -.2)
        physics.step(10_000_000)
        assert physics.get_joint_state("a").velocity_rad_s == pytest.approx(.2, abs=1e-10)
        assert physics.get_joint_state("b").velocity_rad_s == pytest.approx(-.1, abs=1e-10)
        physics.reset()
        assert physics.get_joint_state("a") == physics.get_joint_state("b") == JointState(0, 0)


def test_native_gil_release_cannot_close_an_active_server(native, sdf_path):
    physics = GazeboPhysics(sdf_path, joints={"shaft": ("rotor", "shaft")})
    entered, done = threading.Event(), threading.Event()
    errors = []

    def advance():
        entered.set()
        try:
            physics.step(10_000_000_000)
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=advance, daemon=True)
    try:
        worker.start()
        assert entered.wait(2)
        observed_busy = False
        for _ in range(50_000):
            if done.is_set():
                break
            try:
                physics.time_ns
            except RuntimeError as exc:
                assert "already in progress" in str(exc)
                observed_busy = True
                break
        assert observed_busy, "native parallel PostUpdate must yield an active operation"
        with pytest.raises(RuntimeError, match="already in progress"):
            physics.close()
        with pytest.raises(RuntimeError, match="already in progress"):
            physics.validate_step(1_000_000)
        with pytest.raises(RuntimeError, match="already in progress"):
            physics.validate_steps((1_000_000,))
        with pytest.raises(RuntimeError, match="already in progress"):
            physics.validate_joint_effort("shaft", 0)
        with pytest.raises(RuntimeError, match="already in progress"):
            physics.get_body_pose("missing")
        with pytest.raises(RuntimeError, match="already in progress"):
            physics.get_imu("missing")
        worker.join(10)
        assert not worker.is_alive() and not errors
        assert physics.time_ns == 10_000_000_000
    finally:
        worker.join(10)
        physics.close()


class CanHost(VirtualCanDevice):
    def __init__(self, bus):
        self.frames = []
        super().__init__(bus, "host")

    def receive(self, frame):
        self.frames.append(frame)


def motor_rig(tmp_path, *, timeouts=(100_000_000,) * 4, observe_bodies=False,
              observe_imus=False, independent_buses=False, composed_buses=False, session=None):
    xml = rotor_sdf()
    if observe_imus:
        xml = xml.replace('</inertial></link>',
            '</inertial><sensor name="imu" type="imu"><update_rate>0</update_rate>'
            '<imu/></sensor></link>')
    model = xml[xml.index('<model name="rotor">'):xml.index('</model>') + 8]
    models = "".join(model.replace('name="rotor"', f'name="rotor{i}"')
                     for i in range(1, 5))
    path = tmp_path / "motor-joints.sdf"
    path.write_text(xml.replace(model, models))
    physics = GazeboPhysics(path, joints={f"j{i}": (f"rotor{i}", "shaft")
                                          for i in range(1, 5)}, bodies=(
        {f"b{i}": (f"rotor{i}", "body") for i in range(1, 5)}
        if observe_bodies else None), imus=(
        {f"i{i}": (f"rotor{i}", "body", "imu") for i in range(1, 5)}
        if observe_imus else None))
    buses = (CanBus(), CanBus()) if independent_buses else (CanBus(),)
    hosts = tuple(CanHost(bus) for bus in buses)
    devices = tuple(M3508Device(buses[(i - 1) // 2] if independent_buses else buses[0],
                               (i - 1) % 2 + 1 if independent_buses else i,
                               command_timeout_ns=timeout)
                    for i, timeout in enumerate(timeouts, 1))
    try:
        plant = M3508JointPlant(physics, {device: f"j{i}" for i, device
                                         in enumerate(devices, 1)}, gear_ratio=2)
        coordinator = simulation_for({"world": plant}, session=session,
            buses=buses if not independent_buses or composed_buses else ())
        return (physics, buses if independent_buses else buses[0],
                hosts if independent_buses else hosts[0], devices, plant, coordinator)
    except BaseException:
        for device in devices:
            device.close()
        physics.close()
        raise


def observe_motor_advances(monkeypatch, physics):
    calls = []
    effort, step = physics.set_joint_effort, physics.step
    def set_effort(name, value):
        calls.append(("effort", name, value))
        effort(name, value)
    def advance(dt_ns):
        calls.append(("step", dt_ns))
        step(dt_ns)
    monkeypatch.setattr(physics, "set_joint_effort", set_effort)
    monkeypatch.setattr(physics, "step", advance)
    return calls


def test_actual_can_four_joint_gear_and_completed_feedback(native, tmp_path, monkeypatch):
    physics, bus, host, devices, plant, coordinator = motor_rig(tmp_path, session=Session(1, 7))
    calls = observe_motor_advances(monkeypatch, physics)
    try:
        # Native epoch remains zero even with a non-grid logical session origin.
        coordinator.start()
        host.emit(CanFrame(0x200, data=bytes.fromhex("1000 f000 2000 e000"),
                           timestamp_ns=7))
        result = coordinator.step(10_000_000).diagnostics["components"]["world"]
        assert calls == [("effort", "j1", .2), ("effort", "j2", -.2),
                         ("effort", "j3", .4), ("effort", "j4", -.4),
                         ("step", 10_000_000)]
        # +/-5/10A, kt=.02, gear2, Izz=.02 -> +/-10/20rad/s².
        # Continuous theta is bounded by one 1ms native integration tick.
        for device, name, velocity, position in zip(devices,
                ("j1", "j2", "j3", "j4"), (.1, -.1, .2, -.2),
                (.0005, -.0005, .001, -.001)):
            state = physics.get_joint_state(name)
            assert state.velocity_rad_s == pytest.approx(velocity, abs=1e-10)
            assert state.position_rad == pytest.approx(position, abs=abs(velocity) * .0011)
            assert device.get_rotor_state() == pytest.approx(
                (2 * state.position_rad, 2 * velocity), abs=1e-10)
        # Independent C620 literals for completed native semi-implicit samples;
        # no production encoder/rotor helper produces these expectations.
        assert [(frame.can_id, frame.timestamp_ns, frame.data) for frame in host.frames] == [
            (0x201, 10_000_007, bytes.fromhex("0001 0002 1000 19 00")),
            (0x202, 10_000_007, bytes.fromhex("1ffe fffe f000 19 00")),
            (0x203, 10_000_007, bytes.fromhex("0002 0004 2000 19 00")),
            (0x204, 10_000_007, bytes.fromhex("1ffd fffc e000 19 00"))]
        assert result.status == "ok" and result.reached_time_ns == 10_000_007
        assert physics.time_ns == 10_000_000
        assert plant.t_sim_ns == bus.t_sim_ns == coordinator.t_sim_ns == 10_000_007
        coordinator.close()
        assert plant.closed and all(device.closed for device in devices)
        physics, bus, host, devices, plant, coordinator = motor_rig(
            tmp_path, session=Session(2, 17))
        coordinator.start()
        assert physics.time_ns == 0 and plant.t_sim_ns == 17
        assert all(device.get_rotor_state() == (0, 0) for device in devices)
        coordinator.step(1_000_000)
        assert all(physics.get_joint_state(name) == JointState(0, 0)
                   for name in ("j1", "j2", "j3", "j4"))
    finally:
        coordinator.close()
    assert plant.closed and all(device.closed for device in devices)
    with pytest.raises(RuntimeError, match="closed"):
        physics.get_joint_state("j1")


def test_actual_shared_expiry_intervals_and_coasting(native, tmp_path, monkeypatch):
    physics, _, host, devices, _, coordinator = motor_rig(tmp_path,
        timeouts=(3_000_000, 7_000_000, 10_000_000, 3_000_000))
    calls = observe_motor_advances(monkeypatch, physics)
    try:
        coordinator.start()
        host.emit(CanFrame(0x200, data=bytes.fromhex("1000 f000 2000 e000")))
        coordinator.step(10_000_000)
        assert calls == [
            ("effort", "j1", .2), ("effort", "j2", -.2),
            ("effort", "j3", .4), ("effort", "j4", -.4), ("step", 3_000_000),
            ("effort", "j1", 0), ("effort", "j2", -.2),
            ("effort", "j3", .4), ("effort", "j4", 0), ("step", 4_000_000),
            ("effort", "j1", 0), ("effort", "j2", 0),
            ("effort", "j3", .4), ("effort", "j4", 0), ("step", 3_000_000)]
        assert [device.get_rotor_state()[1] for device in devices] == pytest.approx(
            [.06, -.14, .4, -.12], abs=1e-10)
        assert all(device.current_raw == 0 for device in devices)
        assert physics.time_ns == 10_000_000
        assert [frame.data for frame in host.frames] == [
            bytes.fromhex("0000 0001 0000 19 00"), bytes.fromhex("1ffe ffff 0000 19 00"),
            bytes.fromhex("0002 0004 0000 19 00"), bytes.fromhex("1ffe ffff 0000 19 00")]
    finally:
        coordinator.close()


@pytest.mark.parametrize("composed", [False, True], ids=["manual", "composed"])
def test_actual_multibus_repeated_ids_share_one_world(native, tmp_path, monkeypatch,
                                                     composed):
    physics, buses, hosts, devices, plant, coordinator = motor_rig(tmp_path,
        independent_buses=True, composed_buses=composed,
        timeouts=(3_000_000, 3_000_000, 7_000_000, 7_000_000), session=Session(1, 7))
    calls = observe_motor_advances(monkeypatch, physics)
    deliveries = []
    for index, bus in enumerate(buses):
        step = bus.step
        def observe_delivery(t_sim_ns, *, max_dispatch=None, index=index, step=step):
            count = step(t_sim_ns, max_dispatch=max_dispatch)
            if t_sim_ns == 7 and count:
                deliveries.append((index, count,
                                   tuple(device.current_raw for device in devices)))
            return count
        monkeypatch.setattr(bus, "step", observe_delivery)
    resets = []
    reset = physics.reset
    def reset_native():
        resets.append(physics.time_ns)
        reset()
    monkeypatch.setattr(physics, "reset", reset_native)
    try:
        coordinator.start()
        # Manual delivery retains the lower shared-plant consumer. The composed
        # path delegates both borrowed bus clocks to the sole Simulation.
        if not composed:
            for bus in buses:
                bus.reset(7)
        hosts[0].emit(CanFrame(0x200, data=bytes.fromhex("1000 f000 0000 0000"),
                               timestamp_ns=7))
        if not composed:
            assert buses[0].step(7) == 1
        hosts[1].emit(CanFrame(0x200, data=bytes.fromhex("f000 1000 0000 0000"),
                               timestamp_ns=7))
        if not composed:
            assert buses[1].step(7) == 1
        else:
            assert all(device.current_raw == 0 for device in devices)
            assert all(bus.t_sim_ns == 7 and bus.pending == 1 for bus in buses)
        assert [device.motor_id for device in devices] == [1, 2, 1, 2]
        assert resets == [0] and physics.time_ns == 0
        result = coordinator.step(10_000_000).diagnostics["components"]["world"]
        assert deliveries == [(0, 1, (4096, -4096, 0, 0)),
                              (1, 1, (4096, -4096, -4096, 4096))]
        assert calls == [
            ("effort", "j1", .2), ("effort", "j2", -.2),
            ("effort", "j3", -.2), ("effort", "j4", .2), ("step", 3_000_000),
            ("effort", "j1", 0), ("effort", "j2", 0),
            ("effort", "j3", -.2), ("effort", "j4", .2), ("step", 4_000_000),
            ("effort", "j1", 0), ("effort", "j2", 0),
            ("effort", "j3", 0), ("effort", "j4", 0), ("step", 3_000_000)]
        # +/-5A, kt=.02, gear2, Izz=.02: alpha=+/-10rad/s² until
        # each bus's 3/7ms expiry, then constant velocity to 10ms. Continuous
        # positions allow the same one-tick integration bound as the lower gate.
        for device, name, velocity, position in zip(devices,
                ("j1", "j2", "j3", "j4"), (.03, -.03, -.07, .07),
                (.000255, -.000255, -.000455, .000455)):
            state = physics.get_joint_state(name)
            assert state.velocity_rad_s == pytest.approx(velocity, abs=1e-10)
            assert state.position_rad == pytest.approx(position, abs=abs(velocity) * .0011)
            assert device.get_rotor_state() == pytest.approx(
                (2 * state.position_rad, 2 * velocity), abs=1e-10)
        assert physics.time_ns == 10_000_000 and resets == [0]
        assert result.status == "ok" and result.reached_time_ns == 10_000_007
        assert result.diagnostics == {"physics_segments": 3}
        assert plant.t_sim_ns == coordinator.t_sim_ns == 10_000_007
        assert all(device.current_raw == 0 and device.t_sim_ns == 10_000_007
                   for device in devices)
        if not composed:
            assert all(bus.t_sim_ns == 7 and bus.pending == 2 for bus in buses)
            assert all(host.frames == [] for host in hosts)
            for bus in buses:
                assert bus.step(10_000_007) == 2
        # Independent completed C620 literals: duplicate IDs stay on their
        # own bus, with distinct signed angles and no active current at expiry.
        assert [[(frame.can_id, frame.timestamp_ns, frame.data) for frame in host.frames]
                for host in hosts] == [
            [(0x201, 10_000_007, bytes.fromhex("0000 0001 0000 19 00")),
             (0x202, 10_000_007, bytes.fromhex("1fff ffff 0000 19 00"))],
            [(0x201, 10_000_007, bytes.fromhex("1ffe ffff 0000 19 00")),
             (0x202, 10_000_007, bytes.fromhex("0001 0001 0000 19 00"))]]
    finally:
        coordinator.close()
    assert plant.closed and all(device.closed for device in devices)
    assert all(bus.t_sim_ns == 10_000_007 and bus.pending == 0 for bus in buses)
    with pytest.raises(RuntimeError, match="closed"):
        physics.get_joint_state("j1")


@pytest.mark.parametrize("dt_ns, timeout", [
    (10_000_001, 100_000_000),  # unsupported whole dt
    (10_000_000, 1_500_000),   # aligned total, unsupported interior expiry
    (10_001_000_000, 100_000_000),  # whole operation exceeds native tick budget
])
def test_motor_step_preflight_rejects_before_any_control_or_native_advance(
        native, tmp_path, monkeypatch, dt_ns, timeout):
    physics, bus, host, devices, plant, coordinator = motor_rig(
        tmp_path, timeouts=(timeout,) * 4, session=Session(1, 7))
    calls = observe_motor_advances(monkeypatch, physics)
    try:
        coordinator.start()
        # An already-held valid command must survive failed plant preflight.
        physics.set_joint_effort("j1", .4)
        calls.clear()
        host.emit(CanFrame(0x200, data=bytes.fromhex("1000 0000 0000 0000"),
                           timestamp_ns=7))
        with pytest.raises(ValueError, match="whole SDF ticks"):
            coordinator.step(dt_ns)
        assert calls == [] and physics.time_ns == 0 and bus.pending == 0
        assert plant.failed and coordinator.state == "failed"
        assert plant.t_sim_ns == coordinator.t_sim_ns == bus.t_sim_ns == 7
        assert all(device.t_sim_ns == 7 and device.get_rotor_state() == (0, 0)
                   for device in devices)
        assert host.frames == []
        with pytest.raises(RuntimeError, match="paused"):
            coordinator.step(1_000_000)
        # Independent backend remains usable: no failed transaction wrote torque.
        physics.step(1_000_000)
        assert physics.get_joint_state("j1").velocity_rad_s == pytest.approx(.02, abs=1e-10)
        with pytest.raises(RuntimeError, match="paused"):
            coordinator.reset()  # Failed executions cannot replay partial effects.
        coordinator.close()
        physics, bus, host, devices, plant, coordinator = motor_rig(
            tmp_path, session=Session(2, 17))
        coordinator.start()
        calls.clear()
        coordinator.step(1_000_000)
        assert physics.time_ns == 1_000_000 and not plant.failed
        assert all(device.get_rotor_state() == (0, 0) for device in devices)
    finally:
        coordinator.close()


def test_maximum_motor_effort_admitted_before_ownership_transfer(native, tmp_path, monkeypatch):
    path = tmp_path / "motor-limit.sdf"
    path.write_text(rotor_sdf(effort="0.5"))
    with GazeboPhysics(path, joints={"shaft": ("rotor", "shaft")}) as physics:
        device = M3508Device(CanBus())
        calls = observe_motor_advances(monkeypatch, physics)
        with pytest.raises(ValueError, match="out-of-limit"):
            M3508JointPlant(physics, {device: "shaft"}, gear_ratio=2)  # +/- .8Nm
        assert calls == [] and physics.time_ns == 0 and not device.closed
        # A caller retains ownership after rejection; the inclusive full-scale
        # +/-20A envelope at gear1 (.4Nm) is admitted without executing physics.
        plant = M3508JointPlant(physics, {device: "shaft"}, gear_ratio=1)
        assert calls == [] and physics.time_ns == 0
        plant.close()
        assert device.closed


def test_native_body_pose_is_completed_world_link_frame_and_resets(native, tmp_path):
    path = tmp_path / "placed-rotor.sdf"
    path.write_text(rotor_sdf().replace('<model name="rotor">',
        '<model name="rotor"><pose>1 2 3 0 0 .3</pose>'))
    world = native.World(str(path), {"shaft": ("rotor", "shaft")},
                         {"body": ("rotor", "body")})
    try:
        initial = [1, 2, 3, math.cos(.15), 0, 0, math.sin(.15)]
        assert world.time_ns == 0
        assert world.get_body_pose("body") == pytest.approx(initial, abs=1e-12)
        world.set_joint_effort("shaft", .4)
        world.step(100_000_000)
        pose = world.get_body_pose("body")
        assert pose[:3] == pytest.approx((1, 2, 3), abs=1e-12)
        # Independent alpha20 gives theta=.1 plus <=one tick of integration
        # error. Quaternion is the world link yaw .3 + theta, in wxyz order.
        assert 2 * math.atan2(pose[6], pose[3]) == pytest.approx(.4, abs=.0021)
        assert pose[4:6] == pytest.approx((0, 0), abs=1e-12)
        assert sum(q*q for q in pose[3:]) == pytest.approx(1, abs=1e-12)
        assert world.get_body_pose("body") == pose and world.time_ns == 100_000_000
        world.reset()
        assert world.time_ns == 0
        assert world.get_body_pose("body") == pytest.approx(initial, abs=1e-12)
        world.step(1_000_000)
        assert world.get_body_pose("body") == pytest.approx(initial, abs=1e-12)
    finally:
        world.close()
    with pytest.raises(RuntimeError, match="closed"):
        world.get_body_pose("body")


def test_native_body_pose_binding_admission_precedes_engine_creation(native, tmp_path):
    path = tmp_path / "body-admission.sdf"
    path.write_text(rotor_sdf().replace('name="gz::sim::systems::Physics"/>',
        'name="gz::sim::systems::Physics"><engine>'
        '<filename>dm-test-missing-physics-engine</filename></engine></plugin>'))
    for bindings, message in (({"b": ("rotor", "missing")}, "dynamic model link"),
            ({"b": ("missing", "body")}, "dynamic model link"),
            ({"a": ("rotor", "body"), "b": ("rotor", "body")}, "duplicate")):
        with pytest.raises(ValueError, match=message):
            native.World(str(path), {}, bindings)
    path.write_text(rotor_sdf(static="true"))
    with pytest.raises(ValueError, match="dynamic model link"):
        native.World(str(path), {}, {"b": ("rotor", "body")})


def test_python_body_only_observation_and_public_value_type(native, sdf_path):
    with GazeboPhysics(sdf_path, bodies={"rotor": ("rotor", "body")}) as physics:
        assert physics.time_ns == 0
        pose = physics.get_body_pose("rotor")
        assert pose == BodyPose((0, 0, 0), (1, 0, 0, 0))
        with pytest.raises(FrozenInstanceError):
            pose.position_m = (1, 0, 0)
        physics.step(1_000_000)
        assert physics.get_body_pose("rotor") == pose
        with pytest.raises(ValueError, match="unbound joint"):
            physics.get_joint_state("shaft")
        with pytest.raises(ValueError, match="unbound body"):
            physics.get_body_pose("missing")
        with pytest.raises(ValueError, match="string"):
            physics.get_body_pose(None)
        physics.reset()
        assert physics.time_ns == 0 and physics.get_body_pose("rotor") == pose
    with pytest.raises(RuntimeError, match="closed"):
        physics.get_body_pose("rotor")


def test_python_body_only_free_fall_publishes_target_translation(native, tmp_path):
    xml = rotor_sdf().replace('<gravity>0 0 0</gravity>', '<gravity>0 0 -10</gravity>')
    joint = xml[xml.index('<joint name="shaft"'):xml.index('</joint>') + 8]
    path = tmp_path / "free-body.sdf"
    path.write_text(xml.replace(joint, '').replace('<model name="rotor">',
        '<model name="rotor"><pose>1 2 3 0 0 .3</pose>'))
    with GazeboPhysics(path, bodies={"body": ("rotor", "body")}) as physics:
        assert physics.get_body_pose("body").position_m == (1, 2, 3)
        physics.step(100_000_000)
        pose = physics.get_body_pose("body")
        # z=3 - .5*10*.1², with at most one1ms integration tick of error.
        assert pose.position_m == pytest.approx((1, 2, 2.95), abs=.0011)
        assert pose.quaternion_wxyz == pytest.approx(
            (math.cos(.15), 0, 0, math.sin(.15)), abs=1e-12)
        assert physics.time_ns == 100_000_000 and physics.get_body_pose("body") == pose
        physics.reset()
        assert physics.time_ns == 0 and physics.get_body_pose("body").position_m == (1, 2, 3)


def test_python_body_binding_types_reject_before_native_acquisition(native, monkeypatch):
    calls = []
    def acquire(*args):
        calls.append(args)
        raise AssertionError("invalid Python binding reached native acquisition")
    monkeypatch.setattr(native, "World", acquire)
    for bodies in ([], {"b": "link"}, {"": ("rotor", "body")},
                   {"b": ("rotor", "body\0")},
                   {"a": ("rotor", "body"), "b": ("rotor", "body")}):
        with pytest.raises(ValueError):
            GazeboPhysics("unused.sdf", bodies=bodies)
    assert calls == []


def test_shared_motor_consumer_reads_completed_body_pose(native, tmp_path):
    physics, _, host, _, _, coordinator = motor_rig(tmp_path, observe_bodies=True)
    try:
        coordinator.start()
        host.emit(CanFrame(0x200, data=bytes.fromhex("1000 f000 0000 0000")))
        coordinator.step(100_000_000)
        # 5A*.02*gear2/Izz.02=10rad/s² -> theta +/- .05rad in100ms.
        for name, yaw in (("b1", .05), ("b2", -.05), ("b3", 0), ("b4", 0)):
            pose = physics.get_body_pose(name)
            assert pose.position_m == pytest.approx((0, 0, 0), abs=1e-12)
            w, x, y, z = pose.quaternion_wxyz
            assert 2 * math.atan2(z, w) == pytest.approx(yaw, abs=.0011)
            assert (x, y) == pytest.approx((0, 0), abs=1e-12)
            assert physics.get_body_pose(name) == pose
        assert physics.time_ns == coordinator.t_sim_ns == 100_000_000
        coordinator.close()
        physics, _, host, _, _, coordinator = motor_rig(
            tmp_path, observe_bodies=True, session=Session(2, 7))
        coordinator.start()
        assert physics.time_ns == 0
        assert physics.get_body_pose("b1") == BodyPose((0, 0, 0), (1, 0, 0, 0))
    finally:
        coordinator.close()


def test_native_imu_generates_stationary_and_rotated_proper_acceleration(native, tmp_path):
    for pitch, expected in ((0, (0, 0, 10)), (math.pi/2, (-10, 0, 0))):
        path = tmp_path / "stationary-imu.sdf"
        path.write_text(imu_sdf(pose=f"0 0 0 0 {pitch} 0"))
        world = native.World(str(path), {}, {}, {"imu": ("rotor", "body", "imu")})
        try:
            assert world.time_ns == 0
            assert world.get_imu("imu") == pytest.approx((0, 0, 0, *expected), abs=1e-12)
            world.step(2_000_000)
            # Every native tick reseeds coordinate acceleration. Gravity must
            # not accumulate in the native sensor's in-place generated fields.
            assert world.get_imu("imu") == pytest.approx((0, 0, 0, *expected), abs=1e-12)
            before = world.get_imu("imu")
            assert world.get_imu("imu") == before and world.time_ns == 2_000_000
            world.reset()
            assert world.time_ns == 0
            assert world.get_imu("imu") == pytest.approx((0, 0, 0, *expected), abs=1e-12)
        finally:
            world.close()


def test_native_imu_free_fall_is_zero_proper_acceleration(native, tmp_path):
    xml = imu_sdf()
    joint = xml[xml.index('<joint name="shaft"'):xml.index('</joint>') + 8]
    path = tmp_path / "free-fall-imu.sdf"
    path.write_text(xml.replace(joint, ''))
    world = native.World(str(path), {}, {}, {"imu": ("rotor", "body", "imu")})
    try:
        world.step(10_000_000)
        assert world.time_ns == 10_000_000
        # Physical free fall: a=g, so accelerometer specific force is zero.
        assert world.get_imu("imu") == pytest.approx((0,)*6, abs=1e-12)
    finally:
        world.close()


def test_native_imu_torque_and_offset_use_completed_sensor_frame(native, tmp_path):
    path = tmp_path / "offset-imu.sdf"
    path.write_text(imu_sdf(pose=f".25 0 0 {math.pi/2} 0 0"))
    world = native.World(str(path), {"shaft": ("rotor", "shaft")}, {},
                         {"imu": ("rotor", "body", "imu")})
    try:
        world.set_joint_effort("shaft", .4)
        world.step(100_000_000)
        # alpha=tau/Izz=20, omega=2rad/s. At radius .25m, local x centripetal
        # acceleration=-1 and y tangential=5. Sensor roll pi/2 maps gyro z->y
        # and proper body (x,y,z)->sensor (x,z,-y), including support +10z.
        assert world.get_imu("imu") == pytest.approx((0, 2, 0, -1, 10, -5), abs=.01)
        path.unlink()  # reset must use the admitted DOM, not reread the file
        world.reset()
        assert world.time_ns == 0
        assert world.get_joint_state("shaft") == (0, 0)
        assert world.get_imu("imu") == pytest.approx((0, 0, 0, 0, 10, 0), abs=1e-12)
        world.step(1_000_000)
        assert world.get_imu("imu") == pytest.approx((0, 0, 0, 0, 10, 0), abs=1e-12)
    finally:
        world.close()


def test_native_imu_missing_engine_never_accepts_default_components(native, tmp_path):
    path = tmp_path / "missing-engine-imu.sdf"
    path.write_text(imu_sdf().replace('name="gz::sim::systems::Physics"/>',
        'name="gz::sim::systems::Physics"><engine>'
        '<filename>dm-test-missing-physics-engine</filename></engine></plugin>'))
    with pytest.raises(RuntimeError, match="completed IMU kinematics unavailable"):
        native.World(str(path), {}, {}, {"imu": ("rotor", "body", "imu")})
    path.write_text(imu_sdf())
    world = native.World(str(path), {}, {}, {"imu": ("rotor", "body", "imu")})
    try:
        assert world.get_imu("imu") == pytest.approx((0, 0, 0, 0, 0, 10), abs=1e-12)
    finally:
        world.close()


def test_native_imu_admission_precedes_engine_creation(native, tmp_path):
    path = tmp_path / "imu-admission.sdf"
    xml = imu_sdf().replace('name="gz::sim::systems::Physics"/>',
        'name="gz::sim::systems::Physics"><engine>'
        '<filename>dm-test-missing-physics-engine</filename></engine></plugin>')
    path.write_text(xml)
    for bindings, message in (({"i": ("rotor", "body", "missing")}, "link IMU"),
            ({"i": ("missing", "body", "imu")}, "link IMU"),
            ({"a": ("rotor", "body", "imu"), "b": ("rotor", "body", "imu")}, "duplicate")):
        with pytest.raises(ValueError, match=message):
            native.World(str(path), {}, {}, bindings)
    with pytest.raises(ValueError, match="explicit IMU bindings"):
        native.World(str(path), {"s": ("rotor", "shaft")})
    for invalid, message in ((xml.replace('<update_rate>0', '<update_rate>100'), "rate zero"),
            (xml.replace('<imu/>', '<imu><angular_velocity><x><noise type="gaussian">'
                         '<stddev>.1</stddev></noise></x></angular_velocity></imu>'), "noise"),
            (xml.replace('<static>false', '<static>true'), "dynamic IMU")):
        path.write_text(invalid)
        with pytest.raises(ValueError, match=message):
            native.World(str(path), {}, {}, {"i": ("rotor", "body", "imu")})


def test_python_imu_only_observation_and_binding_admission(native, tmp_path, monkeypatch):
    path = tmp_path / "python-imu.sdf"
    path.write_text(imu_sdf())
    with GazeboPhysics(path, imus={"imu": ("rotor", "body", "imu")}) as physics:
        initial = physics.get_imu("imu")
        assert initial == ImuSample((0, 0, 0), (0, 0, 10)) and physics.time_ns == 0
        with pytest.raises(FrozenInstanceError):
            initial.accel_m_s2 = (1, 0, 0)
        physics.step(2_000_000)
        assert physics.get_imu("imu") == initial and physics.time_ns == 2_000_000
        with pytest.raises(ValueError, match="unbound IMU"):
            physics.get_imu("missing")
        with pytest.raises(ValueError, match="string"):
            physics.get_imu(None)
        physics.reset()
        assert physics.time_ns == 0 and physics.get_imu("imu") == initial
    with pytest.raises(RuntimeError, match="closed"):
        physics.get_imu("imu")
    with pytest.raises(RuntimeError, match="closed"):
        physics.reset()
    calls = []
    def acquire(*args):
        calls.append(args)
        raise AssertionError("invalid IMU binding reached native acquisition")
    monkeypatch.setattr(native, "World", acquire)
    for bindings in ([], {"i": ("rotor", "body")}, {"i": ("rotor", "body", "")},
                     {"i": ("rotor", "body", "imu\0")},
                     {"i": ("rotor", "body", "imu"), "j": ("rotor", "body", "imu")}):
        with pytest.raises(ValueError):
            GazeboPhysics("unused.sdf", imus=bindings)
    assert calls == []


def test_shared_motor_consumer_reads_generated_imu_and_existing_bmi_units(native, tmp_path):
    physics, _, host, _, _, coordinator = motor_rig(tmp_path, observe_imus=True)
    try:
        coordinator.start()
        host.emit(CanFrame(0x200, data=bytes.fromhex("1000 f000 0000 0000")))
        coordinator.step(100_000_000)
        # 5A*.02*gear2/Izz.02=10rad/s² -> omega +/-1 at100ms. Sensor at joint
        # origin has no translation and this fixture's declared gravity is0.
        for name, velocity in (("i1", 1), ("i2", -1), ("i3", 0), ("i4", 0)):
            sample = physics.get_imu(name)
            assert sample.gyro_rad_s == pytest.approx((0, 0, velocity), abs=1e-12)
            assert sample.accel_m_s2 == pytest.approx((0, 0, 0), abs=1e-12)
            gyro, accel = imu_to_bmi088_units(sample)
            assert gyro == pytest.approx((0, 0, velocity*57.29577951308232), abs=1e-10)
            assert accel == pytest.approx((0, 0, 0), abs=1e-12)
            assert physics.get_imu(name) == sample
        assert physics.time_ns == coordinator.t_sim_ns == 100_000_000
        coordinator.close()
        physics, _, host, _, _, coordinator = motor_rig(
            tmp_path, observe_imus=True, session=Session(2, 7))
        coordinator.start()
        assert physics.time_ns == 0
        assert physics.get_imu("i1") == ImuSample((0, 0, 0), (0, 0, 0))
    finally:
        coordinator.close()


def test_physics_participant_completed_native_world_and_off_grid_rejection(native, tmp_path):
    path = tmp_path / "participant.sdf"
    path.write_text(imu_sdf())
    physics = GazeboPhysics(path, joints={"shaft": ("rotor", "shaft")},
                           bodies={"body": ("rotor", "body")},
                           imus={"imu": ("rotor", "body", "imu")})
    participant = PhysicsParticipant(physics)
    origin = (1 << 63) + 7
    coordinator = simulation_for({"world": participant}, session=Session(1, origin))
    try:
        coordinator.start()
        assert physics.time_ns == 0 and coordinator.t_sim_ns == origin
        physics.set_joint_effort("shaft", .4)
        result = coordinator.step(10_000_000).diagnostics["components"]["world"]
        assert result.reached_time_ns == origin + 10_000_000
        assert result.diagnostics == {"native_time_s": .01}
        assert physics.time_ns == 10_000_000
        # Izz=.02, torque=.4 -> alpha20; stationary origin has proper gravity.
        state = physics.get_joint_state("shaft")
        assert state.velocity_rad_s == pytest.approx(.2, abs=1e-12)
        assert physics.get_imu("imu").gyro_rad_s == pytest.approx((0, 0, .2), abs=1e-12)
        assert physics.get_imu("imu").accel_m_s2 == pytest.approx((0, 0, 10), abs=1e-12)
        assert physics.get_body_pose("body").position_m == (0, 0, 0)
        assert physics.time_ns == 10_000_000  # Observation never advances.
        with pytest.raises(ValueError):
            coordinator.step(1)  # Pure backend admission rejects a partial tick.
        assert coordinator.state == "failed" and participant.failed
        assert coordinator.t_sim_ns == origin + 10_000_000
        assert physics.time_ns == 10_000_000 and physics.get_joint_state("shaft") == state
        with pytest.raises(RuntimeError, match="paused"):
            coordinator.reset()
        coordinator.close()
        physics = GazeboPhysics(path, joints={"shaft": ("rotor", "shaft")},
                               bodies={"body": ("rotor", "body")},
                               imus={"imu": ("rotor", "body", "imu")})
        coordinator = simulation_for({"world": PhysicsParticipant(physics)}, session=Session(2, 17))
        coordinator.start()
        assert physics.time_ns == 0 and coordinator.t_sim_ns == 17
        coordinator.step(1_000_000)
        assert physics.get_joint_state("shaft").velocity_rad_s == 0
    finally:
        coordinator.close()
    with pytest.raises(RuntimeError, match="closed"):
        physics.get_imu("imu")
