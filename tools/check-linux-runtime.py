#!/usr/bin/env python3
"""Check a complete Linux directory package from outside its source checkout."""
import argparse
import fnmatch
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from linux_runtime import digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--native-probe", type=Path,
                        help="optional same-feature compiled native acceptance example")
    args = parser.parse_args()
    root = args.package.resolve(strict=True)
    if args.report.exists():
        parser.error("--report must be a new path")
    report = dict(schema=1, package=str(root), passed=False)
    probe = None
    try:
        metadata = json.loads((root / "native-runtime.json").read_text())
        provenance = json.loads((root / "provenance.json").read_text())
        for component in ("qemu", "rust"):
            item = provenance[component]
            if digest(root / item["file"]) != item["sha256"]:
                raise ValueError(f"packaged {component} hash changed")
        settings = json.loads((root / "runtime-env.json").read_text())
        env = {key: value for key, value in os.environ.items()
               if not (key.startswith(("PYTHON", "GZ_", "IGN_", "OGRE", "ROS_", "RMW_"))
                       or key in ("LD_LIBRARY_PATH", "LD_PRELOAD", "AMENT_PREFIX_PATH",
                                  "CMAKE_PREFIX_PATH", "SDF_PATH", "QEMU_SYSTEM_ARM", "DISPLAY"))}
        env.update(PATH="", RMW_IMPLEMENTATION="rmw_fastrtps_cpp",
                   ROS_AUTOMATIC_DISCOVERY_RANGE="LOCALHOST",
                   GZ_PARTITION=f"dm-package-{os.getpid()}")
        # The actual executable must consume its own adjacent runtime-env.json.
        # Supplying those paths here would configure singular Gazebo roots twice.
        closure_env = dict(env)
        for name, relative in settings["paths"].items():
            resolved = [(root / path).resolve(strict=True) for path in relative]
            if any(not path.is_relative_to(root) for path in resolved):
                raise ValueError(f"runtime environment escapes package: {name}")
            closure_env[name] = ":".join(map(str, resolved))
        report["environment"] = {key: closure_env[key] for key in (*settings["paths"], "PATH")}
        checked = []
        files = [root / "sim", root / "bin/qemu-system-arm"]
        files += [root / item["file"] for item in metadata["files"]]
        for path in sorted(set(files)):
            listing = subprocess.check_output(["/usr/bin/ldd", str(path)], env=closure_env, text=True)
            if "libpython" in listing.lower() or "not found" in listing:
                raise ValueError(f"unresolved/Python dependency: {path}\n{listing}")
            for line in listing.splitlines():
                if "=> /" not in line:
                    continue
                name, rest = line.strip().split(" => ", 1)
                library = Path(rest.split(" (", 1)[0]).resolve()
                if library.is_relative_to(root):
                    continue
                if not any(fnmatch.fnmatch(name, pattern) for pattern in metadata["system_library_patterns"]):
                    raise ValueError(f"unbundled non-system dependency {name}: {library}")
            checked.append(str(path.relative_to(root)))
        for item in metadata["files"]:
            if digest(root / item["file"]) != item["sha256"]:
                raise ValueError(f"packaged native hash changed: {item['file']}")
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            if relative.parts[0] in ("sources", "licenses"):
                continue
            if path.suffix in (".py", ".pyc", ".pyo") or "libpython" in path.name.lower():
                raise ValueError(f"Python runtime content: {relative}")
        report["native_dependency_closure"] = checked
        with tempfile.TemporaryDirectory(prefix="dm-linux-package-验收-") as cwd:
            outputs = {}
            for command in (["--help"], ["lab", "--help"], ["worker", "--help"]):
                result = subprocess.run([str(root / "sim"), *command], cwd=cwd, env=env,
                                        capture_output=True, text=True, timeout=15, check=True)
                outputs[" ".join(command)] = result.stdout[-12000:]
            if not all(word in outputs["--help"] for word in ("firmware", "validate", "run", "replay", "lab", "worker")):
                raise ValueError("packaged sim is missing full-runtime CLI consumers")
            report["cli"] = outputs
            scene = Path(cwd) / "scene.yaml"
            scene.write_text("schema: sim/v1\nruntime: {duration: 5ms, quantum: 1ms}\n"
                "nodes:\n  can0: {kind: bus.can}\n  motor0:\n"
                "    kind: device.dji-m3508\n    motor_id: 1\n    plant: fixed-rotor\n"
                "    position_rad: 0\n    velocity_rad_s: 0\n"
                "links: [[motor0.can, can0.can]]\nexperiment: {}\n")
            trace_path = Path(cwd) / "scene.mcap"
            live, replay = Path(cwd) / "live.json", Path(cwd) / "replay.json"
            for command in (["validate", str(scene)],
                            ["run", str(scene), "--trace", str(trace_path), "--report", str(live)],
                            ["replay", str(trace_path), "--report", str(replay)]):
                subprocess.run([str(root / "sim"), *command], cwd=cwd, env=env,
                               capture_output=True, text=True, timeout=15, check=True)
            live_report, replay_report = json.loads(live.read_text()), json.loads(replay.read_text())
            if (live_report["status"] != "passed" or replay_report["status"] != "passed"
                    or live_report["result"] != replay_report["result"]):
                raise ValueError("packaged scene run/MCAP replay results disagree")
            report["scene_record_replay"] = dict(live=live_report, replay=replay_report)
            if args.native_probe:
                probe = root / ".package-native-probe"
                if probe.exists():
                    raise ValueError("native probe destination already exists")
                shutil.copy2(args.native_probe.resolve(strict=True), probe)
                subprocess.run(["patchelf", "--set-rpath", "$ORIGIN/lib", str(probe)], check=True)
                trace = Path(cwd) / "native-files.trace"
                result = subprocess.run(["/usr/bin/strace", "-f", "-e", "trace=file,process",
                                         "-o", str(trace), str(probe)], cwd=cwd, env=env, capture_output=True,
                                        text=True, timeout=40)
                report["native_probe"] = dict(returncode=result.returncode,
                    stdout=result.stdout[-12000:], stderr=result.stderr[-12000:])
                leaked = [line for line in trace.read_text().splitlines()
                          if re.search(r"\b(?:open|openat|execve)\(", line)
                          and re.search(r"(?:/opt/ros/|/opt/mujoco|/build/rust-gazebo-native/|python[0-9.]*(?:\"|/))", line)
                          and re.search(r"= [0-9]+(?:<[^>]+>)?$", line)]
                if leaked:
                    report["native_probe"]["external_sdk_access"] = leaked
                    raise ValueError("native package opened an external SDK/Python file")
                report["native_probe"]["external_sdk_access"] = []
                result.check_returncode()
        report["passed"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        if isinstance(error, subprocess.CalledProcessError):
            report["stdout"] = str(error.stdout)[-12000:]
            report["stderr"] = str(error.stderr)[-12000:]
    finally:
        if probe is not None:
            probe.unlink(missing_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x") as target:
        json.dump(report, target, ensure_ascii=False, indent=2)
        target.write("\n")
    print(f"Linux full package {'PASS' if report['passed'] else 'FAILED'}: {args.report}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
