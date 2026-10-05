"""Native Gazebo admission and independent rigid-rotor mechanics.

Izz=.02 and torque=.4 imply alpha=20 rad/s². The position bound below permits
one fixed integration tick of error around the continuous solution. No project
rotor helper, motor law, wire codec or MCU clock supplies expected values.
"""

from dataclasses import FrozenInstanceError
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from sim.physics import GazeboPhysics, JointState
from sim.physics.mujoco import JointState as LegacyJointState


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
                   lambda: physics.get_joint_state("shaft"),
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


def test_invalid_commands_do_not_advance_or_replace_drive(native, sdf_path):
    with GazeboPhysics(sdf_path, joints={"shaft": ("rotor", "shaft")}) as physics:
        physics.set_joint_effort("shaft", .4)
        for dt in (True, 1.0, 0, -1, 1, 10_001_000_000, 1 << 63):
            with pytest.raises(ValueError):
                physics.step(dt)
        for effort in (True, "0", float("nan"), float("inf"), 10**1000, 10.01):
            with pytest.raises(ValueError):
                physics.set_joint_effort("shaft", effort)
        with pytest.raises(ValueError):
            physics.set_joint_effort("missing", 0)
        with pytest.raises(ValueError):
            physics.get_joint_state("missing")
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
        worker.join(10)
        assert not worker.is_alive() and not errors
        assert physics.time_ns == 10_000_000_000
    finally:
        worker.join(10)
        physics.close()
