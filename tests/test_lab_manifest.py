"""Schema 1 literal YAML admission, independent of processes and transports."""

from dataclasses import FrozenInstanceError
from copy import deepcopy
from pathlib import Path
import socket
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.lab import Assertion, CanMatch, ManifestError, load_manifest, parse_assertion
from sim.lab.manifest import MAX_TIME_NS as LAB_MAX_TIME_NS
from sim.manifest import MAX_TIME_NS, parse_time


HEADER = "schema: 1\nname: admission\nboard: dm-mc02\nfirmware: firmware.elf\n"


def write_manifest(tmp_path, body="duration: 10ms\n", *, header=HEADER):
    (tmp_path / "firmware.elf").write_bytes(b"fixture; no ELF contents required by admission")
    path = tmp_path / "lab.yaml"
    path.write_text(header + body, encoding="utf-8")
    return path


def test_architecture_example_infers_duration_and_resolves_relative_firmware(tmp_path):
    directory = tmp_path / "lab"
    directory.mkdir()
    (directory / "build").mkdir()
    elf = directory / "build" / "student.elf"
    elf.write_bytes(b"fixture")
    path = directory / "example.yaml"
    path.write_text("""schema: 1
name: uart-dma-idle
board: dm-mc02
firmware: build/student.elf
stimulus:
  - at: 100ms
    endpoint: uart1
    bytes: hello
assertions:
  - before: 200ms
    uart_contains:
      endpoint: uart2
      text: received 5 bytes
  - before: 250ms
    digital_equals:
      endpoint: led
      value: 1
""")
    manifest = load_manifest(path)
    assert manifest.path == path.resolve()
    assert manifest.firmware == elf.resolve()
    assert manifest.duration_ns == 250_000_000
    assert manifest.ready is None
    assert [(entry.at_ns, entry.endpoint, entry.kind, entry.value)
            for entry in manifest.stimulus] == [(100_000_000, "uart1", "uart", b"hello")]
    assert [(entry.before_ns, entry.endpoint, entry.kind, entry.value)
            for entry in manifest.assertions] == [
        (200_000_000, "uart2", "uart_contains", b"received 5 bytes"),
        (250_000_000, "led", "digital_equals", 1)]
    with pytest.raises(FrozenInstanceError):
        manifest.duration_ns = 1


def test_all_stimulus_kinds_and_optional_can_match_fields(tmp_path):
    path = write_manifest(tmp_path, """duration: 10ms
ready: {endpoint: uart1, text: LAB_READY}
stimulus:
  - {at: 5us, endpoint: uart10, bytes: "é\\u0000"}
  - {at: 2ms, endpoint: key, pressed: false}
  - {at: 0ns, endpoint: gpio.H15, value: 1}
  - {at: 3ms, endpoint: adc1.ch0, raw: 65535}
  - {at: 4ms, endpoint: adc1.ch31, voltage_uv: 3300000}
  - {at: 10ms, endpoint: can3, frame: {id: 0x123, dlc: 2, data_hex: '0080'}}
assertions:
  - {before: 7ms, can_seen: {endpoint: can1, id: 0x123}}
  - {before: 8ms, can_seen: {endpoint: can2, id: 0x1abcdef, flags: 1, dlc: 1, data_hex: 'ff'}}
  - {before: 9ms, digital_equals: {endpoint: key, value: 0}}
  - {before: 10ms, digital_equals: {endpoint: gpio.A0, value: 1}}
""")
    manifest = load_manifest(path)
    assert manifest.ready.endpoint == "uart1" and manifest.ready.text == b"LAB_READY"
    assert [entry.kind for entry in manifest.stimulus] == [
        "uart", "key", "gpio", "adc_raw", "adc_voltage", "can"]
    assert [entry.at_ns for entry in manifest.stimulus] == [5000, 2_000_000, 0,
                                                           3_000_000, 4_000_000, 10_000_000]
    assert [entry.value for entry in manifest.stimulus[:5]] == [b"\xc3\xa9\0", False, 1, 65535, 3300000]
    frame = manifest.stimulus[5].value
    assert (frame.can_id, frame.flags, frame.dlc, frame.data, frame.timestamp_ns) == (
        0x123, 0, 2, b"\0\x80", 0)
    assert manifest.assertions[0].value == CanMatch(0x123)
    assert manifest.assertions[1].value == CanMatch(0x1abcdef, 1, 1, b"\xff")
    with pytest.raises(FrozenInstanceError):
        manifest.stimulus[0].value = b"changed"


