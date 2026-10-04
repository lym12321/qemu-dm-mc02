#!/usr/bin/env python3
"""Run the fixed ordinary Release / v2 / MuJoCo business and paced performance gates."""

from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import sys
import time

from dm_mc02_firmware_rtf import (
    TickEpochTracker, deadline_qmp_call, process_cpu_seconds, process_rss_kib,
    forward_tick_delta, read_iwdg_timeouts, read_tick, require_stopped_status,
)
from dm_mc02_qmp import QmpSession
from dm_mc02_socketcan import unpack_wire_frame
from dm_mc02_test_harness import stop

ROOT = Path(__file__).resolve().parents[1]
GATES = ROOT / "smoke/dm_mc02_trobot_business_gates.json"
ENABLE = bytes.fromhex("ff ff ff ff ff ff ff fc")
PERFORMANCE_STEPS = 72_000
LIMITS = [
    "Fixed test-only rotor Izz=.02, damping=.05, gear=1; no physical calibration.",
    "Independent undriven IMU fixture: gyro=(0,0,0), accel=(0,0,1) g.",
    "CAN logfile records guest TX only; RX timestamp does not schedule delivery.",
    "Diagnostics maxima and RSS are sampled, not all-time maxima or peak RSS.",
    "14-second functional fixture; no sustained realtime or global lockstep claim.",
]
PERFORMANCE_LIMITS = LIMITS[:-1] + [
    "Three 72-second paced runs; each measures at least 60 seconds after actual enable.",
    "One 0->6->0 trajectory, followed by continuous 1 kHz zero-target control.",
    "Plant trace timestamps precede ACK and pacing sleep; they do not measure loop completion.",
    "Rebased sampled clock drift and 100 ms phase/gap budget; no global lockstep.",
    "No unthrottled capacity or arbitrary plant performance conclusion.",
]


class RunInterrupted(RuntimeError):
    pass


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def _identity(pid: int) -> dict | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": pid, "starttime": int(fields[19])}
    except FileNotFoundError:
        return None


def _sample_process(identity: dict) -> dict:
    if _identity(identity["pid"]) != identity:
        raise RuntimeError("process identity changed while sampling")
    cpu_before_ns = time.monotonic_ns()
    cpu_seconds = process_cpu_seconds(identity["pid"])
    cpu_after_ns = time.monotonic_ns()
    sample = dict(identity, cpu_seconds=cpu_seconds,
                  cpu_sample_wall_time_ns=(cpu_before_ns + cpu_after_ns) // 2,
                  cpu_query_span_ns=cpu_after_ns - cpu_before_ns,
                  rss_kib=process_rss_kib(identity["pid"]))
    if _identity(identity["pid"]) != identity:
        raise RuntimeError("process identity changed while sampling")
    return sample


def _inputs(elf: Path, qemu: Path) -> dict:
    paths = [elf, qemu, GATES, ROOT / "uv.lock", ROOT / "qemu.lock",
             ROOT / "smoke/dm_mc02_trobot_bench.py",
             ROOT / "smoke/dm_mc02_trobot_bench.xml", Path(__file__)]
    paths += [ROOT / "tools" / name for name in (
        "dm_mc02_sim_worker.py", "dm_mc02_step_coordinator.py",
        "dm_mc02_motor_adapter.py", "dm_mc02_step_protocol.py",
        "dm_mc02_socketcan.py", "dm_mc02_qmp.py",
        "dm_mc02_firmware_rtf.py", "dm_mc02_test_harness.py")]
    paths += sorted((ROOT / "sim").rglob("*.py"))
    result = {}
    for path in paths:
        with path.open("rb") as stream:
            result[str(path)] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def _finite(value) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite(item) for item in value)
    return True


