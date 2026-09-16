"""Tests for the v2 step section payload API."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import struct
import sys

import pytest


MODULE_PATH = Path(__file__).parents[1] / "tools" / "dm_mc02_step_protocol.py"
SPEC = importlib.util.spec_from_file_location("dm_mc02_step_protocol", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
protocol = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = protocol
SPEC.loader.exec_module(protocol)


def make_imu() -> protocol.ImuSampleV2:
    return protocol.ImuSampleV2(
        gyro_dps=(1.0, -2.0, 3.5),
        accel_g=(0.1, 0.0, -0.9),
        gyro_bias_dps=(0.01, 0.02, 0.03),
        accel_bias_g=(0.04, 0.05, 0.06),
        sensor_time_ns=123456,
        sample_id=7,
    )


def test_step_payload_round_trips_one_section():
    section = protocol.StepSection(0x1234, 0, b"imu-data")

    encoded = protocol.encode_step_payload((section,))

    assert protocol.decode_step_payload(encoded) == (section,)


def test_new_section_payload_contains_explicit_marker():
    encoded = protocol.encode_step_payload((protocol.StepSection(1, 0, b"x"),))
    assert struct.unpack_from("<H", encoded, 2)[0] == \
        protocol.STEP_PAYLOAD_SECTION_MARKER


def test_non_60_byte_legacy_zero_reserved_section_payload_still_decodes():
    payload = struct.pack("<HH", 1, 0) + struct.pack("<HHI", 1, 0, 1) + b"x"
    assert protocol.decode_step_payload(payload) == (
        protocol.StepSection(1, 0, b"x"),)


def test_60_byte_section_payload_is_explicitly_marked_and_decodes():
    payload = protocol.encode_step_payload((
        protocol.StepSection(protocol.STEP_SECTION_ADC_INPUT, 0,
                             protocol.encode_adc_input_v2(
                                 protocol.AdcInputV2(1, 2))),
        protocol.StepSection(0x1234, 0, b"x" * 32),
    ))
    assert len(payload) == 60
    assert protocol.decode_step_payload(payload)[0].type == \
        protocol.STEP_SECTION_ADC_INPUT


def test_60_byte_old_section_payload_is_not_silently_decoded_as_section():
    payload = bytearray(protocol.encode_step_payload((
        protocol.StepSection(0x1234, 0, b"x" * 48),)))
    struct.pack_into("<H", payload, 2, 0)
    assert len(payload) == 60
    with pytest.raises(protocol.StepProtocolError, match="compact legacy IMU"):
        protocol.decode_step_payload(bytes(payload))


def test_step_payload_round_trips_multiple_sections_and_preserves_order():
    sections = (
        protocol.StepSection(protocol.STEP_SECTION_IMU_SAMPLE, 0, b"imu"),
        protocol.StepSection(protocol.STEP_SECTION_ADC_INPUT, 0, b"adc-in"),
        protocol.StepSection(protocol.STEP_SECTION_ADC_VOLTAGE, 0, b"adc-v"),
        protocol.StepSection(protocol.STEP_SECTION_MOTOR_COMMAND, 0, b"motor"),
    )

    assert protocol.decode_step_payload(protocol.encode_step_payload(sections)) == sections


def test_build_step_input_contains_decodable_imu_adc_and_motor_sections():
    adc_inputs = (protocol.AdcInputV2(2, 2048), protocol.AdcInputV2(7, 4095))
    adc_voltages = (protocol.AdcVoltageV2(3, 1_800_000),)
    motor = protocol.MotorCommand(
        motor_count=1,
        commands=(protocol.MotorCommandItem(0, 12.5),),
    )

    sections = protocol.decode_step_payload(protocol.build_step_input(
        make_imu(), adc_inputs, adc_voltages, motor))

    assert [section.type for section in sections] == [
        protocol.STEP_SECTION_IMU_SAMPLE,
        protocol.STEP_SECTION_ADC_INPUT,
        protocol.STEP_SECTION_ADC_INPUT,
        protocol.STEP_SECTION_ADC_VOLTAGE,
        protocol.STEP_SECTION_MOTOR_COMMAND,
    ]
    assert protocol.decode_imu_sample_v2(sections[0].payload) == protocol.decode_imu_sample_v2(
        protocol.encode_imu_sample_v2(make_imu())
    )
    assert protocol.decode_adc_input_v2(sections[1].payload) == adc_inputs[0]
    assert protocol.decode_adc_input_v2(sections[2].payload) == adc_inputs[1]
    assert protocol.decode_adc_voltage_v2(sections[3].payload) == adc_voltages[0]
    assert protocol.decode_motor_command(sections[4].payload) == motor


def test_step_payload_rejects_invalid_marker_and_trailing_bytes():
    encoded = bytearray(protocol.encode_step_payload((
        protocol.StepSection(1, payload=b"x"),
    )))
    struct.pack_into("<H", encoded, 2, 1)
    with pytest.raises(protocol.StepProtocolError, match="marker"):
        protocol.decode_step_payload(bytes(encoded))

    with pytest.raises(protocol.StepProtocolError, match="trailing"):
        protocol.decode_step_payload(
        protocol.encode_step_payload((protocol.StepSection(1, payload=b"x"),))
            + b"extra"
        )


def test_step_payload_rejects_nonzero_section_flags():
    with pytest.raises(protocol.StepProtocolError, match="section flags"):
        protocol.encode_step_payload((protocol.StepSection(1, 1, b"x"),))

    encoded = bytearray(protocol.encode_step_payload((
        protocol.StepSection(1, payload=b"x"),
    )))
    struct.pack_into("<H", encoded, 6, 1)
    with pytest.raises(protocol.StepProtocolError, match="section flags"):
        protocol.decode_step_payload(bytes(encoded))


@pytest.mark.parametrize("data", [
    b"",
    b"\x01",
    struct.pack("<HH", 1, 0),
    struct.pack("<HHHHI", 1, 0, 1, 0, 2) + b"x",
])
def test_step_payload_rejects_truncated_sections(data):
    with pytest.raises(protocol.StepProtocolError):
        protocol.decode_step_payload(data)


def test_step_payload_rejects_payload_over_limit():
    section = protocol.StepSection(1, payload=b"x" * protocol.STEP_PAYLOAD_MAX)

    with pytest.raises(protocol.StepProtocolError, match="too large"):
        protocol.encode_step_payload((section,))

    oversized = struct.pack("<HH", 0, 0) + b"x" * protocol.STEP_PAYLOAD_MAX
    with pytest.raises(protocol.StepProtocolError, match="invalid step payload length"):
        protocol.decode_step_payload(oversized)


@pytest.mark.parametrize("value", [
    protocol.AdcInputV2(-1, 0),
    protocol.AdcInputV2(32, 0),
    protocol.AdcInputV2(0, -1),
    protocol.AdcInputV2(0, 65536),
])
def test_adc_input_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        protocol.encode_adc_input_v2(value)


@pytest.mark.parametrize("value", [
    protocol.AdcVoltageV2(-1, 0),
    protocol.AdcVoltageV2(32, 0),
    protocol.AdcVoltageV2(0, -1),
    protocol.AdcVoltageV2(0, 3_300_001),
    protocol.AdcVoltageV2(0, 0, 2),
])
def test_adc_voltage_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        protocol.encode_adc_voltage_v2(value)


@pytest.mark.parametrize("decoder,data", [
    (protocol.decode_adc_input_v2, b"\x00" * 7),
    (protocol.decode_adc_input_v2, struct.pack("<HHI", 32, 0, 0)),
    (protocol.decode_adc_input_v2, struct.pack("<HHI", 0, 0, 1)),
    (protocol.decode_adc_voltage_v2, b"\x00" * 11),
    (protocol.decode_adc_voltage_v2, struct.pack("<HHII", 0, 2, 0, 0)),
    (protocol.decode_adc_voltage_v2, struct.pack("<HHII", 0, 0, 0, 1)),
    (protocol.decode_adc_voltage_v2, struct.pack("<HHII", 0, 0, 3_300_001, 0)),
])
def test_adc_decoders_reject_invalid_wire_values(decoder, data):
    with pytest.raises(ValueError):
        decoder(data)
