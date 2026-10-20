"""Literal fixed-profile YAML admission without processes, sockets or solvers."""

from dataclasses import FrozenInstanceError, fields
from pathlib import Path
import socket
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.config.yaml import ManifestError
from sim.robots.manifest import load_manifest


DOCUMENT = """schema: 1
robot: {name: four-wheel}
boards:
  main: {type: qemu, board: dm-mc02, firmware: firmware.elf}
buses:
  chassis_can: {type: can}
links:
  - {from: main.fdcan1, to: chassis_can}
devices:
  rr: {type: dji-m3508, bus: chassis_can, id: 4, joint: wheel_rr}
  fr: {type: dji-m3508, bus: chassis_can, id: 2, joint: wheel_fr}
  fl: {type: dji-m3508, bus: chassis_can, id: 1, joint: wheel_fl}
  rl: {type: dji-m3508, bus: chassis_can, id: 3, joint: wheel_rl}
physics: {backend: mujoco, model: four_wheel.xml}
"""


def write_manifest(directory, text=DOCUMENT):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "firmware.elf").write_bytes(b"admission does not interpret ELF bytes")
    (directory / "four_wheel.xml").write_bytes(b"admission does not compile MuJoCo models")
    path = directory / "robot.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o700)
    return path.resolve()


def command_manifest(tmp_path, command):
    return write_manifest(tmp_path, DOCUMENT + yaml.safe_dump(
        {"upper_computers": {"vision": {"command": command}}}))


def test_literal_profile_resolves_files_preserves_labels_and_sorts_ids(tmp_path):
    path = write_manifest(tmp_path, DOCUMENT + "ready: {endpoint: main.uart10, text: 'READY'}\n")
    result = load_manifest(path)
    assert (result.path, result.name) == (path.resolve(), "four-wheel")
    assert (result.board.name, result.board.type, result.board.board, result.board.firmware) == (
        "main", "qemu", "dm-mc02", (tmp_path / "firmware.elf").resolve())
    assert (result.bus.name, result.bus.type, result.link.source, result.link.target) == (
        "chassis_can", "can", "main.fdcan1", "chassis_can")
    assert [(motor.name, motor.type, motor.bus, motor.motor_id, motor.joint)
            for motor in result.devices] == [
        ("fl", "dji-m3508", "chassis_can", 1, "wheel_fl"),
        ("fr", "dji-m3508", "chassis_can", 2, "wheel_fr"),
        ("rl", "dji-m3508", "chassis_can", 3, "wheel_rl"),
        ("rr", "dji-m3508", "chassis_can", 4, "wheel_rr")]
    assert (result.physics.backend, result.physics.model) == (
        "mujoco", (tmp_path / "four_wheel.xml").resolve())
    assert result.upper_computers == ()
    assert (result.ready.endpoint, result.ready.text) == ("main.uart10", b"READY")


def test_resolved_manifest_directory_is_used_for_symlink_and_absolute_file_paths(tmp_path):
    source = tmp_path / "source"
    path = write_manifest(source)
    alias = tmp_path / "robot.yaml"
    alias.symlink_to(path)
    result = load_manifest(alias)
    assert result.path == path
    assert result.board.firmware == source / "firmware.elf"
    assert result.physics.model == source / "four_wheel.xml"
    path.write_text(DOCUMENT.replace("firmware: firmware.elf", f"firmware: {source / 'firmware.elf'}"))
    assert load_manifest(alias).board.firmware == source / "firmware.elf"


def test_labels_may_change_without_changing_fixed_hardware_or_wheel_identity(tmp_path):
    text = DOCUMENT.replace("main", "controller").replace("chassis_can", "drive_bus")
    text = text.replace("  fl:", "  left_front:")
    result = load_manifest(write_manifest(tmp_path, text + "ready: {endpoint: controller.uart1, text: RUN}\n"))
    assert (result.board.name, result.bus.name, result.devices[0].name) == (
        "controller", "drive_bus", "left_front")
    assert result.ready.endpoint == "controller.uart1"


@pytest.mark.parametrize("old,new", [
    ("schema: 1", "schema: true"),
    ("schema: 1", "schema: 1.0"),
    ("schema: 1", "schema: 2"),
    ("robot: {name: four-wheel}", "robot: {name: '   '}"),
    ("type: qemu", "type: renode"),
    ("board: dm-mc02", "board: another-board"),
    ("buses:\n", "buses:\n  unused: {type: can}\n"),
    ("boards:\n", "boards:\n  other: {type: qemu, board: dm-mc02, firmware: firmware.elf}\n"),
    ("main.fdcan1", "main.can1"),
    ("main.fdcan1", "main.fdcan2"),
    ("main.fdcan1", "missing.fdcan1"),
    ("to: chassis_can", "to: missing"),
    ("bus: chassis_can, id: 1", "bus: missing, id: 1"),
    ("id: 1, joint: wheel_fl", "id: true, joint: wheel_fl"),
    ("id: 1, joint: wheel_fl", "id: 5, joint: wheel_fl"),
    ("id: 2, joint: wheel_fr", "id: 1, joint: wheel_fl"),
    ("id: 1, joint: wheel_fl", "id: 1, joint: wheel_fr"),
    ("  fl: {type: dji-m3508", "  fl: {type: dr16"),
    ("backend: mujoco", "backend: gazebo"),
    ("model: four_wheel.xml", "model: four_wheel.xml, dt: 1ms"),
])
def test_fixed_profile_rejects_incompatible_types_cardinality_and_wiring(tmp_path, old, new):
    with pytest.raises(ManifestError):
        load_manifest(write_manifest(tmp_path, DOCUMENT.replace(old, new)))