def analyze_trace(rows: list[dict], gates: list[dict], *, expected_steps=14000) -> dict:
    """Apply the original eight gates after checking complete trace input."""
    if not rows or not _finite(rows):
        raise RuntimeError("plant trace is empty or contains non-finite values")
    previous_wall = 0
    previous_plant = -math.inf
    for row in rows:
        if (not isinstance(row.get("wall_time_ns"), int) or
                row["wall_time_ns"] < previous_wall or
                not isinstance(row.get("plant_time_s"), (int, float)) or
                row["plant_time_s"] < previous_plant):
            raise RuntimeError("invalid plant trace timestamps")
        previous_wall = row["wall_time_ns"]
        previous_plant = row["plant_time_s"]
    steps = [row for row in rows if row.get("event") == "step"]
    enabled = [row for row in rows if row.get("event") == "enabled"
               and row.get("enabled") is True]
    if len(steps) != expected_steps or len(enabled) != 1:
        raise RuntimeError(f"trace requires {expected_steps} steps and exactly one enable")
    if any(row.get("event") == "reset" for row in rows):
        raise RuntimeError("plant reset invalidated the business trace")
    for index, row in enumerate(steps, 1):
        if (row.get("dt") != 0.001 or
                abs(row["plant_time_s"] - index * .001) > 1e-8 or
                any(not isinstance(row.get(key), (int, float))
                    for key in ("position", "velocity", "effort")) or
                row.get("gyro") != [0, 0, 0] or
                row.get("accel") != [0, 0, 1]):
            raise RuntimeError("incomplete or invalid fixed 1 ms plant trace")
    origin = round(enabled[0]["plant_time_s"] * 1000)
    results = []
    for gate in gates:
        start, end = gate["window_relative_enable_ms"]
        window = [row for row in steps if start <=
                  round(row["plant_time_s"] * 1000) - origin < end]
        if len(window) != end - start:
            raise RuntimeError("incomplete trajectory window: " + gate["id"])
        velocity = [row["velocity"] for row in window]
        effort = [row["effort"] for row in window]
        target = gate.get("target", 0)
        metric = gate["metric"]
        measured = {
            "max_abs_velocity": lambda: max(map(abs, velocity)),
            "max_velocity": lambda: max(velocity),
            "max_abs_target_error": lambda: max(abs(v - target) for v in velocity),
            "abs_mean_target_error": lambda: abs(statistics.mean(velocity) - target),
            "abs_mean_effort_error": lambda: abs(statistics.mean(effort) - target),
            "min_velocity": lambda: min(velocity),
            "abs_mean_velocity": lambda: abs(statistics.mean(velocity)),
        }[metric]()
        threshold = gate.get("minimum", gate.get("limit"))
        passed = measured >= threshold if metric == "min_velocity" else measured <= threshold
        results.append(dict(id=gate["id"], metric=metric, observed=measured,
                            threshold=threshold, passed=passed, samples=len(window),
                            window_relative_enable_ms=[start, end]))
    return dict(gates=results, steps=len(steps),
                enable_plant_time_s=enabled[0]["plant_time_s"],
                last_plant_time_s=steps[-1]["plant_time_s"])


