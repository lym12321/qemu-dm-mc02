"""Host fixture ownership and report checks; device physics has separate gates."""

import importlib.util
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shlex
import socket
import struct
import subprocess
import signal
import sys
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[1] / "tools/dm_mc02_dji_test.py"

SPEC = importlib.util.spec_from_file_location("dm_mc02_dji_consumer_test", PATH)

module = importlib.util.module_from_spec(SPEC)

sys.modules[SPEC.name] = module

SPEC.loader.exec_module(module)

from tools import dm_mc02_scene_smoke as arena_module
from sim.boards.channels import ChannelEvent
from sim.api import Session
from sim.machines.qemu.endpoints import QemuCanEndpoint
from sim.transports.cosim import QemuTimestampMapper
from sim.transports import StreamTransport

SCENARIO_FIELDS = ("angle", "speed", "current", "temp", "ts", "output", "enabled",
                   "update_age", "feedback_age")

SCENARIO_REQUIRED = {"control.rx", "reset.poweron", "bsp.init", "task.stack", "dji.scenario.stop", "dji.can",
    *(f"dji.scenario.{phase}.id{identifier}.{field}" for phase in (1, 2)
      for identifier in (1, 2) for field in SCENARIO_FIELDS)}


def scenario_sample_rows(phase):
    """Independent copied firmware fields; no production validator/encoder oracle."""
    rows = []
    for identifier, current in ((1, 64), (2, 0xFFFFFFC0)):
        values = {"angle": (0, 8192), "speed": (0, 0), "current": (current, current),
            "temp": (25, 25), "ts": (100 if phase == 1 else 200, 50 if phase == 1 else 150),
            "output": (current, current), "enabled": (1, 1), "update_age": (3, 50), "feedback_age": (4, 50)}
        rows += [dict(status="PASS", name=f"dji.scenario.{phase}.id{identifier}.{field}",
                      actual=values[field][0], expected=values[field][1]) for field in SCENARIO_FIELDS]
    return rows


def scenario_case_rows():
    rows = [dict(status="PASS", name=label, actual=value, expected=expected)
            for label, value, expected in (("control.rx", 0, 0), ("reset.poweron", 0x800000, 0x800000),
                ("bsp.init", 0, 0), ("task.stack", 96, 64))]
    return [*rows, *scenario_sample_rows(1), *scenario_sample_rows(2),
        dict(status="PASS", name="dji.scenario.stop", actual=0, expected=0),
        dict(status="PASS", name="dji.can", actual=0, expected=0),
        dict(status="SKIP", name="plant.physics", actual=0, expected=0)]


def scenario_summaries():
    return [{"kind": kind, "values": [42, 0, 1]} for kind in ("PHASE", "DONE")]


class Sink:
    def __init__(self):
        self.frames = []

    def send(self, ident, flags, dlc, stamp, data):
        self.frames.append((ident, flags, dlc, stamp, data))


def command(ident, stamp, raw=0):
    return module.CanFrame(ident, data=struct.pack(">hhhh", raw, 0, 0, 0),
                           timestamp_ns=stamp)


def test_two_groups_advance_one_tick_and_vectors_use_normal_fabric():
    sink = Sink()
    fixture = module.DjiFixture(sink)
    fixture.ingress(command(0x200, 1_010_000))
    fixture.ingress(command(0x1FF, 1_020_000))
    assert fixture.steps == 1
    assert fixture.runtime.t_sim_ns == 1_000_000
    assert sink.frames == []  # startup suppresses normal feedback only
    fixture.ready("dji.vector", 0x201, 0x205)
    assert sink.frames == [
        (0x201, 0, 8, 1_000_000, bytes.fromhex("0800ff40e0002a00")),
        (0x205, 0, 8, 1_000_000, bytes.fromhex("100000c020002b00")),
    ]
    fixture.ready("dji.start", 0, 0)
    fixture.ingress(command(0x200, 2_010_000))
    fixture.ingress(command(0x1FF, 2_020_000))
    assert fixture.steps == 2
    assert [frame[0] for frame in sink.frames[-2:]] == [0x201, 0x205]
    fixture.close()


