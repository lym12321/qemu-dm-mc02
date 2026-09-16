"""Tests for the independent version-2 step/ack codec."""

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


def test_step_header_round_trip_and_wire_layout():
    header = protocol.StepHeader(protocol.STEP_VERSION, protocol.STEP_ACK, 7,
                                 42, 1_000_000, 2_000, 3)
    wire = protocol.encode_step_header(header)
    assert len(wire) == 36
    assert wire == struct.pack("<HHIQQQI", 2, 3, 7, 42, 1_000_000, 2_000, 3)
    assert protocol.decode_step_header(wire) == header


@pytest.mark.parametrize("kind", [protocol.RESET, protocol.STEP,
                                   protocol.STEP_ACK, protocol.DIAGNOSTICS,
                                   protocol.RESET_ACK, protocol.STEP_DONE,
                                   protocol.TELEMETRY, protocol.MOTOR_STATE])
def test_each_message_kind_round_trips_with_header_invariants(kind):
    payload = bytes(range(16))
    encoded = protocol.build_step_frame(kind, step_id=17, t_sim_ns=900,
                                        dt_ns=11, flags=0xFFFFFFFF,
                                        payload=payload)

    decoded = protocol.decode_step_frame(encoded)

    assert decoded.header.version == protocol.STEP_VERSION
    assert decoded.header.kind == kind
    assert decoded.header.payload_len == len(payload)
    assert decoded.header.step_id == 17
    assert decoded.header.t_sim_ns == 900
    assert decoded.header.dt_ns == 11
    assert decoded.header.flags == 0xFFFFFFFF
    assert decoded.payload == payload
    assert protocol.encode_step_frame(decoded) == encoded


@pytest.mark.parametrize("field, value", [
    ("version", -1),
    ("version", protocol.STEP_VERSION + 1),
    ("kind", -1),
    ("kind", 0x10000),
    ("payload_len", -1),
    ("payload_len", 0x100000000),
    ("step_id", -1),
    ("step_id", 0x10000000000000000),
    ("t_sim_ns", -1),
    ("t_sim_ns", 0x10000000000000000),
    ("dt_ns", -1),
    ("dt_ns", 0x10000000000000000),
    ("flags", -1),
    ("flags", 0x100000000),
])
def test_step_header_rejects_out_of_range_fields(field, value):
    header = protocol.StepHeader(**{field: value})
    with pytest.raises(ValueError):
        protocol.encode_step_header(header)


def test_frame_rejects_unknown_message_kind_on_encode():
    with pytest.raises(protocol.StepProtocolError, match="unsupported"):
        protocol.build_step_frame(99, step_id=1, t_sim_ns=1)


def test_frame_accepts_empty_and_maximum_payload():
    for payload in (b"", b"x" * protocol.STEP_MAX_PAYLOAD):
        encoded = protocol.build_step_frame(protocol.DIAGNOSTICS, 1, 2,
                                            payload=payload)
        decoded = protocol.decode_step_frame(encoded)
        assert decoded.payload == payload
        assert decoded.header.payload_len == len(payload)


def test_frame_rejects_payload_over_maximum():
    with pytest.raises(protocol.StepProtocolError, match="too large"):
        protocol.build_step_frame(protocol.STEP, 1, 2,
                                  payload=b"x" * (protocol.STEP_MAX_PAYLOAD + 1))