@pytest.mark.parametrize("extra", [
    "duration: 1s\n",
    "upper_computers: null\n",
    "upper_computers: {vision: {command: ['./vision'], cwd: .}}\n",
    "ready: {endpoint: other.uart1, text: READY}\n",
    "ready: {endpoint: main.uart4, text: READY}\n",
    "ready: {endpoint: main.uart1, text: ''}\n",
    "ready: {endpoint: main.uart1, text: READY, timeout: 1s}\n",
])
def test_unsupported_or_incomplete_optional_fields_rejected(tmp_path, extra):
    with pytest.raises(ManifestError):
        load_manifest(write_manifest(tmp_path, DOCUMENT + extra))


def test_missing_device_and_extra_link_are_rejected(tmp_path):
    missing = DOCUMENT.replace("  fl: {type: dji-m3508, bus: chassis_can, id: 1, joint: wheel_fl}\n", "")
    extra = DOCUMENT.replace("devices:\n", "  - {from: main.fdcan1, to: chassis_can}\ndevices:\n")
    for text in (missing, extra):
        with pytest.raises(ManifestError):
            load_manifest(write_manifest(tmp_path, text))


def test_nested_duplicate_and_anchor_are_rejected_by_shared_yaml_admission(tmp_path):
    for text in (DOCUMENT.replace("id: 1, joint:", "id: 1, id: 1, joint:"),
                 DOCUMENT.replace("robot: {", "robot: &robot {")):
        with pytest.raises(ManifestError):
            load_manifest(write_manifest(tmp_path, text))


def test_label_and_name_byte_boundaries(tmp_path):
    label = "a" * 64
    text = DOCUMENT.replace("main", label).replace("four-wheel", "é" * 128)
    result = load_manifest(write_manifest(tmp_path, text))
    assert result.board.name == label and len(result.name.encode("utf-8")) == 256
    for invalid in (text.replace(label, label + "a"), text.replace("é" * 128, "é" * 129),
                    DOCUMENT.replace("main", "main.board"), DOCUMENT.replace("main", "máin")):
        with pytest.raises(ManifestError):
            load_manifest(write_manifest(tmp_path, invalid))


@pytest.mark.parametrize("replacement", ["missing.elf", ".", '"firmware\\u0000.elf"'])
def test_file_path_admission_rejects_missing_directory_and_nul(tmp_path, replacement):
    with pytest.raises(ManifestError):
        load_manifest(write_manifest(tmp_path, DOCUMENT.replace("firmware.elf", replacement)))


def test_read_permission_failure_is_preserved_as_manifest_error(tmp_path, monkeypatch):
    path = write_manifest(tmp_path)
    model = tmp_path / "four_wheel.xml"
    original = Path.open

    def open_file(self, *args, **kwargs):
        if self == model:
            raise PermissionError("unreadable model fixture")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(ManifestError, match="physics.model cannot be opened"):
        load_manifest(path)


def test_upper_computer_resolves_executable_and_retains_literal_arguments(tmp_path):
    target = executable(tmp_path / "bin" / "node")
    (tmp_path / "vision").symlink_to(target)
    arguments = ["./vision", "", "two words", "$HOME", "$(touch unexpected)", "`touch unexpected`", "*"]
    result = load_manifest(command_manifest(tmp_path, arguments))
    upper = result.upper_computers[0]
    assert (upper.name, upper.command, upper.cwd) == ("vision", (str(target), *arguments[1:]), tmp_path)
    assert result.ready is None
    assert not (tmp_path / "unexpected").exists()


@pytest.mark.parametrize("path_entry", ["bin", ""])
def test_bare_command_path_search_uses_child_cwd_not_parent(tmp_path, monkeypatch, path_entry):
    source = tmp_path / "robot"
    directory = source / path_entry
    target = executable(directory / "vision")
    outside = tmp_path / "outside"
    executable((outside / path_entry) / "vision")
    path = command_manifest(source, ["vision"])
    monkeypatch.chdir(outside)
    monkeypatch.setenv("PATH", path_entry)
    assert load_manifest(path).upper_computers[0].command == (str(target),)