def analyze_pacing(rows: list[dict], execution: dict) -> dict:
    """Measure the fixed continuous workload; trace and epoch are validated first."""
    samples = [s for s in execution["samples"] if "wall_time_ns" in s]
    ready_ns = execution["ready"]["detected_wall_time_ns"]
    if not _finite(samples) or not samples:
        raise RuntimeError("performance samples missing or non-finite")
    start_index = next((i for i, s in enumerate(samples)
                        if s["wall_time_ns"] > ready_ns), None)
    if start_index is None:
        raise RuntimeError("no complete observation after actual ready")
    start = samples[start_index]
    end_index = next((i for i in range(start_index + 1, len(samples))
                      if samples[i]["wall_time_ns"] - start["wall_time_ns"] >= 60_000_000_000
                      and forward_tick_delta(start["tick"], samples[i]["tick"]) >= 60000), None)
    if end_index is None:
        raise RuntimeError("performance window lacks 60 seconds after ready")
    window = samples[start_index:end_index + 1]
    end = window[-1]
    if any(b["wall_time_ns"] <= a["wall_time_ns"] for a, b in zip(window, window[1:])):
        raise RuntimeError("performance observation time did not advance")
    steps = [r for r in rows if r.get("event") == "step"]
    wall_times = [r["wall_time_ns"] for r in steps]

    def paired(sample):
        index = bisect_left(wall_times, sample["wall_time_ns"])
        if index == 0 or index >= len(steps):
            raise RuntimeError("performance endpoint lacks bracketing plant trace")
        row = min(steps[index - 1:index + 1],
                  key=lambda r: abs(r["wall_time_ns"] - sample["wall_time_ns"]))
        span = sample["tick_query_span_ns"]
        if span < 0:
            raise RuntimeError("negative QMP observation bracket")
        before = sample["wall_time_ns"] - span / 2
        after = sample["wall_time_ns"] + span / 2
        lower = bisect_right(wall_times, before) - 1
        upper = bisect_left(wall_times, after)
        if lower < 0 or upper >= len(steps):
            raise RuntimeError("QMP bracket lacks enclosing plant trace")
        # Bound plant progress using the actual surrounding records. During
        # catch-up, plant milliseconds can advance much faster than host time.
        bounds = [steps[lower]["plant_time_s"], steps[upper]["plant_time_s"]]
        wall_offset = abs(row["wall_time_ns"] - sample["wall_time_ns"]) / 1e9
        return row, bounds, wall_offset

    first, first_bounds, _ = paired(start)
    last, _, _ = paired(end)
    host_seconds = (end["wall_time_ns"] - start["wall_time_ns"]) / 1e9
    tick_seconds = forward_tick_delta(start["tick"], end["tick"]) / 1000
    plant_seconds = last["plant_time_s"] - first["plant_time_s"]
    plant_wall_seconds = (last["wall_time_ns"] - first["wall_time_ns"]) / 1e9
    if plant_seconds < 60 - 1e-8 or plant_wall_seconds <= 0:
        raise RuntimeError("performance plant window lacks 60 seconds after ready")
    endpoint_span = (start["tick_query_span_ns"] + end["tick_query_span_ns"]) / 2e9
    if host_seconds <= endpoint_span:
        raise RuntimeError("QMP brackets cannot resolve the performance interval")
    tick_rtf_interval = [tick_seconds / (host_seconds + endpoint_span),
                         tick_seconds / (host_seconds - endpoint_span)]
    trace_window = [r for r in steps if first["wall_time_ns"] <=
                    r["wall_time_ns"] <= last["wall_time_ns"]]
    phases = [(r["wall_time_ns"] - first["wall_time_ns"]) / 1e9 -
              (r["plant_time_s"] - first["plant_time_s"]) for r in trace_window]
    gap = max((b["wall_time_ns"] - a["wall_time_ns"]) / 1e9
              for a, b in zip(trace_window, trace_window[1:]))
    drifts = []
    pair_errors = []
    for sample in window:
        row, bounds, wall_offset = paired(sample)
        tick_delta = forward_tick_delta(start["tick"], sample["tick"]) / 1000
        drift = tick_delta - (
            row["plant_time_s"] - first["plant_time_s"])
        interval = [tick_delta - (bounds[1] - first_bounds[0]),
                    tick_delta - (bounds[0] - first_bounds[1])]
        drifts.append(dict(seconds=drift, interval_seconds=interval,
                           error_bound_seconds=max(abs(value - drift) for value in interval)))
        pair_errors.append(wall_offset)
    enable_time = next(r["plant_time_s"] for r in rows
                       if r.get("event") == "enabled" and r.get("enabled") is True)
    tail = [r["velocity"] for r in trace_window if r["plant_time_s"] >= enable_time + 6]
    if not tail:
        raise RuntimeError("performance quiet tail is missing")
    resources = {}
    for name in ("qemu", "worker"):
        resource_samples = [s.get(name, {}) for s in window]
        for r in resource_samples:
            if any(not isinstance(r.get(key), (int, float)) or not math.isfinite(r[key])
                   for key in ("cpu_seconds", "rss_kib", "cpu_sample_wall_time_ns", "cpu_query_span_ns")):
                raise RuntimeError("missing or invalid performance resource: " + name)
        identities = {(r["pid"], r["starttime"]) for r in resource_samples}
        cpu_elapsed = (resource_samples[-1]["cpu_sample_wall_time_ns"] -
                       resource_samples[0]["cpu_sample_wall_time_ns"]) / 1e9
        cpu_seconds = resource_samples[-1]["cpu_seconds"] - resource_samples[0]["cpu_seconds"]
        if (len(identities) != 1 or cpu_elapsed <= 0 or cpu_seconds < 0 or
                any(r["rss_kib"] < 0 or r["cpu_query_span_ns"] < 0 for r in resource_samples)):
            raise RuntimeError("invalid performance resource interval: " + name)
        resources[name] = dict(cpu_percent=cpu_seconds / cpu_elapsed * 100,
                               cpu_seconds=cpu_seconds, sample_host_seconds=cpu_elapsed,
                               sampled_max_rss_kib=max(r["rss_kib"] for r in resource_samples),
                               cpu_max_query_span_ns=max(r["cpu_query_span_ns"] for r in resource_samples))
    phase = max(map(abs, phases))
    drift_bound = max(abs(value) for d in drifts for value in d["interval_seconds"])
    plant_rtf = plant_seconds / plant_wall_seconds
    checks = []
    def check(name, observed, limit, lower=False):
        checks.append(dict(id=name, observed=observed, limit=limit,
                           passed=observed >= limit if lower else observed <= limit))
    check("firmware_tick_rtf_lower", tick_rtf_interval[0], .999, lower=True)
    check("plant_rtf_min", plant_rtf, .999, lower=True)
    check("plant_rtf_max", plant_rtf, 1.001)
    check("plant_wall_phase_excursion", phase, .100)
    check("sampled_tick_plant_drift_bound", drift_bound, .100)
    check("max_step_gap", gap, .100)
    check("quiet_tail_max_abs_velocity", max(map(abs, tail)), .15)
    check("quiet_tail_abs_mean_velocity", abs(statistics.mean(tail)), .05)
    return dict(passed=all(c["passed"] for c in checks), checks=checks,
                sample_host_seconds=host_seconds, firmware_tick_seconds=tick_seconds,
                firmware_tick_rtf=tick_seconds / host_seconds,
                firmware_tick_rtf_interval=tick_rtf_interval,
                plant_seconds=plant_seconds, plant_wall_seconds=plant_wall_seconds,
                plant_rtf=plant_rtf, resources=resources,
                max_step_gap_seconds=gap, phase_min_seconds=min(phases), phase_max_seconds=max(phases),
                max_rebased_phase_error_seconds=phase, sampled_tick_plant_drift=drifts,
                max_sampled_drift_bound_seconds=drift_bound,
                max_trace_pair_error_seconds=max(pair_errors),
                max_tick_query_span_ns=max(s["tick_query_span_ns"] for s in window),
                quiet_tail_samples=len(tail), start_sample_index=start_index, end_sample_index=end_index)