def test_missing_zero_timeout_wire_cannot_pass_on_firmware_assertion_alone():
    fixture = module.DjiFixture(Sink())
    fixture.stage = "dji.drop"
    fixture.wire_seen[fixture.stage][0x200].add(4096)
    fixture.wire_seen[fixture.stage][0x1FF].add(4096)
    with pytest.raises(RuntimeError, match="slot=0"):
        fixture._finish_stage()
    fixture.close()


def test_early_transition_zero_cannot_prove_strict_feedback_timeout():
    fixture = module.DjiFixture(Sink())
    fixture.stage = "dji.drop"
    fixture.phase_start_ns[fixture.stage] = 0
    fixture.ingress(command(0x200, 1_010_000))
    fixture.ingress(command(0x1FF, 1_020_000, 4096))
    with pytest.raises(RuntimeError, match="beyond 51ms"):
        fixture._finish_stage()
    fixture.ingress(command(0x200, 60_010_000))
    fixture._finish_stage()
    fixture.close()


def test_time_gap_and_reverse_reject_before_more_device_steps():
    fixture = module.DjiFixture(Sink())
    fixture.ingress(command(0x200, 1_500_000))
    with pytest.raises(RuntimeError, match="backwards"):
        fixture.ingress(command(0x1FF, 1_499_999))
    with pytest.raises(RuntimeError, match="catch-up"):
        fixture.ingress(command(0x200, 1_002_000_000))
    assert fixture.steps == 1
    fixture.close()


def test_non_equality_timeout_reports_and_skip_are_checked():
    module.check_case(dict(status="PASS", name="dji.drop.age", actual=86, expected=51))
    module.check_case(dict(status="PASS", name="dji.drop.app_age", actual=3, expected=50))
    module.check_case(dict(status="SKIP", name="plant.physics", actual=0, expected=0))
    with pytest.raises(RuntimeError, match="fields disagree"):
        module.check_case(dict(status="PASS", name="dji.drop.age", actual=50, expected=51))
    with pytest.raises(RuntimeError, match="SKIP"):
        module.check_case(dict(status="SKIP", name="dji.drop.output", actual=0, expected=0))


