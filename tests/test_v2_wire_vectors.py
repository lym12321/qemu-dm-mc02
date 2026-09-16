"""C/Python shared golden vectors for the v2 wire framing layer."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "tools" / "dm_mc02_step_protocol.py"
SPEC = importlib.util.spec_from_file_location("dm_mc02_step_protocol", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
protocol = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = protocol
SPEC.loader.exec_module(protocol)


def vectors():
    for line in (ROOT / "tests" / "v2_wire_vectors.txt").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        name, encoded = line.split()
        yield name, bytes.fromhex(encoded)


@pytest.mark.parametrize("name, encoded", tuple(vectors()))
def test_python_round_trip_matches_shared_vector(name: str, encoded: bytes):
    decoded = protocol.decode_step_frame(encoded)

    assert protocol.encode_step_frame(decoded) == encoded
    assert decoded.header.version == protocol.STEP_VERSION

    if name == "reset":
        assert (decoded.header.kind, decoded.header.payload_len,
                decoded.header.session_id) == (protocol.RESET, 0, 0)
    elif name == "session_reset":
        assert (decoded.header.kind, decoded.header.step_id,
                decoded.header.t_sim_ns, decoded.header.session_id) == (
                    protocol.RESET, 7, 1000, 0x10203040)
    elif name == "compact_imu":
        assert decoded.header.kind == protocol.STEP
        assert protocol.decode_imu_sample_v2(decoded.payload).sensor_time_ns == \
            0x0102030405060708
    elif name == "section_imu":
        sections = protocol.decode_step_payload(decoded.payload)
        assert len(sections) == 1
        assert (sections[0].type, len(sections[0].payload)) == (
            protocol.STEP_SECTION_IMU_SAMPLE, 60)
    elif name == "mixed":
        sections = protocol.decode_step_payload(decoded.payload)
        assert [(section.type, len(section.payload)) for section in sections] == [
            (protocol.STEP_SECTION_ADC_INPUT, 8),
            (protocol.STEP_SECTION_ADC_VOLTAGE, 12),
        ]
        assert decoded.header.session_id == 0x10203040
        protocol.validate_step_input_payload(decoded.payload)
    else:
        raise AssertionError(f"unknown vector: {name}")