def test_step_max_payload_boundary_round_trip_and_rejection():
    payload = bytes(range(256)) * (protocol.STEP_MAX_PAYLOAD // 256)
    assert len(payload) == protocol.STEP_MAX_PAYLOAD

    encoded = protocol.build_step_frame(protocol.STEP, 1, 2, payload=payload)
    assert protocol.decode_step_frame(encoded).payload == payload

    with pytest.raises(protocol.StepProtocolError, match="too large"):
        protocol.build_step_frame(protocol.STEP, 1, 2,
                                  payload=payload + b"x")


@pytest.mark.parametrize("data", [
    b"",
    b"\x00\x00\x00",
    struct.pack("<I", 4 + protocol.STEP_HEADER_SIZE - 1) + b"\x00" *
    (4 + protocol.STEP_HEADER_SIZE - 1),
    struct.pack("<I", 4 + protocol.STEP_HEADER_SIZE +
                protocol.STEP_MAX_PAYLOAD + 1),
])
def test_frame_rejects_truncated_or_out_of_range_outer_lengths(data):
    with pytest.raises(protocol.StepProtocolError):
        protocol.decode_step_frame(data)


def test_frame_rejects_payload_length_mismatch_after_decoding_header():
    encoded = bytearray(protocol.build_step_frame(protocol.STEP, 1, 2,
                                                  payload=b"abc"))
    # Prefix (4), magic (4), then payload_len at offset 12.
    struct.pack_into("<I", encoded, 12, 4)
    with pytest.raises(protocol.StepProtocolError, match="payload length"):
        protocol.decode_step_frame(bytes(encoded))


def test_frame_rejects_non_bytes_payload_and_noncanonical_header():
    with pytest.raises(protocol.StepProtocolError, match="bytes-like"):
        protocol.encode_step_frame(protocol.StepFrame(
            protocol.StepHeader(protocol.STEP_VERSION, protocol.STEP, 0),
            "text"))
    with pytest.raises(protocol.StepProtocolError, match="payload_len"):
        protocol.validate_step_frame(protocol.StepFrame(
            protocol.StepHeader(protocol.STEP_VERSION, protocol.STEP, 2),
            b"x"))


def test_payloads_round_trip():
    command = protocol.MotorCommand(
        2, 9, (protocol.MotorCommandItem(0, 1.25),
               protocol.MotorCommandItem(1, -2.5)))
    assert protocol.decode_motor_command(protocol.encode_motor_command(command)) == command

    state = protocol.MotorState(
        2, (protocol.MotorStateItem(0, 1.0, 2.0, 3.0),
            protocol.MotorStateItem(1, -1.0, -2.0, -3.0)))
    assert protocol.decode_motor_state(protocol.encode_motor_state(state)) == state

    imu = protocol.ImuSampleV2((1.0, 2.0, 3.0), (4.0, 5.0, 6.0),
                               (0.125, 0.25, 0.375), (0.5, 0.625, 0.75),
                               123, 7)
    assert protocol.decode_imu_sample_v2(protocol.encode_imu_sample_v2(imu)) == imu


def test_v2_status_and_capability_constants_match_wire_contract():
    assert (protocol.STATUS_OK, protocol.STATUS_QUEUE_FULL,
            protocol.STATUS_PROTOCOL, protocol.STATUS_UNSUPPORTED) == (0, 1, 2, 3)
    assert protocol.CAPABILITY_MASK == (
        protocol.CAP_STEP | protocol.CAP_IMU | protocol.CAP_DIAGNOSTICS |
        protocol.CAP_ADC | protocol.CAP_TELEMETRY | protocol.CAP_MOTOR)
    assert protocol.V2_STATUS_OK == protocol.STATUS_OK
    assert protocol.V2_CAP_ADC == protocol.CAP_ADC


def test_reset_ack_payload_decodes_native_layout():
    payload = struct.pack(
        "<IIQII", protocol.STATUS_OK,
        protocol.CAP_STEP | protocol.CAP_IMU | protocol.CAP_ADC, 123456, 7, 0)

    decoded = protocol.decode_reset_ack_payload(payload)

    assert len(payload) == protocol.RESET_ACK_PAYLOAD_SIZE == 24
    assert decoded == protocol.ResetAckPayload(
        protocol.STATUS_OK,
        protocol.CAP_STEP | protocol.CAP_IMU | protocol.CAP_ADC,
        123456, 7)
    protocol.validate_reset_ack_payload(payload)


def test_step_ack_payload_decodes_native_layout():
    payload = struct.pack("<IIII", protocol.STATUS_QUEUE_FULL, 11, 23, 0)

    decoded = protocol.decode_step_ack_payload(payload)

    assert len(payload) == protocol.STEP_ACK_PAYLOAD_SIZE == 16
    assert decoded == protocol.StepAckPayload(
        protocol.STATUS_QUEUE_FULL, 11, 23)
    protocol.validate_step_ack_payload(payload)


def test_diagnostics_payload_decodes_native_layout():
    payload = struct.pack("<QQQQQ", 1, 2, 3, 4, 5)

    decoded = protocol.decode_diagnostics_payload(payload)

    assert len(payload) == protocol.DIAGNOSTICS_PAYLOAD_SIZE == 40
    assert decoded == protocol.DiagnosticsPayload(1, 2, 3, 4, 5)
    protocol.validate_diagnostics_payload(payload)


def _board_telemetry_payload(**overrides):
    values = {
        "led_rgb": 0x123456,
        "led_brightness": 777,
        "buzzer": 1,
        "board_flags": 0x5,
        "buzzer_frequency_hz": 4000,
        "buzzer_duty_permille": 500,
    }
    values.update(overrides)
    return protocol.BoardTelemetryPayload(**values)


def test_board_telemetry_payload_round_trips_native_24_byte_layout():
    telemetry = _board_telemetry_payload()
    payload = protocol.encode_board_telemetry_payload(telemetry)

    assert len(payload) == protocol.BOARD_TELEMETRY_PAYLOAD_SIZE == 24
    assert payload == struct.pack(
        "<IIIIII", 0x123456, 777, 1, 0x5, 4000, 500)
    assert protocol.decode_board_telemetry_payload(payload) == telemetry
    protocol.validate_board_telemetry_payload(payload)


@pytest.mark.parametrize("field", [
    "led_rgb",
    "led_brightness",
    "buzzer",
    "board_flags",
    "buzzer_frequency_hz",
    "buzzer_duty_permille",
])
@pytest.mark.parametrize("value", [0, 0xFFFFFFFF])
def test_board_telemetry_u32_fields_accept_wire_boundaries(field, value):
    telemetry = _board_telemetry_payload(**{field: value})
    payload = protocol.encode_board_telemetry_payload(telemetry)

    assert len(payload) == 24
    assert getattr(protocol.decode_board_telemetry_payload(payload), field) == value


@pytest.mark.parametrize("field", [
    "led_rgb",
    "led_brightness",
    "buzzer",
    "board_flags",
    "buzzer_frequency_hz",
    "buzzer_duty_permille",
])
@pytest.mark.parametrize("value", [-1, 0x100000000])
def test_board_telemetry_u32_fields_reject_out_of_range_values(field, value):
    with pytest.raises(ValueError):
        protocol.encode_board_telemetry_payload(
            _board_telemetry_payload(**{field: value}))


@pytest.mark.parametrize("size", [23, 25])
def test_board_telemetry_decoder_requires_exact_24_byte_payload(size):
    with pytest.raises(protocol.StepProtocolError, match="exactly 24 bytes"):
        protocol.decode_board_telemetry_payload(bytes(size))


def test_v2_telemetry_kind_is_7_and_frame_round_trips():
    assert protocol.TELEMETRY == 7
    payload = protocol.encode_board_telemetry_payload(
        _board_telemetry_payload())
    frame = protocol.build_step_frame(protocol.TELEMETRY, step_id=9,
                                      t_sim_ns=90, dt_ns=10, payload=payload)

    decoded = protocol.decode_step_frame(frame)

    assert decoded.header.kind == 7
    assert decoded.header.payload_len == 24
    assert protocol.decode_board_telemetry_payload(decoded.payload) == \
        _board_telemetry_payload()


def test_qemu_to_host_session_accepts_v2_board_telemetry_after_reset():
    validator = protocol.StepSessionValidator(protocol.DIRECTION_QEMU_TO_HOST)
    validator.begin_reset(0, 0)
    validator.accept(_directional_frame(protocol.RESET_ACK, 0, 0,
                                         payload=_reset_ack_payload()))
    payload = protocol.encode_board_telemetry_payload(
        _board_telemetry_payload())

    validator.accept(_directional_frame(protocol.TELEMETRY, 1, 10, 10,
                                         payload))


def test_step_done_payload_decodes_native_layout_and_aliases():
    payload = struct.pack("<IIIII", protocol.STATUS_OK,
                          protocol.STEP_DONE_REQUIRED_MASK, 0, 7, 11)

    decoded = protocol.decode_step_done_payload(payload)

    assert len(payload) == protocol.STEP_DONE_PAYLOAD_SIZE == 20
    assert decoded == protocol.StepDonePayload(
        protocol.STATUS_OK, protocol.STEP_DONE_REQUIRED_MASK, 0, 7, 11)
    assert protocol.StepDonePayloadV2 is protocol.StepDonePayload
    assert protocol.STEP_DONE_V2_PAYLOAD_SIZE == 20
    assert protocol.unpack_step_done_payload(payload) == decoded
    protocol.validate_step_done_payload(payload)


@pytest.mark.parametrize("decoder, expected_size", [
    (protocol.decode_reset_ack_payload, protocol.RESET_ACK_PAYLOAD_SIZE),
    (protocol.decode_step_ack_payload, protocol.STEP_ACK_PAYLOAD_SIZE),
    (protocol.decode_diagnostics_payload, protocol.DIAGNOSTICS_PAYLOAD_SIZE),
    (protocol.decode_step_done_payload, protocol.STEP_DONE_PAYLOAD_SIZE),
])
def test_v2_response_decoders_require_exact_payload_lengths(decoder,
                                                            expected_size):
    for size in (expected_size - 1, expected_size + 1):
        with pytest.raises(protocol.StepProtocolError, match="exactly"):
            decoder(bytes(size))


def test_reset_ack_rejects_unknown_status_capability_and_reserved_bits():
    valid_capabilities = protocol.CAP_STEP
    with pytest.raises(protocol.StepProtocolError, match="status"):
        protocol.decode_reset_ack_payload(
            struct.pack("<IIQII", 99, valid_capabilities, 0, 0, 0))
    with pytest.raises(protocol.StepProtocolError, match="capability"):
        protocol.decode_reset_ack_payload(
            struct.pack("<IIQII", protocol.STATUS_OK,
                        valid_capabilities | (1 << 31), 0, 0, 0))
    with pytest.raises(protocol.StepProtocolError, match="reserved"):
        protocol.decode_reset_ack_payload(
            struct.pack("<IIQII", protocol.STATUS_OK, valid_capabilities,
                        0, 0, 1))


def test_step_ack_rejects_unknown_status_and_reserved_bits():
    with pytest.raises(protocol.StepProtocolError, match="status"):
        protocol.decode_step_ack_payload(struct.pack("<IIII", 99, 0, 0, 0))
    with pytest.raises(protocol.StepProtocolError, match="reserved"):
        protocol.decode_step_ack_payload(
            struct.pack("<IIII", protocol.STATUS_OK, 0, 0, 1))


def test_step_done_rejects_unknown_status_and_requires_u32_fields():
    with pytest.raises(protocol.StepProtocolError, match="status"):
        protocol.decode_step_done_payload(
            struct.pack("<IIIII", 99, 0, 0, 0, 0))

    payload = struct.pack("<IIIII", protocol.STATUS_PROTOCOL, 1, 2,
                          0xFFFFFFFF, 0xFFFFFFFF)
    assert protocol.decode_step_done_payload(payload) == protocol.StepDonePayload(
        protocol.STATUS_PROTOCOL, 1, 2,
        0xFFFFFFFF, 0xFFFFFFFF)


@pytest.mark.parametrize("consumed, missing", [(0, 3), (1, 2), (2, 1), (3, 0)])
def test_step_done_masks_must_partition_accel_and_gyro(consumed, missing):
    payload = struct.pack("<IIIII", protocol.STATUS_PROTOCOL, consumed,
                          missing, 0, 0)
    assert protocol.decode_step_done_payload(payload).consumed_mask == consumed


@pytest.mark.parametrize("consumed, missing, status", [
    (4, 0, protocol.STATUS_PROTOCOL),
    (0, 4, protocol.STATUS_PROTOCOL),
    (1, 1, protocol.STATUS_PROTOCOL),
    (0, 0, protocol.STATUS_PROTOCOL),
    (1, 2, protocol.STATUS_OK),
])
def test_step_done_masks_reject_unknown_or_inconsistent_values(consumed,
                                                               missing,
                                                               status):
    payload = struct.pack("<IIIII", status, consumed, missing, 0, 0)
    with pytest.raises(protocol.StepProtocolError, match="mask|consume"):
        protocol.decode_step_done_payload(payload)


def _reset_ack_payload():
    return struct.pack("<IIQII", protocol.STATUS_OK,
                       protocol.CAPABILITY_MASK, 0, 0, 0)


def _step_ack_payload(status=protocol.STATUS_OK):
    return struct.pack("<IIII", status, 0, 0, 0)


def _step_done_payload(status=protocol.STATUS_OK):
    return struct.pack("<IIIII", status,
                       protocol.STEP_DONE_REQUIRED_MASK, 0, 0, 0)


def _motor_state_payload():
    return protocol.encode_motor_state(protocol.MotorState(
        2, states=(protocol.MotorStateItem(0, 1.0, 2.0, 3.0),
                   protocol.MotorStateItem(1, -1.0, -2.0, -3.0))))


def _directional_frame(kind, step_id, t_sim_ns, dt_ns=0, payload=b"",
                       session_id=0):
    return protocol.StepFrame(
        protocol.StepHeader(protocol.STEP_VERSION, kind, len(payload),
                            step_id, t_sim_ns, dt_ns, session_id), payload)


def _valid_step_payload():
    return protocol.encode_imu_sample_v2(protocol.ImuSampleV2(
        (0.0, 0.0, 0.0), (0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0, 0))


def test_directional_validators_keep_input_and_response_sequences_separate():
    tx = protocol.StepSessionValidator(protocol.DIRECTION_HOST_TO_QEMU)
    rx = protocol.StepSessionValidator(protocol.DIRECTION_QEMU_TO_HOST,
                                       require_step_done=True)
    reset = _directional_frame(protocol.RESET, 0, 0)
    tx.accept(reset)
    rx.begin_reset(0, 0)
    rx.accept(_directional_frame(protocol.RESET_ACK, 0, 0,
                                 payload=_reset_ack_payload()))

    step = _directional_frame(protocol.STEP, 1, 10, 10,
                               payload=_valid_step_payload())
    tx.accept(step)
    with pytest.raises(protocol.StepProtocolError, match="host-to-QEMU"):
        tx.accept(_directional_frame(
            protocol.STEP_ACK, 1, 10, 10, _step_ack_payload()))
    with pytest.raises(protocol.StepProtocolError, match="input frame"):
        rx.accept(step)

    rx.accept(_directional_frame(protocol.STEP_ACK, 1, 10, 10,
                                 _step_ack_payload()))
    rx.accept(_directional_frame(protocol.STEP_DONE, 1, 10, 10,
                                 _step_done_payload()))


def test_reset_session_id_is_echoed_by_all_session_frames():
    session_id = 0x10203040
    validator = protocol.StepSessionValidator(
        protocol.DIRECTION_QEMU_TO_HOST, require_step_done=True)
    validator.begin_reset(0, 0, session_id=session_id)
    validator.accept(_directional_frame(
        protocol.RESET_ACK, 0, 0, payload=_reset_ack_payload(),
        session_id=session_id))
    validator.accept(_directional_frame(
        protocol.STEP_ACK, 1, 10, 10, _step_ack_payload(), session_id))
    validator.accept(_directional_frame(
        protocol.MOTOR_STATE, 1, 10, 10, _motor_state_payload(), session_id))
    validator.accept(_directional_frame(
        protocol.STEP_DONE, 1, 10, 10, _step_done_payload(), session_id))
    validator.accept(_directional_frame(
        protocol.TELEMETRY, 2, 20, 10,
        protocol.encode_board_telemetry_payload(_board_telemetry_payload()),
        session_id))


def test_qemu_to_host_accepts_motor_state_matching_latest_step_ack():
    session_id = 0x10203040
    validator = protocol.StepSessionValidator.qemu_to_host(
        require_step_done=True)
    validator.begin_reset(0, 0, session_id=session_id)
    validator.accept(_directional_frame(
        protocol.RESET_ACK, 0, 0, payload=_reset_ack_payload(),
        session_id=session_id))
    validator.accept(_directional_frame(
        protocol.STEP_ACK, 1, 10, 10, _step_ack_payload(), session_id))

    validator.accept(_directional_frame(
        protocol.MOTOR_STATE, 1, 10, 10, _motor_state_payload(), session_id))


def test_qemu_to_host_rejects_replayed_motor_state_after_new_step_ack():
    validator = protocol.StepSessionValidator.qemu_to_host(
        require_step_done=True)
    validator.begin_reset(0, 0)
    validator.accept(_directional_frame(
        protocol.RESET_ACK, 0, 0, payload=_reset_ack_payload()))
    validator.accept(_directional_frame(
        protocol.STEP_ACK, 1, 10, 10, _step_ack_payload()))
    validator.accept(_directional_frame(
        protocol.MOTOR_STATE, 1, 10, 10, _motor_state_payload()))
    validator.accept(_directional_frame(
        protocol.STEP_ACK, 2, 20, 10, _step_ack_payload()))

    with pytest.raises(protocol.StepProtocolError, match="match"):
        validator.accept(_directional_frame(
            protocol.MOTOR_STATE, 1, 10, 10, _motor_state_payload()))


@pytest.mark.parametrize("field, value", [
    ("step_id", 2),
    ("t_sim_ns", 11),
    ("dt_ns", 9),
    ("session_id", 0x55667788),
])
def test_qemu_to_host_rejects_motor_state_metadata_mismatch(field, value):
    session_id = 0x10203040
    validator = protocol.StepSessionValidator.qemu_to_host(
        require_step_done=True)
    validator.begin_reset(0, 0, session_id=session_id)
    validator.accept(_directional_frame(
        protocol.RESET_ACK, 0, 0, payload=_reset_ack_payload(),
        session_id=session_id))
    validator.accept(_directional_frame(
        protocol.STEP_ACK, 1, 10, 10, _step_ack_payload(), session_id))

    metadata = {
        "step_id": 1,
        "t_sim_ns": 10,
        "dt_ns": 10,
        "session_id": session_id,
    }
    metadata[field] = value
    with pytest.raises(protocol.StepProtocolError, match="match|session_id"):
        validator.accept(_directional_frame(
            protocol.MOTOR_STATE, payload=_motor_state_payload(), **metadata))


@pytest.mark.parametrize("payload", [
    b"",
    struct.pack("<H", 1),
    struct.pack("<Hfff", 1, 0.0, 0.0, 0.0),
])
def test_qemu_to_host_rejects_invalid_motor_state_payload(payload):
    validator = protocol.StepSessionValidator.qemu_to_host(
        require_step_done=True)
    validator.begin_reset(0, 0)
    validator.accept(_directional_frame(
        protocol.RESET_ACK, 0, 0, payload=_reset_ack_payload()))
    validator.accept(_directional_frame(
        protocol.STEP_ACK, 1, 10, 10, _step_ack_payload()))

    with pytest.raises(ValueError, match="truncated|invalid length"):
        validator.accept(_directional_frame(
            protocol.MOTOR_STATE, 1, 10, 10, payload))


def test_reset_invalidates_old_session_frames():
    validator = protocol.StepSessionValidator(
        protocol.DIRECTION_HOST_TO_QEMU)
    old_session = 0x11111111
    new_session = 0x22222222
    validator.accept(_directional_frame(
        protocol.RESET, 0, 0, session_id=old_session))
    validator.accept(_directional_frame(
        protocol.STEP, 1, 10, 10, payload=_valid_step_payload(),
        session_id=old_session))
    validator.accept(_directional_frame(
        protocol.RESET, 0, 0, session_id=new_session))

    with pytest.raises(protocol.StepProtocolError, match="session_id"):
        validator.accept(_directional_frame(
            protocol.STEP, 1, 10, 10, payload=_valid_step_payload(),
            session_id=old_session))
    validator.accept(_directional_frame(
        protocol.STEP, 1, 10, 10, payload=_valid_step_payload(),
        session_id=new_session))


def test_default_validator_adopts_nonzero_reset_and_zero_is_legacy():
    validator = protocol.StepSessionValidator()
    assert validator.session_id == 0
    validator.accept(_directional_frame(
        protocol.RESET, 0, 0, session_id=0x55))
    assert validator.session_id == 0x55
    with pytest.raises(protocol.StepProtocolError, match="session_id"):
        validator.accept(_directional_frame(protocol.STEP, 1, 10, 10))

    legacy = protocol.StepSessionValidator()
    legacy.accept(_directional_frame(protocol.RESET, 0, 0))
    legacy.accept(_directional_frame(protocol.STEP, 1, 10, 10))
    with pytest.raises(protocol.StepProtocolError, match="session_id"):
        legacy.accept(_directional_frame(protocol.STEP, 2, 20, 10,
                                         session_id=0x55))


def test_validator_supports_explicit_and_generated_session_ids():
    explicit = 0xCAFEBABE
    validator = protocol.StepSessionValidator(session_id=explicit)
    validator.accept(_directional_frame(protocol.RESET, 0, 0,
                                        session_id=explicit))
    with pytest.raises(protocol.StepProtocolError, match="RESET session_id"):
        validator.accept(_directional_frame(protocol.RESET, 0, 0,
                                            session_id=explicit + 1))

    generated = protocol.StepSessionValidator(generate_session_id=True)
    assert 0 < generated.session_id <= protocol.UINT32_MAX
    generated.accept(_directional_frame(protocol.RESET, 0, 0,
                                        session_id=generated.session_id))
    with pytest.raises(ValueError):
        protocol.StepSessionValidator(session_id=1,
                                      generate_session_id=True)


def test_response_validator_requires_ack_before_matching_step_done():
    rx = protocol.StepSessionValidator(protocol.DIRECTION_QEMU_TO_HOST,
                                       require_step_done=True)
    rx.begin_reset(0, 0)
    rx.accept(_directional_frame(protocol.RESET_ACK, 0, 0,
                                 payload=_reset_ack_payload()))
    with pytest.raises(protocol.StepProtocolError, match="no outstanding"):
        rx.accept(_directional_frame(protocol.STEP_DONE, 1, 10, 10,
                                     _step_done_payload()))


def test_response_validator_rejects_duplicate_done_and_out_of_order_steps():
    rx = protocol.StepSessionValidator(protocol.DIRECTION_QEMU_TO_HOST,
                                       require_step_done=True)
    rx.begin_reset(0, 0)
    rx.accept(_directional_frame(protocol.RESET_ACK, 0, 0,
                                 payload=_reset_ack_payload()))
    ack1 = _directional_frame(protocol.STEP_ACK, 1, 10, 10,
                               _step_ack_payload())
    rx.accept(ack1)
    # Replayed ACKs are idempotent while the consumer is recovering a lost
    # STEP_DONE; they must not create a second outstanding transaction.
    rx.accept(ack1)
    with pytest.raises(protocol.StepProtocolError, match="out of order"):
        rx.accept(_directional_frame(protocol.STEP_DONE, 2, 20, 10,
                                     _step_done_payload()))
    rx.accept(_directional_frame(protocol.STEP_DONE, 1, 10, 10,
                                 _step_done_payload()))
    with pytest.raises(protocol.StepProtocolError, match="outstanding|duplicate"):
        rx.accept(_directional_frame(protocol.STEP_DONE, 1, 10, 10,
                                     _step_done_payload()))


def test_response_validator_accepts_one_replayed_done_after_replayed_ack():
    rx = protocol.StepSessionValidator(protocol.DIRECTION_QEMU_TO_HOST,
                                       require_step_done=True)
    rx.begin_reset(0, 0)
    rx.accept(_directional_frame(protocol.RESET_ACK, 0, 0,
                                 payload=_reset_ack_payload()))
    ack = _directional_frame(protocol.STEP_ACK, 1, 10, 10,
                             _step_ack_payload())
    done = _directional_frame(protocol.STEP_DONE, 1, 10, 10,
                              _step_done_payload())

    rx.accept(ack)
    rx.accept(done)

    # QEMU's retry response is ACK followed by the same DONE. The original
    # DONE has already been consumed, so this pair must remain idempotent.
    rx.accept(ack)
    rx.accept(done)

    # A second DONE without another replay ACK is a genuine duplicate.
    with pytest.raises(protocol.StepProtocolError, match="outstanding"):
        rx.accept(done)


def test_response_validator_rejects_replayed_done_for_wrong_record():
    rx = protocol.StepSessionValidator(protocol.DIRECTION_QEMU_TO_HOST,
                                       require_step_done=True)
    rx.begin_reset(0, 0)
    rx.accept(_directional_frame(protocol.RESET_ACK, 0, 0,
                                 payload=_reset_ack_payload()))
    ack1 = _directional_frame(protocol.STEP_ACK, 1, 10, 10,
                              _step_ack_payload())
    done1 = _directional_frame(protocol.STEP_DONE, 1, 10, 10,
                                _step_done_payload())
    ack2 = _directional_frame(protocol.STEP_ACK, 2, 20, 10,
                              _step_ack_payload())
    done2 = _directional_frame(protocol.STEP_DONE, 2, 20, 10,
                                _step_done_payload())

    rx.accept(ack1)
    rx.accept(done1)
    rx.accept(ack1)
    with pytest.raises(protocol.StepProtocolError, match="no outstanding"):
        rx.accept(done2)
    rx.accept(ack2)
    rx.accept(done2)


@pytest.mark.parametrize("flags", [1, 0xFFFF])
def test_step_section_flags_are_reserved_and_rejected_on_encode_and_decode(flags):
    section = protocol.StepSection(protocol.STEP_SECTION_IMU_SAMPLE, flags,
                                   b"imu")
    with pytest.raises(protocol.StepProtocolError, match="reserved.*zero"):
        protocol.encode_step_payload((section,))

    encoded = bytearray(protocol.encode_step_payload(
        (protocol.StepSection(protocol.STEP_SECTION_IMU_SAMPLE, 0, b"imu"),)))
    struct.pack_into("<H", encoded, protocol.STEP_PAYLOAD_HEADER_SIZE + 2,
                     flags)
    with pytest.raises(protocol.StepProtocolError, match="reserved.*zero"):
        protocol.decode_step_payload(bytes(encoded))


def test_host_validator_accepts_compact_and_typed_section_inputs():
    tx = protocol.StepSessionValidator.host_to_qemu()
    tx.accept(_directional_frame(protocol.RESET, 0, 0))
    tx.accept(_directional_frame(protocol.STEP, 1, 10, 10,
                                 payload=_valid_step_payload()))
    payload = protocol.build_step_input(
        protocol.ImuSampleV2((0.0,) * 3, (0.0,) * 3, (0.0,) * 3,
                              (0.0,) * 3, 0, 1),
        (protocol.AdcInputV2(4, 123),),
        (protocol.AdcVoltageV2(19, 2_400_000),),
        protocol.MotorCommand(1,
                              commands=(protocol.MotorCommandItem(0, 1.0),)))
    tx.accept(_directional_frame(protocol.STEP, 2, 20, 10, payload=payload))


def test_host_validator_checks_typed_sections_in_60_byte_marker_payload():
    payload = protocol.encode_step_payload((
        protocol.StepSection(
            protocol.STEP_SECTION_ADC_VOLTAGE, 0,
            protocol.encode_adc_voltage_v2(
                protocol.AdcVoltageV2(3, 1_800_000))),
        protocol.StepSection(
            protocol.STEP_SECTION_ADC_VOLTAGE, 0,
            protocol.encode_adc_voltage_v2(
                protocol.AdcVoltageV2(4, 2_400_000))),
        protocol.StepSection(
            protocol.STEP_SECTION_ADC_INPUT, 0,
            protocol.encode_adc_input_v2(protocol.AdcInputV2(5, 2048))),
    ))
    assert len(payload) == 60
    assert struct.unpack_from("<H", payload, 2)[0] == \
        protocol.STEP_PAYLOAD_SECTION_MARKER

    tx = protocol.StepSessionValidator.host_to_qemu()
    tx.accept(_directional_frame(protocol.RESET, 0, 0))
    tx.accept(_directional_frame(protocol.STEP, 1, 10, 10,
                                 payload=payload))

    invalid = bytearray(payload)
    # First ADC voltage field: payload header (4), section header (8),
    # channel/flags (4), then voltage_uv.
    struct.pack_into("<I", invalid, 16, 3_300_001)
    tx = protocol.StepSessionValidator.host_to_qemu()
    tx.accept(_directional_frame(protocol.RESET, 0, 0))
    with pytest.raises(ValueError, match="AdcVoltageV2"):
        tx.accept(_directional_frame(protocol.STEP, 1, 10, 10,
                                     payload=bytes(invalid)))


@pytest.mark.parametrize("payload", [
    protocol.encode_step_payload((protocol.StepSection(
        protocol.STEP_SECTION_MOTOR_STATE, 0, b"bad"),)),
    protocol.encode_step_payload((protocol.StepSection(99, 0, b"bad"),)),
    protocol.encode_step_payload((protocol.StepSection(
        protocol.STEP_SECTION_IMU_SAMPLE, 0, b"bad"),)),
])
def test_host_validator_rejects_invalid_typed_step_payloads(payload):
    tx = protocol.StepSessionValidator.host_to_qemu()
    tx.accept(_directional_frame(protocol.RESET, 0, 0))
    with pytest.raises((protocol.StepProtocolError, ValueError)):
        tx.accept(_directional_frame(protocol.STEP, 1, 10, 10, payload=payload))
    assert tx.last_step_id == 0
    assert tx.last_t_sim_ns == 0


def test_host_validator_rejects_duplicate_singleton_and_adc_sections():
    imu = _valid_step_payload()
    duplicate_imu = protocol.encode_step_payload((
        protocol.StepSection(protocol.STEP_SECTION_IMU_SAMPLE, 0, imu),
        protocol.StepSection(protocol.STEP_SECTION_IMU_SAMPLE, 0, imu)))
    adc = protocol.encode_adc_input_v2(protocol.AdcInputV2(2, 4))
    duplicate_adc = protocol.encode_step_payload((
        protocol.StepSection(protocol.STEP_SECTION_ADC_INPUT, 0, adc),
        protocol.StepSection(protocol.STEP_SECTION_ADC_INPUT, 0, adc)))
    for payload in (duplicate_imu, duplicate_adc):
        tx = protocol.StepSessionValidator.host_to_qemu()
        tx.accept(_directional_frame(protocol.RESET, 0, 0))
        with pytest.raises(protocol.StepProtocolError, match="duplicate"):
            tx.accept(_directional_frame(protocol.STEP, 1, 10, 10,
                                         payload=payload))


def test_host_validator_rejects_nan_in_compact_imu():
    values = [0.0] * 12
    values[0] = float("nan")
    payload = struct.pack("<12fQI", *values, 0, 0)
    tx = protocol.StepSessionValidator.host_to_qemu()
    tx.accept(_directional_frame(protocol.RESET, 0, 0))
    with pytest.raises(ValueError, match="finite"):
        tx.accept(_directional_frame(protocol.STEP, 1, 10, 10,
                                     payload=payload))


def test_qemu_to_host_requires_success_ack_for_motor_state():
    rx = protocol.StepSessionValidator.qemu_to_host(require_step_done=True)
    rx.begin_reset(0, 0)
    rx.accept(_directional_frame(protocol.RESET_ACK, 0, 0,
                                 payload=_reset_ack_payload()))
    state = _motor_state_payload()
    with pytest.raises(protocol.StepProtocolError, match="successful"):
        rx.accept(_directional_frame(protocol.MOTOR_STATE, 1, 10, 10, state))
    rx.accept(_directional_frame(protocol.STEP_ACK, 1, 10, 10,
                                 _step_ack_payload()))
    rx.accept(_directional_frame(protocol.MOTOR_STATE, 1, 10, 10, state))


def test_qemu_to_host_ack_replay_and_queue_full_to_ok_transition():
    rx = protocol.StepSessionValidator.qemu_to_host(require_step_done=True)
    rx.begin_reset(0, 0)
    rx.accept(_directional_frame(protocol.RESET_ACK, 0, 0,
                                 payload=_reset_ack_payload()))
    queue_ack = _directional_frame(protocol.STEP_ACK, 1, 10, 10,
                                   _step_ack_payload(protocol.STATUS_QUEUE_FULL))
    ok_ack = _directional_frame(protocol.STEP_ACK, 1, 10, 10,
                                _step_ack_payload(protocol.STATUS_OK))
    rx.accept(queue_ack)
    rx.accept(queue_ack)
    rx.accept(ok_ack)
    rx.accept(ok_ack)
    rx.accept(_directional_frame(protocol.MOTOR_STATE, 1, 10, 10,
                                 _motor_state_payload()))


@pytest.mark.parametrize("status", [protocol.STATUS_PROTOCOL,
                                     protocol.STATUS_UNSUPPORTED])
def test_qemu_to_host_replays_same_non_ok_terminal_ack_idempotently(status):
    rx = protocol.StepSessionValidator.qemu_to_host(require_step_done=True)
    rx.begin_reset(0, 0)
    rx.accept(_directional_frame(protocol.RESET_ACK, 0, 0,
                                 payload=_reset_ack_payload()))
    ack = _directional_frame(protocol.STEP_ACK, 1, 10, 10,
                             _step_ack_payload(status))
    rx.accept(ack)
    rx.accept(ack)


@pytest.mark.parametrize("first, second", [
    (protocol.STATUS_UNSUPPORTED, protocol.STATUS_PROTOCOL),
    (protocol.STATUS_UNSUPPORTED, protocol.STATUS_OK),
    (protocol.STATUS_OK, protocol.STATUS_UNSUPPORTED),
])
def test_qemu_to_host_rejects_illegal_same_step_terminal_status_change(first,
                                                                        second):
    rx = protocol.StepSessionValidator.qemu_to_host(require_step_done=True)
    rx.begin_reset(0, 0)
    rx.accept(_directional_frame(protocol.RESET_ACK, 0, 0,
                                 payload=_reset_ack_payload()))
    rx.accept(_directional_frame(protocol.STEP_ACK, 1, 10, 10,
                                 _step_ack_payload(first)))
    with pytest.raises(protocol.StepProtocolError, match="status changed"):
        rx.accept(_directional_frame(protocol.STEP_ACK, 1, 10, 10,
                                     _step_ack_payload(second)))


def test_response_validator_allows_ack_pipeline_but_keeps_done_ordered():
    rx = protocol.StepSessionValidator(protocol.DIRECTION_QEMU_TO_HOST,
                                       require_step_done=True)
    rx.begin_reset(0, 0)
    rx.accept(_directional_frame(protocol.RESET_ACK, 0, 0,
                                 payload=_reset_ack_payload()))
    rx.accept(_directional_frame(protocol.STEP_ACK, 1, 10, 10,
                                 _step_ack_payload()))
    rx.accept(_directional_frame(protocol.STEP_ACK, 2, 20, 10,
                                 _step_ack_payload()))
    with pytest.raises(protocol.StepProtocolError, match="out of order"):
        rx.accept(_directional_frame(protocol.STEP_DONE, 2, 20, 10,
                                     _step_done_payload()))


def test_session_accepts_step_done():
    validator = protocol.StepSessionValidator()
    validator.accept(protocol.StepFrame(
        protocol.StepHeader(kind=protocol.RESET, payload_len=0,
                            step_id=0, t_sim_ns=0), b""))
    payload = struct.pack("<IIIII", protocol.STATUS_OK,
                          protocol.STEP_DONE_REQUIRED_MASK, 0, 0, 0)
    validator.accept(protocol.StepFrame(
        protocol.StepHeader(kind=protocol.STEP_DONE, payload_len=len(payload),
                            step_id=1, t_sim_ns=10, dt_ns=10), payload))
    assert validator.last_step_id == 1
    assert validator.last_t_sim_ns == 10


def test_empty_motor_payloads_round_trip():
    assert protocol.decode_motor_command(protocol.encode_motor_command(
        protocol.MotorCommand(0))) == protocol.MotorCommand(0)
    assert protocol.decode_motor_state(protocol.encode_motor_state(
        protocol.MotorState(0))) == protocol.MotorState(0)


@pytest.mark.parametrize("encoder, value", [
    (protocol.encode_motor_command, protocol.MotorCommand(
        2, commands=(protocol.MotorCommandItem(0, 1.0),
                     protocol.MotorCommandItem(0, 2.0)))),
    (protocol.encode_motor_state, protocol.MotorState(
        2, states=(protocol.MotorStateItem(0, 0.0, 0.0, 0.0),
                   protocol.MotorStateItem(0, 0.0, 0.0, 0.0)))),
])
def test_motor_payload_rejects_duplicate_indices(encoder, value):
    with pytest.raises(ValueError, match="unique"):
        encoder(value)


def test_motor_payload_rejects_count_overflow_and_trailing_bytes():
    with pytest.raises(ValueError):
        protocol.encode_motor_command(protocol.MotorCommand(0x10000))
    with pytest.raises(ValueError):
        protocol.encode_motor_state(protocol.MotorState(0x10000))
    with pytest.raises(ValueError, match="invalid length"):
        protocol.decode_motor_command(b"\x00\x00\x00\x00\x00")
    with pytest.raises(ValueError, match="invalid length"):
        protocol.decode_motor_state(b"\x00\x00\x00")


@pytest.mark.parametrize("decoder,data", [
    (protocol.decode_step_header, b""),
    (protocol.decode_motor_command, b"\x01\x00\x00\x00"),
    (protocol.decode_motor_state, b"\x01\x00"),
    (protocol.decode_imu_sample_v2, b"\x00" * 59),
])
def test_bad_lengths_are_rejected(decoder, data):
    with pytest.raises(ValueError):
        decoder(data)


def test_bad_version_is_rejected():
    wire = bytearray(protocol.encode_step_header(protocol.StepHeader()))
    struct.pack_into("<H", wire, 0, 1)
    with pytest.raises(ValueError):
        protocol.decode_step_header(bytes(wire))


def test_nan_and_infinite_floats_are_rejected():
    with pytest.raises(ValueError):
        protocol.encode_motor_command(protocol.MotorCommand(
            1, commands=(protocol.MotorCommandItem(0, float("nan")),)))
    with pytest.raises(ValueError):
        protocol.decode_imu_sample_v2(struct.pack("<12fQI", float("inf"),
                                                   *([0.0] * 11), 0, 0))


@pytest.mark.parametrize("value", [float("nan"), float("inf"),
                                    float("-inf"), 1e100])
def test_all_motor_float_fields_reject_non_binary32_values(value):
    command = protocol.MotorCommand(
        1, commands=(protocol.MotorCommandItem(0, value),))
    with pytest.raises(ValueError):
        protocol.encode_motor_command(command)

    state = protocol.MotorState(
        1, states=(protocol.MotorStateItem(0, value, 0.0, 0.0),))
    with pytest.raises(ValueError):
        protocol.encode_motor_state(state)


def test_imu_payload_accepts_binary32_limits_and_rejects_wrong_types():
    sample = protocol.ImuSampleV2((3.4028234e38,) * 3, (0.0,) * 3,
                                  (0.0,) * 3, (0.0,) * 3, 0, 0)
    decoded = protocol.decode_imu_sample_v2(protocol.encode_imu_sample_v2(sample))
    assert decoded.gyro_dps == (3.4028234663852886e38,) * 3
    with pytest.raises(ValueError):
        protocol.encode_imu_sample_v2(protocol.ImuSampleV2(
            ("bad", 0.0, 0.0), (0.0,) * 3, (0.0,) * 3, (0.0,) * 3, 0, 0))


def test_negative_and_overflow_times_are_rejected():
    with pytest.raises(ValueError):
        protocol.encode_step_header(protocol.StepHeader(t_sim_ns=-1))
    with pytest.raises(ValueError):
        protocol.encode_step_header(protocol.StepHeader(dt_ns=1 << 64))
    with pytest.raises(ValueError):
        protocol.encode_imu_sample_v2(protocol.ImuSampleV2(
            (0.0,) * 3, (0.0,) * 3, (0.0,) * 3, (0.0,) * 3, -1, 0))


def test_motor_count_and_index_are_rejected():
    with pytest.raises(ValueError):
        protocol.encode_motor_command(protocol.MotorCommand(
            2, commands=(protocol.MotorCommandItem(0, 1.0),)))
    with pytest.raises(ValueError):
        protocol.encode_motor_state(protocol.MotorState(
            1, (protocol.MotorStateItem(1, 0.0, 0.0, 0.0),)))
    with pytest.raises(ValueError):
        protocol.decode_motor_command(struct.pack("<HHHf", 1, 0, 1, 0.0))


@pytest.mark.parametrize("kind", [protocol.STEP, protocol.STEP_ACK])
def test_session_accepts_valid_step_and_ack_sequence(kind):
    validator = protocol.StepSessionValidator()
    validator.accept(protocol.StepFrame(
        protocol.StepHeader(kind=protocol.RESET, payload_len=0,
                            step_id=0, t_sim_ns=0), b""))
    validator.accept(protocol.StepFrame(
        protocol.StepHeader(kind=kind, payload_len=0,
                            step_id=1, t_sim_ns=10, dt_ns=10), b""))
    assert validator.last_step_id == 1
    assert validator.last_t_sim_ns == 10


def test_session_allows_zero_dt_for_reset_and_diagnostics_only():
    validator = protocol.StepSessionValidator()
    validator.accept(protocol.StepFrame(
        protocol.StepHeader(kind=protocol.RESET, payload_len=0,
                            step_id=1, t_sim_ns=1), b""))
    validator.accept(protocol.StepFrame(
        protocol.StepHeader(kind=protocol.DIAGNOSTICS, payload_len=0,
                            step_id=2, t_sim_ns=2), b""))
