"""Real YAML consumer tests for Lab observation deadlines and bounded matching."""

from copy import deepcopy
from pathlib import Path
import sys
from textwrap import dedent

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.boards.channels import ChannelEvent
from sim.fabric import CanFrame
from sim.fabric.frame import WIRE_FD, WIRE_BRS
from sim.lab.experiment import LabExperiment
from sim.lab.manifest import LabManifest, parse_assertion
import yaml


def manifest(tmp_path, body):
    document = yaml.safe_load(dedent(body)) or {}
    assertions = tuple(parse_assertion(value, index) for index, value in
                       enumerate(document.get("assertions", ())))
    return LabManifest(tmp_path / "fixture", "observation", "dm-mc02",
                       tmp_path / "fw.elf", 100, (), assertions)


def state(timestamp, *, gpio=0, key=0, rgb=0):
    # Literal native schema; observations do not alter the borrowed object.
    return {
        "schema": 1, "timestamp_ns": timestamp, "board": "DM-MC02",
        "vin_mv": 24000,
        "gpio": {port: {"moder": 0, "sampled": gpio if port == "A" else 0,
                         "odr": 0, "output_mask": 0}
                 for port in "ABCDEFGH"},
        "key": {"pin": "A15", "pressed": False, "level": key},
        "led": {"pin": "A7", "rgb": rgb, "brightness": 0,
                "source": "gpio-latch"},
    }


def uart(time, data, endpoint="uart1"):
    return ChannelEvent(endpoint, time, data)


def can(time, *, flags=0, dlc=1, payload=b"\xff", endpoint="can1"):
    return ChannelEvent(endpoint, time, CanFrame(0x123, flags, dlc, payload, time))


def test_uart_split_matches_only_endpoint_with_post_read_bound(tmp_path):
    config = manifest(tmp_path, """
        assertions:
          - {before: 50ns, uart_contains: {endpoint: uart1, text: "hello"}}
    """)
    experiment = LabExperiment(config, 1000)
    experiment.observe(state(1001), [uart(999, b"hel")], 1001)
    assert experiment.status == ("pending",)
    experiment.observe(state(1002), [uart(1002, b"lo", "uart2")], 1002)
    assert experiment.status == ("pending",)
    # The poll marker can predate the origin; the post-read bound owns UART time.
    experiment.observe(state(1003), [uart(999, b"lo")], 1003)
    assert experiment.status == ("pass",) and experiment.all_passed
    assert experiment.failures == ()
    experiment.observe(state(1050), [], 1050)
    assert experiment.status == ("pass",)


@pytest.mark.parametrize("bound,expected", [(1049, "pass"), (1050, "fail")])
def test_uart_strict_before_uses_upper_bound_not_poll_timestamp(tmp_path, bound, expected):
    config = manifest(tmp_path, """
        assertions:
          - {before: 50ns, uart_contains: {endpoint: uart1, text: OK}}
    """)
    experiment = LabExperiment(config, 1000)
    experiment.observe(state(bound), [uart(1001, b"OK")], bound)
    assert experiment.status == (expected,)
    if expected == "fail":
        experiment.observe(state(1051), [uart(1002, b"OK")], 1051)
        assert experiment.status == ("fail",)
        assert "before 50 ns" in experiment.failures[0]


def test_uart_suffix_memory_bounded_and_no_cross_endpoint_join(tmp_path):
    config = manifest(tmp_path, """
        assertions:
          - {before: 100ns, uart_contains: {endpoint: uart1, text: abc}}
          - {before: 100ns, uart_contains: {endpoint: uart2, text: xy}}
    """)
    experiment = LabExperiment(config, 1000)
    for timestamp in range(1001, 1011):
        experiment.observe(state(timestamp), [uart(timestamp, b"z" * 4096 + b"ab")],
                           timestamp)
        assert experiment.status == ("pending", "pending")
        assert experiment._tails == [b"ab", b""]
    experiment.observe(state(1011), [uart(1011, b"c", "uart2")], 1011)
    assert experiment.status == ("pending", "pending")
    experiment.observe(state(1012), [uart(1012, b"c")], 1012)
    assert experiment.status == ("pass", "pending")
    assert experiment._tails == [b"", b"c"]


def test_digital_uses_only_native_sample_and_snapshot_time(tmp_path):
    config = manifest(tmp_path, """
        assertions:
          - {before: 50ns, digital_equals: {endpoint: gpio.A0, value: 1}}
          - {before: 50ns, digital_equals: {endpoint: key, value: 1}}
          - {before: 50ns, digital_equals: {endpoint: led, value: 1}}
    """)
    experiment = LabExperiment(config, 1000)
    snapshot = state(1049, gpio=1, key=1, rgb=0x12)
    snapshot["gpio"]["A"]["odr"] = 0
    snapshot["key"]["pressed"] = False
    before = deepcopy(snapshot)
    experiment.observe(snapshot, [], 1049)
    assert experiment.status == ("pass", "pass", "pass")
    assert snapshot == before
    late = LabExperiment(config, 1000)
    late.observe(state(1050, gpio=1, key=1, rgb=1), [], 1050)
    assert late.status == ("fail", "fail", "fail")