def test_can_fd_uses_existing_dlc_and_flag_admission(tmp_path):
    path = write_manifest(tmp_path, """duration: 1ms
stimulus:
  - {at: 0ns, endpoint: can1, frame: {id: 0x123, flags: 12, dlc: 9, data_hex: '000102030405060708090a0b'}}
assertions:
  - {before: 1ms, can_seen: {endpoint: can1, id: 0x1abcdef}}
  - {before: 1ms, can_seen: {endpoint: can1, id: 0x123, dlc: 15}}
""")
    manifest = load_manifest(path)
    frame = manifest.stimulus[0].value
    assert frame.flags == 12 and frame.length == 12 and frame.data == bytes(range(12))
    assert manifest.assertions[0].value.flags is None
    assert manifest.assertions[1].value.dlc == 15


@pytest.mark.parametrize("body", [
    "duration: true\n",                         # bool is not a time
    "duration: 0s\n",                           # zero experiment duration
    "duration: 3601s\n",                        # bounded virtual time
    "duration: 1.5ms\n",                        # no floating-point time
    "duration: 1e3ms\n",                        # no exponent grammar
    "duration: -1ms\n",                         # no negative time
    "duration: 1h\n",                           # unknown unit
    "duration: 999999999999999999999999ns\n",    # integer token bound
    "duration: 1ms\nstimulus: [{at: 2ms, endpoint: key, pressed: true}]\n",
    "duration: 1ms\nassertions: [{before: 0ns, digital_equals: {endpoint: key, value: 0}}]\n",
    "duration: 1ms\nassertions: [{before: 2ms, digital_equals: {endpoint: led, value: 1}}]\n",
    "stimulus: [{at: 0ns, endpoint: uart1, bytes: ''}]\n",  # inferred zero
])
def test_time_rejections(tmp_path, body):
    with pytest.raises(ManifestError):
        load_manifest(write_manifest(tmp_path, body))
    if body.count("\n") == 1 and body.startswith("duration:"):
        with pytest.raises(ManifestError):
            parse_time(yaml.safe_load(body)["duration"], "duration", zero=False)


def test_integer_time_units_and_maximum_boundary(tmp_path):
    assert MAX_TIME_NS == LAB_MAX_TIME_NS == 3_600_000_000_000
    assert [parse_time(value, "time") for value in (
        "1ns", "1us", "1ms", "1s", "3600000000000ns", "3600s")] == [
        1, 1000, 1_000_000, 1_000_000_000, 3_600_000_000_000, 3_600_000_000_000]
    assert parse_time("0ns", "time") == 0
    with pytest.raises(ManifestError, match="time must be positive and at most 3600s"):
        parse_time("0ns", "time", zero=False)
    manifest = load_manifest(write_manifest(tmp_path, """duration: 3600s
stimulus:
  - {at: 1ns, endpoint: key, pressed: true}
  - {at: 1us, endpoint: key, pressed: false}
  - {at: 1ms, endpoint: key, pressed: true}
  - {at: 1s, endpoint: key, pressed: false}
  - {at: 3600000000000ns, endpoint: key, pressed: true}
"""))
    assert manifest.duration_ns == 3_600_000_000_000
    assert [entry.at_ns for entry in manifest.stimulus] == [1, 1000, 1_000_000,
                                                           1_000_000_000, 3_600_000_000_000]


@pytest.mark.parametrize("body", [
    "duration: 1ms\nunknown: 1\n",
    "duration: 1ms\nready: {endpoint: uart4, text: LAB_READY}\n",
    "duration: 1ms\nready: {endpoint: uart1, text: ''}\n",
    "duration: 1ms\nready: {endpoint: uart1, text: LAB_READY, timeout: 1s}\n",
    "duration: 1ms\nstimulus: [{at: 0ns, endpoint: uart1, bytes: true}]\n",
    "duration: 1ms\nstimulus: [{at: 0ns, endpoint: uart4, bytes: text}]\n",
    "duration: 1ms\nstimulus: [{at: 0ns, endpoint: key, pressed: 1}]\n",
    "duration: 1ms\nstimulus: [{at: 0ns, endpoint: gpio.A0, value: true}]\n",
    "duration: 1ms\nstimulus: [{at: 0ns, endpoint: gpio.A16, value: 0}]\n",
    "duration: 1ms\nstimulus: [{at: 0ns, endpoint: gpio.I0, value: 0}]\n",
    "duration: 1ms\nstimulus: [{at: 0ns, endpoint: adc1.ch32, raw: 0}]\n",
    "duration: 1ms\nstimulus: [{at: 0ns, endpoint: adc2.ch0, raw: 0}]\n",
    "duration: 1ms\nstimulus: [{at: 0ns, endpoint: adc1.ch0, raw: 65536}]\n",
    "duration: 1ms\nstimulus: [{at: 0ns, endpoint: adc1.ch0, voltage_uv: 3300001}]\n",
    "duration: 1ms\nstimulus: [{at: 0ns, endpoint: adc1.ch0, raw: 0, voltage_uv: 0}]\n",
    "duration: 1ms\nstimulus: [{at: 0ns, endpoint: adc1.ch0}]\n",
    "duration: 1ms\nstimulus: [{at: 0ns, endpoint: uart1, bytes: text, pressed: true}]\n",
    "duration: 1ms\nassertions: [{before: 1ms, uart_contains: {endpoint: can1, text: text}}]\n",
    "duration: 1ms\nassertions: [{before: 1ms, digital_equals: {endpoint: gpio.A0, value: true}}]\n",
    "duration: 1ms\nassertions: [{before: 1ms, digital_equals: {endpoint: uart1, value: 0}}]\n",
    "duration: 1ms\nassertions: [{before: 1ms}]\n",
    "duration: 1ms\nassertions: [{before: 1ms, digital_equals: {endpoint: led, value: 0}, uart_contains: {endpoint: uart1, text: hi}}]\n",
    "duration: 1ms\nassertions: [{before: 1ms, can_seen: {endpoint: can4, id: 1}}]\n",
])
def test_unknown_fields_types_endpoints_and_ranges_rejected(tmp_path, body):
    with pytest.raises(ManifestError):
        load_manifest(write_manifest(tmp_path, body))


