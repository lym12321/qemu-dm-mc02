"""Native render admission followed by actual standard Sensors consumers."""

from dataclasses import FrozenInstanceError
from pathlib import Path
import math
import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
import _dm_gazebo_native as native
from sim.physics import CameraFrame, GazeboPhysics, LidarScan
from test_world import imu_sdf

SCENE = Path(__file__).with_name("rendering.sdf")
CAMERAS = {"front": ("sensors", "body", "camera")}
LIDARS = {"scan": ("sensors", "body", "lidar")}


def test_compile_off():
    assert not native.rendering_enabled
    # Even a nonexistent SDF is rejected before resource, Node or Server load.
    with pytest.raises(ValueError, match="DM_GAZEBO_RENDERING"):
        native.World("unused.sdf", {}, cameras=CAMERAS)


@pytest.mark.parametrize("kind, bindings", [
    ("cameras", []), ("cameras", {"a": ("sensors", "body")}),
    ("lidars", {"a": ("sensors", "body", "")}),
    ("lidars", {"a": ("sensors", "body", "lidar"), "b": ("sensors", "body", "lidar")}),
])
def test_admission_python_bindings(kind, bindings):
    with pytest.raises(ValueError):
        GazeboPhysics("unused.sdf", **{kind: bindings})


@pytest.mark.parametrize("before, after, message", [
    ("<update_rate>20</update_rate>", "<update_rate>0</update_rate>", "1..1000Hz"),
    ("<update_rate>20</update_rate>", "<update_rate>1e-300</update_rate>", "1..1000Hz"),
    ("<update_rate>20</update_rate>", "<update_rate>1001</update_rate>", "1..1000Hz"),
    ("<width>64</width>", "<width>1025</width>", "bounded plain RGB8"),
    ("R8G8B8", "L8", "plain RGB8"),
    ("<near>0.1</near>", "<near>0</near>", "FOV/clip"),
    ("</camera>", '<save enabled="true"><path>/tmp/unused</path></save></camera>', "option: save"),
    ("</camera>", "<triggered>true</triggered></camera>", "option: triggered"),
    ("</camera>", "<lens><intrinsics><fx>10</fx></intrinsics></lens></camera>", "option: lens"),
    ("</camera>", "<distortion><k1>.1</k1></distortion></camera>", "option: distortion"),
    ("</camera>", '<noise type="gaussian"><stddev>.1</stddev></noise></camera>', "no noise"),
    ("<samples>65</samples>", "<samples>4097</samples>", "lidar requires"),
    ("<samples>65</samples>", "<samples>1</samples>", "lidar requires"),
    ("<resolution>1</resolution>", "<resolution>2</resolution>", "unit-resolution"),
    ("<samples>1</samples>", "<samples>2</samples>", "single-row"),
    ("<min>0.1</min>", "<min>-1</min>", "lidar requires"),
    ("/dm_render/image", "relative/image", "absolute native topics"),
    ("/dm_render/image", "/invalid topic", "absolute native topics"),
    ("/dm_render/image", "/double//image", "absolute native topics"),
    ("/dm_render/image", "/tail/image/", "absolute native topics"),
    ("/dm_render/scan", "/dm_render/image", "unique canonical"),
    ("/dm_render/scan", "/dm_render/camera_info", "unique canonical"),
    ("/dm_render/image", "/dm_render/scan/points", "unique canonical"),
    ("/dm_render/image", "/dm_render/scan/set_rate", "unique canonical"),
    ("/dm_render/image", "/sensors/marker", "unique canonical"),
    ("<render_engine>ogre2</render_engine>", "<render_engine>ogre</render_engine>", "Ogre2 Sensors"),
])
def test_admission_native_profile(tmp_path, before, after, message):
    assert native.rendering_enabled
    path = tmp_path / "rejected.sdf"
    path.write_text(SCENE.read_text().replace(before, after))
    with pytest.raises(ValueError, match=message):
        native.World(str(path), {}, cameras=CAMERAS, lidars=LIDARS)