def test_can_original_time_late_arrival_can_match_before_first_failure(tmp_path):
    config = manifest(tmp_path, """
        assertions:
          - {before: 50ns, can_seen: {endpoint: can1, id: 0x123, data_hex: ff}}
    """)
    experiment = LabExperiment(config, 1000)
    experiment.observe(state(1055), [can(1049)], 1055)
    assert experiment.status == ("pass",)
    failed = LabExperiment(config, 1000)
    failed.observe(state(1050), [], 1050)
    failed.observe(state(1055), [can(1049)], 1055)
    assert failed.status == ("fail",)


def test_can_preorigin_exact_deadline_unknown_endpoint_and_optional_fields(tmp_path):
    config = manifest(tmp_path, """
        assertions:
          - {before: 50ns, can_seen: {endpoint: can1, id: 0x123}}
          - {before: 50ns, can_seen: {endpoint: can1, id: 0x123,
                                     flags: 0, dlc: 1, data_hex: ff}}
    """)
    experiment = LabExperiment(config, 1000)
    experiment.observe(state(1040), [can(999), can(1039, endpoint="can2")], 1040)
    assert experiment.status == ("pending", "pending")
    experiment.observe(state(1041), [can(1041, flags=WIRE_FD | WIRE_BRS,
                                         dlc=9, payload=b"\xff" * 12)], 1041)
    assert experiment.status == ("pass", "pending")
    experiment.observe(state(1050), [can(1050)], 1050)
    assert experiment.status == ("pass", "fail")


def test_finish_requires_duration_and_missing_conditions_fail(tmp_path):
    config = manifest(tmp_path, """
        assertions:
          - {before: 20ns, uart_contains: {endpoint: uart1, text: OK}}
          - {before: 100ns, can_seen: {endpoint: can1, id: 0x123}}
    """)
    experiment = LabExperiment(config, 1000)
    with pytest.raises(ValueError, match="before its duration"):
        experiment.finish(1099)
    assert experiment.status == ("pending", "pending")
    assert experiment.finish(1100) == ("fail", "fail")
    assert experiment.finish(1100) == ("fail", "fail")
    assert len(experiment.failures) == 2 and not experiment.all_passed
    with pytest.raises(RuntimeError, match="finished"):
        experiment.observe(state(1100), [can(1001)], 1100)


@pytest.mark.parametrize("invalid", [True, -1, 2**64, float("inf")])
def test_clock_admission_preserves_pending_inputs_and_assertions(tmp_path, invalid):
    config = manifest(tmp_path, """
        stimulus: [{at: 0ns, endpoint: uart1, bytes: hello}]
        assertions: [{before: 50ns, uart_contains: {endpoint: uart1, text: OK}}]
    """)
    with pytest.raises((TypeError, ValueError)):
        LabExperiment(config, invalid)
    experiment = LabExperiment(config, 1000)
    with pytest.raises((TypeError, ValueError)):
        experiment.observe(state(invalid), [], invalid)
    assert experiment.status == ("pending",)


def test_reverse_bounds_and_bad_batch_are_rejected_before_matching(tmp_path):
    config = manifest(tmp_path, """
        assertions: [{before: 50ns, uart_contains: {endpoint: uart1, text: OK}}]
    """)
    experiment = LabExperiment(config, 1000)
    experiment.observe(state(1001), [], 1001)
    with pytest.raises(ValueError):
        experiment.advance(1000)
    with pytest.raises(ValueError, match="post-read"):
        experiment.observe(state(1001), [uart(1001, b"OK")], 1002)
    with pytest.raises(ValueError, match="later"):
        experiment.observe(state(1002), [uart(1003, b"OK")], 1002)
    with pytest.raises(ValueError, match="disagree"):
        experiment.observe(state(1002), [uart(1002, b"OK"),
                           ChannelEvent("can1", 1001, CanFrame(1, timestamp_ns=1002))],
                           1002)
    assert experiment.status == ("pending",)
    experiment.observe(state(1002), [uart(1002, b"OK")], 1002)
    assert experiment.status == ("pass",)


def test_origin_plus_duration_cannot_overflow_native_timestamp(tmp_path):
    config = manifest(tmp_path, "")
    with pytest.raises(ValueError):
        LabExperiment(config, 2**64 - 100)
