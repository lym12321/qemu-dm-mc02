"""Pure shared assertion admission; finite scene admission is tested separately."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
import subprocess
import sys

import pytest
from sim.lab import Assertion, CanMatch, ManifestError, parse_assertion


def test_public_assertion_parser_import_and_literals_are_pure_in_fresh_process():
    program = r'''
import builtins
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys

assert not any(name == "sim" or name.startswith("sim.") for name in sys.modules)
forbidden_modules = (
    "mujoco", "numpy", "sim.physics", "sim.devices", "sim.boards", "sim.transports",
    "sim.arena.runtime", "sim.robots.runtime", "sim.robots.shared_world",
    "sim.robots.four_wheel", "sim.scenario.runtime", "sim.lab.runtime", "tools",
)
original_import = builtins.__import__
def pure_import(name, globals=None, locals=None, fromlist=(), level=0):
    resolved = (importlib.util.resolve_name("." * level + name, globals["__package__"])
                if level else name)
    candidates = (resolved, *(resolved + "." + item for item in (fromlist or ()) if item != "*"))
    for candidate in candidates:
        assert not any(candidate == prefix or candidate.startswith(prefix + ".")
                       for prefix in forbidden_modules), candidate
    return original_import(name, globals, locals, fromlist, level)
def forbidden(*args, **kwargs):
    raise AssertionError("assertion admission acquired IO or read configuration")
builtins.__import__ = pure_import
socket.socket = socket.create_connection = forbidden
subprocess.Popen = os.system = forbidden
Path.read_bytes = Path.read_text = Path.open = forbidden

from sim.lab import Assertion, CanMatch, parse_assertion
from sim.lab.manifest import parse_assertion as canonical
assert parse_assertion is canonical
values = [
    {"before": "1ns", "uart_contains": {"endpoint": "uart7", "text": "é\0"}},
    {"before": "2us", "digital_equals": {"endpoint": "gpio.H15", "value": 1}},
    {"before": "3ms", "can_seen": {"endpoint": "can1", "id": 0x7ff,
                                 "flags": 0, "dlc": 2, "data_hex": "0080"}},
    {"before": "4s", "can_seen": {"endpoint": "can3", "id": 0x1abcdef,
                                 "flags": 13, "dlc": 9,
                                 "data_hex": "000102030405060708090a0b"}},
]
assert [parse_assertion(value, index) for index, value in enumerate(values)] == [
    Assertion(1, "uart7", "uart_contains", b"\xc3\xa9\0"),
    Assertion(2000, "gpio.H15", "digital_equals", 1),
    Assertion(3000000, "can1", "can_seen", CanMatch(0x7ff, 0, 2, b"\0\x80")),
    Assertion(4000000000, "can3", "can_seen",
              CanMatch(0x1abcdef, 13, 9, b"\0\1\2\3\4\5\6\7\10\11\12\13")),
]
assert not any(name == prefix or name.startswith(prefix + ".")
               for name in sys.modules for prefix in forbidden_modules)
print("pure assertion parser passed")
'''
    result = subprocess.run([sys.executable, "-c", program],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "pure assertion parser passed\n" and result.stderr == ""


def test_public_assertion_parser_is_canonical_frozen_and_preserves_source():
    from sim.lab.manifest import parse_assertion as canonical

    assert parse_assertion is canonical
    source = {"before": "3600s", "can_seen": {"endpoint": "can2", "id": 0x1fffffff}}
    before = deepcopy(source)
    result = parse_assertion(source, 1024)
    assert source == before
    assert result == Assertion(3_600_000_000_000, "can2", "can_seen", CanMatch(0x1fffffff))
    for record, attribute in ((result, "before_ns"), (result.value, "can_id")):
        with pytest.raises(FrozenInstanceError):
            setattr(record, attribute, 0)
    source["can_seen"]["id"] = 1
    assert result.value.can_id == 0x1fffffff
    text = {"before": "1ns", "uart_contains": {"endpoint": "uart1", "text": "é" * 2048}}
    assert parse_assertion(text, 0).value == b"\xc3\xa9" * 2048
    text["uart_contains"]["text"] += "é"
    with pytest.raises(ManifestError, match=r"assertions\[7\]\.text.*UTF-8 bytes"):
        parse_assertion(text, 7)


@pytest.mark.parametrize("value", [
    [],
    {"before": "1ms"},
    {"before": "0ns", "digital_equals": {"endpoint": "key", "value": 0}},
    {"before": "1ms", "uart_contains": {"endpoint": "uart4", "text": "ready"}},
    {"before": "1ms", "digital_equals": {"endpoint": "gpio.H16", "value": 0}},
    {"before": "1ms", "digital_equals": {"endpoint": "key", "value": True}},
    {"before": "1ms", "can_seen": {"endpoint": "can1", "id": 0x800, "flags": 0}},
    {"before": "1ms", "can_seen": {"endpoint": "can1", "id": 1, "flags": 0, "dlc": 9}},
], ids=["mapping", "kind", "positive-deadline", "uart", "gpio", "bool-level",
        "standard-id", "classic-dlc"])
def test_public_assertion_parser_preserves_contextual_rejection(value):
    original = deepcopy(value)
    with pytest.raises(ManifestError, match=r"assertions\[7\]"):
        parse_assertion(value, 7)
    assert value == original
