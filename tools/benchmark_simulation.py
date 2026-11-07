#!/usr/bin/env python3
"""Fixed, instrumented Simulation baselines; these are not realtime guarantees."""

import argparse
from collections import Counter
from contextlib import ExitStack
import errno
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
from statistics import median
import subprocess
import sys
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smoke.simulation_smoke import Blocked, Observations, _plain, build_example, run_case
from sim.api import BackendUnavailable, LinkSpec, NodeSpec, Topology
from sim.fabric import CanBusFactory, CanFrame
from sim.devices.can_host import CanHostFactory
from sim.runtime import Simulation


MMIO_WINDOWS_NS = (1_000_000, 10_000_000, 100_000_000, 199_000_000)
MMIO_WINDOW_NS = 10_000
MMIO_MAX_BYTES = 8 * 1024 * 1024
MMIO_MAX_LINES = 65536
MMIO_MAX_LINE_BYTES = 4096
_MMIO_EVENTS = ("memory_region_ops_read", "memory_region_ops_write")


def _parse_mmio_log(path):
    """Bounded reader for the pinned QEMU log backend, not a guest clock."""
    accesses, recompiles, exec_lines = Counter(), Counter(), Counter()
    digest, size, lines = hashlib.sha256(), 0, 0
    mmio = re.compile(r"memory_region_ops_(read|write) cpu (\d+) mr 0x[0-9a-fA-F]+ "
                      r"addr (0x[0-9a-fA-F]+) value (0x[0-9a-fA-F]+) size (\d+) name '([^']*)'")
    rewind = re.compile(r"cpu_io_recompile: rewound execution of TB to ([0-9a-fA-F]{1,16})")
    execution = {
        "trace": re.compile(r"Trace \d+: 0x[0-9a-fA-F]+ \["
                            r"[0-9a-fA-F]+/[0-9a-fA-F]+/[0-9a-fA-F]+/[0-9a-fA-F]+\](?: .*)?"),
        "link": re.compile(r"Linking TBs 0x[0-9a-fA-F]+ index [01] -> 0x[0-9a-fA-F]+"),
        "stop": re.compile(r"Stopped execution of TB chain before 0x[0-9a-fA-F]+ \[[0-9a-fA-F]+\](?: .*)?"),
    }
    with Path(path).open("rb") as source:
        while raw := source.readline(MMIO_MAX_LINE_BYTES + 1):
            size += len(raw)
            lines += 1
            if size >= MMIO_MAX_BYTES or lines > MMIO_MAX_LINES or len(raw) > MMIO_MAX_LINE_BYTES:
                raise BufferError("MMIO log capacity exceeded")
            if not raw.endswith(b"\n"):
                raise ValueError("truncated MMIO log record")
            digest.update(raw)
            # QEMU's optional prefix is a host timestamp, never a guest timestamp.
            line = re.sub(r"^\d+@\d+\.\d+: ?", "", raw.decode("utf-8").rstrip("\n"))
            if match := mmio.fullmatch(line):
                operation, cpu, address, value, width, region = match.groups()
                if (int(cpu) > 0x7fffffff or int(address, 16) > 0xffffffffffffffff
                        or int(value, 16) > 0xffffffffffffffff or int(width) not in (1, 2, 4, 8)):
                    raise ValueError(f"invalid MMIO fields at line {lines}")
                accesses[(operation, hex(int(address, 16)), int(width), region)] += 1
            elif match := rewind.fullmatch(line):
                recompiles[hex(int(match[1], 16))] += 1
            else:
                kind = next((name for name, pattern in execution.items() if pattern.fullmatch(line)), None)
                if kind is None:
                    raise ValueError(f"unrecognized MMIO log record at line {lines}")
                exec_lines[kind] += 1
    return {"path": str(Path(path).resolve()), "sha256": digest.hexdigest(), "bytes": size,
            "lines": lines, "accesses": [dict(operation=op, address=addr, size=width, region=region, count=n)
                for (op, addr, width, region), n in sorted(accesses.items())],
            "recompiles": [dict(pc=pc, count=n) for pc, n in sorted(recompiles.items())],
            "exec_lines": dict(exec_lines)}


def _limit_capture_file_size(pid):
    """Bound this owned Linux child, without relaxing an inherited limit."""
    import resource
    if not hasattr(resource, "prlimit"):
        raise Blocked("Linux prlimit is required for bounded native capture")
    try:
        soft, hard = resource.prlimit(pid, resource.RLIMIT_FSIZE)
        if soft != resource.RLIM_INFINITY and soft < MMIO_MAX_BYTES:
            raise Blocked("inherited QEMU file limit is below the fixed8MiB capture budget")
        resource.prlimit(pid, resource.RLIMIT_FSIZE, (MMIO_MAX_BYTES, hard))
        if resource.prlimit(pid, resource.RLIMIT_FSIZE) != (MMIO_MAX_BYTES, hard):
            raise RuntimeError("QEMU capture file limit was not applied")
    except OSError as error:
        if error.errno in (errno.EPERM, errno.EACCES, errno.ENOSYS):
            raise Blocked("cannot establish QEMU capture file limit: " + str(error)) from error
        raise


