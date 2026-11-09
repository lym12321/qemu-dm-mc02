#!/usr/bin/env python3
"""Measure fixed workloads through the compiled Rust CLI and Linux process data."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
from statistics import median
import subprocess
import sys
import time

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from smoke.simulation_smoke import Blocked, build_example, _case_document
from smoke.support.experiment import Events
from smoke.support.native_cli import NativeCommand, regular_file, resolve_sim
from smoke.support.presets import four_wheel_document


def identity(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def execute(document, directory, *, sim, qemu, output_mode="mcap"):
    directory.mkdir()
    manifest, result_path, trace = (directory / name for name in ("scene.yaml", "result.json", "events.mcap"))
    manifest.write_text(yaml.safe_dump(document, sort_keys=False))
    paths = [Path(node[key]) for node in document["nodes"].values() for key in ("firmware", "model") if key in node and Path(node[key]).is_file()]
    inputs = {str(path): identity(path) for path in [manifest, sim, *paths, *([qemu] if qemu else [])]}
    argv = [str(sim), "run", str(manifest), "--report", str(result_path),
            "--startup-timeout", "10", "--timeout", "30"]
    if qemu is not None:
        argv += ["--qemu", str(qemu)]
    if output_mode == "mcap":
        argv += ["--trace", str(trace)]
    row = {"output": output_mode, "bytes": {"stdout": 0, "stderr": 0}, "inputs_before": inputs}
    stream = (directory / "stdout.jsonl").open("xb") if output_mode == "file" else None
    command, primary = None, None
    started = time.monotonic()
    def observed(name, data):
        row["bytes"][name] += len(data)
        if stream is not None and name == "stdout":
            stream.write(data)
    try:
        command = NativeCommand(argv, started + 35, row, measure=True)
        command.child.on_output = observed
        code = command.finish()
        # Once the process exited, drain both pipes to EOF with the same bounded
        # reader. A live producer cannot extend this finite post-exit operation.
        while command.child.readers():
            command.child.pump()
        if code == 78:
            raise Blocked(command.child.diagnostics()["stderr"])
        result = json.loads(result_path.read_text())
        row["result"] = result
        if code != 0 or result["status"] != "passed":
            raise AssertionError("compiled scene failed: " + repr(result))
    except BaseException as error:
        primary = error
    finally:
        exited = time.monotonic()
        if command is not None:
            try:
                command.close(primary)
            except BaseException as error:
                primary = error
        if stream is not None:
            stream.close()
        row.update(command_wall_s=exited - started,
            elapsed_including_cleanup_s=time.monotonic() - started)
    if primary is not None:
        primary.benchmark_observations = row
        raise primary
    try:
        row["inputs_before"] = inputs
        row["inputs_after"] = {path: identity(path) for path in inputs}
        if row["inputs_before"] != row["inputs_after"]:
            raise AssertionError("benchmark inputs changed during execution/cleanup")
        resources = row["resources"]
        expected = sorted(str(Path(node["firmware"]).resolve()) for node in document["nodes"].values()
                          if node["kind"] == "machine.qemu")
        if sorted(item["firmware"]["path"] for item in resources) != expected:
            raise AssertionError("actual -kernel differs from the benchmark fixture")
        if output_mode == "mcap":
            from smoke.simulation_smoke import _read_observations
            events = Events()
            _read_observations(trace, events)
            row["recorded_events"] = len(events)
            row["trace_bytes"] = trace.stat().st_size
            row["cuts"] = len({event.delivery_time_ns for event in events if event.kind in ("snapshot", "physics.state")})
            if "sink" in document["nodes"]:
                received = [event.payload["frame"].data for event in events
                            if event.kind == "can-host.received" and event.source_node == "sink"]
                expected_data = [index.to_bytes(8, "little") for index in range(1000)]
                if received != expected_data:
                    raise AssertionError("host CAN order/payload differs from the independent1000-frame sequence")
                row["delivered_frames"] = len(received)
        return row
    except BaseException as error:
        error.benchmark_observations = row
        raise


def host_documents():
    idle = {"schema": "sim/v1", "runtime": {"duration": "10s", "quantum": "1ms"},
        "nodes": {"bus": {"kind": "bus.can"}}, "experiment": {}}
    can = {"schema": "sim/v1", "runtime": {"duration": "100ms", "quantum": "1ms"},
        "nodes": {"bus": {"kind": "bus.can"}, "source": {"kind": "device.can-host"},
                  "sink": {"kind": "device.can-host"}},
        "links": [["source.can", "bus.can"], ["sink.can", "bus.can"]],
        "experiment": {"inputs": [{"at": str(index // 10 + 1) + "ms", "endpoint": "source.tx",
            "value": {"id": 0x123, "dlc": 8, "data_hex": index.to_bytes(8, "little").hex()}}
            for index in range(1000)]}}
    return {"idle": idle, "can": can}


def busy_fixture(directory):
    directory.mkdir()
    firmware = directory / "busy-mmio.elf"
    command = ["arm-none-eabi-gcc", "-mcpu=cortex-m7", "-mthumb", "-O2", "-Wall", "-Wextra", "-Werror",
        "-ffreestanding", "-fno-builtin", "-fno-stack-protector", "-nostdlib", "-nostartfiles",
        "-Wl,--gc-sections", "-Wl,--build-id=none", "-Wl,-T," + str(ROOT / "smoke/dm_mc02_uart_smoke.ld"),
        "-o", str(firmware), str(ROOT / "smoke/dm_mc02_lab_smoke.c")]
    built = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
    if built.returncode:
        raise RuntimeError("busy-MMIO build failed: " + built.stdout + built.stderr)
    return {"elf": str(firmware), "sha256": identity(firmware)["sha256"],
            "build": [{"command": command, "exit": built.returncode, "output": built.stdout + built.stderr}]}


def benchmark_cases(name, assets, item):
    if name == "host":
        return [(label, document, "mcap") for label, document in host_documents().items()]
    if name in ("single", "dual"):
        case = "minimal-board" if name == "single" else "dual-board"
        build = build_example(case, build_dir=assets / case / "Debug")
        item["firmware"] = build
        return [(case, _case_document(case, firmware=build["elf"]), "mcap")]
    wfi = build_example("minimal-board", build_dir=assets / "wfi")
    busy = busy_fixture(assets / "busy-mmio")
    item["firmware"] = {"wfi": wfi, "busy-mmio": busy}
    cells = []
    for workload, build in item["firmware"].items():
        for topology in (("machine",) if name == "native-cost" else ("machine", "four-wheel")):
            if topology == "four-wheel":
                document = four_wheel_document({"main": build["elf"]}, ROOT / "models/four_wheel.xml", duration="200ms")
            else:
                document = {"schema": "sim/v1", "runtime": {"duration": "200ms", "quantum": "1ms"},
                    "nodes": {"main": {"kind": "machine.qemu", "firmware": build["elf"]}}, "experiment": {}}
            document["experiment"]["assertions"] = [{"before": "200ms", "origin": "session",
                "uart_contains": {"endpoint": "main.uart1", "text": "SIM_READY minimal-board\n" if workload == "wfi" else "LAB_READY\n"}}]
            for mode in (("discard",) if name == "native-cost" else ("discard", "file", "mcap")):
                cells.append((workload + "-" + topology + "-" + mode, document, mode))
    return cells


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("host", "single", "dual", "all", "attribution", "native-cost"), default="all")
    parser.add_argument("--capture", choices=("none", "mmio"), default="none")
    parser.add_argument("--repeat", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--qemu", type=Path, default=Path(os.environ.get("QEMU_SYSTEM_ARM", ROOT / "build/qemu/qemu-system-arm")))
    parser.add_argument("--sim", help="compiled Rust CLI (or SIM_EXECUTABLE)")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.capture != "none":
        parser.error("private Python MMIO-window instrumentation retired; use native QEMU tracing or an external profiler")
    report_path = args.report.parent.resolve() / args.report.name
    assets = report_path.parent / (report_path.stem + "-assets")
    for path in (report_path, assets):
        if os.path.lexists(path):
            raise FileExistsError(path)
    sim = resolve_sim(args.sim)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    assets.mkdir()
    payload = {"status": "FAIL", "platform": platform.platform(), "sim": identity(sim), "cases": {},
        "conditions": "Every sample is a fresh compiled CLI, includes startup/output/process cleanup. "
            "Linux /proc CPU and RSS are sampled cumulative process values; final unsampled CPU may be absent. "
            "All modes serialize CLI JSON; discard drains it, file writes it, mcap additionally records MCAP. "
            "No QMP/world/encoder exclusive-cost attribution or realtime claim. Build time excluded. "
            "Host idle=10000 cuts; CAN=1000 ordered frames within sim/v1's1024-input admission quota. "
            "Native workloads=200ms/1ms; private Python QMP,World and MMIO monkeypatches retired."}
    interrupted = False
    with report_path.open("x") as output:
        try:
            for name in (("host", "single", "dual") if args.case == "all" else (args.case,)):
                item = payload["cases"][name] = {"status": "FAIL", "cells": []}
                try:
                    if name != "host" and not os.access(args.qemu, os.X_OK):
                        raise Blocked(f"project QEMU executable missing: {args.qemu}")
                    qemu = None if name == "host" else regular_file(args.qemu, "QEMU", executable=True)
                    if qemu is not None:
                        item["qemu"] = identity(qemu)
                    cells = benchmark_cases(name, assets, item)
                    for label, document, mode in cells:
                        cell = {"label": label, "output": mode, "runs": []}
                        item["cells"].append(cell)
                        for index in range(args.repeat):
                            cell["runs"].append(execute(document, assets / f"{name}-{label}-{index + 1}", sim=sim, qemu=qemu, output_mode=mode))
                        cell["median_wall_s"] = median(row["elapsed_including_cleanup_s"] for row in cell["runs"])
                    item["status"] = "PASS"
                except Exception as error:
                    item.update(status="BLOCKED" if isinstance(error, (Blocked, FileNotFoundError)) else "FAIL",
                        error=f"{type(error).__name__}: {error}", observations=getattr(error, "benchmark_observations", None))
            if payload["sim"] != identity(sim):
                raise AssertionError("Rust executable changed during benchmark")
            statuses = {item["status"] for item in payload["cases"].values()}
            payload["status"] = "FAIL" if "FAIL" in statuses else "BLOCKED" if "BLOCKED" in statuses else "PASS"
        except KeyboardInterrupt as error:
            interrupted = True
            payload["error"] = "KeyboardInterrupt"
            payload["partial_observations"] = getattr(error, "benchmark_observations", None)
        except Exception as error:
            payload["error"] = f"{type(error).__name__}: {error}"
        json.dump(payload, output, indent=2)
        output.write("\n")
    print(f"Simulation benchmark: {payload['status']} ({report_path})")
    return 130 if interrupted else {"PASS": 0, "FAIL": 1, "BLOCKED": 78}[payload["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
