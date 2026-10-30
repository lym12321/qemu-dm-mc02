"""Independent native-report fixtures for the known-CAN-due consumer gate."""

from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from tools import dm_mc02_arena_smoke as tool


def deadline_receipt():
    """Literal native identities; this report does not execute a simulator."""
    events, feedback, pending, steps = [], [], [], []
    goals = [0, 0]

    def observation(peer, target, initial=False):
        native = (1000, 2000)[peer] + target
        return {"kind": "can-observation", "peer": peer, "submitted_frames": goals[peer],
            "state": {"execution": ({"state": "idle", "current-ns": native} if initial else
                {"state": "reached", "current-ns": native, "target-ns": native}),
                "status": {"running": False, "status": "prelaunch" if initial else "paused"},
                "fdcan-rx-state": {"timestamp_ns": native, "controllers": {
                    "dm-mc02.fdcan1": {"completed_frames": goals[peer], "partial_bytes": 0}}},
                "fdcan-transports": {"dm-mc02.fdcan1": {"connected": True}}}}

    events.extend(observation(peer, 0, True) for peer in (0, 1))
    targets = sorted({*range(1_000_000, 50_000_001, 1_000_000),
                      *range(1_250_000, 50_000_000, 1_000_000)})
    previous = 0
    for step_id, target in enumerate(targets, 1):
        dt = target - previous
        steps.append(dt)
        events.extend({"kind": "cpu-request", "peer": peer, "target_ns": epoch + target}
                      for peer, epoch in enumerate((1000, 2000)))
        events.append({"kind": "world-completed", "dt_ns": dt, "native_time_s": target / 1e9})
        if target % 1_000_000 == 0:
            for peer in (0, 1):
                for can_id in range(0x201, 0x205):
                    source = {"peer": peer, "can_id": can_id, "world_ns": target,
                              "due_ns": target + (250_000 if peer == 0 else 0),
                              "data_hex": "0000000000001900"}
                    events.append({"kind": "feedback-produced", **source})
                    if source["due_ns"] > 50_000_000:
                        pending.append(source)
                    else:
                        feedback.append({**source, "delivered_ns": source["due_ns"],
                                         "accepted": True})
        for peer in (0, 1):
            header = dict(version=2, flags=peer + 1, step_id=step_id, t_sim_ns=target, dt_ns=dt)
            events.append({"kind": "step-send", "peer": peer, "event": {
                "header": dict(header, kind=2), "accepted": True,
                "imu": {"sample_id": step_id, "sensor_time_ns": target}}})
            events.append({"kind": "step-ack", "peer": peer, "event": {
                "header": dict(header, kind=3), "decoded": {"status": 0}}})
            goals[peer] = sum(row["peer"] == peer and row["delivered_ns"] <= target for row in feedback)
        events.extend(observation(peer, target) for peer in (0, 1))
        previous = target
    return {"can_deadlines": {"delay_ns": 250000, "outer_step_ns": 1000000,
        "members": ["east", "west"], "events": events, "pending_feedback": pending,
        "final_buses": [{"time_ns": 50000000, "pending": 4, "next_due_ns": 50250000},
                        {"time_ns": 50000000, "pending": 0, "next_due_ns": None}]},
        "native_calls": {"step_ns": steps}, "feedback": feedback,
        "runtime_result": {"steps": len(steps), "origin_ns": 0, "end_ns": 50000000},
        "responses": [{"name": name, "header": {"kind": 5}, "decoded": {"virtual_time_ns": epoch}}
                      for name, epoch in (("east", 1000), ("west", 2000))]}


def test_native_due_validator_checks_actual_extra_cuts_and_retained_future_feedback():
    report = deadline_receipt()
    tool.check_can_deadline_observations(report)
    evidence = report["can_deadlines"]
    assert evidence["verified"] and evidence["cuts"] == 99
    assert evidence["delayed_delivery_cuts"][0] == 1250000
    assert evidence["delivered_feedback"] == 396 and evidence["future_feedback"] == 4
    assert evidence["final_receipt_goals"] == {0: 196, 1: 200}


@pytest.mark.parametrize("fault", ["late-delivery", "source-rewritten", "missing-feedback",
    "future-flushed", "future-hidden", "wrong-final-pending", "wrong-next-due", "longer-duration",
    "missing-ack", "incomplete-native-receipt"])
def test_native_due_validator_rejects_bad_source_due_cut_or_completion(fault):
    report = deadline_receipt()
    evidence = report["can_deadlines"]
    delayed = next(row for row in report["feedback"] if row["peer"] == 0)
    if fault == "late-delivery":
        delayed["delivered_ns"] = 2000000
    elif fault == "source-rewritten":
        delayed["world_ns"] = delayed["due_ns"]
    elif fault == "missing-feedback":
        report["feedback"].remove(delayed)
    elif fault == "future-flushed":
        row = evidence["pending_feedback"].pop()
        report["feedback"].append(dict(row, accepted=True, delivered_ns=row["due_ns"]))
    elif fault == "future-hidden":
        evidence["pending_feedback"].clear()
    elif fault == "wrong-final-pending":
        evidence["final_buses"][0]["pending"] = 0
    elif fault == "wrong-next-due":
        evidence["final_buses"][0]["next_due_ns"] = 51000000
    elif fault == "longer-duration":
        report["runtime_result"]["origin_ns"] = -1000000
    elif fault == "missing-ack":
        evidence["events"].remove(next(row for row in evidence["events"] if row["kind"] == "step-ack"))
    else:
        receipt = next(row for row in evidence["events"] if row["kind"] == "can-observation"
                       and row["state"]["execution"]["state"] == "reached")
        receipt["state"]["fdcan-rx-state"]["controllers"]["dm-mc02.fdcan1"]["partial_bytes"] = 1
    with pytest.raises(ValueError, match="CAN-deadlines"):
        tool.check_can_deadline_observations(report)
    assert not evidence.get("verified", False)


@pytest.mark.parametrize("overrides", [{"cli": True}, {"multi_cut": True}, {"mode": "reset"}])
def test_can_deadline_python_admission_precedes_result_and_native_access(overrides):
    arguments = SimpleNamespace(can_deadlines=True, cli=False, multi_cut=False, mode="normal")
    for key, value in overrides.items():
        setattr(arguments, key, value)
    with pytest.raises(ValueError, match="--can-deadlines|--multi-cut"):
        tool.main(arguments)


@pytest.mark.parametrize("arguments", [
    ["--can-deadlines"], ["--arena-cli", "--can-deadlines"],
    ["--arena-runtime", "--can-deadlines", "--multi-cut"],
    ["--arena-runtime", "--can-deadlines", "--peer-exit"],
])
def test_shell_can_deadline_admission_precedes_native_tools(arguments):
    path = Path(__file__).resolve().parents[1] / "tools/run-qemu-shared-world-smoke.sh"
    result = subprocess.run(["bash", str(path), *arguments], capture_output=True, text=True, timeout=5)
    assert result.returncode == 2 and "usage:" in result.stderr