def test_absolute_path_search_preserves_standard_order(tmp_path, monkeypatch):
    first = executable(tmp_path / "first" / "vision")
    executable(tmp_path / "second" / "vision")
    path = command_manifest(tmp_path, ["vision"])
    monkeypatch.setenv("PATH", str(first.parent) + ":" + str(tmp_path / "second"))
    assert load_manifest(path).upper_computers[0].command == (str(first),)


def test_upper_computer_count_boundary_and_empty_mapping(tmp_path):
    executable(tmp_path / "vision")
    computers = {f"node_{index}": {"command": ["./vision"]} for index in range(8)}
    path = write_manifest(tmp_path, DOCUMENT + yaml.safe_dump({"upper_computers": computers}))
    assert len(load_manifest(path).upper_computers) == 8
    computers["extra"] = {"command": ["./vision"]}
    path.write_text(DOCUMENT + yaml.safe_dump({"upper_computers": computers}))
    with pytest.raises(ManifestError, match="at most 8"):
        load_manifest(path)
    assert load_manifest(write_manifest(tmp_path, DOCUMENT + "upper_computers: {}\n")).upper_computers == ()


@pytest.mark.parametrize("command", [[], "./vision", [""], ["   "], ["./vision", True], ["./vision", "a\0b"]])
def test_command_shape_type_and_nul_rejections(tmp_path, command):
    executable(tmp_path / "vision")
    with pytest.raises(ManifestError):
        load_manifest(command_manifest(tmp_path, command))


def test_executable_must_exist_and_have_execute_permission(tmp_path, monkeypatch):
    path = command_manifest(tmp_path, ["./missing"])
    with pytest.raises(ManifestError):
        load_manifest(path)
    node = executable(tmp_path / "vision")
    node.chmod(0o600)
    with pytest.raises(ManifestError, match="executable regular file"):
        load_manifest(command_manifest(tmp_path, ["./vision"]))
    with pytest.raises(ManifestError, match="executable regular file"):
        load_manifest(command_manifest(tmp_path, ["./"]))
    monkeypatch.setenv("PATH", str(tmp_path / "not-a-directory"))
    with pytest.raises(ManifestError, match="not found in PATH"):
        load_manifest(command_manifest(tmp_path, ["vision"]))


def test_argument_count_and_utf8_byte_boundaries(tmp_path):
    executable(tmp_path / "vision")
    command = ["./vision", *([""] * 127)]
    assert len(load_manifest(command_manifest(tmp_path, command)).upper_computers[0].command) == 128
    with pytest.raises(ManifestError, match="1..128"):
        load_manifest(command_manifest(tmp_path, command + [""]))
    payload = "é" * 2048
    assert load_manifest(command_manifest(tmp_path, ["./vision", payload])).upper_computers[0].command[1] == payload
    with pytest.raises(ManifestError, match="UTF-8 bytes"):
        load_manifest(command_manifest(tmp_path, ["./vision", payload + "é"]))


def test_command_total_counts_resolved_executable_and_all_terminators(tmp_path):
    node = executable(tmp_path / "vision")
    prefix_bytes = len(str(node).encode("utf-8")) + 1 + 3 * (4096 + 1) + 1
    remaining = 16384 - prefix_bytes
    assert 0 < remaining < 4096
    command = ["./vision", *(["x" * 4096] * 3), "y" * remaining]
    result = load_manifest(command_manifest(tmp_path, command))
    assert sum(len(argument.encode("utf-8")) + 1 for argument in result.upper_computers[0].command) == 16384
    command[-1] += "y"
    with pytest.raises(ManifestError, match="16 KiB"):
        load_manifest(command_manifest(tmp_path, command))


def test_all_admitted_objects_are_frozen_and_uart_marker_preserves_bytes(tmp_path):
    executable(tmp_path / "vision")
    path = command_manifest(tmp_path, ["./vision"])
    path.write_text(path.read_text() + 'ready: {endpoint: main.uart1, text: "é\\u0000"}\n')
    result = load_manifest(path)
    assert result.ready.text == b"\xc3\xa9\0"
    for value in (result, result.board, result.bus, result.link, *result.devices,
                  result.physics, *result.upper_computers, result.ready):
        attribute = fields(value)[0].name
        with pytest.raises(FrozenInstanceError):
            setattr(value, attribute, getattr(value, attribute))


def test_admission_never_opens_processes_or_sockets(tmp_path, monkeypatch):
    executable(tmp_path / "vision")
    path = command_manifest(tmp_path, ["./vision"])

    def forbidden(*args, **kwargs):
        pytest.fail("parser acquired a simulation resource")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    assert load_manifest(path).physics.model == tmp_path / "four_wheel.xml"
    path.write_text(path.read_text() + "duration: 1s\n")
    with pytest.raises(ManifestError):
        load_manifest(path)
