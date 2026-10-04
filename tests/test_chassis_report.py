"""Isolated report gates; these observations do not simulate a plant or CPU."""

import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys

import pytest

PATH = Path(__file__).resolve().parents[1] / "tools/dm_mc02_chassis_test.py"
SPEC = importlib.util.spec_from_file_location("dm_mc02_chassis_report_test", PATH)
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)

STAGES = (
    ("chassis.warm", 0, 0), ("chassis.forward", 500, 500),
    ("chassis.turn", 200, 600), ("chassis.stop", 0, 0),
    ("chassis.disable", 0, 0),
)
BASE = 1_000_000_000
QEMU_ORIGIN = 10_000_000_000


def test_fixed_firmware_coverage_and_only_physics_skip():
    expected = {"control.rx", "reset.poweron", "bsp.init", "task.stack",
                "chassis.warm.gravity", "chassis.disable", "chassis.can"}
    for phase, _, _ in STAGES[:4]:
        expected.update(f"{phase}.{name}" for name in (
            "samples", "fresh", "speed.id1", "speed.id2", "speed.id3", "speed.id4", "imu", "gyro"))
    assert module.REQUIRED == expected
    assert module.STAGES == STAGES
    module.check_case(dict(status="SKIP", name="plant.physics", actual=0, expected=0))
    for row in (
        dict(status="SKIP", name="chassis.warm.imu", actual=0, expected=0),
        dict(status="PASS", name="plant.physics", actual=0, expected=0),
        dict(status="FAIL", name="control.rx", actual=0, expected=0),
    ):
        with pytest.raises(ValueError):
            module.check_case(row)


@pytest.mark.parametrize("name,good,bad,expected", [
    ("reset.poweron", 0x800000, 0, 0x800000),
    ("task.stack", 64, 63, 64),
    ("chassis.warm.samples", 100, 99, 100),
    ("chassis.forward.fresh", 49, 50, 50),
    ("chassis.forward.speed.id1", 150, 151, 150),
    ("chassis.stop.speed.id4", 80, 81, 80),
    ("chassis.warm.gravity", 100, 101, 100),
    ("chassis.turn.imu", 100, 99, 100),
    ("chassis.stop.gyro", 0xffffffec, 0xffffffeb, 20),  # signed -20/-21
    ("chassis.turn.gyro", 20, 19, 20),  # truncated >.020 rad/s may report 20
])
def test_firmware_gate_limits_and_literal_expected_fields(name, good, bad, expected):
    row = dict(status="PASS", name=name, actual=good, expected=expected)
    module.check_case(row)
    with pytest.raises(ValueError, match="fields disagree"):
        module.check_case(dict(row, actual=bad))
    with pytest.raises(ValueError, match="fields disagree"):
        module.check_case(dict(row, expected=expected + 1))


@pytest.mark.parametrize("field,value", [("actual", True), ("actual", -1),
                                          ("expected", 0x100000000)])
def test_firmware_numeric_fields_are_uint32(field, value):
    row = dict(status="PASS", name="control.rx", actual=0, expected=0)
    row[field] = value
    with pytest.raises(ValueError, match="uint32"):
        module.check_case(row)


