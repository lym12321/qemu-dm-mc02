#!/usr/bin/env python3
"""Actual Simulation and Lab/GDB consumers of the first GPIO teaching lesson.

Only public native board snapshots establish the ordered key/output sequence.
The YAML consumer is a separate entry-point check: `before` assertions alone
cannot prove a release followed a press. No guest memory or register reads are
used by this host consumer. The lesson is a digital fixture, not calibration.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smoke.support.native_cli import NativeCommand, regular_file, resolve_sim
from smoke.support.process import identity, stop
from smoke.support.processes import ChildProcess


def resolve_qemu(value):
    value = value or os.environ.get("QEMU_SYSTEM_ARM") or ROOT / "build/qemu/qemu-system-arm"
    if isinstance(value, str) and "/" not in value:
        value = shutil.which(value) or value
    return regular_file(value, "QEMU", executable=True)


class StageFailure(AssertionError):
    def __init__(self, stage, last):
        self.stage = stage
        super().__init__(f"GPIO lesson stage {stage!r} did not match; last={last!r}")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("teaching consumer host deadline expired")
    return value


def levels(state):
    a, b = state["gpio"]["A"], state["gpio"]["B"]
    return {"timestamp_ns": state["timestamp_ns"],
            "a15": (a["sampled"] >> 15) & 1,
            "a15_mode": (a["moder"] >> 30) & 3,
            "b0": b["sampled"] & 1, "b0_odr": b["odr"] & 1,
            "b0_mode": b["moder"] & 3,
            "b0_output_mask": b["output_mask"] & 1}


def matches(state, key, output):
    value = levels(state)
    return (value["a15_mode"] == 0 and value["a15"] == key
            and value["b0_mode"] == 1 and value["b0_output_mask"] == 1
            and value["b0"] == value["b0_odr"] == output)


def ordered_sequence(snapshot, press, release, wait, deadline, record):
    """The same acceptance is used for both valid and constant-low firmware."""
    for name, key, output, action in (("initial", 1, 0, None),
                                     ("pressed", 0, 1, press),
                                     ("released", 1, 0, release)):
        if action is not None:
            action()
        stage_deadline = min(deadline, time.monotonic() + 2.0)
        last = None
        while True:
            remaining(deadline)
            state = snapshot()
            last = levels(state)
            if matches(state, key, output):
                record["stages"].append({"name": name, **last})
                break
            if time.monotonic() >= stage_deadline:
                record["failed_stage"] = {"name": name, "last": last}
                raise StageFailure(name, last)
            wait()


def cleanup(primary, resources):
    failures = []
    for label, release in resources:
        try:
            release()
        except BaseException as error:
            failures.append((label, error))
    if failures:
        error = primary if primary is not None else failures[0][1]
        for label, failure in failures:
            BaseException.add_note(error, f"Teaching cleanup {label} failed: {failure}")
        if primary is None:
            raise error


def reap_record(process, record):
    record["returncode"] = process.returncode
    original = record["identity"]
    record["identity_gone"] = identity(original["pid"]) != original
    if process.returncode is None or not record["identity_gone"]:
        raise AssertionError("teaching consumer child remains alive or unreaped")


def command(argv, deadline, record):
    """Borrow the existing bounded pipe sampler and TERM/KILL/reap helper."""
    child = None
    primary = None
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        child = ChildProcess("teaching command", process)
        record.update(argv=list(argv), identity=identity(process.pid))
        if record["identity"] is None:
            raise AssertionError("command exited before its identity was recorded")
        child.prepare()
        while True:
            remaining(deadline)
            child.pump()
            if process.poll() is not None:
                child.pump()
                break
            select.select(child.readers(), [], [], min(.01, remaining(deadline)))
        record.update(child.diagnostics())
    except BaseException as error:
        primary = error
        raise
    finally:
        if child is not None:
            process = child.process
            cleanup(primary, [("command stop", lambda: stop(process)),
                              ("command stdout", process.stdout.close),
                              ("command stderr", process.stderr.close)])
            record.update(child.diagnostics())
            cleanup(primary, [("command reap", lambda: reap_record(process, record))])
    return record


def free_gdb_port():
    # Native GDB is TCP. Bind briefly to choose an unused local port; a race
    # with another host process fails explicitly rather than changing ports.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        return reserved.getsockname()[1]


def direct(elf, qemu, gdb, record, executable):
    return interactive(elf, qemu, record, executable, gdb=gdb)


def simulation_cli(elf, qemu, manifest_path, directory, record, executable, *, expect_failure=False):
    import yaml

    document = yaml.safe_load(manifest_path.read_text())
    document["nodes"]["main"]["firmware"] = str(elf)
    manifest = directory / "lesson.yaml"
    manifest.write_text(yaml.safe_dump(document, sort_keys=False))
    report = directory / "simulation-result.json"
    result = command([str(executable), "run", str(manifest),
                      "--qemu", str(qemu), "--startup-timeout", "10", "--timeout", "15",
                      "--report", str(report)], time.monotonic() + 20.0, record)
    record["report"] = json.loads(report.read_text())
    payload = record["report"]
    summary = payload.get("result")
    if (not isinstance(summary, dict) or summary.get("end_ns") != 800_000_000
            or summary.get("origin_ns") != 0 or summary.get("ready_receipts") != {}
            or summary.get("delivered_inputs") != 2):
        raise AssertionError(f"Simulation CLI did not complete the GPIO lesson: {record!r}")
    if expect_failure:
        if (result["returncode"] != 1 or payload["status"] != "failed"
                or summary.get("status") != ["fail"]
                or not any("digital_equals main.gpio.B0 did not match before" in item
                           for item in payload["result"]["failures"])
                or "failed" not in result["stdout"] or "passed" in result["stdout"]):
            raise AssertionError(f"constant-low Simulation CLI unexpectedly passed: {record!r}")
    elif (result["returncode"] != 0 or payload["status"] != "passed"
            or summary.get("status") != ["pass"]):
        raise AssertionError(f"Simulation CLI lesson failed: {record!r}")
    # This entrypoint gate does not claim its `before` assertions prove order.


def interactive(elf, qemu, record, executable, *, gdb=None):
    primary = None
    deadline = time.monotonic() + 20.0
    consumer = None
    with tempfile.TemporaryDirectory(prefix="dm-qemu.teaching.", dir="/tmp") as temporary:
        qmp_path = Path(temporary) / "user-qmp.sock"
        argv = [str(executable), "lab", "interactive", "--elf", str(elf),
                "--qemu", str(qemu), "--qmp", str(qmp_path),
                "--startup-timeout", "10", "--timeout", "15"]
        if gdb is not None:
            port = free_gdb_port()
            argv.extend(("--gdb-port", str(port), "--paused"))
        try:
            consumer = NativeCommand(argv, deadline, record, stdin=True)
            consumer.ready()
            if gdb is not None:
                debug = command([str(gdb), "-q", "--batch", str(elf),
                    "-ex", "set pagination off", "-ex", "set confirm off",
                    "-ex", f"target remote 127.0.0.1:{port}",
                    "-ex", "hbreak lesson_step", "-ex", "continue",
                    "-ex", "info symbol $pc", "-ex", "detach"],
                    min(deadline, time.monotonic() + 5.0), record.setdefault("gdb", {}))
                if (debug["returncode"] != 0 or not re.search(
                        r"Breakpoint [0-9]+,\s+lesson_step\b", debug["stdout"])):
                    raise AssertionError(f"native GDB did not hit lesson_step: {debug!r}")
                consumer.send("cont")
            ordered_sequence(consumer.snapshot, lambda: consumer.send("key press"),
                             lambda: consumer.send("key release"), consumer.wait, deadline, record)
            state = consumer.snapshot()
            if not matches(state, 1, 0):
                raise AssertionError("interactive state differs from the released observation")
            record["published_state"] = levels(state)
            consumer.send("quit")
            if consumer.finish() != 0:
                raise AssertionError(f"interactive CLI failed: {consumer.child.diagnostics()!r}")
        except BaseException as error:
            primary = error
            raise
        finally:
            if consumer is not None:
                consumer.close(primary)
                resources = record["resources"]
                record["qemu_children"] = [{key: row[key] for key in ("pid", "starttime")}
                                            for row in resources]
                record["qemu_reaped"] = record["qemu_identity_gone"] = record["cleanup_completed"]
                record["socket_paths_removed"] = record["cleanup_completed"]
                record["external_qmp_removed"] = not qmp_path.exists()
                if primary is None and (len(resources) != 1 or not record["external_qmp_removed"]
                        or resources[0]["firmware"] != {"path": str(elf), "sha256": sha(elf)}):
                    raise AssertionError("interactive native ELF/identity/cleanup evidence incomplete")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debug-elf", required=True, type=Path)
    parser.add_argument("--release-elf", required=True, type=Path)
    parser.add_argument("--bad-elf", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--qemu", type=Path)
    parser.add_argument("--gdb", default=os.environ.get("GDB_ARM"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--sim", type=Path, help="compiled Rust sim executable (or SIM_EXECUTABLE)")
    args = parser.parse_args()
    elfs = {name: regular_file(path, f"{name} lesson ELF") for name, path in (
        ("Debug", args.debug_elf), ("Release", args.release_elf), ("constant-low", args.bad_elf))}
    manifest = regular_file(args.manifest, "teaching scene")
    qemu = resolve_qemu(args.qemu)
    executable = resolve_sim(args.sim)
    selected = args.gdb or shutil.which("arm-none-eabi-gdb") or shutil.which("gdb-multiarch")
    if selected is None:
        raise ValueError("native ARM GDB is required; use --gdb or GDB_ARM")
    gdb = regular_file(shutil.which(selected) or selected, "ARM GDB", executable=True)
    args.output.mkdir(parents=True, exist_ok=False)
    protected = {str(path): sha(path) for path in (*elfs.values(), manifest, qemu, gdb, executable)}
    report = {"status": "failed", "inputs": protected, "cases": [],
              "limits": ["Digital GPIO/HAL teaching consumer only; no physical calibration.",
                         "Sequential public observations are not a global CPU/peripheral barrier."]}
    code = 1
    try:
        for profile in ("Debug", "Release"):
            for name, function in (("interactive-and-native-gdb", direct), ("simulation-cli", simulation_cli),
                                   ("interactive-cli", interactive)):
                record = {"profile": profile, "case": name, "status": "failed", "stages": []}
                report["cases"].append(record)
                if function is direct:
                    function(elfs[profile], qemu, gdb, record, executable)
                elif function is simulation_cli:
                    directory = args.output / profile
                    directory.mkdir()
                    function(elfs[profile], qemu, manifest, directory, record, executable)
                else:
                    function(elfs[profile], qemu, record, executable)
                record["status"] = "passed"
        record = {"profile": "constant-low", "case": "same-direct-acceptance",
                  "status": "failed", "stages": []}
        report["cases"].append(record)
        try:
            direct(elfs["constant-low"], qemu, None, record, executable)
        except StageFailure as error:
            if (error.stage != "pressed" or [item["name"] for item in record["stages"]] != ["initial"]
                    or any(not note.startswith("QEMU stderr tail:\n")
                           for note in getattr(error, "__notes__", ()))
                    or not all(record.get(key) for key in ("qemu_reaped", "qemu_identity_gone",
                                                          "socket_paths_removed"))):
                raise
            record.update(status="expected-failure", error=str(error),
                          error_type=type(error).__name__, notes=list(getattr(error, "__notes__", ())))
        else:
            raise AssertionError("constant-low firmware unexpectedly passed the original acceptance")
        record = {"profile": "constant-low", "case": "same-simulation-cli-assertion",
                  "status": "failed", "stages": []}
        report["cases"].append(record)
        directory = args.output / "constant-low"
        directory.mkdir()
        simulation_cli(elfs["constant-low"], qemu, manifest, directory, record, executable, expect_failure=True)
        record["status"] = "expected-failure"
        if {path: sha(path) for path in protected} != protected:
            raise AssertionError("teaching consumer inputs changed during the gate")
        report["status"] = "passed"
        code = 0
    except (Exception, KeyboardInterrupt) as error:
        report.update(error=str(error), error_type=type(error).__name__,
                      notes=list(getattr(error, "__notes__", ())))
        code = 130 if isinstance(error, KeyboardInterrupt) else 1
    finally:
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"Teaching GPIO consumer: {report['status']} ({args.output / 'report.json'})", flush=True)
    return code


def interrupted(signum, _frame):
    raise KeyboardInterrupt(f"interrupted by signal {signum}")


if __name__ == "__main__":
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        raise SystemExit(main())
    finally:
        signal.signal(signal.SIGTERM, previous)
