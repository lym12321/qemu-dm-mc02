"""Pure fixed two-robot admission, using the existing complete Robot records.

The firmware fixture is readable configuration input, not an executable ELF.
Native model composition and resource lifecycle are separate consumer gates.
"""

import builtins
from dataclasses import FrozenInstanceError, fields
import importlib
from pathlib import Path
import socket
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.manifest import ManifestError
from sim.robots.manifest import Physics, RobotManifest, load_manifest as load_robot


ROOT = Path(__file__).resolve().parents[1]
ROBOT_MODEL = ROOT / "models" / "four_wheel.xml"
WORLD_MODEL = ROOT / "models" / "two_four_wheel.xml"
ROBOT_DOCUMENT = """schema: 1
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
physics: {backend: mujoco, model: MODEL}
""".replace("MODEL", str(ROBOT_MODEL))


def write_arena(directory, *, robot_documents=(ROBOT_DOCUMENT, ROBOT_DOCUMENT),
                members=None, name="two-four-wheel", world_model=None):
    """Write only temporary configuration inputs; return the outer manifest.

    Native consumers may reuse this fixture and its canonical model references.
    Default YAML order and arbitrary aliases deliberately differ from prefixes.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for prefix, document in zip(("alpha", "bravo"), robot_documents, strict=True):
        child = directory / "robots" / prefix
        child.mkdir(parents=True, exist_ok=True)
        (child / "firmware.elf").write_bytes(b"readability fixture; not an ELF image")
        (child / "robot.yaml").write_text(document, encoding="utf-8")
    if members is None:
        members = {
            "west": {"manifest": "robots/bravo/robot.yaml", "prefix": "bravo/",
                     "physics": "shared-world"},
            "east": {"manifest": "robots/alpha/robot.yaml", "prefix": "alpha/",
                     "physics": "shared-world"},
        }
    document = {"schema": 1, "arena": {"name": name},
                "world": {"backend": "mujoco", "model": str(world_model or WORLD_MODEL)},
                "robots": members}
    path = directory / "arena.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def load_manifest(path):
    # Keep importing this test fixture usable by native consumers without
    # importing Arena or any runtime during collection.
    from sim.arena.manifest import load_manifest as load
    return load(path)


def write_executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path.resolve()


def test_import_and_success_failure_never_acquire_runtime_resources(tmp_path, monkeypatch):
    computers = "upper_computers: {vision: {command: ['./node']}}\n"
    path = write_arena(tmp_path, robot_documents=(ROBOT_DOCUMENT + computers,) * 2)
    for prefix in ("alpha", "bravo"):
        write_executable(tmp_path / "robots" / prefix / "node")

    def forbidden(*args, **kwargs):
        pytest.fail("Arena admission acquired a process or socket")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    original_import = builtins.__import__
    forbidden_modules = ("mujoco", "numpy", "sim.physics", "sim.fabric", "sim.devices",
                         "sim.boards", "sim.lab.runtime", "sim.robots.runtime",
                         "sim.robots.four_wheel", "sim.robots.shared_world", "tools")

    def pure_import(name, globals=None, locals=None, fromlist=(), level=0):
        resolved = (importlib.util.resolve_name("." * level + name, globals["__package__"])
                    if level else name)
        candidates = (resolved, *(resolved + "." + item for item in (fromlist or ()) if item != "*"))
        for candidate in candidates:
            if any(candidate == prefix or candidate.startswith(prefix + ".")
                   for prefix in forbidden_modules):
                pytest.fail(f"Arena admission imported a runtime dependency: {candidate}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", pure_import)
    package = importlib.import_module("sim.arena")
    importlib.reload(package)
    arena = importlib.import_module("sim.arena.manifest")
    importlib.reload(arena)  # Exercise source imports even if another test loaded it.
    result = arena.load_manifest(path)
    assert sum(len(member.robot.upper_computers) for member in result.robots) == 2
    child = tmp_path / "robots" / "alpha" / "robot.yaml"
    child.write_text(ROBOT_DOCUMENT + "unexpected: true\n", encoding="utf-8")
    with pytest.raises(ManifestError):
        arena.load_manifest(path)


def test_complete_typed_frozen_records_preserve_aliases_and_canonical_order(tmp_path):
    path = write_arena(tmp_path)
    result = load_manifest(path)
    from sim.arena.manifest import ArenaManifest, ArenaRobot

    assert isinstance(result, ArenaManifest)
    assert (result.path, result.name, result.world) == (
        path.resolve(), "two-four-wheel", Physics("mujoco", WORLD_MODEL.resolve()))
    assert type(result.robots) is tuple
    assert [(member.name, member.prefix, member.physics) for member in result.robots] == [
        ("east", "alpha/", "shared-world"), ("west", "bravo/", "shared-world")]
    for member in result.robots:
        child = tmp_path / "robots" / member.prefix[:-1] / "robot.yaml"
        assert isinstance(member, ArenaRobot) and isinstance(member.robot, RobotManifest)
        assert member.robot == load_robot(child)
        assert (member.robot.board.name, member.robot.bus.name, member.robot.physics.model) == (
            "main", "chassis_can", ROBOT_MODEL.resolve())
        assert [(motor.motor_id, motor.joint) for motor in member.robot.devices] == [
            (1, "wheel_fl"), (2, "wheel_fr"), (3, "wheel_rl"), (4, "wheel_rr")]
    scoped_ids = {(member.name, motor.motor_id)
                  for member in result.robots for motor in member.robot.devices}
    assert scoped_ids == {(name, motor_id) for name in ("east", "west") for motor_id in (1, 2, 3, 4)}
    assert [member.prefix + motor.joint for member in result.robots for motor in member.robot.devices] == [
        "alpha/wheel_fl", "alpha/wheel_fr", "alpha/wheel_rl", "alpha/wheel_rr",
        "bravo/wheel_fl", "bravo/wheel_fr", "bravo/wheel_rl", "bravo/wheel_rr"]
    for record in (result, result.world, *result.robots):
        attribute = fields(record)[0].name
        with pytest.raises(FrozenInstanceError):
            setattr(record, attribute, getattr(record, attribute))
    document = yaml.safe_load(path.read_text())
    document["robots"] = dict(reversed(tuple(document["robots"].items())))
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    assert load_manifest(path) == result


def test_absent_and_actual_uart_ready_bytes_stay_in_child_scope(tmp_path):
    documents = (ROBOT_DOCUMENT,
                 ROBOT_DOCUMENT + 'ready: {endpoint: main.uart7, text: "é\\u0000READY"}\n')
    result = load_manifest(write_arena(tmp_path, robot_documents=documents))
    assert result.robots[0].robot.ready is None
    ready = result.robots[1].robot.ready
    assert (ready.endpoint, ready.text) == ("main.uart7", b"\xc3\xa9\0READY")


def test_symlink_sources_resolve_child_cwd_and_keep_literal_arguments(tmp_path, monkeypatch):
    source = tmp_path / "child-source"
    source.mkdir()
    target = write_executable(source / "bin" / "vision")
    (source / "firmware.elf").write_bytes(b"explicit readability fixture")
    (source / "single.xml").symlink_to(ROBOT_MODEL)
    arguments = ["vision", "", "two words", "$HOME", "$(touch unexpected)", "`touch unexpected`", "*"]
    child = source / "robot.yaml"
    child.write_text(ROBOT_DOCUMENT.replace(str(ROBOT_MODEL), "single.xml") +
                     yaml.safe_dump({"upper_computers": {"vision": {"command": arguments}}}),
                     encoding="utf-8")
    directory = tmp_path / "arena-source"
    members = {
        "west": {"manifest": "robots/bravo/robot.yaml", "prefix": "bravo/", "physics": "shared-world"},
        "east": {"manifest": "alpha.yaml", "prefix": "alpha/", "physics": "shared-world"},
    }
    path = write_arena(directory, members=members, world_model="scene.xml")
    (directory / "scene.xml").symlink_to(WORLD_MODEL)
    (directory / "alpha.yaml").symlink_to(child)
    alias = tmp_path / "arena-alias.yaml"
    alias.symlink_to(path)
    outside = tmp_path / "unrelated-cwd"
    write_executable(outside / "bin" / "vision")
    monkeypatch.chdir(outside)
    monkeypatch.setenv("PATH", "bin")
    result = load_manifest(alias)
    robot = result.robots[0].robot
    assert result.path == path.resolve() and result.world.model == WORLD_MODEL.resolve()
    assert (robot.path, robot.board.firmware, robot.physics.model) == (
        child.resolve(), source / "firmware.elf", ROBOT_MODEL.resolve())
    upper = robot.upper_computers[0]
    assert (upper.name, upper.cwd, upper.command) == ("vision", source, (str(target), *arguments[1:]))
    assert robot.ready is None
    assert not (source / "unexpected").exists() and not (outside / "unexpected").exists()


DELETE = object()


@pytest.mark.parametrize("location,replacement", [
    (("schema",), True),
    (("scenario",), {}),
    (("arena", "name"), "场地"),
    (("robots",), {"only": {}}),
    (("robots", "east", "extra"), True),
    (("robots", "east", "prefix"), "bravo/"),
    (("robots", "east", "prefix"), "charlie/"),
    (("robots", "east", "prefix"), ["alpha/"]),
    (("robots", "east", "physics"), DELETE),
    (("robots", "east", "physics"), "robot-world"),
    (("robots", "east", "physics"), {"source": "shared-world"}),
    (("robots", "west", "manifest"), {"schema": 1}),
    (("world", "backend"), "gazebo"),
], ids=["bool-schema", "unknown-root", "non-ascii-name", "one-member",
        "unknown-last-member-field", "duplicate-prefix", "unknown-prefix", "unhashable-prefix",
        "missing-shared-world", "wrong-selector", "unhashable-selector", "inline-reference", "world-backend"])
def test_complete_outer_admission_precedes_any_child_file_open(tmp_path, monkeypatch, location, replacement):
    path = write_arena(tmp_path)
    document = yaml.safe_load(path.read_text())
    item = document
    for key in location[:-1]:
        item = item[key]
    if replacement is DELETE:
        del item[location[-1]]
    else:
        item[location[-1]] = replacement
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    child_paths = {(tmp_path / "robots" / prefix / "robot.yaml").resolve()
                   for prefix in ("alpha", "bravo")}
    original_open = Path.open

    def open_file(self, *args, **kwargs):
        if self.resolve() in child_paths:
            pytest.fail("outer rejection opened a child before complete admission")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_member_alias_uses_ascii_identifier_instead_of_prefix_policy(tmp_path):
    members = {
        "A_" + "x" * 62: {"manifest": "robots/alpha/robot.yaml", "prefix": "alpha/", "physics": "shared-world"},
        "B-two": {"manifest": "robots/bravo/robot.yaml", "prefix": "bravo/", "physics": "shared-world"},
    }
    path = write_arena(tmp_path, members=members, name="Arena_1")
    assert [member.name for member in load_manifest(path).robots] == ["A_" + "x" * 62, "B-two"]
    members["máin"] = members.pop("B-two")
    with pytest.raises(ManifestError, match="ASCII"):
        load_manifest(write_arena(tmp_path, members=members))


@pytest.mark.parametrize("change", ["duplicate", "anchor", "unsafe-tag"])
def test_outer_yaml_uses_shared_safe_unique_loader(tmp_path, change):
    path = write_arena(tmp_path)
    document = path.read_text()
    if change == "duplicate":
        document = "schema: 1\n" + document
    elif change == "anchor":
        document = document.replace("arena:", "arena: &named", 1)
    else:
        document = document.replace("arena:\n  name: two-four-wheel\n",
                                    "arena: !!python/object:builtins.object {}\n", 1)
    path.write_text(document, encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_byte_identical_world_copy_is_outside_fixed_profile(tmp_path):
    copied = tmp_path / "copied-world.xml"
    copied.write_bytes(WORLD_MODEL.read_bytes())
    with pytest.raises(ManifestError):
        load_manifest(write_arena(tmp_path, world_model=copied))


def test_identical_custom_child_models_do_not_implicitly_replace_source(tmp_path):
    copied = tmp_path / "copied-robot.xml"
    copied.write_bytes(ROBOT_MODEL.read_bytes())
    custom = ROBOT_DOCUMENT.replace(str(ROBOT_MODEL), str(copied))
    with pytest.raises(ManifestError):
        load_manifest(write_arena(tmp_path, robot_documents=(custom, custom)))


@pytest.mark.parametrize("counts,same_reference,accepted", [
    ((4, 4), True, True),
    ((4, 5), False, False),
    ((5, 5), True, False),
], ids=["repeated-four-is-eight", "different-four-plus-five-is-nine", "repeated-five-is-ten"])
def test_upper_budget_counts_each_member_even_for_repeated_references(tmp_path, counts, same_reference, accepted):
    documents = tuple(ROBOT_DOCUMENT + yaml.safe_dump({"upper_computers": {
        f"node_{index}": {"command": ["./node"]} for index in range(count)}}) for count in counts)
    path = write_arena(tmp_path, robot_documents=documents)
    for prefix in ("alpha", "bravo"):
        write_executable(tmp_path / "robots" / prefix / "node")
    if same_reference:
        document = yaml.safe_load(path.read_text())
        document["robots"]["west"]["manifest"] = "robots/alpha/robot.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    if accepted:
        result = load_manifest(path)
        assert sum(len(member.robot.upper_computers) for member in result.robots) == 8
        assert result.robots[0].robot == result.robots[1].robot
        assert result.robots[0].name != result.robots[1].name
    else:
        with pytest.raises(ManifestError, match="8"):
            load_manifest(path)


@pytest.mark.parametrize("child_is_arena", [False, True], ids=["malformed-robot", "arena-not-robot"])
def test_child_error_keeps_member_reference_context_and_original_cause(tmp_path, child_is_arena):
    path = write_arena(tmp_path)
    child = tmp_path / "robots" / "alpha" / "robot.yaml"
    child.write_text(path.read_text() if child_is_arena else ROBOT_DOCUMENT + "unexpected: true\n",
                     encoding="utf-8")
    with pytest.raises(ManifestError) as caught:
        load_manifest(path)
    assert "east" in str(caught.value) and str(child.resolve()) in str(caught.value)
    assert isinstance(caught.value.__cause__, ManifestError)
    assert "unknown" in str(caught.value.__cause__)


def test_reference_rejects_nul_before_filesystem_use(tmp_path):
    path = write_arena(tmp_path)
    document = yaml.safe_load(path.read_text())
    document["robots"]["east"]["manifest"] = "robots/alpha/robot\0.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    with pytest.raises(ManifestError, match="NUL"):
        load_manifest(path)