def test_harness_failure_replaces_prior_pass_report(tmp_path):
    # Isolate only process acquisition: no mock CAN/model/protocol state machine.
    python = tmp_path / "python"
    python.write_text("#!/usr/bin/env bash\n"
                      'if [[ "$1" == *dm_mc02_test_harness.py ]]; then exit 7; fi\n'
                      f"exec {shlex.quote(sys.executable)} \"$@\"\n")
    python.chmod(0o755)
    elf = tmp_path / "firmware.elf"
    elf.write_bytes(b"fixture")
    report = tmp_path / "report.json"
    report.write_text('{"result":"passed"}\n')
    env = dict(os.environ, PYTHON=str(python), QEMU_SYSTEM_ARM="/bin/true")
    result = subprocess.run(
        ["bash", str(PATH.with_name("run-trobot-dji-test.sh")), str(elf), str(report)],
        env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 7
    actual = json.loads(report.read_text())
    assert actual["result"] == "failed"
    assert actual["harness_status"] == 7
    assert actual["cases"] == []
    assert "harness/cleanup failed" in actual["error"]


def test_dji_scenario_case_contract_has_exact_literal_coverage_and_only_physics_skip():
    assert arena_module.DJI_SCENARIO_REQUIRED == SCENARIO_REQUIRED and len(SCENARIO_REQUIRED) == 42
    rows = scenario_case_rows()
    for row in rows:
        arena_module.check_dji_scenario_case(row)
    arena_module.check_dji_scenario_coverage(rows, scenario_summaries())


@pytest.mark.parametrize("label,field,value", [
    ("dji.scenario.1.id1.current", "actual", 65),
    ("dji.scenario.1.id2.current", "expected", 64),
    ("dji.scenario.1.id1.angle", "actual", 8192),
    ("dji.scenario.1.id1.angle", "expected", 0),
    ("dji.scenario.1.id1.speed", "actual", 32768),
    ("dji.scenario.1.id1.speed", "actual", 0xFFFF7FFF),
    ("dji.scenario.1.id1.temp", "actual", 24),
    ("dji.scenario.1.id1.ts", "actual", 0),
    ("dji.scenario.1.id1.ts", "expected", 100),
    ("dji.scenario.1.id1.update_age", "actual", 50),
    ("dji.scenario.1.id1.feedback_age", "expected", 49),
    ("task.stack", "actual", 63),
    ("dji.can", "actual", 1),
    ("control.rx", "actual", True),
    ("control.rx", "expected", -1),
    ("control.rx", "actual", 1 << 32),
])
def test_dji_scenario_case_rejects_fixed_contract_or_uint32_violation(label, field, value):
    row = next(row for row in scenario_case_rows() if row["name"] == label)
    arena_module.check_dji_scenario_case(row)
    with pytest.raises((ValueError, TypeError)):
        arena_module.check_dji_scenario_case(dict(row, **{field: value}))


@pytest.mark.parametrize("corruption", ["unknown", "fail", "unexpected-skip", "missing-key", "extra-key"])
def test_dji_scenario_case_rejects_status_label_and_shape(corruption):
    row = dict(status="PASS", name="control.rx", actual=0, expected=0)
    if corruption == "unknown":
        row["name"] = "dji.other"
    elif corruption == "fail":
        row["status"] = "FAIL"
    elif corruption == "unexpected-skip":
        row["status"] = "SKIP"
    elif corruption == "missing-key":
        del row["expected"]
    else:
        row["surprise"] = 0
    with pytest.raises((ValueError, TypeError)):
        arena_module.check_dji_scenario_case(row)


@pytest.mark.parametrize("corruption", ["missing-case", "duplicate-case", "missing-skip", "duplicate-skip",
    "missing-summary", "wrong-summary", "duplicate-summary", "second-not-new"])
def test_dji_scenario_coverage_rejects_missing_duplicate_summary_and_stale_samples(corruption):
    rows, summaries = scenario_case_rows(), scenario_summaries()
    if corruption == "missing-case":
        rows.pop(0)
    elif corruption == "duplicate-case":
        rows.append(rows[0])
    elif corruption == "missing-skip":
        rows.pop()
    elif corruption == "duplicate-skip":
        rows.append(rows[-1])
    elif corruption == "missing-summary":
        summaries.pop()
    elif corruption == "wrong-summary":
        summaries[1]["values"] = [41, 0, 1]
    elif corruption == "duplicate-summary":
        summaries.append(summaries[0])
    else:
        for row in rows:
            if row["name"] == "dji.scenario.2.id1.ts":
                row.update(actual=100, expected=50)
    with pytest.raises((ValueError, TypeError)):
        arena_module.check_dji_scenario_coverage(rows, summaries)


@pytest.mark.parametrize("scenario", [False, True])
def test_dji_scenario_cli_dispatches_exactly_one_literal_consumer(monkeypatch, scenario):
    calls = []
    monkeypatch.setattr(arena_module, "run_dji_scenario", lambda *args, **options: calls.append((args, options)) or 7)
    monkeypatch.setattr(module, "run", lambda *args: calls.append((args, {})) or 7)
    argv = ["dji", "--scenario", "same.elf", "output path", "firmware", "--qemu", "qemu path"] if scenario else [
        "dji", "qmp", "uart", "can", "same.elf", "qemu", "report", "firmware"]
    monkeypatch.setattr(sys, "argv", argv)
    assert module.main() == 7
    expected = [(("same.elf", "output path", "firmware"), {"qemu": "qemu path"})] if scenario else [
        (("qmp", "uart", "can", "same.elf", "qemu", "report", "firmware"), {})]
    assert calls == expected


@pytest.mark.parametrize("extra", [["--duration", "2s"], ["--referee"], ["additional-positional"]])
def test_dji_scenario_cli_refuses_profile_overrides_before_execution(monkeypatch, extra):
    monkeypatch.setattr(arena_module, "run_dji_scenario", lambda *args, **options: pytest.fail("unadmitted execution"))
    monkeypatch.setattr(sys, "argv", ["dji", "--scenario", "same.elf", "result", "firmware", *extra])
    with pytest.raises(SystemExit) as caught:
        module.main()
    assert caught.value.code == 2


def feedback_case_rows():
    """Literal copied-field receipts, independent of the host's validators."""
    values = (("control.rx", 0, 0), ("reset.poweron", 0x800000, 0x800000),
        ("bsp.init", 0, 0), ("task.stack", 96, 64),
        ("dji.feedback.baseline.id1", 110, 0), ("dji.feedback.baseline.id2", 110, 0),
        ("dji.feedback.loss.frozen", 200, 200), ("dji.feedback.loss.age", 54, 51),
        ("dji.feedback.loss.app_age", 3, 50), ("dji.feedback.loss.output", 0, 0),
        ("dji.feedback.loss.id2", 257, 254), ("dji.feedback.recover.id1", 360, 200),
        ("dji.feedback.recover.id2", 360, 0), ("dji.feedback.stop", 0, 0),
        ("dji.feedback.can", 0, 0))
    return [dict(status="PASS", name=name, actual=actual, expected=expected)
            for name, actual, expected in values] + [
        dict(status="SKIP", name="plant.physics", actual=0, expected=0)]


def test_dji_feedback_literal_case_coverage_and_new_timestamp_history():
    rows = feedback_case_rows()
    required = {row["name"] for row in rows if row["status"] == "PASS"}
    assert arena_module.DJI_FEEDBACK_REQUIRED == required and len(required) == 15
    arena_module.check_dji_feedback_coverage(rows, [{"kind": kind, "values": [15, 0, 1]}
                                                  for kind in ("PHASE", "DONE")])


@pytest.mark.parametrize("label,field,value", [
    ("dji.feedback.loss.age", "actual", 50), ("dji.feedback.loss.age", "expected", 50),
    ("dji.feedback.loss.app_age", "actual", 50), ("dji.feedback.loss.output", "actual", 64),
    ("dji.feedback.loss.frozen", "actual", 199), ("dji.feedback.loss.frozen", "actual", 0),
    ("dji.feedback.loss.id2", "actual", 254), ("dji.feedback.recover.id1", "actual", 200),
    ("dji.feedback.baseline.id1", "actual", 0), ("dji.feedback.can", "actual", 1),
    ("task.stack", "actual", 63), ("control.rx", "actual", True),
    ("control.rx", "expected", -1), ("control.rx", "actual", 1 << 32)])
def test_dji_feedback_case_rejects_stale_or_nonliteral_unsigned_fields(label, field, value):
    row = next(row for row in feedback_case_rows() if row["name"] == label)
    with pytest.raises((TypeError, ValueError)):
        arena_module.check_dji_feedback_case(dict(row, **{field: value}))


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "skip", "summary", "frozen-baseline",
                                        "recovery-origin", "stale-control"])