def _capture_receipt(machine, cut):
    execution, status = machine.command_many((("x-query-virtual-execution", None), ("query-status", None)))
    native = machine.to_native(cut)
    if (execution.get("state") != "reached" or execution.get("target-ns") != native
            or execution.get("current-ns") != native or status.get("running") is not False
            or status.get("status") != "paused"):
        raise RuntimeError("MMIO window lacks an exact paused native receipt")
    return {"mapped_time_ns": cut, "native_time_ns": native, "execution": execution, "status": status}


def _capture_jit(machine):
    raw = machine.command("x-query-jit")["human-readable-text"]
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 65536:
        raise BufferError("JIT diagnostic exceeds65536 bytes")
    result = {"raw": raw}
    for field, label in (("tb_count", "TB count"), ("tb_flush_count", "TB flush count"),
                         ("tb_invalidate_count", "TB invalidate count")):
        matches = re.findall(r"^" + label + r"\s+(\d+)\s*$", raw, re.MULTILINE)
        if len(matches) != 1:
            raise ValueError("JIT diagnostic lacks unique " + label)
        result[field] = int(matches[0])
    return result


def _capture_trace_state(machine, expected):
    # Unknown exact names are QMP errors; a wildcard safely discovers absence.
    states = machine.command("trace-event-get-state", {"name": "memory_region_ops_*"})
    for name in _MMIO_EVENTS:
        found = [state for state in states if state.get("name") == name]
        if not found or any(state.get("state") == "unavailable" for state in found):
            raise Blocked("QEMU log trace is unavailable: " + name)
        if len(found) != 1 or found[0].get("state") != expected:
            raise RuntimeError("unexpected QEMU trace state: " + repr(states))


def _mmio_capture(simulation, machine, directory, row):
    capture = row["capture"] = {"mode": "mmio", "windows": []}
    available = {item["name"] for item in machine.command("query-commands")}
    needed = {"human-monitor-command", "trace-event-get-state", "trace-event-set-state", "x-query-jit"}
    if missing := needed - available:
        raise Blocked("QEMU capture commands unavailable: " + ", ".join(sorted(missing)))
    _capture_trace_state(machine, "disabled")
    _limit_capture_file_size(machine.connection_info["pid"])
    for start in MMIO_WINDOWS_NS:
        window = {"start_ns": start, "end_ns": start + MMIO_WINDOW_NS, "status": "FAIL"}
        capture["windows"].append(window)
        simulation.run_until(start)
        window["before"] = _capture_receipt(machine, start)
        window["jit_before"] = _capture_jit(machine)
        path = (directory / f"mmio-{start}.log").resolve()
        window["log_path"] = str(path)
        if path.exists():
            raise FileExistsError(path)
        primary = None
        try:
            quoted = '"' + str(path).replace('\\', '\\\\').replace('"', '\\"') + '"'
            for command in ("logfile " + quoted, "log exec"):
                if machine.command("human-monitor-command", {"command-line": command}) != "":
                    raise RuntimeError("QEMU rejected capture command: " + command)
            # HMP logfile errors may only go to stderr. Do not manufacture this file.
            if not path.is_file() or path.is_symlink() or path.stat().st_size != 0:
                raise RuntimeError("QEMU did not create an empty capture file")
            for name in _MMIO_EVENTS:
                machine.command("trace-event-set-state", {"name": name, "enable": True})
            _capture_trace_state(machine, "enabled")
            simulation.run_until(start + MMIO_WINDOW_NS)
        except BaseException as error:
            primary = error
        finally:
            # Once failed, the existing Simulation.close/reap is the only cleanup
            # authority; no further command can safely use the failed Machine.
            if str(simulation.state) == "paused":
                commands = [("trace-event-set-state", {"name": name, "enable": False})
                            for name in _MMIO_EVENTS]
                commands.append(("human-monitor-command", {"command-line": "log none"}))
                for name, arguments in commands:
                    if str(simulation.state) != "paused":
                        break
                    try:
                        result = machine.command(name, arguments)
                        if name == "human-monitor-command" and result != "":
                            raise RuntimeError("QEMU rejected log none during capture cleanup")
                    except BaseException as error:
                        if primary is None:
                            primary = error
                        else:
                            primary.add_note(f"trace disable failed: {type(error).__name__}: {error}")
            if primary is not None:
                raise primary
        _capture_trace_state(machine, "disabled")
        window["after"] = _capture_receipt(machine, start + MMIO_WINDOW_NS)
        window["jit_after"] = _capture_jit(machine)
        window["log"] = _parse_mmio_log(path)
        window["status"] = "PASS"
        # Restore the ordinary cut grid; each diagnostic window adds one cut.
        simulation.run_until(start + 1_000_000)
    simulation.run_until(200_000_000)


def json_bytes(value):
    return len(json.dumps(_plain(value), separators=(",", ":")).encode("ascii"))


def usage():
    if not sys.platform.startswith("linux"):
        return {"host_peak_rss_bytes": None, "host_cpu_s": None, "children_cpu_s": None}
    import resource
    host, children = (resource.getrusage(who) for who in
                      (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN))
    return {"host_peak_rss_bytes": host.ru_maxrss * 1024,
            "host_current_rss_bytes": int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE"),
            "host_cpu_s": host.ru_utime + host.ru_stime,
            "children_cpu_s": children.ru_utime + children.ru_stime}


