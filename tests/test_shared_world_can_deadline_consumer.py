"""Independent public-event report mutants; these fixtures do not run QEMU."""
from copy import deepcopy
import pytest
from tools import dm_mc02_scene_smoke as tool


def deadline_receipt():
    return {"cut_times_ns": [0, 1_000_000, 1_250_000, *range(2_000_000, 51_000_000, 1_000_000), 50_250_000],
        "feedback": [{"member": "east", "source_ns": 1_000_000, "due_ns": 1_250_000,
                      "delivery_ns": 1_250_000, "dropped": False}],
        "feedback_emitted": [{"member": "east", "source_ns": 50_250_000, "can_id": 0x201 + index}
                             for index in range(4)],
        "runtime_result": {"end_ns": 50_250_000}}


def test_deadline_report_requires_exact_delivery_and_four_future_emissions():
    report = deadline_receipt()
    tool.check_can_deadline_observations(report)
    assert report["deadline_verified"]["future_frames"] == 4
    assert report["deadline_verified"]["next_expected_due_ns"] == 50_500_000


@pytest.mark.parametrize("fault", ["missing-cut", "source-rewritten", "late-delivery", "lost-future", "future-flushed"])
def test_deadline_validator_rejects_missing_or_premature_public_evidence(fault):
    report = deepcopy(deadline_receipt())
    if fault == "missing-cut":
        report["cut_times_ns"].remove(1_250_000)
    elif fault == "source-rewritten":
        report["feedback"][0]["source_ns"] = 1_250_000
    elif fault == "late-delivery":
        report["feedback"][0]["delivery_ns"] = 2_000_000
    elif fault == "lost-future":
        report["feedback_emitted"].pop()
    else:
        report["feedback"].append({"member": "east", "source_ns": 50_250_000,
            "due_ns": 50_500_000, "delivery_ns": 50_250_000, "dropped": False})
    with pytest.raises(ValueError, match="CAN-deadlines"):
        tool.check_can_deadline_observations(report)
    assert "deadline_verified" not in report


def test_completed_world_cuts_cannot_exceed400us_or_miss_endpoint():
    report = {"cut_times_ns": [0, 400_000, 800_000], "runtime_result": {"end_ns": 800_000}}
    tool.check_multi_cut_observations(report)
    report["cut_times_ns"][1] = 399_999
    with pytest.raises(ValueError, match="400us"):
        tool.check_multi_cut_observations(report)