def test_dji_feedback_coverage_requires_complete_reports_and_distinct_history(corruption):
    rows = feedback_case_rows()
    summaries = [{"kind": kind, "values": [15, 0, 1]} for kind in ("PHASE", "DONE")]
    values = {row["name"]: row for row in rows}
    if corruption == "missing":
        rows.pop(0)
    elif corruption == "duplicate":
        rows.append(rows[0])
    elif corruption == "skip":
        rows[-1]["name"] = "hardware.calibration"
    elif corruption == "summary":
        summaries[-1]["values"] = [14, 0, 1]
    elif corruption == "frozen-baseline":
        values["dji.feedback.baseline.id1"]["actual"] = 200
    elif corruption == "recovery-origin":
        values["dji.feedback.recover.id1"]["expected"] = 199
    else:
        values["dji.feedback.recover.id2"]["actual"] = 110
    with pytest.raises((TypeError, ValueError)):
        arena_module.check_dji_feedback_coverage(rows, summaries)


def test_dji_feedback_cli_dispatches_only_dedicated_literal_consumer(monkeypatch):
    calls = []
    monkeypatch.setattr(arena_module, "run_dji_feedback_scenario", lambda *args, **options: calls.append((args, options)) or 7)
    monkeypatch.setattr(sys, "argv", ["dji", "--feedback-scenario", "same.elf", "output path", "firmware", "--qemu", "qemu path"])
    assert module.main() == 7
    assert calls == [(("same.elf", "output path", "firmware"), {"qemu": "qemu path"})]


@pytest.mark.parametrize("extra", [["--duration", "1s"], ["--scenario"], ["--timeout", "30"]])
def test_dji_feedback_cli_refuses_profile_and_deadline_overrides(monkeypatch, extra):
    monkeypatch.setattr(arena_module, "run_dji_feedback_scenario", lambda *args, **options: pytest.fail("unadmitted execution"))
    monkeypatch.setattr(sys, "argv", ["dji", "--feedback-scenario", "same.elf", "result", "firmware", *extra])
    with pytest.raises(SystemExit) as caught:
        module.main()
    assert caught.value.code == 2
