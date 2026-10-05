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

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tools"))
from dm_mc02_test_harness import stop


def identity(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat[stat.rfind(")") + 2 :].split()[19]
    except FileNotFoundError:
        return None


def interrupted(signum: int, _frame: object) -> None:
    raise KeyboardInterrupt(f"signal {signum}")


def main() -> int:
    if len(sys.argv) != 5:
        print("usage: ros_smoke.py bridge_node consumer rendering.sdf bridge.yaml", file=sys.stderr)
        return 1
    bridge, consumer, sdf, config = [str(Path(arg).resolve(strict=True)) for arg in sys.argv[1:]]
    inputs = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
              for path in (bridge, consumer, sdf, config)}
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

    def launch(name: str, argv: list[str]) -> subprocess.Popen[bytes]:
        output = Path(directory) / f"{name}.log"
        stream = output.open("wb")
        logs.append((name, stream, output))
        child = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                                 stdout=stream, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        # Register immediately, before /proc inspection can fail.
        processes.append((name, child, None))
        processes[-1] = (name, child, identity(child.pid))
        return child

    try:
        bridge_process = launch("bridge", [bridge, "--ros-args", "-p", f"config_file:={config}",
                                          "-r", f"/dm_render/ros_image:={topic_prefix}/ros_image",
                                          "-r", f"/dm_render/ros_scan:={topic_prefix}/ros_scan"])
        native = launch("consumer", [consumer, sdf, "--ros"])
        while True:
            if bridge_process.poll() is not None:
                raise RuntimeError(f"bridge exited before cleanup: {bridge_process.returncode}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("ROS integration exceeded its 20-second host deadline")
            try:
                code = native.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                continue
            if code != 0:
                raise RuntimeError(f"native ROS consumer exited {code}")
            if time.monotonic() >= deadline:
                raise TimeoutError("native consumer completed after the host deadline")
            if bridge_process.poll() is not None:
                raise RuntimeError(f"bridge exited before cleanup: {bridge_process.returncode}")
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
                      "children": [{"name": name, "pid": process.pid, "starttime": start,
                                    "exit_code": process.returncode}
                                   for name, process, start in processes]}, indent=2))
    return 0 if result == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
