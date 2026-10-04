"""Isolation checks for the fixed business trace gate and bounded cleanup."""

from __future__ import annotations

import importlib.util
from io import StringIO
import json
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest


PROJECT = Path(__file__).resolve().parents[1]
TOOLS = PROJECT / "tools"
sys.path.insert(0, str(TOOLS))


def _load_tool(name, directory=TOOLS):
    spec = importlib.util.spec_from_file_location(
        f"{name}_business_test", directory / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def business():
    return _load_tool("dm_mc02_trobot_business")


@pytest.fixture(scope="module")
def gates():
    return json.loads((PROJECT / "smoke" /
                       "dm_mc02_trobot_business_gates.json").read_text())["gates"]


@pytest.fixture
def trace():
    # An independently chosen piecewise trajectory: at rest, 6 rad/s for
    # enable+1..4 s with the declared 0.3 Nm equilibrium effort, then at rest.
    # This is input to the analyzer, not a plant or protocol implementation.
    rows = []
    for tick in range(1, 14_001):
        running = 1_100 <= tick < 4_100
        rows.append(dict(
            event="step", plant_time_s=tick / 1000,
            wall_time_ns=tick * 1_000_000, dt=0.001,
            position=0.0, velocity=6.0 if running else 0.0,
            effort=0.3 if running else 0.0,
            gyro=[0.0, 0.0, 0.0], accel=[0.0, 0.0, 1.0],
        ))
        if tick == 100:
            rows.append(dict(event="enabled", plant_time_s=0.1,
                             wall_time_ns=100_000_000, index=0, enabled=True))
    return rows


def test_complete_trace_passes_predeclared_windows(business, gates, trace):
    report = business.analyze_trace(trace, gates)
    observed = {gate["id"]: gate for gate in report["gates"]}

    assert set(observed) == {gate["id"] for gate in gates}
    assert all(gate["passed"] for gate in observed.values())
    assert {name: gate["samples"] for name, gate in observed.items()} == {
        "stable_idle": 500,
        "rise_peak": 3000,
        "rise_settled": 2500,
        "plateau_mean_velocity": 1800,
        "plateau_mean_effort": 1800,
        "stop_undershoot": 2000,
        "stop_settled": 1500,
        "late_stop_mean": 1000,
    }


def test_valid_trace_with_idle_motion_reports_failed_gate(business, gates, trace):
    next(row for row in trace if row["plant_time_s"] == 0.8)["velocity"] = 1.0

    report = business.analyze_trace(trace, gates)

    assert [gate["id"] for gate in report["gates"] if not gate["passed"]] == [
        "stable_idle",
    ]


def test_missing_step_is_rejected(business, gates, trace):
    del trace[1000]
    with pytest.raises(RuntimeError):
        business.analyze_trace(trace, gates)


def test_incomplete_enable_relative_window_is_rejected(business, gates, trace):
    enable = next(row for row in trace if row["event"] == "enabled")
    trace.remove(enable)
    enable.update(plant_time_s=9.0, wall_time_ns=9_000_000_000)
    trace.insert(9000, enable)

    with pytest.raises(RuntimeError):
        business.analyze_trace(trace, gates)


def test_nonfinite_trace_value_is_rejected(business, gates, trace):
    trace[700]["velocity"] = float("nan")
    with pytest.raises(RuntimeError):
        business.analyze_trace(trace, gates)


def test_enable_event_must_be_unique(business, gates, trace):
    enable = next(row for row in trace if row["event"] == "enabled")
    trace.insert(101, enable.copy())
    with pytest.raises(RuntimeError):
        business.analyze_trace(trace, gates)


@pytest.mark.parametrize("field", ["plant_time_s", "wall_time_ns"])
def test_timestamp_rollback_is_rejected(business, gates, trace, field):
    trace[701][field] = trace[699][field]
    with pytest.raises(RuntimeError):
        business.analyze_trace(trace, gates)


@pytest.fixture
def pacing_observation(trace):
    # Reuse the declared trajectory, then keep the rotor at rest. These are
    # independently specified observations, not a MuJoCo solver or scheduler.
    trace.extend(dict(
        event="step", plant_time_s=tick / 1000,
        wall_time_ns=tick * 1_000_000, dt=0.001,
        position=0.0, velocity=0.0, effort=0.0,
        gyro=[0.0, 0.0, 0.0], accel=[0.0, 0.0, 1.0],
    ) for tick in range(14_001, 65_001))
    samples = []
    for tick in range(0, 65_001, 100):
        wall_ns = tick * 1_000_000
        sample = dict(tick=tick, wall_time_ns=wall_ns,
                      tick_query_span_ns=2_000_000)
        for name, pid, starttime, offset_ns, usage, rss in (
                ("qemu", 123, 7, 3_000_000, 1.25, 4096),
                ("worker", 456, 8, 5_000_000, .25, 2048)):
            cpu_ns = wall_ns + offset_ns
            sample[name] = dict(
                pid=pid, starttime=starttime,
                cpu_seconds=10 + usage * cpu_ns / 1_000_000_000,
                rss_kib=rss, cpu_sample_wall_time_ns=cpu_ns,
                cpu_query_span_ns=200_000)
        samples.append(sample)
    # Maxima belong to the selected window, not the whole execution lifetime.
    samples[0]["qemu"]["rss_kib"] = 65_536
    samples[0]["worker"]["rss_kib"] = 65_536
    samples[300]["qemu"]["rss_kib"] = 8192
    samples[300]["worker"]["rss_kib"] = 3072
    return trace, dict(ready=dict(detected_wall_time_ns=150_000_000),
                       samples=samples)


def test_pacing_exact_minute_reports_one_core_cpu_and_sampled_rss(
        business, pacing_observation):
    rows, execution = pacing_observation

    report = business.analyze_pacing(rows, execution)

    assert report["passed"] is True
    assert report["sample_host_seconds"] == pytest.approx(60)
    assert report["firmware_tick_seconds"] == pytest.approx(60)
    assert report["plant_seconds"] == pytest.approx(60)
    assert report["firmware_tick_rtf"] == pytest.approx(1)
    assert report["plant_rtf"] == pytest.approx(1)
    assert report["resources"]["qemu"]["cpu_percent"] == pytest.approx(125)
    assert report["resources"]["qemu"]["sampled_max_rss_kib"] == 8192
    assert report["resources"]["worker"]["cpu_percent"] == pytest.approx(25)
    assert report["resources"]["worker"]["sampled_max_rss_kib"] == 3072
    assert {check["id"] for check in report["checks"]} == {
        "firmware_tick_rtf_lower", "plant_rtf_min", "plant_rtf_max",
        "plant_wall_phase_excursion", "sampled_tick_plant_drift_bound",
        "max_step_gap", "quiet_tail_max_abs_velocity",
        "quiet_tail_abs_mean_velocity",
    }
    assert all(check["passed"] for check in report["checks"])
    assert all({"id", "passed", "observed", "limit"} <= check.keys()
               for check in report["checks"])


def test_pacing_average_rtf_cannot_hide_pause_and_catchup(
        business, pacing_observation):
    rows, execution = pacing_observation
    # The 20 s step waits 200 ms; the next 400 steps arrive every 0.5 ms.
    # Both ends remain exact, while the observation contains a 201 ms gap.
    for row in rows:
        if row["event"] == "step" and 20 <= row["plant_time_s"] < 20.4:
            elapsed_ms = round(row["plant_time_s"] * 1000) - 20_000
            row["wall_time_ns"] += 200_000_000 - elapsed_ms * 500_000

    report = business.analyze_pacing(rows, execution)

    assert report["firmware_tick_rtf"] == pytest.approx(1)
    assert report["plant_rtf"] == pytest.approx(1)
    assert report["passed"] is False
    assert {check["id"] for check in report["checks"] if not check["passed"]} == {
        "plant_wall_phase_excursion", "sampled_tick_plant_drift_bound",
        "max_step_gap",
    }


def test_pacing_one_percent_slow_firmware_fails(
        business, pacing_observation):
    rows, execution = pacing_observation
    for sample in execution["samples"]:
        sample["tick"] = sample["tick"] * 99 // 100

    report = business.analyze_pacing(rows, execution)

    assert report["firmware_tick_rtf"] == pytest.approx(.99)
    assert report["plant_rtf"] == pytest.approx(1)
    assert report["passed"] is False
    assert next(check for check in report["checks"]
                if check["id"] == "firmware_tick_rtf_lower")["passed"] is False


def test_pacing_requires_full_minute_after_ready(
        business, pacing_observation):
    rows, execution = pacing_observation
    execution["samples"] = [sample for sample in execution["samples"]
                            if sample["wall_time_ns"] <= 60_100_000_000]

    with pytest.raises(RuntimeError):
        business.analyze_pacing(rows, execution)


def test_pacing_wide_qmp_bracket_does_not_pass_from_midpoint(
        business, pacing_observation):
    rows, execution = pacing_observation
    for sample in execution["samples"]:
        sample["tick_query_span_ns"] = 200_000_000

    report = business.analyze_pacing(rows, execution)

    assert report["firmware_tick_rtf"] == pytest.approx(1)
    assert report["firmware_tick_rtf_interval"][0] < .999
    assert next(check for check in report["checks"]
                if check["id"] == "firmware_tick_rtf_lower")["passed"] is False


def test_pacing_drift_interval_accounts_for_fast_catchup(
        business, pacing_observation):
    rows, execution = pacing_observation
    # Wait 99 ms, then emit 100 one-millisecond plant observations in 1 ms
    # of host time. The 20.1 s QMP bracket spans this whole burst. Host-time
    # distance to a nearest point cannot bound the plant progress in it.
    for row in rows:
        if row["event"] == "step" and 20 <= row["plant_time_s"] < 20.1:
            tick = round(row["plant_time_s"] * 1000)
            row["wall_time_ns"] = 20_099_000_000 + (tick - 20_000) * 10_000

    report = business.analyze_pacing(rows, execution)

    assert report["plant_rtf"] == pytest.approx(1)
    assert report["max_rebased_phase_error_seconds"] < .1
    assert report["max_step_gap_seconds"] == pytest.approx(.1)
    assert max(d["error_bound_seconds"] for d in report["sampled_tick_plant_drift"]) >= .099
    assert next(check for check in report["checks"]
                if check["id"] == "sampled_tick_plant_drift_bound")["passed"] is False


def test_pacing_missing_interior_cpu_sample_is_rejected(
        business, pacing_observation):
    rows, execution = pacing_observation
    execution["samples"][302]["worker"]["cpu_seconds"] = None

    with pytest.raises(RuntimeError):
        business.analyze_pacing(rows, execution)


@pytest.mark.parametrize("velocity, one_step", [(.2, True), (.06, False)])
def test_pacing_quiet_tail_is_checked_through_sample_end(
        business, pacing_observation, velocity, one_step):
    rows, execution = pacing_observation
    for row in rows:
        if (row["event"] == "step" and
                (row["plant_time_s"] == 60 if one_step else
                 row["plant_time_s"] >= 6.1)):
            row["velocity"] = velocity

    report = business.analyze_pacing(rows, execution)

    assert report["passed"] is False
    assert [check["id"] for check in report["checks"] if not check["passed"]] == [
        "quiet_tail_max_abs_velocity" if one_step else "quiet_tail_abs_mean_velocity",
    ]


@pytest.mark.parametrize("configured, limit", [(None, 50_000),
                                               (str(3 * 72_000), 216_000)])
def test_bench_trace_event_budget_is_explicit(
        monkeypatch, tmp_path, configured, limit):
    bench = _load_tool("dm_mc02_trobot_bench", PROJECT / "smoke")
    monkeypatch.setenv("DM_TROBOT_BENCH_TRACE", str(tmp_path / "trace.jsonl"))
    if configured is None:
        monkeypatch.delenv("DM_TROBOT_BENCH_MAX_EVENTS", raising=False)
    else:
        monkeypatch.setenv("DM_TROBOT_BENCH_MAX_EVENTS", configured)
    config = SimpleNamespace(mujoco_model="unused.xml", motor_count=1,
                             rate=1000, dm_t_max=7)
    # Isolate trace configuration; this is not MuJoCo solver validation.
    with patch.object(bench.MujocoEngine, "__init__", return_value=None):
        engine = bench.BenchEngine(config)
    try:
        assert engine.max_events == limit
    finally:
        engine.close()

    engine = bench.BenchEngine.__new__(bench.BenchEngine)
    engine.trace = StringIO()
    engine.physics = SimpleNamespace(time_s=0.001)
    engine.events = limit - 1
    engine.max_events = limit
    engine.record("step", dt=.001)
    with pytest.raises(BufferError):
        engine.record("step", dt=.001)
    assert engine.events == limit
    assert len(engine.trace.getvalue().splitlines()) == 1


def test_bench_zero_event_budget_is_rejected(monkeypatch, tmp_path):
    bench = _load_tool("dm_mc02_trobot_bench", PROJECT / "smoke")
    monkeypatch.setenv("DM_TROBOT_BENCH_TRACE", str(tmp_path / "trace.jsonl"))
    monkeypatch.setenv("DM_TROBOT_BENCH_MAX_EVENTS", "0")
    config = SimpleNamespace(mujoco_model="unused.xml", motor_count=1,
                             rate=1000, dm_t_max=7)

    # Invalid admission must not start the external solver.
    with patch.object(bench.MujocoEngine, "__init__", return_value=None) as init:
        with pytest.raises(ValueError):
            bench.BenchEngine(config)
    init.assert_not_called()


def test_direct_worker_cleanup_reaps_without_group_signals():
    harness = _load_tool("dm_mc02_test_harness")
    process = Mock(pid=1234)
    process.wait.return_value = 0
    with patch.object(harness.os, "killpg") as killpg:
        harness.stop(process, process_group=False)

    process.send_signal.assert_called_once_with(signal.SIGTERM)
    process.wait.assert_called_once_with(timeout=1.0)
    killpg.assert_not_called()


def test_default_cleanup_removes_remaining_process_group():
    harness = _load_tool("dm_mc02_test_harness")
    process = Mock(pid=1234)
    process.wait.return_value = 0
    with patch.object(harness.os, "killpg") as killpg:
        harness.stop(process)

    assert killpg.call_args_list == [
        call(1234, signal.SIGTERM), call(1234, signal.SIGKILL),
    ]
    process.send_signal.assert_not_called()
    assert process.wait.call_args_list == [call(timeout=1.0), call(timeout=1.0)]


def test_cleanup_reports_unreapable_process():
    harness = _load_tool("dm_mc02_test_harness")
    process = Mock(pid=1234)
    process.wait.side_effect = subprocess.TimeoutExpired("worker", 1.0)
    with patch.object(harness.os, "killpg"):
        with pytest.raises(RuntimeError):
            harness.stop(process, process_group=False)

    assert process.send_signal.call_args_list == [
        call(signal.SIGTERM), call(signal.SIGKILL),
    ]
    assert process.wait.call_args_list == [call(timeout=1.0), call(timeout=1.0)]


def test_consumer_preserves_qmp_close_failure(business, monkeypatch, tmp_path):
    qmp = Mock()
    qmp.command.return_value = "not initialized"
    qmp.close.side_effect = RuntimeError("QMP close failed")
    process = Mock(pid=1234)
    process.poll.return_value = 1
    monkeypatch.setattr(business, "QmpSession", lambda *a, **kw: qmp)
    monkeypatch.setattr(business.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(business, "_identity", lambda pid: dict(pid=pid, starttime=7))
    monkeypatch.setattr(business, "deadline_qmp_call", lambda q, d, p, fn: fn())

    with pytest.raises(RuntimeError, match="business consumer failed"):
        business._run_inside("qmp", "cosim", "can", "123",
                             str(business.time.monotonic_ns()), str(tmp_path), "0x1")

    candidate = json.loads((tmp_path / "candidate.json").read_text())
    assert candidate["cleanup_errors"] == ["QMP close failed"]
    assert "worker exited" in candidate["error"]


@pytest.mark.parametrize("harness_status", [0, 1])
def test_failed_consumer_cannot_publish_passed_report(
        business, monkeypatch, tmp_path, harness_status):
    executable = tmp_path / "input"
    executable.write_bytes(b"fixture")
    executable.chmod(0o755)
    output = tmp_path / "report"
    process = Mock(returncode=harness_status)
    process.poll.return_value = harness_status

    def launch(*args, **kwargs):
        business._write_json(output / "candidate.json", dict(
            error=None, cleanup_errors=["QMP close failed"],
            processes={"qemu": dict(pid=123, starttime=7),
                       "worker": dict(pid=456, starttime=8)}))
        return process

    monkeypatch.setitem(sys.modules, "mujoco", SimpleNamespace(__version__="test"))
    monkeypatch.setattr(business, "_inputs", lambda *args: {"fixture": "same"})
    monkeypatch.setattr(business, "_identity", lambda pid: None)
    monkeypatch.setattr(business.subprocess, "check_output",
                        lambda *a, **kw: "24000304 b xTickCount\n")
    monkeypatch.setattr(business.subprocess, "Popen", launch)

    assert business.main(["--elf", str(executable), "--qemu", str(executable),
                          "--output", str(output)]) == 1
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "failed" and report["errors"]
    assert report["cleanup_verified"] is True
    assert report["execution"]["cleanup_errors"] == ["QMP close failed"]


@pytest.mark.parametrize("different_input", [False, True])
def test_performance_summary_requires_three_matching_successes(
        business, monkeypatch, tmp_path, different_input):
    reports = iter([dict(status="passed", errors=[], inputs_sha256={"fixture": value})
                    for value in ("same", "changed" if different_input else "same", "same")])
    monkeypatch.setattr(business, "_run_report", lambda args: next(reports))
    output = tmp_path / "summary"

    assert business.main(["--performance", "--output", str(output)]) == int(different_input)
    summary = json.loads((output / "report.json").read_text())
    assert len(summary["runs"]) == 3 and summary["skipped"] == []
    assert summary["status"] == ("failed" if different_input else "passed")
    if different_input:
        assert summary["runs"][1]["status"] == "failed"
        assert "changed between" in summary["runs"][1]["errors"][0]


def test_performance_interrupt_between_runs_preserves_failed_summary(
        business, monkeypatch, tmp_path):
    calls = []
    def run(args):
        calls.append(args.output)
        if len(calls) == 2:
            raise business.RunInterrupted("interrupt between runs")
        return dict(status="passed", errors=[], inputs_sha256={"fixture": "same"})
    monkeypatch.setattr(business, "_run_report", run)
    output = tmp_path / "summary"

    assert business.main(["--performance", "--output", str(output)]) == 1
    summary = json.loads((output / "report.json").read_text())
    assert summary["status"] == "failed" and len(calls) == 2
    assert [r["run"] for r in summary["runs"]] == [1]
    assert [r["run"] for r in summary["skipped"]] == [2, 3]
    assert summary["errors"] == ["interrupt between runs"]


def test_performance_interrupt_during_summary_write_preserves_failure(
        business, monkeypatch, tmp_path):
    monkeypatch.setattr(business, "_run_report", lambda args:
                        dict(status="passed", errors=[], inputs_sha256={"fixture": "same"}))
    write_json = business._write_json
    writes = []
    def interrupted_write(path, value):
        writes.append(path)
        if len(writes) == 1:
            raise business.RunInterrupted("interrupt during summary write")
        write_json(path, value)
    monkeypatch.setattr(business, "_write_json", interrupted_write)
    output = tmp_path / "summary"

    assert business.main(["--performance", "--output", str(output)]) == 1
    summary = json.loads((output / "report.json").read_text())
    assert summary["status"] == "failed" and summary["skipped"] == []
    assert len(summary["runs"]) == 3 and len(writes) == 2
    assert summary["errors"] == ["interrupt during summary write"]
