"""Pure assertion admission and bounded matching, shared with execution/replay."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
import subprocess
import sys

import pytest

from sim.config.yaml import ManifestError
from sim.fabric import CanFrame
from sim.fabric.frame import WIRE_BRS, WIRE_FD
from sim.testing import (Assertion, AssertionEvaluator, AssertionObservation,
                         CanMatch, parse_assertion)


def endpoint(_kind, value):
    """This fixture's consumer admits arbitrary nonempty endpoint names."""
    if type(value) is not str or not value:
        raise ManifestError("assertions endpoint must be nonempty text")
    return value


def test_parser_and_evaluator_are_resource_free_in_fresh_process():
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
    "sim.lab", "sim.apps", "sim.runtime", "tools",
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
    raise AssertionError("assertion admission/matching acquired IO or read configuration")
builtins.__import__ = pure_import
socket.socket = socket.create_connection = forbidden
subprocess.Popen = os.system = forbidden
Path.read_bytes = Path.read_text = Path.open = forbidden

from sim.testing import (Assertion, CanMatch, parse_assertion, AssertionEvaluator,
                         AssertionObservation)
from sim.testing.assertions import parse_assertion as canonical
assert parse_assertion is canonical
values = [
    {"before": "1ns", "uart_contains": {"endpoint": "sensor.serial", "text": "é\0"}},
    {"before": "2us", "digital_equals": {"endpoint": "switch.level", "value": 1}},
    {"before": "3ms", "can_seen": {"endpoint": "drive.can", "id": 0x7ff,
                                 "flags": 0, "dlc": 2, "data_hex": "0080"}},
    {"before": "4s", "can_seen": {"endpoint": "drive.fd", "id": 0x1abcdef,
                                 "flags": 13, "dlc": 9,
                                 "data_hex": "000102030405060708090a0b"}},
]
assertions = tuple(parse_assertion(value, index, endpoint_validator=lambda kind, name: name)
                   for index, value in enumerate(values))
assert assertions == (
    Assertion(1, "sensor.serial", "uart_contains", b"\xc3\xa9\0"),
    Assertion(2000, "switch.level", "digital_equals", 1),
    Assertion(3000000, "drive.can", "can_seen", CanMatch(0x7ff, 0, 2, b"\0\x80")),
    Assertion(4000000000, "drive.fd", "can_seen",
              CanMatch(0x1abcdef, 13, 9, b"\0\1\2\3\4\5\6\7\10\11\12\13")),
)
evaluator = AssertionEvaluator(assertions, 4000000000, 0)
evaluator.observe_values({"switch.level": 1},
    (AssertionObservation("sensor.serial", 0, b"\xc3\xa9\0"),), 0)
assert evaluator.status == ("pass", "pass", "pending", "pending")
assert not any(name == prefix or name.startswith(prefix + ".")
               for name in sys.modules for prefix in forbidden_modules)
