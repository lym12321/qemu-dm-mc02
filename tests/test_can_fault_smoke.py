"""Pure verifier fixtures are crafted data, never evidence of native execution."""
from copy import deepcopy
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from smoke import simulation_smoke as smoke


def evidence():
    observations = smoke.Observations(capture_can_faults=True)
    origin = 2_000_000
    result = SimpleNamespace(passed=True, delivered_inputs=47, origin_ns=origin,
                             ready_receipt=object())
    for ms, previous, profile, delay in ((0, "normal", "normal", 0),
            (20, "normal", "missing-heartbeat", 0), (50, "missing-heartbeat", "normal", 0),
            (60, "normal", "delayed", 7_000_000), (80, "delayed", "normal", 0)):
        cut = origin + ms * 1_000_000
        observations.fault_events.append({"kind": "can.fault.applied", "source_node": "can0",
            "source_port": "fault", "source_time_ns": cut,
            "mapped_time_ns": cut, "delivery_time_ns": cut,
            "payload": {"previous_profile": previous, "profile": profile,
                "delay_ns": delay, "cut_ns": cut, "drop": [{"source": "heartbeat.can",
                    "destination": "main.fdcan1", "can_id": 0x321}] if ms == 20 else []}})
    deliveries = []
    for ms in range(5, 106, 5):
        for source, can_id, initial, stride in (("heartbeat", 0x321, 0x13579bdf, 0x10203),
                                               ("diagnostic", 0x322, 0xc001d00d, 0x30101)):
            nonce = initial + ms * stride
            raw = (nonce.to_bytes(4, "little") + (nonce ^ 0xa55a96c3).to_bytes(4, "little")).hex()
            cut = origin + ms * 1_000_000
            due = cut + (7_000_000 if 60 <= ms < 80 else 0)
            frame = {"can_id": can_id, "flags": 0, "dlc": 8,
                     "data": {"bytes_hex": raw}, "timestamp_ns": cut}
            for destination in ("main.fdcan1", "observer.can",
                                "diagnostic.can" if source == "heartbeat" else "heartbeat.can"):
                deliveries.append({"kind": "can.delivery", "source_node": source,
                    "source_port": "can", "source_time_ns": cut, "mapped_time_ns": cut,
                    "delivery_time_ns": due, "payload": {"frame": deepcopy(frame),
                        "destination": destination, "due_ns": due, "late": False,
                        "dropped": source == "heartbeat" and 20 <= ms < 50 and destination == "main.fdcan1"}})
            observations.fault_events.append({"kind": "can-host.received", "source_node": "observer",
                "source_time_ns": due, "mapped_time_ns": due, "delivery_time_ns": due,
                "payload": {"frame": {**frame, "timestamp_ns": due}}})
    counter = 0
    for sequence, event in enumerate(sorted(deliveries, key=lambda e: e["delivery_time_ns"]), 10):
        event["sequence"] = sequence * 3
        observations.fault_events.append(event)
        frame = event["payload"]["frame"]
        if event["payload"]["destination"] == "main.fdcan1" and not event["payload"]["dropped"]:
            counter += 1
            observations.receipts.append({"event_sequence": sequence * 3 + 1,
                "mapped_time_ns": event["delivery_time_ns"], "payload": {
                    "completed_frames": counter, "rx_dropped": 0, "partial_bytes": 0}})
            nonce = int.from_bytes(bytes.fromhex(frame["data"]["bytes_hex"])[:4], "little")
            raw = ((nonce ^ 0x6d2b79f5).to_bytes(4, "little") +
                   ((((nonce << 7) | (nonce >> 25)) + frame["can_id"]) & 0xffffffff).to_bytes(4, "little"))
            source_ns = event["delivery_time_ns"] + 100
            observations.can.append({"source": "main", "can_id": frame["can_id"] + 0x100,
                "data": raw.hex(), "flags": 0, "dlc": 8, "source_time_ns": source_ns,
                "wire_timestamp_ns": source_ns, "quality": "exact", "event_sequence": 10000 + sequence})
    # input_commit receipts cover the complete admitted group at each cut.
    groups = {}
    for item in observations.receipts:
        groups[item["mapped_time_ns"]] = item
    observations.receipts = list(groups.values())
    for data, cut in ((b"SIM_READY can-faults\n", origin), (b"HB_ACTIVE\n", origin + 6_000_000),
            (b"HB_TIMEOUT\n", origin + 30_000_000), (b"HB_RECOVERED\n", origin + 51_000_000),
            (b"CAN_FAULT_PASS\n", origin + 111_000_000)):
        observations.uart.extend(data)
        observations.uart_batches.append({"source": "main", "data_hex": data.hex(),
                                         "mapped_time_ns": cut, "quality": "upper_bound"})
    observations.snapshots = [{"source": "main", "b0_output": True, "b0": level,
        "mapped_time_ns": origin + ms * 1_000_000} for ms, level in ((30, 1), (51, 0))]
    return observations, result


def test_fixed_verifier_keeps_per_recipient_drop_and_real_nonce_challenge_evidence():
    observations, result = evidence()
    verified = smoke.verify_can_faults(observations, result)
    assert len(verified["computed_replies"]) == 36
    assert verified["observer_challenges"] == 42
    assert verified["dropped_main_heartbeats"] == 6
    assert verified["diagnostic_replies_during_drop"] == 6


@pytest.mark.parametrize("mutation, message", [
    ("no_rx", "nonce-derived CAN RX replies are missing"),
    ("stale_nonce", "nonce-derived CAN RX reply differs"),
    ("other_recipient_dropped", "per-recipient drop"),
    ("pending_retimed", "immutable due time"),
    ("gpio_missing", "GPIO watchdog"),
])
def test_apparent_pass_cannot_replace_consumption_or_fabric_receipts(mutation, message):
    observations, result = evidence()
    if mutation == "no_rx":
        observations.can.clear()
    elif mutation == "stale_nonce":
        observations.can[0]["data"] = "00" * 8
    elif mutation == "gpio_missing":
        observations.snapshots[0]["b0"] = 0
    else:
        row = next(e for e in observations.fault_events if e["kind"] == "can.delivery"
                   and e["source_node"] == "heartbeat"
                   and e["payload"]["destination"] == "observer.can"
                   and e["mapped_time_ns"] == result.origin_ns + (20 if mutation == "other_recipient_dropped" else 75) * 1_000_000)
        if mutation == "other_recipient_dropped":
            row["payload"]["dropped"] = True
        else:
            row["payload"]["due_ns"] -= 7_000_000
    with pytest.raises(AssertionError, match=message):
        smoke.verify_can_faults(observations, result)


def test_real_fixture_builds_profiles_and_rx_mutant_without_vendor_sdk(tmp_path):
    if any(shutil.which(program) is None for program in ("cmake", "arm-none-eabi-gcc")):
        pytest.skip("optional ARM cross compiler/CMake unavailable")
    built = []
    for profile, skip in (("Debug", False), ("Release", False), ("Debug", True)):
        result = smoke.build_example("can-faults", build_type=profile, can_skip_rx=skip,
            build_dir=tmp_path / (profile + ("-skip-rx" if skip else "")))
        raw = Path(result["elf"]).read_bytes()
        assert raw[:6] == b"\x7fELF\x01\x01" and int.from_bytes(raw[18:20], "little") == 40
        assert all(row["exit"] == 0 for row in result["build"])
        built.append(result)
    assert len({row["elf"] for row in built}) == 3
    assert len({row["sha256"] for row in built}) == 3
