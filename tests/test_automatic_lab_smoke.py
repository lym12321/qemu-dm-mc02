"""The native acceptance gate requires actual raw/identity evidence beyond PASS."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from smoke import automatic_lab_smoke as smoke


def combined_observations():
    origin = 40_000_000
    events = []

    def event(kind, port, payload, mapped, delivered=None, quality="sampled"):
        item = SimpleNamespace(session_id=1, sequence=len(events), source_node="main",
            source_port=port, kind=kind, payload=payload, source_time_ns=None,
            mapped_time_ns=mapped, delivery_time_ns=mapped if delivered is None else delivered,
            quality=quality)
        events.append(item)
        return item

    event("uart", "uart1", b"ADC_IMU_READY\n", origin, quality="upper_bound")
    for number, (cut, value, raw, ticks) in enumerate((
            (60_000_000, {"raw": 12345}, "3039", "000600"),
            (80_000_000, {"voltage_uv": 1650000}, "8000", "000800")), 1):
        event("input.delivered", "adc1.ch4", value, cut)
        identity = {"session_id": 37, "step_id": number + 5,
                    "t_sim_ns": cut, "dt_ns": 10_000_000}
        event("adc-input-accepted", "adc1.ch4", {**identity, "input": value, "status": 0}, cut)
        event("imu-input-accepted", "imu", {**identity, "status": 0, "wire": {
            "sensor_time_ns": cut, "gyro_dps": [0.0] * 3,
            "accel_g": [0.0, 0.0, 1.0003416538238525]}}, cut)
        event("imu-consumption", "imu", {**identity, "status": 0,
            "consumed_mask": 3, "missing_mask": 0}, cut, cut + 10_000_000)
        data = f"ADC_IMU_READ n={number} adc={raw} g=000000000000 a=000000005715 t={ticks}\n".encode()
        event("uart", "uart1", data + (b"ADC_IMU_PASS\n" if number == 2 else b""),
              cut + 10_000_000, quality="upper_bound")
    result = SimpleNamespace(passed=True, delivered_inputs=2, origin_ns=origin)
    return events, result


def test_actual_joint_evidence_retains_both_conversion_reads_and_full_done_identity():
    events, result = combined_observations()
    verified = smoke.verify_combined(events, result)
    assert [row["adc_raw"] for row in verified["readbacks"]] == [12345, 32768]
    assert [row["sensor_ticks_u24"] for row in verified["readbacks"]] == [1536, 2048]
    assert [row["wire_identity"] for row in verified["readbacks"]] == [
        (37, 6, 60_000_000, 10_000_000), (37, 7, 80_000_000, 10_000_000)]


@pytest.mark.parametrize("missing", ["adc-input-accepted", "imu-consumption"])
def test_pass_labels_cannot_replace_native_adc_acceptance_or_imu_consumption(missing):
    events, result = combined_observations()
    events = [event for event in events if event.kind != missing]
    with pytest.raises(AssertionError):
        smoke.verify_combined(events, result)


@pytest.mark.parametrize("change, message", [
    ("different_step", "same-cut STEP identity"),
    ("missing_gyro", "full-consumed DONE"),
    ("incorrect_adc", "actual guest ADC/raw/sensortime"),
    ("stale_sensor_time", "actual guest ADC/raw/sensortime"),
    ("early_uart", "later native UART publication"),
])
def test_independent_verifier_rejects_mismatched_actual_evidence(change, message):
    events, result = deepcopy(combined_observations())
    if change == "different_step":
        next(event for event in events if event.kind == "adc-input-accepted").payload["step_id"] = 99
    elif change == "missing_gyro":
        done = next(event for event in events if event.kind == "imu-consumption")
        done.payload.update(consumed_mask=2, missing_mask=1)
    elif change == "incorrect_adc":
        row = next(event for event in events if event.kind == "uart" and b"n=1" in event.payload)
        row.payload = row.payload.replace(b"adc=3039", b"adc=3038")
    elif change == "stale_sensor_time":
        row = next(event for event in events if event.kind == "uart" and b"n=1" in event.payload)
        row.payload = row.payload.replace(b"t=000600", b"t=000400")
    else:
        row = next(event for event in events if event.kind == "uart" and b"n=1" in event.payload)
        row.mapped_time_ns = 60_000_000
    with pytest.raises(AssertionError, match=message):
        smoke.verify_combined(events, result)