@pytest.fixture
def observed_trace():
    # Independent C620 literals: encoder 2048, RPM 0/96/48/112, raw current
    # 0/32/64, 25 C. DJI M3508 P19 V1.0 specifies the ratio 3591/187.
    # This is a table of analyzer inputs, not a dynamics or firmware model.
    payloads = (
        ("0800000000001900",) * 4,
        ("0800006000201900",) * 4,
        ("0800003000201900", "0800007000401900") * 2,
        ("0800000000001900",) * 4,
    )
    speeds = tuple(tuple(rpm * math.tau / 60 * 187 / 3591 for rpm in phase)
                   for phase in ((0, 0, 0, 0), (96, 96, 96, 96), (48, 112, 48, 112), (0, 0, 0, 0)))
    currents = ((0, 0, 0, 0), (32, 32, 32, 32), (32, 64, 32, 64), (0, 0, 0, 0))
    joint_angle = (math.pi / 2 + math.pi / 8192) * 187 / 3591
    rows = [dict(event="reset", wall_time_ns=BASE, session_id=123, timestamp_ns=0,
                 qemu_origin_ns=QEMU_ORIGIN, worker_origin_ns=0)]
    for index in range(1, 7501):
        phase = min(index // 1000, 3)
        yaw = min(max(index - 2000, 0), 1000) * .0001
        stamp = index * 1_000_000
        rows.append(dict(event="step", wall_time_ns=BASE + stamp, timestamp_ns=stamp,
                         plant_time_s=index * .001,
                         position_m=[min(max(index - 1000, 0), 1000) * .00001, 0, .2],
                         quaternion_wxyz=[math.cos(yaw / 2), 0, 0, math.sin(yaw / 2)],
                         joint_position_rad=[joint_angle] * 4,
                         joint_velocity_rad_s=list(speeds[phase]), current_raw=list(currents[phase]),
                         gyro_rad_s=[0, 0, .05 if phase == 2 else 0],
                         accel_m_s2=[0, 0, 9.80665]))
        rows.append(dict(event="control", wall_time_ns=BASE + stamp, kind=3,
                         session_id=123, step_id=index, timestamp_ns=stamp,
                         status=0, queue_depth=0, dropped_count=0))
    for phase, index in enumerate((900, 1900, 2900, 3900)):
        stamp = index * 1_000_000
        rows.append(dict(event="control", wall_time_ns=BASE + stamp, kind=6,
                         session_id=123, step_id=index, timestamp_ns=stamp,
                         status=0, consumed_mask=3, missing_mask=0,
                         queue_depth=0, dropped_count=0))
        for ident, data in zip((0x201, 0x202, 0x203, 0x204), payloads[phase]):
            rows.append(dict(event="can_tx_queued", wall_time_ns=BASE + stamp,
                             timestamp_ns=QEMU_ORIGIN + stamp, can_id=ident,
                             flags=0, dlc=8, data=data))
    rows.append(dict(event="control", wall_time_ns=BASE + 901_000_000, kind=6,
                     session_id=123, step_id=901, timestamp_ns=901_000_000,
                     status=2, consumed_mask=2, missing_mask=1, queue_depth=0, dropped_count=0))
    for index, data in ((50, "0000000000000000"), (1050, "0020002000200020"),
                        (2050, "0020004000200040"), (3050, "0000000000000000"),
                        (4011, "0000000000000000")):
        rows.append(dict(event="can_rx", wall_time_ns=BASE + index * 1_000_000,
                         timestamp_ns=QEMU_ORIGIN + index * 1_000_000,
                         can_id=0x200, flags=0, dlc=8, data=data))
    rows.sort(key=lambda row: row["wall_time_ns"])
    stages = [dict(name=name, actual=left, expected=right,
                   wall_time_ns=BASE + index * 1_000_000_000,
                   ack_wall_time_ns=BASE + index * 1_000_000_000 + 10_000_000)
              for index, (name, left, right) in enumerate(STAGES)]
    return rows, stages


def test_four_phase_report_keeps_advisory_done_and_unobserved_coverage(observed_trace):
    report = module.analyze_trace(*observed_trace)
    assert report["steps"] == 7500
    assert [phase["name"] for phase in report["phases"]] == [s[0] for s in STAGES[:4]]
    assert [phase["samples"] for phase in report["phases"]] == [250] * 4
    assert report["feedback_queued_by_id"] == {0x201: 4, 0x202: 4, 0x203: 4, 0x204: 4}
    assert report["done_status_masks"] == {"(0, 3, 0)": 4, "(2, 2, 1)": 1}
    assert report["done_observed"] == 5
    assert report["done_full_consumed"] == 4
    assert report["phases"][0]["done_status_masks"] == {"(0, 3, 0)": 1, "(2, 2, 1)": 1}
    coverage = report["control_coverage"]
    assert coverage["ack_observed_steps"] == 7500
    assert coverage["ack_missing_step_ids"] == []
    assert coverage["done_observed_steps"] == 5
    assert coverage["done_missing_step_ids"] == [
        ident for ident in range(1, 7501) if ident not in {900, 901, 1900, 2900, 3900}]


def test_partial_failed_trace_retains_missing_and_duplicate_responses():
    ack = dict(event="control", kind=3, step_id=1, status=0)
    rejected = dict(event="control", kind=3, step_id=2, status=1)
    done = dict(event="control", kind=6, step_id=2, status=2, consumed_mask=2, missing_mask=1)
    report = module.control_coverage([ack, rejected, done, dict(done)], expected_steps=3)
    assert report["ack_missing_step_ids"] == [2, 3]
    assert report["done_missing_step_ids"] == [1, 3]
    assert report["done_observed_steps"] == 1
    assert report["duplicate_done_responses"] == 1
    assert report["done_status_masks"] == {"(2, 2, 1)": 2}


@pytest.mark.parametrize("event,kind,message", [
    ("step", None, "incomplete step"),
    ("reset", None, "unexpected runtime reset"),
    ("control", 3, "ACK coverage"),
    ("control", 6, "full-consumed DONE"),
    ("can_rx", None, "tail state/command"),
    ("can_tx_queued", None, "four feedback IDs"),
])
def test_missing_observations_cannot_pass(observed_trace, event, kind, message):
    rows, stages = observed_trace
    rows = [row for row in rows if not (row["event"] == event and
            (kind is None or row.get("kind") == kind))]
    with pytest.raises(ValueError, match=message):
        module.analyze_trace(rows, stages)


@pytest.mark.parametrize("data", ["0801006000201900", "0800006100201900",
                                  "08000060ffe01900", "0800006000201a00"])
def test_feedback_quantization_uses_independent_literal_bytes(observed_trace, data):
    rows, stages = observed_trace
    row = next(row for row in rows if row["event"] == "can_tx_queued" and
               row["wall_time_ns"] == BASE + 1_900_000_000)
    row["data"] = data
    with pytest.raises(ValueError, match="independent rotor quantization"):
        module.analyze_trace(rows, stages)


def test_feedback_must_map_worker_target_to_qemu_epoch(observed_trace):
    rows, stages = observed_trace
    row = next(row for row in rows if row["event"] == "can_tx_queued")
    row["timestamp_ns"] -= QEMU_ORIGIN
    with pytest.raises(ValueError, match="unmatched target-time"):
        module.analyze_trace(rows, stages)


def test_encoder_zero_bin_includes_negative_sub_ulp_wrap(observed_trace):
    rows, stages = observed_trace
    target = next(row for row in rows if row["event"] == "step" and
                  row["timestamp_ns"] == 900_000_000)
    target["joint_position_rad"][0] = -3.652659725485924e-21
    feedback = next(row for row in rows if row["event"] == "can_tx_queued" and
                    row["wall_time_ns"] == BASE + 900_000_000 and row["can_id"] == 0x201)
    feedback["data"] = "0000000000001900"
    assert module.analyze_trace(rows, stages)["feedback_queued_by_id"][0x201] == 4


@pytest.mark.parametrize("phase,key,value,message", [
    (1, "position_m", [0, 0, .2], "forward native"),
    (2, "quaternion_wxyz", [1, 0, 0, 0], "turn native"),
    (2, "gyro_rad_s", [0, 0, .02], "turn native"),
])
def test_native_motion_evidence_is_required_alongside_valid_wire(
        observed_trace, phase, key, value, message):
    rows, stages = observed_trace
    for row in rows:
        if row["event"] == "step" and (BASE + phase * 1_000_000_000 <=
                row["wall_time_ns"] < BASE + (phase + 1) * 1_000_000_000):
            row[key] = value
    with pytest.raises(ValueError, match=message):
        module.analyze_trace(rows, stages)


def test_motion_requires_actual_nonzero_command_to_each_motor(observed_trace):
    rows, stages = observed_trace
    row = next(row for row in rows if row["event"] == "can_rx" and
               row["wall_time_ns"] == BASE + 1_050_000_000)
    row["data"] = "0020002000200000"
    with pytest.raises(ValueError, match="commands to each motor"):
        module.analyze_trace(rows, stages)


def test_uart_stage_coverage_and_nonfinite_target_rejected(observed_trace):
    rows, stages = observed_trace
    with pytest.raises(ValueError, match="UART stages"):
        module.analyze_trace(rows, stages[:-1])
    step = next(row for row in rows if row["event"] == "step")
    step["accel_m_s2"][2] = math.nan
    with pytest.raises(ValueError, match="invalid public target state: accel"):
        module.analyze_trace(rows, stages)
    step["accel_m_s2"][2] = 9.80665
    step["plant_time_s"] = math.nan
    with pytest.raises(ValueError, match="completed 1 ms target"):
        module.analyze_trace(rows, stages)


def test_phase_needs_all_four_feedback_ids(observed_trace):
    rows, stages = observed_trace
    rows = [row for row in rows if not (row["event"] == "can_tx_queued" and
            row["can_id"] == 0x204 and row["wall_time_ns"] == BASE + 2_900_000_000)]
    with pytest.raises(ValueError, match="phase lacks four"):
        module.analyze_trace(rows, stages)


def test_disable_zero_before_actual_ack_is_insufficient(observed_trace):
    rows, stages = observed_trace
    row = next(row for row in rows if row["event"] == "can_rx" and
               row["wall_time_ns"] > stages[-1]["wall_time_ns"])
    row["wall_time_ns"] = stages[-1]["ack_wall_time_ns"] - 1
    rows.sort(key=lambda row: row["wall_time_ns"])
    with pytest.raises(ValueError, match="disable lacks actual"):
        module.analyze_trace(rows, stages)


def final_report(tmp_path):
    source = tmp_path / "input.bin"
    source.write_bytes(b"fixed consumer input")
    return dict(result="candidate", errors=[], cleanup_errors=[], worker_reaped=True,
                processes={"qemu": dict(pid=100, starttime=1), "worker": dict(pid=101, starttime=2)},
                inputs_before={str(source): hashlib.sha256(source.read_bytes()).hexdigest()})


@pytest.mark.parametrize("failure", [None, "harness", "connection", "identity", "unreaped",
                                     "source", "missing_hashes", "prior"])
def test_final_pass_requires_cleanup_and_unchanged_inputs(tmp_path, monkeypatch, failure):
    report = final_report(tmp_path)
    # Only isolate external process observation. Source hashes use real files.
    monkeypatch.setattr(module, "_identity", lambda pid: None)
    if failure == "connection":
        report["cleanup_errors"].append("QMP disconnect failed")
    elif failure == "identity":
        monkeypatch.setattr(module, "_identity", lambda pid: report["processes"]["qemu"])
    elif failure == "unreaped":
        report["worker_reaped"] = False
    elif failure == "source":
        (tmp_path / "input.bin").write_bytes(b"changed during cleanup")
    elif failure == "missing_hashes":
        report.pop("inputs_before")
    elif failure == "prior":
        report["result"] = "failed"
        report["errors"].append("missing firmware observation")
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    status = module.finalize(tmp_path, 7 if failure == "harness" else 0)
    actual = json.loads(path.read_text())
    assert status == (0 if failure is None else 1)
    assert actual["result"] == ("passed" if failure is None else "failed")
    if failure is None:
        assert actual["cleanup_verified"] is True
        assert actual["inputs_final"] == actual["inputs_before"]
    elif failure == "connection":
        assert actual["cleanup_errors"] == ["QMP disconnect failed"]
    elif failure == "harness":
        assert "bounded harness failed (consumer or process cleanup)" in actual["errors"]