@pytest.mark.parametrize("frame", [
    "{id: true, dlc: 0, data_hex: ''}",
    "{id: 1, flags: true, dlc: 0, data_hex: ''}",
    "{id: 1, dlc: true, data_hex: ''}",
    "{id: 0x800, dlc: 0, data_hex: ''}",
    "{id: 0x20000000, flags: 1, dlc: 0, data_hex: ''}",
    "{id: 1, flags: 16, dlc: 0, data_hex: ''}",
    "{id: 1, flags: 8, dlc: 0, data_hex: ''}",
    "{id: 1, flags: 6, dlc: 0, data_hex: ''}",
    "{id: 1, dlc: 9, data_hex: ''}",
    "{id: 1, dlc: 0, data_hex: '00'}",
    "{id: 1, dlc: 1, data_hex: 'f'}",
    "{id: 1, dlc: 1, data_hex: 'zz'}",
    "{id: 1, dlc: 1, data_hex: '00', timestamp_ns: 7}",
    "{id: 1, data_hex: ''}",
])
def test_can_configuration_rejections(tmp_path, frame):
    body = f"duration: 1ms\nstimulus: [{{at: 0ns, endpoint: can1, frame: {frame}}}]\n"
    with pytest.raises(ManifestError):
        load_manifest(write_manifest(tmp_path, body))


@pytest.mark.parametrize("condition", [
    "{endpoint: can1, id: 0x800, flags: 0}",
    "{endpoint: can1, id: 1, flags: 0, dlc: 15}",
    "{endpoint: can1, id: 1, dlc: 0, data_hex: '00'}",
    "{endpoint: can1, id: 1, flags: true}",
])
def test_optional_can_fields_are_validated_when_present(tmp_path, condition):
    body = f"duration: 1ms\nassertions: [{{before: 1ms, can_seen: {condition}}}]\n"
    with pytest.raises(ManifestError):
        load_manifest(write_manifest(tmp_path, body))


@pytest.mark.parametrize("header", [
    HEADER.replace("schema: 1", "schema: true"),
    HEADER.replace("schema: 1", "schema: 2"),
    HEADER.replace("name: admission", "name: ''"),
    HEADER.replace("board: dm-mc02", "board: other-board"),
    HEADER.replace("firmware: firmware.elf", "firmware: missing.elf"),
    HEADER.replace("firmware: firmware.elf", "firmware: ."),
    HEADER.replace("firmware: firmware.elf", "firmware: 1"),
])
def test_schema_identity_and_firmware_path_rejections(tmp_path, header):
    with pytest.raises(ManifestError):
        load_manifest(write_manifest(tmp_path, header=header))


@pytest.mark.parametrize("body", [
    "duration: 1ms\nduration: 2ms\n",
    "duration: 1ms\nready: {endpoint: uart1, endpoint: uart2, text: ready}\n",
    "duration: 1ms\nstimulus: [{at: 0ns, endpoint: can1, frame: {id: 1, dlc: 0, dlc: 1, data_hex: ''}}]\n",
    "duration: 1ms\nready: &ready {endpoint: uart1, text: ready}\n",
    "duration: 1ms\nunknown: &recursive [*recursive]\n",
    "duration: 1ms\nunknown: !!python/object/apply:os.system [echo unsafe]\n",
    "duration: 1ms\nunknown: !!bool invalid\n",
    "duration: 1ms\nunknown: " + "[" * 17 + "0" + "]" * 17 + "\n",
    "duration: 1ms\n---\nschema: 1\n",
    "duration: 1ms\ntrue: value\n",
])
def test_yaml_structural_rejections(tmp_path, body):
    with pytest.raises(ManifestError):
        load_manifest(write_manifest(tmp_path, body))


