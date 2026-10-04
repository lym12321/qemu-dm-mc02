"""Host fixture ownership and report checks; device physics has separate gates."""

import importlib.util
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import sys

import pytest

PATH = Path(__file__).resolve().parents[1] / "tools/dm_mc02_dji_test.py"
SPEC = importlib.util.spec_from_file_location("dm_mc02_dji_consumer_test", PATH)
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


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
