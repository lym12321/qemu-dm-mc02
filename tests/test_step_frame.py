"""Tests for the v2 step-frame codec and session validator."""

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


def frame(kind=protocol.STEP, step_id=1, t_sim_ns=100, dt_ns=10,
          payload=b"payload"):
    return protocol.StepFrame(
        protocol.StepHeader(protocol.STEP_VERSION, kind, len(payload),
                             step_id, t_sim_ns, dt_ns, 7),
        payload,
    )


def test_encode_decode_and_build_step_frame_round_trip():
    original = frame(protocol.STEP_ACK, 42, 1_000, 25, b"abc")

    encoded = protocol.encode_step_frame(original)
    decoded = protocol.decode_step_frame(encoded)
    built = protocol.build_step_frame(protocol.STEP_ACK, 42, 1_000, 25,
                                      flags=7, payload=b"abc")

    assert decoded == original
    assert built == encoded
    assert struct.unpack_from("<I", encoded)[0] == len(encoded) - 4
    assert struct.unpack_from("<I", encoded, 4)[0] == protocol.STEP_MAGIC
    assert decoded.header.version == protocol.STEP_VERSION
    assert decoded.header.kind == protocol.STEP_ACK
    assert decoded.header.payload_len == 3
    assert decoded.payload == b"abc"


def test_session_id_reuses_flags_slot_without_changing_v2_header():
    session_id = 0xA1B2C3D4
    encoded = protocol.build_step_frame(
        protocol.RESET, 0, 0, session_id=session_id)
    decoded = protocol.decode_step_frame(encoded)

    assert protocol.STEP_HEADER_SIZE == 36
    assert decoded.header.flags == session_id
    assert decoded.header.session_id == session_id
    assert protocol.build_step_frame(
        protocol.RESET, 0, 0, flags=session_id) == encoded


def test_build_rejects_conflicting_legacy_flags_and_session_id():
    with pytest.raises(protocol.StepProtocolError, match="do not match"):
        protocol.build_step_frame(protocol.RESET, 0, 0, flags=1,
                                   session_id=2)


def test_encode_rejects_payload_length_mismatch():
    with pytest.raises(protocol.StepProtocolError, match="payload_len"):
        protocol.encode_step_frame(
            protocol.StepFrame(protocol.StepHeader(kind=protocol.STEP,
                                                   payload_len=2), b"one"))


@pytest.mark.parametrize("mutate,match", [
    (lambda data: struct.pack("<I", 0) + data[4:], "length"),
    (lambda data: data[:4] + b"BAD!" + data[8:], "magic"),
    (lambda data: data[:8] + struct.pack("<H", 1) + data[10:], "version"),
    (lambda data: data[:10] + struct.pack("<H", 99) + data[12:], "kind"),
    (lambda data: data[:12] + struct.pack("<I", 99) + data[16:], "payload length"),
])
def test_decode_rejects_malformed_length_magic_version_kind_and_payload(
        mutate, match):
    data = mutate(protocol.encode_step_frame(frame()))
    with pytest.raises((ValueError, protocol.StepProtocolError), match=match):
        protocol.decode_step_frame(data)


def test_decode_rejects_trailing_bytes():
    data = protocol.encode_step_frame(frame()) + b"extra"
    with pytest.raises(protocol.StepProtocolError, match="trailing"):
        protocol.decode_step_frame(data)


def test_session_requires_reset_first():
    validator = protocol.StepSessionValidator()
    with pytest.raises(protocol.StepProtocolError, match="RESET first"):
        validator.accept(frame(protocol.STEP))


def test_session_requires_nonzero_dt_for_step_and_ack():
    for kind in (protocol.STEP, protocol.STEP_ACK):
        validator = protocol.StepSessionValidator()
        validator.accept(frame(protocol.RESET, 1, 100, 0))
        with pytest.raises(protocol.StepProtocolError, match="non-zero"):
            validator.accept(frame(kind, 2, 200, 0))


@pytest.mark.parametrize("field", ["step_id", "t_sim_ns"])
def test_session_requires_strictly_increasing_step_and_sim_time(field):
    validator = protocol.StepSessionValidator()
    validator.accept(frame(protocol.RESET, 1, 100, 0))

    values = {"step_id": 2, "t_sim_ns": 200, "dt_ns": 10}
    values[field] = getattr(validator, "last_" + field)
    with pytest.raises(protocol.StepProtocolError, match=field):
        validator.accept(frame(protocol.STEP, **values))


def test_reset_allows_a_new_monotonic_sequence():
    validator = protocol.StepSessionValidator()
    validator.accept(frame(protocol.RESET, 10, 1_000, 0))
    validator.accept(frame(protocol.STEP, 11, 1_010, 10))

    validator.accept(frame(protocol.RESET, 1, 100, 0))
    validator.accept(frame(protocol.STEP_ACK, 2, 110, 10))

    assert validator.last_step_id == 2
    assert validator.last_t_sim_ns == 110


def test_reset_ack_may_echo_the_reset_baseline():
    validator = protocol.StepSessionValidator()
    validator.accept(frame(protocol.RESET, 0, 0, 0, payload=b""))
    validator.accept(frame(protocol.RESET_ACK, 0, 0, 0, payload=b""))
    assert validator.last_step_id == 0
    assert validator.last_t_sim_ns == 0