def _enable_frames(wire: bytes) -> list:
    frames = []
    for offset in range(0, len(wire) - 83, 84):
        frame = unpack_wire_frame(wire[offset:offset + 84])
        if (frame.can_id, frame.flags, frame.dlc, frame.data) == (1, 0, 8, ENABLE):
            frames.append(frame)
    return frames


def _run_inside(qmp_path: str, cosim: str, can: str, pid: str,
                start_ns: str, output: str, tick_address: str,
                performance: str = "0") -> None:
    out = Path(output)
    sustained = performance == "1"
    frames = PERFORMANCE_STEPS if sustained else 14000
    run_seconds = 85 if sustained else 22
    candidate = dict(error=None, cleanup_errors=[], samples=[], processes={}, ready=None)
    qmp = worker = None
    started = int(start_ns) / 1_000_000_000
    stdout = (out / "worker.stdout").open("w")
    stderr = (out / "worker.stderr").open("w")
    try:
        candidate["processes"]["qemu"] = _identity(int(pid))
        qmp = QmpSession(qmp_path, timeout=2,
                         connect_timeout=min(2, started + 10 - time.monotonic()))
        def call(callback, deadline=started + run_seconds):
            return deadline_qmp_call(qmp, deadline, "business run", callback)

        environment = dict(os.environ, DM_TROBOT_BENCH_TRACE=str(out / "plant.jsonl"))
        environment["DM_TROBOT_BENCH_MAX_EVENTS"] = str(3 * frames if sustained else 50000)
        environment["PYTHONPATH"] = os.pathsep.join([
            str(ROOT / "tools"), str(ROOT / "smoke"),
            environment.get("PYTHONPATH", "")])
        worker = subprocess.Popen([
            sys.executable, str(ROOT / "tools/dm_mc02_sim_worker.py"),
            "--cosim", cosim, "--fdcan", can, "--protocol", "v2",
            "--engine", "mujoco", "--backend", "dm_mc02_trobot_bench:create_backend",
            "--mujoco-model", str(ROOT / "smoke/dm_mc02_trobot_bench.xml"),
            "--motor-count", "1", "--motor-protocol", "dm-mit",
            "--rate", "1000", "--realtime", "--frames", str(frames),
            "--connect-timeout", "2"], stdout=stdout, stderr=stderr, env=environment)
        candidate["processes"]["worker"] = _identity(worker.pid)
        initialization_deadline = min(started + 10, time.monotonic() + 3)
        while time.monotonic() < initialization_deadline:
            diagnostics = call(lambda: qmp.command("qom-get", {
                "path": "/machine", "property": "cosim-diagnostics"}),
                initialization_deadline)
            if "v2_initialized=1" in diagnostics:
                break
            if worker.poll() is not None:
                raise RuntimeError("worker exited before v2 initialization")
            time.sleep(.01)
        else:
            raise RuntimeError("worker v2 initialization deadline expired")
        initial_timeouts, _ = call(lambda: read_iwdg_timeouts(qmp), started + 10)
        if initial_timeouts:
            raise RuntimeError("watchdog already timed out during startup")
        tracker = TickEpochTracker(call(lambda: read_tick(qmp, tick_address),
                                       started + 10), initial_timeouts)
        qmp.clear_events()
        call(lambda: qmp.command("cont"), started + 10)
        while time.monotonic() < started + run_seconds:
            deadline = started + (10 if candidate["ready"] is None else run_seconds)
            tick_before_ns = time.monotonic_ns()
            tick = call(lambda: read_tick(qmp, tick_address), deadline)
            tick_after_ns = time.monotonic_ns()
            timeouts, watchdog = call(lambda: read_iwdg_timeouts(qmp), deadline)
            tracker.observe(tick, events=qmp.get_events(wait=False),
                            watchdog_timeouts=timeouts)
            sample = dict(wall_seconds=time.monotonic() - started, tick=tick,
                          wall_time_ns=(tick_before_ns + tick_after_ns) // 2,
                          tick_query_span_ns=tick_after_ns - tick_before_ns,
                          watchdog=watchdog, cosim=call(lambda: qmp.command("qom-get", {
                              "path": "/machine", "property": "cosim-diagnostics"}), deadline),
                          qemu=_sample_process(candidate["processes"]["qemu"]))
            if worker.poll() is None:
                sample["worker"] = _sample_process(candidate["processes"]["worker"])
            candidate["samples"].append(sample)
            if candidate["ready"] is None and time.monotonic() >= started + 10:
                raise RuntimeError("actual application motor enable deadline expired")
            if candidate["ready"] is None:
                enable = _enable_frames((out / "can.bin").read_bytes())
                if time.monotonic() >= started + 10:
                    raise RuntimeError("actual application motor enable deadline expired")
                if enable:
                    candidate["ready"] = dict(
                        detected_wall_seconds=time.monotonic() - started,
                        detected_wall_time_ns=time.monotonic_ns(),
                        can_timestamp_ns=enable[0].timestamp_ns)
            status = worker.poll()
            if status is not None:
                if status:
                    raise RuntimeError("worker exited with status " + str(status))
                break
            time.sleep(.1)
        else:
            raise RuntimeError("business run deadline expired")
        call(lambda: qmp.command("stop"))
        call(lambda: require_stopped_status(qmp, "business stop", expected_status="paused"))
        timeouts, watchdog = call(lambda: read_iwdg_timeouts(qmp))
        tick = call(lambda: read_tick(qmp, tick_address))
        tracker.observe(tick, events=qmp.get_events(wait=False), watchdog_timeouts=timeouts)
        final_cosim = call(lambda: qmp.command("qom-get", {
            "path": "/machine", "property": "cosim-diagnostics"}))
        candidate["samples"].append(dict(tick=tick, watchdog=watchdog, cosim=final_cosim,
                                          wall_seconds=time.monotonic() - started, stopped=True))
        candidate.update(final_tick=tick, final_watchdog=watchdog)
        call(lambda: qmp.command("quit"))
    except BaseException as exc:
        candidate["error"] = str(exc) or type(exc).__name__
    finally:
        for action in (lambda: stop(worker, process_group=False),
                       lambda: qmp.close() if qmp is not None else None,
                       stdout.close, stderr.close):
            try:
                action()
            except Exception as exc:
                candidate["cleanup_errors"].append(str(exc))
        _write_json(out / "candidate.json", candidate)
    if candidate["error"] or candidate["cleanup_errors"]:
        raise RuntimeError("business consumer failed: " + str(candidate["error"]) +
                           "; cleanup=" + str(candidate["cleanup_errors"]))