def test_utf8_limit_counts_bytes_not_codepoints(tmp_path):
    text = "é" * 2048
    body = f"duration: 1ms\nstimulus: [{{at: 0ns, endpoint: uart1, bytes: '{text}'}}]\n"
    manifest = load_manifest(write_manifest(tmp_path, body))
    assert len(manifest.stimulus[0].value) == 4096
    with pytest.raises(ManifestError, match="UTF-8 bytes"):
        load_manifest(write_manifest(tmp_path, body.replace(text, text + "é")))


def test_lists_have_independent_capacity_limits(tmp_path):
    stimuli = "  - {at: 0ns, endpoint: key, pressed: true}\n" * 1024
    assertions = "  - {before: 1ms, digital_equals: {endpoint: key, value: 0}}\n" * 1024
    body = "duration: 1ms\nstimulus:\n" + stimuli + "assertions:\n" + assertions
    manifest = load_manifest(write_manifest(tmp_path, body))
    assert len(manifest.stimulus) == len(manifest.assertions) == 1024
    with pytest.raises(ManifestError, match="at most 1024"):
        load_manifest(write_manifest(tmp_path, body + assertions.splitlines(keepends=True)[0]))
    with pytest.raises(ManifestError, match="at most 1024"):
        load_manifest(write_manifest(tmp_path, "duration: 1ms\nstimulus:\n" + stimuli + stimuli.splitlines(keepends=True)[0]))


def test_file_bound_and_no_process_or_transport_side_effects(tmp_path, monkeypatch):
    path = write_manifest(tmp_path)
    original = path.read_bytes()
    path.write_bytes(original + b"#" + b"x" * ((1 << 20) - len(original) - 1))

    def forbidden(*args, **kwargs):
        raise AssertionError("admission must not acquire runtime resources")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert load_manifest(path).duration_ns == 10_000_000
    path.write_bytes(path.read_bytes() + b"x")
    monkeypatch.setattr(yaml, "load", forbidden)
    with pytest.raises(ManifestError, match="1 MiB"):
        load_manifest(path)


def test_manifest_must_be_a_readable_regular_utf8_file(tmp_path):
    with pytest.raises(ManifestError):
        load_manifest(tmp_path)
    with pytest.raises(ManifestError):
        load_manifest(tmp_path / "missing.yaml")
    with pytest.raises(ManifestError):
        load_manifest(True)
    path = write_manifest(tmp_path)
    path.write_bytes(b"\xff")
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_node_budget_rejects_large_unused_structures(tmp_path):
    body = "duration: 1ms\nunknown: [" + ",".join("0" for _ in range(40001)) + "]\n"
    with pytest.raises(ManifestError, match="node limit"):
        load_manifest(write_manifest(tmp_path, body))


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


def test_lab_loader_delegates_original_assertions_and_owns_quota_and_duration(tmp_path, monkeypatch):
    from sim.lab import manifest as lab

    path = write_manifest(tmp_path, """duration: 2ms
assertions:
  - {before: 2ms, digital_equals: {endpoint: gpio.H15, value: 1}}
  - {before: 1ms, uart_contains: {endpoint: uart7, text: ready}}
""")
    read = lab.load_document
    sources, calls = [], []

    def load_document(path):
        resolved, source = read(path)
        sources.append(source)
        return resolved, source

    def parse(value, index):
        assert value is sources[-1]["assertions"][index]
        result = parse_assertion(value, index)
        calls.append((index, result))
        return result

    monkeypatch.setattr(lab, "load_document", load_document)
    monkeypatch.setattr(lab, "parse_assertion", parse)
    manifest = load_manifest(path)
    assert [index for index, _ in calls] == [0, 1]
    assert all(result is manifest.assertions[index] for index, result in calls)
    assert [entry.before_ns for entry in manifest.assertions] == [2_000_000, 1_000_000]
    path.write_text(path.read_text().replace("duration: 2ms", "duration: 1ms"))
    calls.clear()
    with pytest.raises(ManifestError, match="duration must be positive and include"):
        load_manifest(path)
    assert [index for index, _ in calls] == [0, 1]
    body = "duration: 1ms\nassertions:\n" + (
        "  - {before: 1ms, digital_equals: {endpoint: key, value: 0}}\n" * 1025)
    calls.clear()
    with pytest.raises(ManifestError, match="assertions must be a list with at most 1024"):
        load_manifest(write_manifest(tmp_path, body))
    assert calls == []
