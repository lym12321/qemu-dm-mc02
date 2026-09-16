"""C/Python golden vectors for the shared fixed v2 payload codec."""

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
    for line in (ROOT / "tests" / "v2_payload_vectors.txt").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        name, encoded = line.split()
        yield name, bytes.fromhex(encoded)


def _encode_payload(name: str, decoded: object) -> bytes:
    if name == "reset_ack":
        return protocol.encode_reset_ack_payload(decoded)
    if name == "step_ack":
        return protocol.encode_step_ack_payload(decoded)
    if name == "diagnostics":
        return protocol.encode_diagnostics_payload(decoded)
    if name == "step_done":
        return protocol.encode_step_done_payload(decoded)
    return protocol.encode_board_telemetry_payload(decoded)


VALID_DECODERS = {
    "reset_ack": protocol.decode_reset_ack_payload,
    "step_ack": protocol.decode_step_ack_payload,
    "diagnostics": protocol.decode_diagnostics_payload,
    "step_done": protocol.decode_step_done_payload,
    "board_telemetry": protocol.decode_board_telemetry_payload,
}

INVALID_DECODERS = {
    "invalid_reset_ack_reserved": protocol.decode_reset_ack_payload,
    "invalid_step_done_mask": protocol.decode_step_done_payload,
}


@pytest.mark.parametrize("name, encoded", tuple(vectors()))
def test_python_decode_reencode_matches_complete_payload(name: str,
                                                          encoded: bytes):
    if name.startswith("invalid_"):
        with pytest.raises(protocol.StepProtocolError):
            INVALID_DECODERS[name](encoded)
        return

    decoded = VALID_DECODERS[name](encoded)
    assert _encode_payload(name, decoded) == encoded
