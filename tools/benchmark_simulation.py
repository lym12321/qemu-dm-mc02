#!/usr/bin/env python3
"""Fixed, instrumented Simulation baselines; these are not realtime guarantees."""

import argparse
from collections import Counter
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smoke.simulation_smoke import Blocked, Observations, _plain, build_example, run_case
from sim.api import LinkSpec, NodeSpec, Topology
from sim.fabric import CanBusFactory, CanFrame
from sim.devices.can_host import CanHostFactory
from sim.runtime import Simulation


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


def native(case, qemu):
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
        observations = run_case(case, qemu=qemu)
    metrics["elapsed_including_cleanup_s"] = perf_counter() - begun
    cuts = {row["mapped_time_ns"] for row in observations["snapshots"]}
    metrics.update(cuts=len(cuts - {0}), simulated_ns=max(cuts),
                   machine_snapshot_counts=Counter(row["source"] for row in observations["snapshots"]),
                   events=observations["events"], observations_json_bytes=json_bytes(observations),
                   observations=observations)
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("host", "single", "dual", "all"), default="all")
    parser.add_argument("--repeat", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--qemu", type=Path, default=Path(os.environ.get(
        "QEMU_SYSTEM_ARM", ROOT / "build/qemu/qemu-system-arm")))
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    payload = {"status": "FAIL", "python": sys.version, "platform": platform.platform(),
        "initial_usage": usage(), "cases": {}, "limits":
        "Instrumented fixed workloads, observer/JSON costs included; no realtime claim. "
        "Host peak RSS is cumulative process lifetime; QEMU RSS is sampled at snapshots. "
        "Native scenes: 200ms/1ms quantum; host: 10000 idle cuts or 100x100 CAN frames. "
        "Host queues128/history16; native observation budget8192; build Debug. "
        "No trace file is produced; JSON volume is measured separately from MCAP."}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x", encoding="utf-8") as output:
        try:
            for name in (("host", "single", "dual") if args.case == "all" else (args.case,)):
                item = payload["cases"][name] = {"status": "FAIL", "runs": []}
                try:
                    if name != "host":
                        if not args.qemu.is_file() or not os.access(args.qemu, os.X_OK):
                            raise Blocked(f"project QEMU executable missing: {args.qemu}")
                        item["qemu"] = {"path": str(args.qemu.resolve()),
                            "sha256": hashlib.sha256(args.qemu.read_bytes()).hexdigest(),
                            "version": subprocess.check_output([str(args.qemu), "--version"],
                                text=True, timeout=3).strip()}
                        case = "minimal-board" if name == "single" else "dual-board"
                        item["firmware"] = build_example(case)
                    for _ in range(args.repeat):
                        before = usage()
                        row = host() if name == "host" else native(case, args.qemu)
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
                    item.update(status="BLOCKED" if isinstance(error, Blocked) else "FAIL",
                                error=f"{type(error).__name__}: {error}",
                                notes=list(getattr(error, "__notes__", ())))
                    if hasattr(error, "simulation_observations"):
                        item["partial_observations"] = error.simulation_observations
            statuses = {item["status"] for item in payload["cases"].values()}
            payload["status"] = "FAIL" if "FAIL" in statuses else "BLOCKED" if "BLOCKED" in statuses else "PASS"
        except KeyboardInterrupt:
            payload.update(status="FAIL", error="KeyboardInterrupt")
        finally:
            json.dump(payload, output, indent=2)
            output.write("\n")
    print(f"Simulation benchmark: {payload['status']} ({args.report})")
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 78}[payload["status"]]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
