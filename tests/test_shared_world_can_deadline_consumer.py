"""Public-event native report admission; these literals do not execute QEMU."""

from copy import deepcopy
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from tools import dm_mc02_scene_smoke as tool
from simulation_support import simulation_for


def multi_cut_receipt():
    """Independent wire/native literals; this fixture does not execute QEMU."""
    events = []
    def append(kind, **values):
        events.append({"kind": kind, "order": len(events) + 1, **values})
    def receipt(member, target, goal):
        native = {"east": 1000, "west": 2000}[member] + target
        append("can-observation", member=member, cut_ns=target, submitted_frames=goal,
            state={"execution": {"state": "reached" if target else "idle",
                                  "current-ns": native, "target-ns": native},
                "status": {"running": False, "status": "paused"},
                "fdcan-rx-state": {"timestamp_ns": native, "controllers": {
                    "dm-mc02.fdcan1": {"completed_frames": goal, "partial_bytes": 0}}},
                "fdcan-transports": {"dm-mc02.fdcan1": {"connected": True}}})
    for member in ("east", "west"):
        receipt(member, 0, 0)
    for step, target in enumerate((400_000, 800_000, 1_200_000), 1):
        for member, origin in (("east", 1000), ("west", 2000)):
            append("cpu-request", member=member, native_target_ns=origin + target)
        append("world-completed", target_ns=target, dt_ns=400_000, native_time_s=target / 1e9)
        for member, session in (("east", 11), ("west", 22)):
            header = {"version": 2, "flags": session, "step_id": step,
                      "t_sim_ns": target, "dt_ns": 400_000}
            append("step-send", member=member, header={**header, "kind": 2})
            append("step-ack", member=member, header={**header, "kind": 3}, payload={"status": 0})
        for member in ("east", "west"):
            receipt(member, target, 4 if step == 3 else 0)
    return {"quantum_ns": 400_000, "cut_times_ns": [0, 400_000, 800_000, 1_200_000],
        "multi_cut": {"members": ["east", "west"], "events": events},
        "qemu": {"native_origins": {"east": 1000, "west": 2000}},
        "native_calls": {"step_ns": [400_000] * 3}, "runtime_result": {"end_ns": 1_200_000}}


def test_multi_cut_validator_requires_raw_ack_identity_and_unchanged_early_receipts():
    report = multi_cut_receipt()
    tool.check_multi_cut_observations(report)
    evidence = report["multi_cut"]
    assert evidence["verified"] and evidence["cuts"] == 3
    assert evidence["unchanged_receipt_goals"] == 4
    assert evidence["final_receipt_goals"] == {"east": 4, "west": 4}
    assert evidence["wire_sessions"] == {"east": 11, "west": 22}


@pytest.mark.parametrize("fault", ["missing-ack", "wrong-session", "wrong-dt", "missing-receipt",
    "receipt-before-ack", "partial-receipt", "unreached-target", "duplicate-world", "wrong-world-dt", "early-goal"])
def test_multi_cut_validator_rejects_missing_rewritten_or_premature_evidence(fault):
    report = multi_cut_receipt()
    events = report["multi_cut"]["events"]
    ack = next(row for row in events if row["kind"] == "step-ack")
    receipt = next(row for row in events if row["kind"] == "can-observation" and row["cut_ns"] == 400_000)
    rx = receipt["state"]["fdcan-rx-state"]["controllers"]["dm-mc02.fdcan1"]
    if fault == "missing-ack":
        events.remove(ack)
    elif fault == "wrong-session":
        ack["header"]["flags"] = 22
    elif fault == "wrong-dt":
        ack["header"]["dt_ns"] = 1_000_000
    elif fault == "missing-receipt":
        events.remove(receipt)
    elif fault == "receipt-before-ack":
        receipt["order"] = ack["order"] - 1
    elif fault == "partial-receipt":
        rx["partial_bytes"] = 1
    elif fault == "unreached-target":
        receipt["state"]["execution"]["state"] = "running"
    elif fault == "duplicate-world":
        events.append(deepcopy(next(row for row in events if row["kind"] == "world-completed")))
    elif fault == "wrong-world-dt":
        report["native_calls"]["step_ns"] = [1_200_000]
    else:
        receipt["submitted_frames"] = rx["completed_frames"] = 4
    with pytest.raises(ValueError, match="multi-cut"):
        tool.check_multi_cut_observations(report)
    assert not report["multi_cut"].get("verified", False)