def host():
    rows = {}
    for mode in ("idle", "can"):
        count = events = size = 0
        def observe(event):
            nonlocal count, events, size
            events += 1
            size += json_bytes(event)
            if event.kind == "can-host.received":
                assert event.source_node == "sink"
                assert event.payload["frame"].data == count.to_bytes(8, "little")
                count += 1
        nodes = [NodeSpec("bus", "bus", {"max_pending": 128, "dispatch_budget": 128})]
        links = []
        if mode == "can":
            nodes += [NodeSpec(name, "host") for name in ("source", "sink")]
            links = [LinkSpec(f"{name}.can", "bus.can") for name in ("source", "sink")]
        sim = Simulation(Topology(tuple(nodes), tuple(links)),
                         {"bus": CanBusFactory(), "host": CanHostFactory()},
                         quantum_ns=1_000_000, max_pending_inputs=128,
                         event_history=16, on_event=observe)
        begun = perf_counter()
        with sim:
            started = perf_counter()
            if mode == "can":
                for group in range(100):
                    cut = (group + 1) * 1_000_000
                    for index in range(group * 100, (group + 1) * 100):
                        sim.schedule_input("source.tx", CanFrame(0x123,
                            data=index.to_bytes(8, "little")), at_ns=cut)
                    sim.run_until(cut)
            else:
                sim.run_until(10_000 * 1_000_000)
            elapsed = perf_counter() - started
            assert count == (10_000 if mode == "can" else 0)
            assert sim.components["bus"].pending == 0
            rows[mode] = {"startup_s": started - begun, "execution_s": elapsed,
                "cuts": sim.step_id, "simulated_ns": sim.t_sim_ns,
                "delivered_frames": count, "events": events, "event_json_bytes": size,
                "retained_events": len(sim.events), "dropped_history_events": sim.events_dropped}
        assert str(sim.state) == "closed" and not sim.components
        rows[mode]["cleanup_completed"] = True
    assert not any(name.startswith(("sim.machines.qemu", "mujoco")) for name in sys.modules)
    return rows


def native(case, qemu, *, firmware):
    from sim.machines.qemu.machine import QemuMachine
    metrics = {"qmp_calls": Counter(command=0, command_many=0), "qmp_commands": Counter(), "qmp_s": 0,
               "event_json_bytes": 0, "qemu_rss_samples": 0, "qemu_rss_max_bytes": {}}
    pids = set()
    command, many = QemuMachine.command, QemuMachine.command_many
    start, observe = Simulation.start, Observations.__call__
    def call(machine, requests, callback):
        pids.add(machine.connection_info["pid"])
        metrics["qmp_commands"].update(name for name, _ in requests)
        begun = perf_counter()
        try:
            return callback()
        finally:
            metrics["qmp_s"] += perf_counter() - begun
    def single(machine, name, arguments=None):
        metrics["qmp_calls"]["command"] += 1
        return call(machine, ((name, arguments),), lambda: command(machine, name, arguments))
    def grouped(machine, requests):
        metrics["qmp_calls"]["command_many"] += 1
        return call(machine, requests, lambda: many(machine, requests))
    def started(simulation, *args, **kwargs):
        begun = perf_counter()
        result = start(simulation, *args, **kwargs)
        metrics["startup_s"] = perf_counter() - begun
        return result
    def observed(observer, event):
        metrics["event_json_bytes"] += json_bytes(event)
        if event.kind == "snapshot" and sys.platform.startswith("linux"):
            for pid in pids:
                try:
                    resident = int(Path(f"/proc/{pid}/statm").read_text().split()[1])
                except FileNotFoundError:
                    continue
                rss = resident * os.sysconf("SC_PAGE_SIZE")
                metrics["qemu_rss_max_bytes"][str(pid)] = max(rss,
                    metrics["qemu_rss_max_bytes"].get(str(pid), 0))
                metrics["qemu_rss_samples"] += 1
        return observe(observer, event)
    begun = perf_counter()
    with ExitStack() as stack:
        for owner, name, replacement in ((QemuMachine, "command", single),
                (QemuMachine, "command_many", grouped), (Simulation, "start", started),
                (Observations, "__call__", observed)):
            stack.enter_context(patch.object(owner, name, replacement))
        observations = run_case(case, qemu=qemu, firmware=firmware)
    metrics["elapsed_including_cleanup_s"] = perf_counter() - begun
    cuts = {row["mapped_time_ns"] for row in observations["snapshots"]}
    metrics.update(cuts=len(cuts - {0}), simulated_ns=max(cuts),
                   machine_snapshot_counts=Counter(row["source"] for row in observations["snapshots"]),
                   events=observations["events"], observations_json_bytes=json_bytes(observations),
                   observations=observations)
    return metrics