def test_admission_all_sensors_and_binding_identity():
    for cameras, lidars in (({}, LIDARS), (CAMERAS, {}),
                           ({"wrong": ("sensors", "body", "lidar")}, LIDARS),
                           ({"missing": ("sensors", "body", "missing")}, LIDARS),
                           ({"a": CAMERAS["front"], "b": CAMERAS["front"]}, LIDARS)):
        with pytest.raises(ValueError, match="render.*binding|render sensors"):
            native.World(str(SCENE), {}, cameras=cameras, lidars=lidars)
    many = {str(n): ("sensors", "body", str(n)) for n in range(9)}
    with pytest.raises(ValueError, match="at most eight"):
        native.World(str(SCENE), {}, cameras=many)
    with pytest.raises(ValueError, match="frame name exceeds"):
        native.World(str(SCENE), {}, cameras={"a": ("m" * 1024, "body", "camera")}, lidars=LIDARS)


def combined_scene(imu_topic):
    rotor = imu_sdf()
    if imu_topic is not None:
        rotor = rotor.replace('<sensor name="imu" type="imu">',
                              '<sensor name="imu" type="imu"><topic>' + imu_topic + '</topic>')
    model = rotor[rotor.index('<model name="rotor">'):rotor.index("</model>") + len("</model>")]
    return SCENE.read_text().replace("</world>", model + "</world>")


@pytest.mark.parametrize("topic", [None, "/dm_render/image", "/dm_render/scan/points"])
def test_admission_combined_imu_endpoints(tmp_path, topic):
    path = tmp_path / "combined-rejected.sdf"
    path.write_text(combined_scene(topic))
    with pytest.raises(ValueError, match="unique canonical absolute"):
        native.World(str(path), {}, imus={"imu": ("rotor", "body", "imu")},
                     cameras=CAMERAS, lidars=LIDARS)


def collect(physics, count=2):
    """Bound ordinary plant ticks and observation time, without a frame barrier."""
    deadline = time.monotonic() + 8
    frames, scans = [], []
    for _ in range(800):
        if time.monotonic() >= deadline:
            pytest.fail("timed out waiting for actual standard Sensors messages")
        physics.step(1_000_000)
        frame, scan = physics.get_camera("front"), physics.get_lidar("scan")
        if frame is not None and (not frames or frame.timestamp_ns > frames[-1].timestamp_ns):
            frames.append(frame)
        if scan is not None and (not scans or scan.timestamp_ns > scans[-1].timestamp_ns):
            scans.append(scan)
        if len(frames) >= count and len(scans) >= count:
            return frames, scans
        # Allow native asynchronous transport/render workers to run; this is
        # host observation polling, not a replacement simulation scheduler.
        time.sleep(.001)
    pytest.fail("800 ordinary native ticks produced insufficient rendering samples")


def check_samples(frames, scans):
    for frame in frames:
        assert isinstance(frame, CameraFrame)
        assert (frame.width, frame.height, frame.step) == (64, 48, 192)
        assert len(frame.data) == 64 * 48 * 3
        assert isinstance(frame.data, bytes)
        assert frame.frame_id == "sensors::body::camera"
        centre = (24 * 64 + 32) * 3
        red, green, blue = frame.data[centre:centre + 3]
        assert red > 100 and green < 30 and blue < 30
        assert max(frame.data[:3]) < 30
        assert frame.timestamp_ns >= 0
    for scan in scans:
        assert isinstance(scan, LidarScan)
        assert scan.frame_id == "sensors::body::lidar"
        assert len(scan.ranges_m) == len(scan.intensities) == 65
        assert scan.angle_min_rad == -.3 and scan.angle_max_rad == .3
        assert scan.angle_step_rad == .6 / 64
        assert (scan.range_min_m, scan.range_max_m) == (.1, 10)
        assert scan.vertical_angle_min_rad == scan.vertical_angle_max_rad == 0
        assert math.isnan(scan.vertical_angle_step_rad)
        # Independent fixed scene: box front is 3 - 1/2 = 2.5m.
        assert scan.ranges_m[32] == pytest.approx(2.5, abs=.02)
        assert scan.ranges_m[0] == scan.ranges_m[-1] == math.inf
        assert scan.timestamp_ns >= 0