def deadline_receipt():
    cuts = [0, 1_000_000, 1_250_000, 2_000_000]
    cuts += list(range(2_250_000, 50_000_001, 250_000))
    return {"cut_times_ns": cuts,
        "feedback": [{"member": "east", "source_ns": 1_000_000, "due_ns": 1_250_000,
            "delivery_ns": 1_250_000, "dropped": False}],
        "final_networks": {"east_can": {"pending": 4, "next_due_ns": 50_250_000}},
        "runtime_result": {"end_ns": 50_000_000}}


def test_actual_cut_source_due_delivery_and_unflushed_future_are_required():
    report = deadline_receipt()
    tool.check_can_deadline_observations(report)
    assert report["deadline_verified"]["first_due_ns"] == 1_250_000
    assert report["deadline_verified"]["future_frames"] == 4


def test_failure_observer_checks_real_runtime_refusal_before_owned_cleanup():
    error = OSError("pure fixture fails after its physical-side-effect stand-in")
    calls = []
    def fail_step(context):
        calls.append(context.t_sim_ns)
        raise error
    owner = SimpleNamespace(step=fail_step, close=lambda: calls.append("closed"))
    simulation = simulation_for({"component": owner})
    report = {"expected_mode": "normal", "cuts": 0, "cut_times_ns": [],
              "native_calls": {"step_ns": []}}
    observer = tool.NativeObservation(report, (), "pure-observer-contract")
    observer.cut(0, simulation)
    simulation.start()
    with pytest.raises(OSError) as raised:
        simulation.run_until(10)
    assert raised.value is error
    simulation.close()
    assert calls == [10, "closed"]
    assert report["before_cleanup"] == {"state": "failed", "committed_ns": 0,
        "failure_type": "OSError", "retry_rejected": "run_until requires a paused, healthy Simulation",
        "effects_preserved": True}


@pytest.mark.parametrize("fault", ["missing-cut", "source-rewritten", "late-delivery", "lost-future", "future-flushed", "next-due"])
def test_deadline_validator_refuses_incomplete_or_rewritten_evidence(fault):
    report = deepcopy(deadline_receipt())
    if fault == "missing-cut":
        report["cut_times_ns"].remove(1_250_000)
    elif fault == "source-rewritten":
        report["feedback"][0]["source_ns"] = 1_250_000
    elif fault == "late-delivery":
        report["feedback"][0]["delivery_ns"] = 2_000_000
    elif fault == "lost-future":
        report["final_networks"]["east_can"]["pending"] = 0
    elif fault == "future-flushed":
        report["runtime_result"]["end_ns"] = 50_250_000
    else:
        report["final_networks"]["east_can"]["next_due_ns"] = 51_000_000
    with pytest.raises(ValueError, match="CAN-deadlines"):
        tool.check_can_deadline_observations(report)
    assert "deadline_verified" not in report


@pytest.mark.parametrize("overrides", [{"cli": True}, {"multi_cut": True}, {"mode": "reset"}])
def test_invalid_native_profile_rejects_before_result_or_resource_access(overrides):
    args = SimpleNamespace(can_deadlines=True, cli=False, multi_cut=False, mode="normal")
    for key, value in overrides.items():
        setattr(args, key, value)
    with pytest.raises(ValueError, match="--can-deadlines"):
        tool.main(args)


@pytest.mark.parametrize("arguments", [["--unknown"], ["--arena-cli"], ["--arena-runtime"]])
def test_deleted_shell_entry_profiles_are_rejected(arguments):
    path = Path(__file__).resolve().parents[1] / "tools/run-qemu-shared-world-smoke.sh"
    result = subprocess.run(["bash", str(path), *arguments], capture_output=True, text=True, timeout=5)
    assert result.returncode == 2 and "usage:" in result.stderr
