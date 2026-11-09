"""Bounded test-only owner for the official bridge and native ROS consumer."""

from __future__ import annotations

import hashlib
import json
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


def main() -> int:
    if len(sys.argv) not in (5, 7) or (len(sys.argv) == 7 and sys.argv[5] != "--rust-producer"):
        print("usage: ros_smoke.py bridge_node consumer rendering.sdf bridge.yaml "
              "[--rust-producer executable]", file=sys.stderr)
        return 1
    bridge, consumer, sdf, config = [str(Path(arg).resolve(strict=True)) for arg in sys.argv[1:5]]
    rust_producer = Path(sys.argv[6]).resolve(strict=True) if len(sys.argv) == 7 and sys.argv[5] == "--rust-producer" else None
    production = rust_producer is not None
    sources = [Path(path) for path in (bridge, consumer, sdf, config)]
    sources += [Path(__file__).resolve(), ROOT / "tools/dm_mc02_test_harness.py",
                Path(__file__).with_name("rendering_smoke.cc"),
                Path(__file__).with_name("ros_observer.hh")]
    if rust_producer is not None:
        sources += [rust_producer, ROOT / "crates/sim-host/examples/gazebo_render_producer.rs",
                    ROOT / "crates/sim-host/src/physics/gazebo.rs",
                    ROOT / "crates/sim-host/src/physics/gazebo/ffi.rs",
                    *sorted((ROOT / "sim/physics/gazebo_native").glob("*.cc")),
                    *sorted((ROOT / "sim/physics/gazebo_native").glob("*.hh")),
                    ROOT / "sim/physics/gazebo_native/rust_bridge.inc"]
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
                        if production else [consumer, sdf, "--ros"])
        producer = None
        if production:
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
            producer = launch("producer", [str(rust_producer), sdf, str(finish)])
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
                      "mode": "rust" if rust_producer is not None else "native",
                      "children": [{"name": name, "pid": process.pid, "starttime": start,
                                    "exit_code": process.returncode}
                                   for name, process, start in processes]}, indent=2))
    return 0 if result == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