def test_consumer_snapshots_reset_and_single_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("GZ_PARTITION", "dm-python-render-" + uuid.uuid4().hex)
    path = tmp_path / "retained.sdf"
    path.write_text(SCENE.read_text())
    with GazeboPhysics(path, cameras=CAMERAS, lidars=LIDARS) as physics:
        assert physics.time_ns == 0
        with pytest.raises(RuntimeError, match="another Gazebo rendering owner"):
            GazeboPhysics(path, cameras=CAMERAS, lidars=LIDARS)
        frames, scans = collect(physics)
        check_samples(frames, scans)
        with pytest.raises(FrozenInstanceError):
            frames[0].timestamp_ns = 99
        with pytest.raises(TypeError):
            scans[0].ranges_m[0] = 0
        before = physics.time_ns
        for _ in range(5):
            physics.get_camera("front")
            physics.get_lidar("scan")
        assert physics.time_ns == before
        with pytest.raises(ValueError, match="unbound camera"):
            physics.get_camera("absent")
        original_partition = os.environ["GZ_PARTITION"]
        monkeypatch.setenv("GZ_PARTITION", original_partition + "-changed")
        with pytest.raises(RuntimeError, match="partition changed"):
            physics.reset()
        monkeypatch.setenv("GZ_PARTITION", original_partition)
        assert physics.time_ns == before
        path.unlink()  # Reset must retain the admitted DOM, not reread XML.
        physics.reset()
        assert physics.time_ns == 0
        for sample in (physics.get_camera("front"), physics.get_lidar("scan")):
            assert sample is None or sample.timestamp_ns == 0
        new_frames, new_scans = collect(physics)
        check_samples(new_frames, new_scans)
        print("actual snapshots", [f.timestamp_ns for f in frames],
              [s.timestamp_ns for s in scans], "reset", [f.timestamp_ns for f in new_frames],
              [s.timestamp_ns for s in new_scans])
    with pytest.raises(RuntimeError, match="closed"):
        physics.get_camera("front")
    # Native RenderUtil engine unload/recreation is an actual runtime gate.
    with GazeboPhysics(SCENE, cameras=CAMERAS, lidars=LIDARS) as replacement:
        check_samples(*collect(replacement))


def test_consumer_same_owner_plant_and_rendering(tmp_path, monkeypatch):
    monkeypatch.setenv("GZ_PARTITION", "dm-combined-render-" + uuid.uuid4().hex)
    path = tmp_path / "combined.sdf"
    path.write_text(combined_scene("/dm_combined/imu"))
    with GazeboPhysics(path, joints={"shaft": ("rotor", "shaft")},
                       bodies={"body": ("rotor", "body")},
                       imus={"imu": ("rotor", "body", "imu")},
                       cameras=CAMERAS, lidars=LIDARS) as physics:
        physics.set_joint_effort("shaft", .4)
        frames, scans = collect(physics)
        check_samples(frames, scans)
        seconds = physics.time_s
        # Izz=.02 and torque=.4 imply alpha=20, independent of motor code.
        joint = physics.get_joint_state("shaft")
        assert joint.velocity_rad_s == pytest.approx(20 * seconds, abs=.021)
        assert physics.get_imu("imu").gyro_rad_s[2] == pytest.approx(joint.velocity_rad_s, abs=1e-9)
        assert physics.get_body_pose("body").position_m == (0, 0, 0)
        before = physics.time_ns
        physics.get_camera("front")
        physics.get_lidar("scan")
        assert physics.time_ns == before
        physics.reset()
        assert physics.time_ns == 0
        assert physics.get_joint_state("shaft").velocity_rad_s == 0
