"""Bounded test-only owner for the official bridge and native ROS consumer."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "tools"))
from dm_mc02_test_harness import stop


def identity(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat[stat.rfind(")") + 2 :].split()[19]
    except FileNotFoundError:
        return None


def interrupted(signum: int, _frame: object) -> None:
    raise KeyboardInterrupt(f"signal {signum}")


def marker(path: Path, expected: bytes) -> bool:
    try:
        with path.open("rb") as stream:
            actual = stream.read(len(expected) + 1)
    except FileNotFoundError:
        return False
    if actual != expected:
        raise RuntimeError(f"invalid {path.name} marker")
    return True


def produce(sdf: str, finish: Path) -> int:
    """Exercise the public production owner in a separate bridge-visible process."""
    sys.path.insert(0, str(ROOT))
    import _dm_gazebo_native as native
    from sim.physics import CameraFrame, GazeboPhysics, LidarScan

    # PYTHONPATH admits a directory; verify the exact binary that Python selected.
    actual_module = Path(native.__file__).resolve(strict=True)
    expected_module = Path(os.environ["DM_GAZEBO_NATIVE_MODULE"]).resolve(strict=True)
    if actual_module != expected_module:
        raise RuntimeError(f"production imported unexpected native module: {actual_module}")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    deadline = time.monotonic() + 10
    camera_stamps: list[int] = []
    lidar_stamps: list[int] = []
    ticks = 0
    with GazeboPhysics(sdf, cameras={"front": ("sensors", "body", "camera")},
                       lidars={"scan": ("sensors", "body", "lidar")}) as physics:
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("production rendering exceeded its 10-second host deadline")
            if ticks < 900:
                physics.step(1_000_000)
                ticks += 1
            frame, scan = physics.get_camera("front"), physics.get_lidar("scan")
            if frame is not None and (not camera_stamps or frame.timestamp_ns > camera_stamps[-1]):
                if not (isinstance(frame, CameraFrame) and
                        (frame.width, frame.height, frame.step, len(frame.data)) ==
                        (64, 48, 192, 9216) and frame.frame_id == "sensors::body::camera" and
                        0 <= frame.timestamp_ns < 1_000_000_000):
                    raise RuntimeError("invalid production camera snapshot")
                centre = (24 * 64 + 32) * 3
                red, green, blue = frame.data[centre:centre + 3]
                if not (red > 100 and green < 30 and blue < 30 and max(frame.data[:3]) < 30):
                    raise RuntimeError("production camera missed the fixed red-box scene")
                if len(camera_stamps) < 2:
                    camera_stamps.append(frame.timestamp_ns)
            if scan is not None and (not lidar_stamps or scan.timestamp_ns > lidar_stamps[-1]):
                if not (isinstance(scan, LidarScan) and scan.frame_id == "sensors::body::lidar" and
                        len(scan.ranges_m) == len(scan.intensities) == 65 and
                        scan.angle_min_rad == -.3 and scan.angle_max_rad == .3 and
                        scan.angle_step_rad == .6 / 64 and
                        (scan.range_min_m, scan.range_max_m) == (.1, 10) and
                        scan.vertical_angle_min_rad == scan.vertical_angle_max_rad == 0 and
                        math.isnan(scan.vertical_angle_step_rad) and
                        0 <= scan.timestamp_ns < 1_000_000_000):
                    raise RuntimeError("invalid production lidar snapshot")
                # Primitive scene source: box centre x=3, width=1; outer rays miss.
                if not (abs(scan.ranges_m[32] - 2.5) <= .02 and
                        scan.ranges_m[0] == scan.ranges_m[-1] == math.inf):
                    raise RuntimeError("production lidar missed the fixed box geometry")
                if len(lidar_stamps) < 2:
                    lidar_stamps.append(scan.timestamp_ns)
            if (marker(finish, b"finish\n") and
                    len(camera_stamps) == len(lidar_stamps) == 2):
                if time.monotonic() >= deadline:
                    raise TimeoutError("production rendering completed after the host deadline")
                break
            # Host polling gives native transport/render workers discovery time;
            # it neither replaces native sensor scheduling nor barriers delivery.
            time.sleep(.005)
    if time.monotonic() >= deadline:
        raise TimeoutError("production rendering closed after the host deadline")
    print(json.dumps({"production": "passed", "ticks": ticks,
                      "camera_stamps_ns": camera_stamps, "lidar_stamps_ns": lidar_stamps,
                      "native_module": str(actual_module)}))
    return 0


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--produce":
        return produce(str(Path(sys.argv[2]).resolve(strict=True)), Path(sys.argv[3]).resolve())
    if len(sys.argv) not in (5, 7) or (len(sys.argv) == 7 and sys.argv[5] != "--production"):
        print("usage: ros_smoke.py bridge_node consumer rendering.sdf bridge.yaml "
              "[--production native-module]", file=sys.stderr)
        return 1
    bridge, consumer, sdf, config = [str(Path(arg).resolve(strict=True)) for arg in sys.argv[1:5]]
    module = Path(sys.argv[6]).resolve(strict=True) if len(sys.argv) == 7 else None
    sources = [Path(path) for path in (bridge, consumer, sdf, config)]
    sources += [Path(__file__).resolve(), ROOT / "tools/dm_mc02_test_harness.py",
                Path(__file__).with_name("rendering_smoke.cc"),
                Path(__file__).with_name("ros_observer.hh")]
    if module is not None:
        sources += [module, *sorted((ROOT / "sim").rglob("*.py")),
                    *sorted((ROOT / "sim/physics/gazebo_native").rglob("*.cc")),
                    *sorted((ROOT / "sim/physics/gazebo_native").rglob("*.hh")),
                    ROOT / "sim/physics/gazebo_native/CMakeLists.txt"]
    inputs = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    topic_prefix = f"/dm_render/run_{os.getpid()}"
    env = dict(os.environ, GZ_PARTITION=f"dm-ros-render-gate-{os.getpid()}",
               ROS_DOMAIN_ID=str(100 + os.getpid() % 100),
               DM_GAZEBO_ROS_TOPIC_PREFIX=topic_prefix,
               ROS_AUTOMATIC_DISCOVERY_RANGE="LOCALHOST")
    env.pop("DISPLAY", None)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    processes: list[tuple[str, subprocess.Popen[bytes], str | None]] = []
    logs = []
    primary: BaseException | None = None
    cleanup_errors = []
    deadline = time.monotonic() + 20
    temporary = tempfile.TemporaryDirectory(prefix="dm-gazebo-ros.")
    directory = temporary.name
    ready = Path(directory) / "ready"
    finish = Path(directory) / "finish"
    production_done = False

    def launch(name: str, argv: list[str], child_env: dict[str, str] = env) -> subprocess.Popen[bytes]:
        output = Path(directory) / f"{name}.log"
        stream = output.open("wb")
        logs.append((name, stream, output))
        child = subprocess.Popen(argv, env=child_env, stdin=subprocess.DEVNULL,
                                 stdout=stream, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        # Register immediately, before /proc inspection can fail.
        processes.append((name, child, None))
        processes[-1] = (name, child, identity(child.pid))
        return child

    def running(name: str, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            raise RuntimeError(f"{name} exited before cleanup: {process.returncode}")

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("ROS integration exceeded its 20-second host deadline")
        return value

    try:
        bridge_process = launch("bridge", [bridge, "--ros-args", "-p", f"config_file:={config}",
                                          "-r", f"/dm_render/ros_image:={topic_prefix}/ros_image",
                                          "-r", f"/dm_render/ros_scan:={topic_prefix}/ros_scan"])
        native = launch("consumer", [consumer, sdf, "--ros-external", str(ready)]
                        if module is not None else [consumer, sdf, "--ros"])
        producer = None
        if module is not None:
            while True:
                remaining()
                running("bridge", bridge_process)
                running("consumer", native)
                if marker(ready, b"ready\n"):
                    remaining()
                    running("bridge", bridge_process)
                    running("consumer", native)
                    break
                time.sleep(min(.01, remaining()))
            producer_env = dict(env)
            producer_env["DM_GAZEBO_NATIVE_MODULE"] = str(module)
            producer_env["PYTHONPATH"] = str(module.parent) + (
                os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            producer = launch("producer", [sys.executable, str(Path(__file__).resolve()),
                                           "--produce", sdf, str(finish)], producer_env)
        while True:
            running("bridge", bridge_process)
            if producer is not None:
                running("producer", producer)
            try:
                code = native.wait(timeout=min(0.1, remaining()))
            except subprocess.TimeoutExpired:
                continue
            if code != 0:
                raise RuntimeError(f"native ROS consumer exited {code}")
            remaining()
            running("bridge", bridge_process)
            if producer is not None:
                running("producer", producer)
                # Consumer success is the only producer shutdown request. Publish
                # the exact marker atomically inside the same private directory.
                pending = finish.with_name("finish.tmp")
                with pending.open("xb") as stream:
                    stream.write(b"finish\n")
                remaining()
                running("bridge", bridge_process)
                running("producer", producer)
                pending.replace(finish)
                while True:
                    running("bridge", bridge_process)
                    try:
                        code = producer.wait(timeout=min(.1, remaining()))
                    except subprocess.TimeoutExpired:
                        continue
                    if code != 0:
                        raise RuntimeError(f"production rendering producer exited {code}")
                    remaining()
                    running("bridge", bridge_process)
                    production_done = True
                    break
            break
    except BaseException as failure:
        primary = failure
    finally:
        # Preserve the initial interruption and finish the bounded cleanup.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        for name, process, start in reversed(processes):
            try:
                if name == "bridge" and primary is None and process.poll() is not None:
                    raise RuntimeError(f"bridge exited before cleanup: {process.returncode}")
                if name == "producer" and primary is None and not production_done:
                    raise RuntimeError("production producer did not complete before cleanup")
            except BaseException as failure:
                cleanup_errors.append(f"{name}: {failure}")
            try:
                stop(process)
                if process.poll() is None or (start is not None and identity(process.pid) == start):
                    raise RuntimeError("child identity still exists after cleanup")
            except BaseException as failure:
                cleanup_errors.append(f"{name}: {failure}")
        for name, stream, _ in logs:
            try:
                stream.close()
            except BaseException as failure:
                cleanup_errors.append(f"{name} log: {failure}")
    for name, _, path in logs:
        try:
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 12000))
                tail = stream.read(12000).decode("utf-8", "replace")
            print(f"{name} output:\n{tail}")
        except BaseException as failure:
            cleanup_errors.append(f"{name} diagnostics: {failure}")
    try:
        temporary.cleanup()
    except BaseException as failure:
        cleanup_errors.append(f"temporary directory: {failure}")
    for path, expected in inputs.items():
        try:
            if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
                cleanup_errors.append(f"input changed during execution: {path}")
        except BaseException as failure:
            cleanup_errors.append(f"input verification: {path}: {failure}")
    result = "passed" if primary is None and not cleanup_errors else "failed"
    print(json.dumps({"result": result, "error": None if primary is None else str(primary),
                      "cleanup_errors": cleanup_errors, "input_sha256": inputs,
                      "gz_partition": env["GZ_PARTITION"], "ros_domain_id": env["ROS_DOMAIN_ID"],
                      "ros_topic_prefix": topic_prefix,
                      "mode": "production" if module is not None else "native",
                      "children": [{"name": name, "pid": process.pid, "starttime": start,
                                    "exit_code": process.returncode}
                                   for name, process, start in processes]}, indent=2))
    return 0 if result == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