print("pure assertion parser and evaluator passed")
'''
    result = subprocess.run([sys.executable, "-c", program],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "pure assertion parser and evaluator passed\n" and result.stderr == ""


def test_parser_is_canonical_frozen_and_preserves_source():
    from sim.testing.assertions import parse_assertion as canonical

    assert parse_assertion is canonical
    source = {"before": "3600s", "can_seen": {"endpoint": "motor.can", "id": 0x1fffffff}}
    before = deepcopy(source)
    result = parse_assertion(source, 1024, endpoint_validator=endpoint)
    assert source == before
    assert result == Assertion(3_600_000_000_000, "motor.can", "can_seen", CanMatch(0x1fffffff))
    for record, attribute in ((result, "before_ns"), (result.value, "can_id")):
        with pytest.raises(FrozenInstanceError):
            setattr(record, attribute, 0)
    source["can_seen"]["id"] = 1
    assert result.value.can_id == 0x1fffffff
    text = {"before": "1ns", "uart_contains": {"endpoint": "sensor.serial", "text": "é" * 2048}}
    assert parse_assertion(text, 0, endpoint_validator=endpoint).value == b"\xc3\xa9" * 2048
    text["uart_contains"]["text"] += "é"
    with pytest.raises(ManifestError, match=r"assertions\[7\]\.text.*UTF-8 bytes"):
        parse_assertion(text, 7, endpoint_validator=endpoint)


def test_parser_requires_explicit_caller_endpoint_admission_without_board_defaults():
    source = {"before": "1ns", "uart_contains": {"endpoint": "custom.rx", "text": "OK"}}
    with pytest.raises(TypeError, match="endpoint_validator"):
        parse_assertion(source, 0)
    with pytest.raises(TypeError, match="callable"):
        parse_assertion(source, 0, endpoint_validator=None)
    calls = []
    def admit(kind, value):
        calls.append((kind, value))
        return "qualified." + value
    assert parse_assertion(source, 0, endpoint_validator=admit).endpoint == "qualified.custom.rx"
    assert calls == [("uart_contains", "custom.rx")]
    error = ValueError("consumer has no RX observation")
    def reject(*args):
        raise error
    with pytest.raises(ValueError) as caught:
        parse_assertion(source, 0, endpoint_validator=reject)
    assert caught.value is error and source["uart_contains"]["endpoint"] == "custom.rx"


@pytest.mark.parametrize("value", [
    [],
    {"before": "1ms"},
    {"before": "0ns", "digital_equals": {"endpoint": "switch.level", "value": 0}},
    {"before": "1ms", "uart_contains": {"endpoint": "", "text": "ready"}},
    {"before": "1ms", "digital_equals": {"endpoint": "switch.level", "value": True}},
    {"before": "1ms", "can_seen": {"endpoint": "drive.can", "id": 0x800, "flags": 0}},
    {"before": "1ms", "can_seen": {"endpoint": "drive.can", "id": 1, "flags": 0, "dlc": 9}},
], ids=["mapping", "kind", "positive-deadline", "endpoint", "bool-level",
        "standard-id", "classic-dlc"])
def test_parser_preserves_contextual_rejection(value):
    original = deepcopy(value)
    with pytest.raises(ManifestError, match="assertions"):
        parse_assertion(value, 7, endpoint_validator=endpoint)
    assert value == original


def uart(time, data, name="sensor.serial"):
    return AssertionObservation(name, time, data)


def can(time, *, flags=0, dlc=1, payload=b"\xff", name="drive.can"):
    return AssertionObservation(name, time, CanFrame(0x123, flags, dlc, payload, time))


def test_uart_split_matches_only_endpoint_and_retains_terminal_status():
    evaluator = AssertionEvaluator((Assertion(50, "sensor.serial", "uart_contains", b"hello"),), 100, 1000)
    evaluator.observe_values({}, (uart(1001, b"hel"),), 1001)
    assert evaluator.status == ("pending",)
    evaluator.observe_values({}, (uart(1002, b"lo", "other.serial"),), 1002)
    assert evaluator.status == ("pending",)
    evaluator.observe_values({}, (uart(1003, b"lo"),), 1003)
    assert evaluator.status == ("pass",) and evaluator.all_passed and evaluator.failures == ()
    evaluator.advance(1050)
    assert evaluator.status == ("pass",)


@pytest.mark.parametrize("bound,expected", [(1049, "pass"), (1050, "fail")])
def test_uart_strict_before_uses_admitted_observation_bound(bound, expected):
    evaluator = AssertionEvaluator((Assertion(50, "sensor.serial", "uart_contains", b"OK"),), 100, 1000)
    evaluator.observe_values({}, (uart(bound, b"OK"),), bound)
    assert evaluator.status == (expected,)
    if expected == "fail":
        evaluator.observe_values({}, (uart(1051, b"OK"),), 1051)
        assert evaluator.status == ("fail",) and "before 50 ns" in evaluator.failures[0]


def test_uart_suffix_memory_bounded_and_no_cross_endpoint_join():
    evaluator = AssertionEvaluator((Assertion(100, "sensor.serial", "uart_contains", b"abc"),
                                    Assertion(100, "other.serial", "uart_contains", b"xy")), 100, 1000)
    for timestamp in range(1001, 1011):
        evaluator.observe_values({}, (uart(timestamp, b"z" * 4096 + b"ab"),), timestamp)
        assert evaluator.status == ("pending", "pending") and evaluator._tails == [b"ab", b""]
    evaluator.observe_values({}, (uart(1011, b"c", "other.serial"),), 1011)
    assert evaluator.status == ("pending", "pending")
    evaluator.observe_values({}, (uart(1012, b"c"),), 1012)
    assert evaluator.status == ("pass", "pending") and evaluator._tails == [b"", b"c"]


def test_digital_consumes_supplied_levels_at_the_completed_bound_without_mutation():
    assertions = tuple(Assertion(50, name, "digital_equals", 1)
                       for name in ("switch.level", "button.level", "indicator.level"))
    digital = dict.fromkeys((item.endpoint for item in assertions), 1)
    before = deepcopy(digital)
    evaluator = AssertionEvaluator(assertions, 100, 1000)
    evaluator.observe_values(digital, (), 1049)
    assert evaluator.status == ("pass",) * 3 and digital == before
    late = AssertionEvaluator(assertions, 100, 1000)
    late.observe_values(digital, (), 1050)
    assert late.status == ("fail",) * 3


def test_can_wire_time_late_arrival_matches_only_before_first_failure():
    assertions = (Assertion(50, "drive.can", "can_seen", CanMatch(0x123, data=b"\xff")),)
    evaluator = AssertionEvaluator(assertions, 100, 1000)
    evaluator.observe_values({}, (can(1049),), 1055)
    assert evaluator.status == ("pass",)
    failed = AssertionEvaluator(assertions, 100, 1000)
    failed.advance(1050)
    failed.observe_values({}, (can(1049),), 1055)
    assert failed.status == ("fail",)


def test_can_preorigin_exact_deadline_wrong_endpoint_and_optional_fields():
    evaluator = AssertionEvaluator((Assertion(50, "drive.can", "can_seen", CanMatch(0x123)),
        Assertion(50, "drive.can", "can_seen", CanMatch(0x123, 0, 1, b"\xff"))), 100, 1000)
    evaluator.observe_values({}, (can(999), can(1039, name="other.can")), 1040)
    assert evaluator.status == ("pending", "pending")
    evaluator.observe_values({}, (can(1041, flags=WIRE_FD | WIRE_BRS, dlc=9, payload=b"\xff" * 12),), 1041)
    assert evaluator.status == ("pass", "pending")
    evaluator.observe_values({}, (can(1050),), 1050)
    assert evaluator.status == ("pass", "fail")


def test_finish_requires_duration_and_missing_conditions_fail():
    evaluator = AssertionEvaluator((Assertion(20, "sensor.serial", "uart_contains", b"OK"),
        Assertion(100, "drive.can", "can_seen", CanMatch(0x123))), 100, 1000)
    with pytest.raises(ValueError, match="before its duration"):
        evaluator.finish(1099)
    assert evaluator.status == ("pending", "pending")
    assert evaluator.finish(1100) == evaluator.finish(1100) == ("fail", "fail")
    assert len(evaluator.failures) == 2 and not evaluator.all_passed
    with pytest.raises(RuntimeError, match="finished"):
        evaluator.observe_values({}, (can(1001),), 1100)


@pytest.mark.parametrize("invalid", [True, -1, 2**64, float("inf")])
def test_clock_admission_preserves_pending_assertions(invalid):
    assertions = (Assertion(50, "sensor.serial", "uart_contains", b"OK"),)
    with pytest.raises((TypeError, ValueError)):
        AssertionEvaluator(assertions, 100, invalid)
    evaluator = AssertionEvaluator(assertions, 100, 1000)
    with pytest.raises((TypeError, ValueError)):
        evaluator.observe_values({}, (), invalid)
    assert evaluator.status == ("pending",)


def test_reverse_bounds_and_bad_batch_reject_before_matching():
    evaluator = AssertionEvaluator((Assertion(50, "sensor.serial", "uart_contains", b"OK"),), 100, 1000)
    evaluator.advance(1001)
    with pytest.raises(ValueError):
        evaluator.advance(1000)
    with pytest.raises(ValueError, match="later"):
        evaluator.observe_values({}, (uart(1003, b"OK"),), 1002)
    with pytest.raises(TypeError, match="AssertionObservation"):
        evaluator.observe_values({}, (uart(1002, b"OK"), object()), 1002)
    assert evaluator.status == ("pending",)
    evaluator.observe_values({}, (uart(1002, b"OK"),), 1002)
    assert evaluator.status == ("pass",)


def test_origin_plus_duration_cannot_overflow_timestamp():
    with pytest.raises(ValueError):
        AssertionEvaluator((), 100, 2**64 - 100)


def test_same_cut_batches_match_before_one_expiry_without_retaining_transcript():
    evaluator = AssertionEvaluator((Assertion(5, "sensor.serial", "uart_contains", b"OK"),), 10, 1000)
    evaluator.observe_values({}, (uart(1004, b"O"),), 1005, expire=False)
    evaluator.observe_values({}, (uart(1004, b"K"),), 1005, expire=False)
    assert evaluator.status == ("pass",) and evaluator._tails == [b""]
    evaluator.advance(1005)
    assert evaluator.status == ("pass",)