def _run_report(args) -> dict:
    report = dict(status="failed", errors=[], skipped=[],
                  limits=PERFORMANCE_LIMITS if args.performance else LIMITS,
                  profile="sustained-paced" if args.performance else "functional")
    before = None
    harness = None
    try:
        if not args.elf.is_file() or not os.access(args.qemu, os.X_OK):
            raise RuntimeError("firmware ELF or QEMU missing; build the ordinary Release and project QEMU first")
        import mujoco
        report["mujoco_version"] = mujoco.__version__
        if args.performance:
            report["host"] = dict(uname=list(os.uname()), cpu_count=os.cpu_count(),
                                  load_average_start=list(os.getloadavg()),
                                  cpu_accounting_hz=os.sysconf("SC_CLK_TCK"),
                                  python_version=sys.version,
                                  cpu_info=Path("/proc/cpuinfo").read_text().split("\n\n")[0])
        symbols = subprocess.check_output(["arm-none-eabi-nm", "-n", str(args.elf)],
                                          text=True, timeout=2)
        match = re.search(r"^([0-9a-fA-F]+)\s+\w\s+xTickCount$", symbols, re.M)
        if not match:
            raise RuntimeError("xTickCount is missing from the supplied ELF")
        tick_address = "0x" + match[1]
        report["tick_address"] = tick_address
        before = _inputs(args.elf, args.qemu)
        report["inputs_sha256"] = before
        _write_json(args.output / "inputs-before.json", before)
        command = [sys.executable, str(ROOT / "tools/dm_mc02_test_harness.py"),
                   "--timeout", "95" if args.performance else "30", "--expect-qemu-quit"]
        for name in ("qmp", "cosim", "can"):
            command += ["--socket", name, "--python-arg", "{" + name + "}"]
        command += ["--python-arg", "{qemu_pid}", "--python-arg", "{qemu_start_ns}",
                    "--python-arg", str(args.output),
                    "--python-arg", tick_address,
                    "--python-arg", "1" if args.performance else "0", "--", str(args.qemu),
                    "-machine", "dm-mc02,fdcan-host-ack=on,iwdg-boot-grace-ms=0,adc-accurate-timing=on",
                    "-kernel", str(args.elf), "-nodefaults", "-display", "none",
                    "-monitor", "none", "-S", "-qmp", "unix:{qmp},server=on,wait=off",
                    "-chardev", "socket,id=cosim,path={cosim},server=on,wait=off", "-serial", "chardev:cosim"]
        command += [value for _ in range(6) for value in ("-serial", "none")]
        command += ["-chardev", "socket,id=can,path={can},server=on,wait=off,logfile=" +
                    str(args.output / "can.bin").replace(",", ",,") +
                    ",logappend=off", "-serial", "chardev:can"]
        source = b"from dm_mc02_trobot_business import _run_inside\nimport signal,sys\ndef interrupted(sig,frame): raise SystemExit(128+sig)\nsignal.signal(signal.SIGTERM,interrupted)\n_run_inside(*sys.argv[1:])\n"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "tools")
        with (args.output / "harness.log").open("wb") as log:
            harness = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log,
                                       stderr=subprocess.STDOUT, env=environment)
            harness.communicate(input=source)
        report["harness_exit_code"] = harness.returncode
        candidate_path = args.output / "candidate.json"
        if candidate_path.exists():
            report["execution"] = json.loads(candidate_path.read_text())
        if harness.returncode:
            raise RuntimeError("integration harness failed; see harness.log and worker.stderr")
        execution = report["execution"]
        identities = execution["processes"]
        if (set(identities) != {"worker", "qemu"} or any(identity is None for identity in identities.values())
                or any(_identity(identity["pid"]) == identity for identity in identities.values())):
            raise RuntimeError("process identity missing or process remains after harness cleanup")
        report["cleanup_verified"] = True
        if execution["error"] or execution["cleanup_errors"]:
            raise RuntimeError("consumer execution or cleanup failed")
        trace = [json.loads(line) for line in (args.output / "plant.jsonl").read_text().splitlines()]
        report.update(analyze_trace(trace, json.loads(GATES.read_text())["gates"],
                                   expected_steps=PERFORMANCE_STEPS if args.performance else 14000))
        if args.performance:
            report["performance"] = analyze_pacing(trace, execution)
        wire = (args.output / "can.bin").read_bytes()
        if len(wire) % 84 or len(_enable_frames(wire)) != 1 or execution["ready"] is None:
            raise RuntimeError("CAN trace incomplete or actual enable missing/repeated")
        report["guest_tx_can_frames"] = len(wire) // 84
        maximums = {}
        for key in ("consume_queue", "consume_dropped", "imu_queue", "rx_bad_frames", "tx_dropped"):
            values = []
            for sample in execution["samples"]:
                match = re.search(r"(?:^|[ ,])" + key + r"=(\d+)", sample["cosim"])
                if not match:
                    raise RuntimeError("missing co-sim diagnostic: " + key)
                values.append(int(match[1]))
            maximums[key] = max(values)
        report["sampled_max_diagnostics"] = maximums
        if any(maximums[key] for key in ("consume_dropped", "rx_bad_frames", "tx_dropped")):
            raise RuntimeError("co-sim diagnostics report dropped or malformed traffic")
        if not all(gate["passed"] for gate in report["gates"]):
            raise RuntimeError("fixed trajectory gate failed")
        if args.performance and not report["performance"]["passed"]:
            raise RuntimeError("sustained paced performance gate failed")
        report["status"] = "passed"
    except ImportError as exc:
        report["errors"].append("MuJoCo environment unavailable: " + str(exc) +
                                "; run uv sync --locked --group dev --extra mujoco in the chosen toolchain environment")
    except BaseException as exc:
        report["errors"].append(str(exc) or type(exc).__name__)
        if isinstance(exc, (RunInterrupted, KeyboardInterrupt)):
            report["interrupted"] = True
    finally:
        if harness is not None and harness.poll() is None:
            try:
                # Let the shared owner finish both groups and its temporary
                # directory before the outer process publishes a verdict.
                harness.send_signal(signal.SIGTERM)
                try:
                    harness.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    stop(harness, process_group=False)
                    report["errors"].append("harness cleanup deadline expired")
            except Exception as exc:
                report["errors"].append("harness cleanup failed: " + str(exc))
        if harness is not None:
            report["harness_exit_code"] = harness.returncode
            candidate_path = args.output / "candidate.json"
            if candidate_path.exists():
                try:
                    report["execution"] = json.loads(candidate_path.read_text())
                    identities = report["execution"]["processes"]
                    report["cleanup_verified"] = bool(identities) and all(
                        identity is not None and _identity(identity["pid"]) != identity
                        for identity in identities.values())
                    if not report["cleanup_verified"]:
                        report["errors"].append("recorded process remains or identity is missing after cleanup")
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    report["errors"].append("cannot verify consumer cleanup: " + str(exc))
        if before is not None:
            try:
                after = _inputs(args.elf, args.qemu)
                report["source_changed_during_run"] = [path for path in before if before[path] != after[path]]
                if report["source_changed_during_run"]:
                    report["errors"].append("input sources changed during run")
            except OSError as exc:
                report["errors"].append("cannot recheck input hashes: " + str(exc))
        if report["errors"]:
            report["status"] = "failed"
        if args.performance and "host" in report:
            report["host"]["load_average_end"] = list(os.getloadavg())
        _write_json(args.output / "report.json", report)
    print("RESULT: ordinary Release business fixture " + report["status"])
    print("Report: " + str(args.output / "report.json"))
    for error in report["errors"]:
        print(error, file=sys.stderr)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elf", type=Path, default=ROOT.parent / "trobot/build/Release/trobot.elf")
    parser.add_argument("--qemu", type=Path, default=Path(os.environ.get(
        "QEMU_SYSTEM_ARM", ROOT / "build/qemu/qemu-system-arm")))
    parser.add_argument("--output", type=Path, default=ROOT / "build/test-results" /
                        ("trobot-business-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")))
    parser.add_argument("--performance", action="store_true",
                        help="three fixed 72-second paced runs with a 60-second post-enable measurement")
    args = parser.parse_args(argv)
    args.elf, args.qemu, args.output = [p.resolve() for p in (args.elf, args.qemu, args.output)]
    try:
        args.output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error("output directory already exists; choose a new path")
    if not args.performance:
        return 0 if _run_report(args)["status"] == "passed" else 1
    summary = dict(status="failed", runs=[], skipped=[], errors=[], limits=PERFORMANCE_LIMITS)
    first_inputs = None
    try:
        for number in range(1, 4):
            run_dir = args.output / f"run-{number}"
            run_dir.mkdir()
            run_args = argparse.Namespace(**(vars(args) | {"output": run_dir}))
            report = _run_report(run_args)
            inputs = report.get("inputs_sha256")
            if first_inputs is None:
                first_inputs = inputs
            elif inputs != first_inputs:
                report["status"] = "failed"
                report["errors"].append("selected inputs changed between performance runs")
                _write_json(run_dir / "report.json", report)
            summary["runs"].append(dict(run=number, status=report["status"],
                                        report=str(run_dir / "report.json"), errors=report["errors"],
                                        ready=report.get("execution", {}).get("ready"),
                                        performance=report.get("performance")))
            if report.get("interrupted"):
                summary["skipped"] = [dict(run=n, reason="interrupted") for n in range(number + 1, 4)]
                break
        if len(summary["runs"]) == 3 and all(r["status"] == "passed" for r in summary["runs"]):
            summary["status"] = "passed"
        _write_json(args.output / "report.json", summary)
    except (RunInterrupted, KeyboardInterrupt) as exc:
        summary["status"] = "failed"
        summary["errors"].append(str(exc) or type(exc).__name__)
        summary["skipped"] = [dict(run=n, reason="interrupted before completion")
                              for n in range(len(summary["runs"]) + 1, 4)]
        _write_json(args.output / "report.json", summary)
    print("RESULT: three-run sustained paced fixture " + summary["status"])
    print("Report: " + str(args.output / "report.json"))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    def interrupted(signum, _frame):
        raise RunInterrupted("business run interrupted by signal " + str(signum))

    signal.signal(signal.SIGTERM, interrupted)
    sys.exit(main())
