#!/usr/bin/env python3
"""Black-box launcher of the compiled real MotorTest firmware consumer."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from smoke.support.process import identity, running_qemu, stop
from smoke.support.processes import ChildProcess
from smoke.support.qmp import qmp_session_class


def snapshot(paths):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("elf", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--consumer", type=Path, default=Path(os.environ.get(
        "SIM_MOTOR_CONSUMER", ROOT / "target/debug/examples/firmware_motor")))
    args = parser.parse_args()
    model = ROOT / "smoke/dm_mc02_mujoco_smoke.xml"
    candidate = args.report.with_suffix(".candidate.json")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report = dict(result="failed", error="consumer not finished", cases=[], trace=[])
    args.report.write_text(json.dumps(report) + "\n")
    candidate.unlink(missing_ok=True)
    paths = [args.elf, args.qemu, model, args.consumer, Path(__file__),
             ROOT / "smoke/support/process.py", ROOT / "smoke/support/qmp.py"]
    primary = None
    inputs = {}
    owner = None
    try:
        inputs = snapshot(paths)
        command = [str(args.qemu), "-machine", "dm-mc02,fdcan-host-ack=on,iwdg-boot-grace-ms=0",
                   "-kernel", str(args.elf), "-nodefaults", "-display", "none", "-monitor", "none", "-S",
                   "-qmp", "unix:{qmp},server=on,wait=off", "-serial", "none",
                   "-chardev", "socket,id=uart,path={uart},server=on,wait=off", "-serial", "chardev:uart"]
        command += [value for _ in range(5) for value in ("-serial", "none")]
        command += ["-chardev", "socket,id=can,path={can},server=on,wait=off", "-serial", "chardev:can"]
        with running_qemu(command, sockets=("qmp", "uart", "can"), startup_timeout=3) as owner:
            qmp = process = None
            run_error = None
            try:
                qmp = qmp_session_class()(str(owner.sockets["qmp"]), timeout=2)
                deadline = time.monotonic() + 28
                process = subprocess.Popen([
                    str(args.consumer), str(owner.sockets["uart"]), str(owner.sockets["can"]),
                    str(model), str(candidate)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True)
                child = ChildProcess("Rust MotorTest consumer", process)
                child.prepare()
                begun = False
                while process.poll() is None:
                    child.pump()
                    if not begun and b"MOTOR_HOST_CONNECTED\n" in child.tails["stdout"]:
                        qmp.command("cont")
                        begun = True
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"compiled MotorTest consumer deadline exceeded: {child.diagnostics()}")
                    select.select(child.readers(), [], [], .001)
                child.pump()
                print(child.tails["stdout"].decode(errors="replace"), end="", flush=True)
                if process.returncode:
                    raise RuntimeError(f"compiled MotorTest consumer exited{process.returncode}: {child.diagnostics()}")
                watchdog = qmp.command("qom-get", {"path": "/machine", "property": "iwdg-diagnostics"})
                fields = dict(item.split("=", 1) for item in watchdog.split(","))
                if fields.get("timeouts") != "0" or fields.get("window-violations") != "0":
                    raise RuntimeError("unexpected watchdog event: " + watchdog)
                report = json.loads(candidate.read_text())
                if report["result"] != "passed":
                    raise RuntimeError("compiled consumer candidate failed")
                report["watchdog"] = watchdog
            except BaseException as error:
                run_error = error
                raise
            finally:
                cleanup_errors = []
                if process is not None:
                    for operation in (lambda: stop(process), process.stdout.close, process.stderr.close):
                        try:
                            operation()
                        except BaseException as error:
                            cleanup_errors.append(str(error))
                if qmp is not None:
                    try:
                        qmp.close()
                    except BaseException as error:
                        cleanup_errors.append(str(error))
                if cleanup_errors:
                    message = "MotorTest child cleanup failed: " + "; ".join(cleanup_errors)
                    if run_error is None:
                        raise RuntimeError(message)
                    run_error.add_note(message)
        if identity(owner.process.pid) is not None or owner.stderr_path.parent.exists():
            raise RuntimeError("native QEMU identity or temporary directory remains after cleanup")
        if snapshot(paths) != inputs:
            raise RuntimeError("MotorTest inputs changed during execution/cleanup")
    except BaseException as error:
        primary = str(error) or type(error).__name__
        if getattr(error, "__notes__", ()):
            primary += "; " + "; ".join(error.__notes__)
        if candidate.exists():
            report = json.loads(candidate.read_text())
    report.update(result="failed" if primary else "passed", error=primary, source_inputs=inputs,
                  native_cleanup_complete=owner is not None and owner.process.poll() is not None,
                  dropped_first_feedback=os.environ.get("DM_MOTOR_DROP_FEEDBACK", "0") == "1")
    args.report.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    if primary:
        print("MotorTest failed: " + primary, file=sys.stderr)
        return 1
    print("RESULT: trobot motor/MuJoCo Rust consumer and cleanup passed; physical calibration SKIP")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