def _identity(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def attribution_assets(qemu, directory, result):
    """Build the two existing guests once, outside all execution measurements."""
    if not qemu.is_file() or not os.access(qemu, os.X_OK):
        raise Blocked(f"project QEMU executable missing: {qemu}")
    if not sys.platform.startswith("linux") or not Path("/proc/self/stat").is_file():
        raise Blocked("execution attribution requires Linux /proc process evidence")
    compiler = shutil.which("arm-none-eabi-gcc")
    if compiler is None:
        raise Blocked("arm-none-eabi-gcc is required for the real attribution guests")
    directory.mkdir(exist_ok=False)
    result["qemu"] = {**_identity(qemu), "version": subprocess.check_output(
        [str(qemu), "--version"], text=True, timeout=3).strip()}
    result["compiler"] = {**_identity(compiler), "version": subprocess.check_output(
        [compiler, "--version"], text=True, timeout=3).strip()}
    from sim.machines.qemu.qmp import qmp_client_provenance
    result["environment"] = {"python": sys.version, "executable": str(Path(sys.executable).resolve()),
                             "qmp_client": qmp_client_provenance()}
    try:
        result["environment"]["mujoco_version"] = metadata.version("mujoco")
    except metadata.PackageNotFoundError:
        result["environment"]["mujoco_version"] = None
    source = ROOT / "examples/minimal-board"
    inputs = [path for path in sorted(source.iterdir())
              if path.is_file() and (path.suffix in (".c", ".h", ".cmake", ".ld")
                                     or path.name == "CMakeLists.txt")]
    inputs += [ROOT / "smoke/dm_mc02_lab_smoke.c", ROOT / "smoke/dm_mc02_uart_smoke.ld",
               ROOT / "tools/run-lab-channels-smoke.sh", ROOT / "models/four_wheel.xml",
               Path(__file__).resolve()]
    result["inputs"] = [_identity(path) for path in inputs]
    result["firmware"] = {}
    result["firmware"]["wfi"] = build_example("minimal-board", build_dir=directory / "wfi",
                                                 build_type="Debug")
    busy = directory / "busy-mmio.elf"
    command = [compiler, "-mcpu=cortex-m7", "-mthumb", "-O2", "-Wall", "-Wextra", "-Werror",
        "-ffreestanding", "-fno-builtin", "-fno-stack-protector", "-nostdlib", "-nostartfiles",
        "-Wl,--gc-sections", "-Wl,--build-id=none", "-Wl,-T," + str(ROOT / "smoke/dm_mc02_uart_smoke.ld"),
        "-o", str(busy), str(ROOT / "smoke/dm_mc02_lab_smoke.c")]
    built = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
    result["firmware"]["busy-mmio"] = {"elf": str(busy), "build": [{"command": command,
        "exit": built.returncode, "output": (built.stdout + built.stderr)[-16000:]}]}
    if built.returncode:
        raise RuntimeError("existing busy-MMIO fixture failed to compile")
    result["firmware"]["busy-mmio"]["sha256"] = _identity(busy)["sha256"]
    result["model"] = _identity(ROOT / "models/four_wheel.xml")


def _process_sample(pid):
    """Read Linux process identity and cumulative CPU, never a QMP clock."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        resident = int(Path(f"/proc/{pid}/statm").read_text().split()[1])
    except FileNotFoundError:
        return None
    return {"starttime": fields[19],
            "cpu_s": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
            "rss_bytes": resident * os.sysconf("SC_PAGE_SIZE")}


def _attribution_scene(workload, topology, assets, scene_path, registry):
    from sim.apps.presets import four_wheel_document
    from sim.config.scene import load_scene

    firmware = assets["firmware"][workload]["elf"]
    if topology == "four-wheel":
        document = four_wheel_document({"main": firmware}, assets["model"]["path"],
                                       duration="200ms", quantum="1ms")
    else:
        document = {"schema": "sim/v1", "runtime": {"duration": "200ms", "quantum": "1ms"},
                    "nodes": {"main": {"kind": "machine.qemu", "firmware": firmware}}}
    # JSON is a YAML subset; use the ordinary bounded scene loader and factories.
    scene_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return load_scene(scene_path, registry)


def _attribution_output(mode, output, row):
    """Use the CLI encoder once; file mode executes the actual CLI emitter."""
    from sim import cli

    conversion, dumps = cli._json, json.dumps
    depth = 0

    def encode(value):
        nonlocal depth
        begun = perf_counter() if not depth else None
        depth += 1
        try:
            return conversion(value)
        finally:
            depth -= 1
            if not depth:
                row["encoding_s"] += perf_counter() - begun

    def serialized(*args, **kwargs):
        begun = perf_counter()
        try:
            return dumps(*args, **kwargs)
        finally:
            row["encoding_s"] += perf_counter() - begun

    class Writer:
        def write(self, text):
            begun = perf_counter()
            try:
                count = output.write(text)
                row["output_bytes"] += len(text[:count].encode("utf-8"))
                return count
            finally:
                row["writing_s"] += perf_counter() - begun

        def flush(self):
            begun = perf_counter()
            try:
                return output.flush()
            finally:
                row["writing_s"] += perf_counter() - begun

    writer = Writer()

    def observe(event):
        if event.kind == "snapshot":
            return
        row["output_events"] += 1
        if mode == "count":
            return
        begun = perf_counter()
        try:
            with patch.object(cli, "_json", encode):
                if mode == "json":
                    text = serialized(cli._json(event), ensure_ascii=True)
                    row["encoded_bytes"] += len(text.encode("ascii")) + 1
                else:
                    with patch.object(cli, "json", SimpleNamespace(dumps=serialized)), \
                            patch.object(cli.sys, "stdout", writer):
                        cli._emit(event)
        finally:
            row["output_callback_s"] += perf_counter() - begun

    return observe


def attribution_run(workload, topology, mode, assets, directory, row, *, capture="none"):
    """One fresh Simulation; instrumentation has no execution authority."""
    from sim.composition import builtin_registry
    from sim.machines.qemu.machine import QemuMachine

    if capture not in ("none", "mmio") or capture == "mmio" and (topology, mode) != ("machine", "count"):
        raise ValueError("MMIO capture requires the fixed machine/count workload")
    if capture == "mmio" and any(c == "%" or ord(c) < 32 for c in str(directory.resolve())):
        raise ValueError("MMIO capture path cannot contain control characters or QEMU '%' templates")
    row.update(status="FAIL", qmp_calls=Counter(), qmp_commands=Counter(), qmp_wall_s={},
        finish_calls=0, finish_step_s=0.0, world_step_calls=0, world_step_s=0.0,
        encoding_s=0.0, writing_s=0.0, output_callback_s=0.0, output_bytes=0,
        encoded_bytes=0, output_events=0, events=0, snapshots=0, uart_hex="",
        startup_s=0.0, active_s=0.0, cleanup_s=0.0, identities=[], qemu_samples=0,
        qemu_peak_rss_bytes=0, qemu_cpu_sampled_s=0.0, capture={"mode": capture})
    directory.mkdir(exist_ok=False)
    output = simulation = primary = None
    machines, identities, uart = [], {}, bytearray()
    last_sequence, last_snapshot = -1, None
    command, many, finish = QemuMachine.command, QemuMachine.command_many, QemuMachine.finish_step
    before = usage()
    started_usage = active_usage = before
    phase, phase_begun = "startup", perf_counter()
    run_begun = phase_begun

    def sample(machine):
        info = _plain(machine.connection_info)
        pid = info["pid"]
        current = _process_sample(pid)
        if current is None:
            raise RuntimeError("QEMU process disappeared during measurement")
        identity = identities.setdefault(pid, {**info, "starttime": current["starttime"]})
        if identity["starttime"] != current["starttime"]:
            raise RuntimeError("QEMU process identity changed during measurement")
        identity["native_origin_ns"] = info["native_origin_ns"]
        row["qemu_samples"] += 1
        row["qemu_peak_rss_bytes"] = max(row["qemu_peak_rss_bytes"], current["rss_bytes"])
        row["qemu_cpu_sampled_s"] = current["cpu_s"]

    def rpc(machine, kind, requests, callback):
        if machine not in machines:
            machines.append(machine)
        if machine.connection_info["pid"] not in identities:
            sample(machine)
        row["qmp_calls"][kind] += 1
        row["qmp_commands"].update(name for name, _ in requests)
        label = phase + ":" + kind + ":" + ",".join(
            name + ":" + (arguments or {}).get("property", "") for name, arguments in requests)
        begun = perf_counter()
        try:
            return callback()
        finally:
            row["qmp_wall_s"][label] = row["qmp_wall_s"].get(label, 0.0) + perf_counter() - begun

    def single(machine, name, arguments=None):
        return rpc(machine, "command", ((name, arguments),), lambda: command(machine, name, arguments))

    def grouped(machine, requests):
        return rpc(machine, "command_many", requests, lambda: many(machine, requests))

    def finished(machine, context):
        begun = perf_counter()
        row["finish_calls"] += 1
        try:
            return finish(machine, context)
        finally:
            row["finish_step_s"] += perf_counter() - begun

    try:
        registry = builtin_registry(qemu=assets["qemu"]["path"])
        world_factory = registry["physics.mujoco"] if topology == "four-wheel" else None
        create_world = world_factory.create if world_factory is not None else None
        machine_factory, create_machine = registry["machine.qemu"], registry["machine.qemu"].create

        def machine_created(*args, **kwargs):
            machine = create_machine(*args, **kwargs)
            machines.append(machine)
            return machine

        def world_created(*args, **kwargs):
            world = create_world(*args, **kwargs)
            step = world.step

            def stepped(context):
                begun = perf_counter()
                row["world_step_calls"] += 1
                try:
                    return step(context)
                finally:
                    row["world_step_s"] += perf_counter() - begun

            world.step = stepped
            return world

        scene = _attribution_scene(workload, topology, assets, directory / "scene.json", registry)
        if mode == "file":
            output = (directory / "events.jsonl").open("x", encoding="utf-8", newline="\n")
        emit = _attribution_output(mode, output, row)

        def observe(event):
            nonlocal last_sequence, last_snapshot
            row["events"] += 1
            if row["events"] > 65536 or event.sequence <= last_sequence:
                raise BufferError("attribution event order/capacity exceeded")
            last_sequence = event.sequence
            if event.kind == "uart" and event.source_node == "main" and event.source_port == "uart1":
                if type(event.payload) is not bytes or len(uart) + len(event.payload) > 16384:
                    raise BufferError("attribution UART observation exceeds16384 bytes")
                uart.extend(event.payload)
            if event.kind == "snapshot" and event.source_node == "main":
                row["snapshots"] += 1
                last_snapshot = event.mapped_time_ns
                for machine in machines:
                    sample(machine)
            emit(event)

        simulation = Simulation(scene.topology, registry, quantum_ns=1_000_000,
            startup_timeout_s=10, host_timeout_s=30, event_history=16, on_event=observe)
        with ExitStack() as stack:
            for owner, name, replacement in ((QemuMachine, "command", single),
                    (QemuMachine, "command_many", grouped), (QemuMachine, "finish_step", finished),
                    (machine_factory, "create", machine_created)):
                stack.enter_context(patch.object(owner, name, replacement))
            if world_factory is not None:
                stack.enter_context(patch.object(world_factory, "create", world_created))
            simulation.start()
            row["session_id"] = simulation.session.session_id
            row["startup_s"] = perf_counter() - phase_begun
            started_usage = usage()
            phase, phase_begun = "active", perf_counter()
            if capture == "mmio":
                _mmio_capture(simulation, simulation.components["main"], directory, row)
            else:
                simulation.run_until(200_000_000)
            row["active_s"] = perf_counter() - phase_begun
            active_usage = usage()
            phase = "acceptance"
            for machine in machines:
                sample(machine)
            ready = b"SIM_READY minimal-board\n" if workload == "wfi" else b"LAB_READY\n"
            if ready not in uart or b"FAIL" in uart:
                raise AssertionError("actual firmware ready is missing or firmware reported failure")
            expected_cuts = 204 if capture == "mmio" else 200
            if (simulation.t_sim_ns != 200_000_000 or simulation.step_id != expected_cuts
                    or last_snapshot != 200_000_000):
                raise AssertionError(f"benchmark did not complete the fixed200ms/{expected_cuts}cuts workload")
            if capture == "mmio" and workload == "busy-mmio" and any(
                    not window["log"]["accesses"] for window in row["capture"]["windows"]):
                raise AssertionError("busy firmware produced no MMIO evidence in a capture window")
            row["ready"] = True
    except BaseException as error:
        primary = error
    finally:
        elapsed = perf_counter() - phase_begun
        if phase == "startup":
            row["startup_s"] = elapsed
            started_usage = active_usage = usage()
        elif phase == "active":
            row["active_s"] = elapsed
            active_usage = usage()
        try:
            row["uart_hex"] = uart.hex()
            row["last_committed_cut_ns"] = simulation.t_sim_ns if simulation is not None else 0
            row["cuts"] = simulation.step_id if simulation is not None else 0
            row["pending"] = {}
            for machine in machines:
                if not machine.started:
                    continue
                info = _plain(machine.connection_info)
                identities.setdefault(info["pid"], {**info, "starttime": None})
                if machine.pending is not None:
                    row["pending"][str(info["pid"])] = _plain(machine.pending)
            row["simulation_state_before_cleanup"] = str(simulation.state) if simulation is not None else None
        except BaseException as error:
            if primary is None:
                primary = error
            else:
                primary.add_note(f"failure diagnostics failed: {type(error).__name__}: {error}")
        row["identities"] = list(identities.values())
        before_cleanup = usage()
        cleanup_begun = perf_counter()
        for label, close in (("Simulation", simulation.close if simulation is not None else None),
                             ("event file", output.close if output is not None else None)):
            if close is not None:
                try:
                    close()
                except BaseException as error:
                    if primary is None:
                        primary = error
                    else:
                        primary.add_note(f"{label} cleanup failed: {type(error).__name__}: {error}")
        row["cleanup_s"] = perf_counter() - cleanup_begun
        after_cleanup = usage()
        verification_begun = perf_counter()
        try:
            gone = all((current := _process_sample(identity["pid"])) is None or
                       current["starttime"] != identity["starttime"] for identity in identities.values())
            sockets = [Path(path) for identity in identities.values() for path in identity["sockets"].values()]
            row["resource_cleanup"] = {"process_identities_gone": gone,
                "sockets_gone": all(not path.exists() for path in sockets),
                "socket_directories_gone": all(not path.parent.exists() for path in sockets),
                "simulation_closed": simulation is None or str(simulation.state) == "closed",
                "components_released": simulation is None or not simulation.components,
                "event_file_closed": output is None or output.closed}
            if not all(row["resource_cleanup"].values()):
                raise RuntimeError("attribution resources were not completely released")
            for window in row["capture"].get("windows", ()):
                log = window.get("log")
                if log is not None and (Path(log["path"]).stat().st_size != log["bytes"]
                        or _identity(log["path"])["sha256"] != log["sha256"]):
                    raise RuntimeError("MMIO log changed after its paused capture window")
            for expected in (assets["qemu"], assets["compiler"], assets["model"], *assets["inputs"],
                             {"path": assets["firmware"][workload]["elf"],
                              "sha256": assets["firmware"][workload]["sha256"]}):
                if _identity(expected["path"])["sha256"] != expected["sha256"]:
                    raise RuntimeError("benchmark input changed: " + expected["path"])
            row["input_hashes_verified"] = True
            if output is not None:
                row["output"] = _identity(directory / "events.jsonl")
                if (directory / "events.jsonl").stat().st_size != row["output_bytes"]:
                    raise RuntimeError("event file size disagrees with measured writes")
        except BaseException as error:
            if primary is None:
                primary = error
            else:
                primary.add_note(f"post-cleanup verification failed: {type(error).__name__}: {error}")
        after = usage()
        row["verification_s"] = perf_counter() - verification_begun
        row["elapsed_including_cleanup_s"] = perf_counter() - run_begun
        row["qmp_total_s"] = sum(row["qmp_wall_s"].values())
        row["usage"] = {"host_peak_rss_bytes": after["host_peak_rss_bytes"],
            "host_current_rss_bytes": after.get("host_current_rss_bytes"),
            "startup_host_cpu_s": started_usage["host_cpu_s"] - before["host_cpu_s"],
            "active_host_cpu_s": active_usage["host_cpu_s"] - started_usage["host_cpu_s"],
            "cleanup_host_cpu_s": after_cleanup["host_cpu_s"] - before_cleanup["host_cpu_s"],
            "verification_host_cpu_s": after["host_cpu_s"] - after_cleanup["host_cpu_s"],
            "children_cpu_reaped_s": after_cleanup["children_cpu_s"] - before["children_cpu_s"]}
        if primary is not None:
            row.update(status="BLOCKED" if isinstance(primary, (Blocked, BackendUnavailable)) else "FAIL",
                       error=f"{type(primary).__name__}: {primary}",
                       notes=list(getattr(primary, "__notes__", ())))
        else:
            row["status"] = "PASS"
    if isinstance(primary, (KeyboardInterrupt, SystemExit)):
        raise primary


def attribution(item, qemu, directory, repeat, *, native_cost=False, capture="none"):
    directory = directory.resolve()
    item.update(status="FAIL", conditions={"duration_ns": 200_000_000, "quantum_ns": 1_000_000,
        "startup_timeout_s": 10, "host_timeout_s": 30, "icount": "shift=0,align=off,sleep=off",
        "event_history": 16, "event_budget": 65536, "uart_budget_bytes": 16384,
        "timing": "QMP, finish, world and output callback times are inclusive and overlap; do not add them. "
        "Encoding measures conversion+dumps once; writing measures actual write+flush. "
        "Output callback time includes local instrumentation/patching, not a pure encoder cost. "
        "Count skips serialization; encoded_bytes counts discarded JSON only, output_bytes actual file writes. "
        "QMP wall labels include their startup/active phase and exclude native client negotiation. "
        "Every planned repetition uses a new Session; failures are retained, never retried. "
        "Compile time is outside all runs. "
        "Host peak RSS is cumulative. QEMU RSS/CPU are sampled for the entire process, not guest-only; "
        "CPU resolution is1/CLK_TCK. Reaped child CPU includes optional dependency probes. "
        "Ready verifies actual UART and does not establish a CPU/physics barrier."}, assets={}, cells=[])
    if native_cost:
        item["conditions"].update(capture=capture, cuts=204 if capture == "mmio" else 200,
            windows_ns=list(MMIO_WINDOWS_NS) if capture == "mmio" else [],
            window_duration_ns=MMIO_WINDOW_NS if capture == "mmio" else None,
            log_max_bytes=MMIO_MAX_BYTES, log_max_lines=MMIO_MAX_LINES, log_max_line_bytes=MMIO_MAX_LINE_BYTES,
            interpretation="Single-machine/count only. MMIO capture adds four cuts and trace IO; "
            "its timings are diagnostic, not a baseline. Log prefixes are host wall time, not guest timestamps. "
            "MMIO addresses are absolute; PCs identify icount rewind sites, not extra device deliveries. "
            "JIT TB count is resident blocks, not total translations; retain flush/invalidation counts. "
            "External sampling is separate: preserve collector warnings and unresolved PCs. "
            "Capture=none disables this tool's tracing, not externally applied profilers.")
    attribution_assets(qemu, directory, item["assets"])
    for workload in ("wfi", "busy-mmio"):
        for topology in (("machine",) if native_cost else ("machine", "four-wheel")):
            for mode in (("count",) if native_cost else ("count", "json", "file")):
                cell = {"workload": workload, "topology": topology, "output": mode,
                        "status": "FAIL", "planned_runs": repeat, "runs": []}
                item["cells"].append(cell)
                for index in range(repeat):
                    row = {"run": index + 1}
                    cell["runs"].append(row)
                    attribution_run(workload, topology, mode, item["assets"],
                        directory / f"{workload}-{topology}-{mode}-{index + 1}", row, capture=capture)
                statuses = {row["status"] for row in cell["runs"]}
                cell["status"] = "FAIL" if "FAIL" in statuses else "BLOCKED" if "BLOCKED" in statuses else "PASS"
                passed = [row for row in cell["runs"] if row["status"] == "PASS"]
                cell["passed_runs"] = len(passed)
                cell["summary_valid"] = len(passed) == repeat
                cell["median"] = {name: median(row[name] for row in passed) for name in
                    ("startup_s", "active_s", "cleanup_s", "verification_s", "elapsed_including_cleanup_s",
                     "qmp_total_s", "finish_step_s", "world_step_s",
                     "encoding_s", "writing_s", "output_callback_s", "qemu_cpu_sampled_s",
                     "qemu_peak_rss_bytes") } if cell["summary_valid"] else {}
    statuses = {cell["status"] for cell in item["cells"]}
    item["status"] = "FAIL" if "FAIL" in statuses else "BLOCKED" if "BLOCKED" in statuses else "PASS"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("host", "single", "dual", "all", "attribution", "native-cost"), default="all")
    parser.add_argument("--capture", choices=("none", "mmio"), default=None,
                        help="native-cost only: bounded QEMU MMIO/EXEC logs (default: none)")
    parser.add_argument("--repeat", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--qemu", type=Path, default=Path(os.environ.get(
        "QEMU_SYSTEM_ARM", ROOT / "build/qemu/qemu-system-arm")))
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.capture is not None and args.case != "native-cost":
        parser.error("--capture is only valid with --case native-cost")
    payload = {"status": "FAIL", "python": sys.version, "platform": platform.platform(),
        "initial_usage": usage(), "cases": {}, "limits":
        "Instrumented fixed workloads, observer/JSON costs included; no realtime claim. "
        "Host peak RSS is cumulative process lifetime; QEMU RSS is sampled at snapshots. "
        "Native scenes: 200ms/1ms quantum; host: 10000 idle cuts or 100x100 CAN frames. "
        "Host queues128/history16; native observation budget8192; build Debug. "
        "No trace file is produced; JSON volume is measured separately from MCAP."}
    if args.case in ("attribution", "native-cost"):
        payload["limits"] = ("Fixed12-cell execution attribution,65536 event/16384 UART byte bounds per run, "
            "200ms/1ms quantum, startup10s/host30s. File mode creates CLI JSONL; no MCAP. "
            "See cases.attribution.conditions for inclusive instrumentation and precision limits.")
    if args.case == "native-cost":
        payload["limits"] = ("Fixed WFI/busy-MMIO single-machine/count workloads;200ms, "
            "200cuts without trace or204cuts with four10us trace windows. See cases.native-cost.conditions. "
            "Log capacity exhaustion/truncation fails. Trace/sampling times do not establish baseline performance.")
    assets = args.report.parent / (args.report.stem + "-assets")
    for path in ((args.report,) if args.case == "host" else (args.report, assets)):
        if os.path.lexists(path):
            raise FileExistsError(path)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x", encoding="utf-8") as output:
        assets_reserved = False
        try:
            for name in (("host", "single", "dual") if args.case == "all" else (args.case,)):
                item = payload["cases"][name] = {"status": "FAIL", "runs": []}
                try:
                    if name in ("attribution", "native-cost"):
                        attribution(item, args.qemu, assets,
                                    args.repeat, native_cost=name == "native-cost", capture=args.capture or "none")
                        continue
                    if name != "host":
                        if not args.qemu.is_file() or not os.access(args.qemu, os.X_OK):
                            raise Blocked(f"project QEMU executable missing: {args.qemu}")
                        if not assets_reserved:
                            assets.mkdir(exist_ok=False)
                            assets_reserved = True
                        item["qemu"] = {"path": str(args.qemu.resolve()),
                            "sha256": hashlib.sha256(args.qemu.read_bytes()).hexdigest(),
                            "version": subprocess.check_output([str(args.qemu), "--version"],
                                text=True, timeout=3).strip()}
                        case = "minimal-board" if name == "single" else "dual-board"
                        item["firmware"] = build_example(case, build_dir=(assets / case / "Debug").resolve())
                    for _ in range(args.repeat):
                        before = usage()
                        row = host() if name == "host" else native(case, args.qemu,
                            firmware=Path(item["firmware"]["elf"]))
                        row["usage"] = usage()
                        for key in ("host_cpu_s", "children_cpu_s"):
                            if before[key] is not None:
                                row["usage"][key] -= before[key]
                        item["runs"].append(row)
                        if name != "host":
                            assert hashlib.sha256(Path(item["firmware"]["elf"]).read_bytes()).hexdigest() == item["firmware"]["sha256"]
                            assert hashlib.sha256(args.qemu.read_bytes()).hexdigest() == item["qemu"]["sha256"]
                    item["status"] = "PASS"
                except Exception as error:
                    item.update(status="BLOCKED" if isinstance(error, (Blocked, BackendUnavailable)) else "FAIL",
                                error=f"{type(error).__name__}: {error}",
                                notes=list(getattr(error, "__notes__", ())))
                    if hasattr(error, "simulation_observations"):
                        item["partial_observations"] = error.simulation_observations
            for item in payload["cases"].values():
                if item["status"] == "PASS" and "firmware" in item:
                    try:
                        if _identity(item["firmware"]["elf"])["sha256"] != item["firmware"]["sha256"]:
                            raise RuntimeError("firmware ELF changed after native execution")
                        if _identity(args.qemu)["sha256"] != item["qemu"]["sha256"]:
                            raise RuntimeError("QEMU executable changed after native execution")
                    except Exception as error:
                        item.update(status="FAIL", error=f"{type(error).__name__}: {error}")
            statuses = {item["status"] for item in payload["cases"].values()}
            payload["status"] = "FAIL" if "FAIL" in statuses else "BLOCKED" if "BLOCKED" in statuses else "PASS"
        except KeyboardInterrupt as error:
            payload.update(status="FAIL", error="KeyboardInterrupt", interrupted=True)
            if hasattr(error, "simulation_observations"):
                item["partial_observations"] = error.simulation_observations
        finally:
            json.dump(payload, output, indent=2)
            output.write("\n")
    print(f"Simulation benchmark: {payload['status']} ({args.report})")
    if payload.get("interrupted"):
        return 130
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 78}[payload["status"]]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
