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
from sim.config.yaml import ManifestError
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
                members=None, name="two-four-wheel", world_model=None, referees=None):
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
    if referees is not None:
        document["referees"] = referees
    path = directory / "arena.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def load_manifest(path):
    # Keep importing this test fixture usable by native consumers without
    # importing Arena or any runtime during collection.
    from sim.arena.manifest import load_manifest as load
    return load(path)


def forbid_child_reads(directory, monkeypatch, *, include_arena=False):
    child_paths = {(directory / "robots" / prefix / "robot.yaml").resolve()
                   for prefix in ("alpha", "bravo")}
    if include_arena:
        child_paths.add((directory / "arena.yaml").resolve())
    original_open = Path.open

    def open_file(self, *args, **kwargs):
        if self.resolve() in child_paths:
            pytest.fail("outer rejection read referenced configuration before complete admission")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)


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
    importlib.import_module("sim.arena")
    # Execute source imports without replacing the canonical record classes
    # already borrowed by runtime consumers collected in the same process.
    spec = importlib.util.spec_from_file_location("arena_manifest_purity", ROOT / "sim/arena/manifest.py")
    arena = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, arena)
    spec.loader.exec_module(arena)
    result = arena.load_manifest(path)
    assert sum(len(member.robot.upper_computers) for member in result.robots) == 2
    assert result.referees == ()
    document = yaml.safe_load(path.read_text())
    document["referees"] = {}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    assert arena.load_manifest(path).referees == ()
    child = tmp_path / "robots" / "alpha" / "robot.yaml"
    child.write_text(ROBOT_DOCUMENT + "unexpected: true\n", encoding="utf-8")
    with pytest.raises(ManifestError):
        arena.load_manifest(path)


def test_complete_typed_frozen_records_preserve_aliases_and_canonical_order(tmp_path):
    path = write_arena(tmp_path)
    result = load_manifest(path)
    from sim.arena.manifest import ArenaManifest, ArenaRobot

    assert isinstance(result, ArenaManifest)
    assert result.referees == ()
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
    forbid_child_reads(tmp_path, monkeypatch)
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


def referee_configuration(*, uart="main.uart7", scripted=False):
    """Explicit protocol values; no engine, owner or rule defaults in this fixture."""
    initial = {
        "game": {"game_type": 1, "game_progress": 2, "stage_remain_time": 1,
                 "sync_timestamp": 0x0102030405060708},
        "robot": {"robot_id": 3, "robot_level": 2, "current_hp": 100,
                  "maximum_hp": 200, "cooling_per_s": 3, "heat_limit": 100,
                  "chassis_power_limit": 80, "power_outputs": 5},
        "power_heat": {"buffer_energy_j": 40, "heat_17mm": 10, "heat_42mm": 20},
        "buff": {"recovery_percent": 5, "cooling_per_s": 9, "defence_percent": 7,
                 "vulnerability_percent": 11, "attack_percent": 13,
                 "remaining_energy": 254},
    }
    result = {"uart": uart, "initial": initial,
              "durations": {"countdown_s": 3, "battle_s": 4}}
    if scripted:
        result["cooling_42mm_per_s"] = 2
        result["events"] = [
            {"offset_ns": 0, "hit": {"damage_hp": 20}},
            {"offset_ns": 0, "robot": initial["robot"].copy()},
            {"offset_ns": 0, "power_heat": initial["power_heat"].copy()},
            {"offset_ns": 500_000_000, "shot": {"heat_17mm": 5, "heat_42mm": 7}},
            {"offset_ns": 500_000_000, "hit": {"damage_hp": 30}},
            {"offset_ns": 1_000_000_000,
             "game": {**initial["game"], "game_progress": 3, "stage_remain_time": 3}},
            {"offset_ns": 1_000_000_000,
             "robot": {**initial["robot"], "current_hp": 70,
                       "chassis_power_limit": 120, "power_outputs": 6}},
            {"offset_ns": 1_000_000_000,
             "buff": {"recovery_percent": 8, "cooling_per_s": 17, "defence_percent": 19,
                      "vulnerability_percent": 23, "attack_percent": 29,
                      "remaining_energy": 128}},
        ]
    return result


def test_public_referee_event_parser_is_pure_typed_frozen_and_preserves_input_order(monkeypatch):
    from sim.arena.manifest import parse_referee_events
    from sim.core import Coordinator
    from sim.devices.referee import (
        Buff, GameStatus, Hit, PowerHeat, RefereeEngine, RefereeUartEndpoint,
        RobotStatus, ScriptEvent, Shot, validate_referee_events,
    )

    def forbidden(*args, **kwargs):
        pytest.fail("pure event admission acquired or executed a runtime owner")
    for owner in (RefereeEngine, RefereeUartEndpoint, Coordinator):
        monkeypatch.setattr(owner, "__init__", forbidden)
    for method in ("reset", "step", "queue_events", "get_state", "close"):
        monkeypatch.setattr(RefereeEngine, method, forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    robot = RobotStatus(3, 2, 100, 200, 3, 100, 80, 5)
    expected = (
        ScriptEvent(0, Hit(20)), ScriptEvent(0, robot), ScriptEvent(0, PowerHeat(40, 10, 20)),
        ScriptEvent(500_000_000, Shot(5, 7)), ScriptEvent(500_000_000, Hit(30)),
        ScriptEvent(1_000_000_000, GameStatus(1, 3, 3, 0x0102030405060708)),
        ScriptEvent(1_000_000_000, RobotStatus(3, 2, 70, 200, 3, 100, 120, 6)),
        ScriptEvent(1_000_000_000, Buff(8, 17, 19, 23, 29, 128)),
    )
    calls = []
    def validate(events):
        calls.append(events)
        return validate_referee_events(events)
    from sim.devices import referee as referee_module
    monkeypatch.setattr(referee_module, "validate_referee_events", validate)
    raw = referee_configuration(scripted=True)["events"]
    result = parse_referee_events(raw)
    assert type(result) is tuple and result == expected
    assert calls == [result] and calls[0] is result
    assert validate_referee_events(expected) is None
    for event in result:
        with pytest.raises(FrozenInstanceError):
            event.offset_ns = 99
        attribute = fields(event.value)[0].name
        with pytest.raises(FrozenInstanceError):
            setattr(event.value, attribute, 99)
    raw[0]["hit"]["damage_hp"] = 99
    raw.reverse()
    assert result == expected
    assert parse_referee_events([]) == ()


def test_referee_records_are_scoped_frozen_and_canonical_without_resource_acquisition(
        tmp_path, monkeypatch):
    from sim.arena.manifest import ArenaReferee
    from sim.core import Coordinator
    from sim.devices.referee import RefereeEngine, RefereeUartEndpoint

    def forbidden(*args, **kwargs):
        pytest.fail("Arena referee admission constructed or executed a runtime owner")

    for owner in (RefereeEngine, RefereeUartEndpoint, Coordinator):
        monkeypatch.setattr(owner, "__init__", forbidden)
    for method in ("reset", "step", "get_state", "close"):
        monkeypatch.setattr(RefereeEngine, method, forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    original_import = builtins.__import__

    def pure_import(name, globals=None, locals=None, fromlist=(), level=0):
        resolved = (importlib.util.resolve_name("." * level + name, globals["__package__"])
                    if level else name)
        candidates = (resolved, *(resolved + "." + item for item in (fromlist or ()) if item != "*"))
        for candidate in candidates:
            if any(candidate == prefix or candidate.startswith(prefix + ".") for prefix in (
                    "mujoco", "numpy", "sim.physics", "sim.arena.runtime", "sim.robots.runtime",
                    "sim.robots.shared_world", "sim.lab.runtime", "tools")):
                pytest.fail(f"Arena referee admission imported a runtime backend: {candidate}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", pure_import)
    from sim.arena import manifest as arena_manifest
    translate = arena_manifest.parse_referee_events
    translated = []
    def parse_events(value, label="referee events"):
        result = translate(value, label)
        translated.append((label, result))
        return result
    monkeypatch.setattr(arena_manifest, "parse_referee_events", parse_events)
    path = write_arena(tmp_path, referees={"west": referee_configuration(),
                                            "east": referee_configuration(scripted=True)})
    result = load_manifest(path)
    assert type(result.referees) is tuple
    east, west = result.referees
    assert len(translated) == 2
    assert dict(translated) == {"referees.east.events": east.events, "referees.west.events": west.events}
    assert all(isinstance(item, ArenaReferee) for item in result.referees)
    assert [(item.member, item.uart) for item in result.referees] == [
        ("east", "main.uart7"), ("west", "main.uart7")]
    assert (west.events, west.cooling_42mm_per_s) == ((), 0)
    assert east.initial == west.initial and east.cooling_42mm_per_s == 2
    assert [type(event.value).__name__ for event in east.events] == [
        "Hit", "RobotStatus", "PowerHeat", "Shot", "Hit", "GameStatus", "RobotStatus", "Buff"]
    assert [event.offset_ns for event in east.events] == [0, 0, 0, 500_000_000, 500_000_000,
                                                          1_000_000_000, 1_000_000_000, 1_000_000_000]
    for record in (east, east.initial, east.initial.game, east.initial.robot,
                   east.initial.power_heat, east.initial.buff, east.durations,
                   *east.events, *(event.value for event in east.events)):
        attribute = fields(record)[0].name
        with pytest.raises(FrozenInstanceError):
            setattr(record, attribute, getattr(record, attribute))
    document = yaml.safe_load(path.read_text())
    document["referees"] = dict(reversed(tuple(document["referees"].items())))
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    assert load_manifest(path) == result


EVENT_REJECTIONS = (
    ({}, "non-list-events"),
    ([{"offset_ns": 0}], "no-event-value"),
    ([{"offset_ns": 0, "hit": {"damage_hp": 1},
                         "shot": {"heat_17mm": 1, "heat_42mm": 0}}], "two-event-values"),
    ([{"offset_ns": 0, "damage": {"damage_hp": 1}}], "unknown-event"),
    ([{"offset_ns": True, "hit": {"damage_hp": 0}}], "bool-offset"),
    ([{"offset_ns": 1 << 64, "hit": {"damage_hp": 0}}], "offset-overflow"),
    ([{"offset_ns": 1, "hit": {"damage_hp": 0}},
      {"offset_ns": 0, "hit": {"damage_hp": 0}}], "unsorted-offsets"),
    ([{"offset_ns": 0, "hit": {"damage_hp": True}}], "bool-hit"),
    ([{"offset_ns": 0, "shot": {"heat_17mm": 0}}], "partial-shot"),
    ([{"offset_ns": 0, "shot": {"heat_17mm": 0, "heat_42mm": 65536}}], "shot-overflow"),
)


@pytest.mark.parametrize("value", [pytest.param(value, id=ident) for value, ident in EVENT_REJECTIONS
                                  if ident in ("non-list-events", "two-event-values", "unsorted-offsets",
                                               "partial-shot", "shot-overflow")])
def test_public_referee_event_parser_reuses_contextual_shape_range_and_order_rejection(value):
    from sim.arena.manifest import parse_referee_events
    with pytest.raises(ManifestError, match=r"scenario\.script"):
        parse_referee_events(value, "scenario.script")


@pytest.mark.parametrize("location,replacement", [
    (("referees",), None),
    (("referees",), []),
    (("referees",), {"unknown": {}}),
    (("referees", "east"), []),
    (("referees", "east", "period_ns"), 10),
    (("referees", "east", "uart"), True),
    (("referees", "east", "uart"), "main\0.uart7"),
    (("referees", "east", "initial", "buff"), DELETE),
    (("referees", "east", "initial", "game", "sync_timestamp"), DELETE),
    (("referees", "east", "initial", "robot", "automatic_revive"), False),
    (("referees", "east", "initial", "power_heat", "heat_17mm"), True),
    (("referees", "east", "initial", "buff", "remaining_energy"), 256),
    (("referees", "east", "initial", "game", "sync_timestamp"), 1 << 64),
    (("referees", "east", "initial", "robot", "current_hp"), 201),
    (("referees", "east", "initial", "game", "stage_remain_time"), 0),
    (("referees", "east", "durations", "countdown_s"), 0),
    (("referees", "east", "durations", "battle_s"), True),
    (("referees", "east", "cooling_42mm_per_s"), -1),
    *((("referees", "east", "events"), value) for value, _ in EVENT_REJECTIONS),
], ids=[
        "null-table", "non-mapping-table", "unbound-alias", "non-mapping-entry", "unknown-policy",
        "bool-uart", "nul-uart", "missing-initial-group", "missing-explicit-timestamp",
        "unknown-message-field", "bool-wire-field", "uint8-overflow", "timestamp-overflow",
        "hp-policy", "active-phase-zero", "zero-duration", "bool-duration", "negative-cooling",
        *(ident for _, ident in EVENT_REJECTIONS)])
def test_referee_outer_shape_and_domain_reject_before_any_child_read(
        tmp_path, monkeypatch, location, replacement):
    path = write_arena(tmp_path, referees={"west": referee_configuration(),
                                            "east": referee_configuration()})
    document = yaml.safe_load(path.read_text())
    item = document
    for key in location[:-1]:
        item = item[key]
    if replacement is DELETE:
        del item[location[-1]]
    else:
        item[location[-1]] = replacement
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    forbid_child_reads(tmp_path, monkeypatch)
    with pytest.raises(ManifestError):
        load_manifest(path)


@pytest.mark.parametrize("count,accepted", [(1024, True), (1025, False)])
def test_referee_script_capacity_and_maximum_offset(tmp_path, monkeypatch, count, accepted):
    from sim.arena.manifest import parse_referee_events
    configuration = referee_configuration()
    configuration["events"] = [{"offset_ns": 0, "hit": {"damage_hp": 0}}
                               for _ in range(count - 1)]
    configuration["events"].append({"offset_ns": 0xffffffffffffffff, "hit": {"damage_hp": 1}})
    path = write_arena(tmp_path, referees={"east": configuration})
    if accepted:
        events = parse_referee_events(configuration["events"])
        assert len(events) == 1024 and events[-1].offset_ns == 0xffffffffffffffff
        record, = load_manifest(path).referees
        assert len(record.events) == 1024 and record.events[-1].offset_ns == 0xffffffffffffffff
    else:
        with pytest.raises(ManifestError, match="referee events.*1024"):
            parse_referee_events(configuration["events"])
        forbid_child_reads(tmp_path, monkeypatch)
        with pytest.raises(ManifestError, match="1024"):
            load_manifest(path)


@pytest.mark.parametrize("uart", ["main.uart7", "bravo_cpu.uart7", "alpha_cpu.uart1"])
def test_referee_uart_association_uses_its_loaded_child_board(tmp_path, uart):
    documents = tuple(ROBOT_DOCUMENT.replace("main", name) for name in ("alpha_cpu", "bravo_cpu"))
    path = write_arena(tmp_path, robot_documents=documents,
                       referees={"east": referee_configuration(uart=uart)})
    with pytest.raises(ManifestError, match="uart7"):
        load_manifest(path)
    path = write_arena(tmp_path, robot_documents=documents,
                       referees={"west": referee_configuration(uart="bravo_cpu.uart7"),
                                 "east": referee_configuration(uart="alpha_cpu.uart7")})
    assert [(record.member, record.uart) for record in load_manifest(path).referees] == [
        ("east", "alpha_cpu.uart7"), ("west", "bravo_cpu.uart7")]


@pytest.mark.parametrize("public_events", [False, True], ids=["arena", "public_parser"])
def test_admitted_referees_feed_real_coordinator_with_literal_wire_and_reset_origins(tmp_path, public_events):
    from sim.arena.manifest import parse_referee_events
    from sim.core import Coordinator, Session, StepResult
    from sim.devices.referee import RefereeEngine, encode_message

    admitted = load_manifest(write_arena(tmp_path, referees={"west": referee_configuration(),
                                                              "east": referee_configuration(scripted=True)}))
    scripts = {record.member: (parse_referee_events(
        referee_configuration(scripted=record.member == "east").get("events", []))
        if public_events else record.events) for record in admitted.referees}
    engines = {record.member: RefereeEngine(record.initial, durations=record.durations,
                                           events=scripts[record.member],
                                           cooling_42mm_per_s=record.cooling_42mm_per_s)
               for record in admitted.referees}
    coordinator = Coordinator(engines)
    # Official 2026V1.1.0 layouts; fixed CRC bytes calculated independently with
    # pinned crccheck, as in the existing actual RefereeTest direct consumer.
    literals = tuple(bytes.fromhex(value) for value in (
        "a50b000002010021010008070605040302017d07"
        "a50d00018d010203026400c80003006400500005ef8a"
        "a50e00028b0202000000000000000028000a001400c1da"
        "a5080003040402050900070b0d00feec0b",
        "a50b00046301003103000807060504030201f59e"
        "a50d0005ec010203024600c800030064007800060a27"
        "a50e0006ea0202000000000000000028000c001900c5f8"
        "a508000765040208110013171d0080d577",
    ))

    def batch(state, sequence):
        return b"".join(encode_message(value, sequence + index) for index, value in enumerate(
            (state.game, state.robot, state.power_heat, state.buff)))

    try:
        for session_id, origin in ((7, 12_345_678), (8, 50_000_000_000)):
            coordinator.reset(Session(session_id, origin))
            assert batch(engines["east"].get_state(), 0) == literals[0]
            assert engines["east"].get_state().robot.current_hp == 100  # hit precedes replacement
            assert coordinator.step(1_000_000_000) == {
                "east": StepResult("ok", origin + 1_000_000_000),
                "west": StepResult("ok", origin + 1_000_000_000)}
            assert batch(engines["east"].get_state(), 4) == literals[1]
            west = engines["west"].get_state()
            assert (west.robot.current_hp, west.power_heat.heat_17mm, west.power_heat.heat_42mm) == (
                100, 7, 20)
            assert coordinator.t_sim_ns == engines["east"].t_sim_ns == engines["west"].t_sim_ns == (
                origin + 1_000_000_000)
    finally:
        coordinator.close()
    assert coordinator.closed and all(engine.closed for engine in engines.values())


def test_scenario_admission_is_pure_relative_scoped_canonical_and_frozen(tmp_path, monkeypatch):
    from sim.core import Coordinator
    from sim.devices.referee import RefereeEngine, RefereeUartEndpoint

    arena_path = write_arena(tmp_path, referees={"west": referee_configuration(),
                                                "east": referee_configuration()})
    directory = tmp_path / "scenarios"
    directory.mkdir()
    path = directory / "trial.yaml"
    raw_events = referee_configuration(scripted=True)["events"]
    times = ("0s", "0ns", "0us", "500ms", "500ms", "1s", "1000ms", "1000000us")
    events = [{"at": at, **{key: value for key, value in row.items() if key != "offset_ns"}}
              for at, row in zip(times, raw_events)]
    document = {"schema": 1, "arena": "../arena.yaml", "duration": "2s",
                "referee_events": {"west": [], "east": events}}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    def forbidden(*args, **kwargs):
        pytest.fail("Scenario admission constructed or executed a runtime owner")
    for owner in (RefereeEngine, RefereeUartEndpoint, Coordinator):
        monkeypatch.setattr(owner, "__init__", forbidden)
    for method in ("reset", "step", "queue_events", "get_state", "close"):
        monkeypatch.setattr(RefereeEngine, method, forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    original_import = builtins.__import__
    def pure_import(name, globals=None, locals=None, fromlist=(), level=0):
        resolved = (importlib.util.resolve_name("." * level + name, globals["__package__"])
                    if level else name)
        candidates = (resolved, *(resolved + "." + item for item in (fromlist or ()) if item != "*"))
        for candidate in candidates:
            if any(candidate == prefix or candidate.startswith(prefix + ".") for prefix in (
                    "mujoco", "numpy", "sim.physics", "sim.arena.runtime", "sim.robots.runtime",
                    "sim.robots.shared_world", "sim.lab.runtime", "sim.boards", "tools")):
                pytest.fail(f"Scenario admission imported a runtime dependency: {candidate}")
        return original_import(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, "__import__", pure_import)
    from sim.scenario import ScenarioManifest, ScenarioReferee, load_manifest as load_scenario

    result = load_scenario(path)
    assert isinstance(result, ScenarioManifest)
    assert (result.path, result.duration_ns, result.arena.path) == (path.resolve(), 2_000_000_000, arena_path.resolve())
    assert result.arena == load_manifest(arena_path)
    assert type(result.referees) is tuple
    assert all(isinstance(item, ScenarioReferee) for item in result.referees)
    east, west = result.referees
    assert [item.member for item in result.referees] == ["east", "west"] and west.events == ()
    assert type(east.events) is tuple
    assert [event.offset_ns for event in east.events] == [0, 0, 0, 500_000_000, 500_000_000,
                                                          1_000_000_000, 1_000_000_000, 1_000_000_000]
    assert [type(event.value).__name__ for event in east.events] == [
        "Hit", "RobotStatus", "PowerHeat", "Shot", "Hit", "GameStatus", "RobotStatus", "Buff"]
    assert east.events[0].value.damage_hp == 20 and east.events[3].value.heat_42mm == 7
    for record in (result, *result.referees, *east.events, *(event.value for event in east.events)):
        attribute = fields(record)[0].name
        with pytest.raises(FrozenInstanceError):
            setattr(record, attribute, getattr(record, attribute))
    document["referee_events"] = dict(reversed(tuple(document["referee_events"].items())))
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    assert load_scenario(path) == result


@pytest.mark.parametrize("referees", [None, {}, {"east": []}])
def test_scenario_absent_events_and_valid_empty_target_have_explicit_defaults(tmp_path, referees):
    from sim.scenario import load_manifest as load_scenario
    write_arena(tmp_path, referees={"east": referee_configuration()})
    document = {"schema": 1, "arena": "arena.yaml", "duration": "1ns"}
    if referees is not None:
        document["referee_events"] = referees
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    result = load_scenario(path)
    assert result.duration_ns == 1
    assert result.can_delays == ()
    assert result.assertions == ()
    assert [(item.member, item.events) for item in result.referees] == ([] if not referees else [("east", ())])
    document["events"] = []
    document["assertions"] = []
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    assert load_scenario(path) == result


def test_scenario_can_delay_admission_is_pure_uses_actual_bus_and_frozen_fifo(tmp_path, monkeypatch):
    from sim.core import Coordinator
    from sim.fabric import CanBus
    from sim.scenario import ScenarioCanDelay, ScenarioManifest

    documents = tuple(ROBOT_DOCUMENT.replace("chassis_can", label)
                      for label in ("drive_left", "drive-right"))
    arena_path = write_arena(tmp_path, robot_documents=documents)
    directory = tmp_path / "scenarios"
    directory.mkdir()
    path = directory / "can.yaml"
    actions = [
        {"at": "0ns", "action": "can_delay", "target": "west.drive-right", "value": "0us"},
        {"at": "0us", "action": "can_delay", "target": "east.drive_left", "value": "1ns"},
        {"at": "500us", "action": "can_delay", "target": "east.drive_left", "value": "2ms"},
        {"at": "500000ns", "action": "can_delay", "target": "west.drive-right", "value": "3us"},
        {"at": "2ms", "action": "can_delay", "target": "east.drive_left", "value": "3600s"},
    ]
    path.write_text(yaml.safe_dump({"schema": 1, "arena": "../arena.yaml", "duration": "2ms",
                                    "events": actions}, sort_keys=False), encoding="utf-8")
    def forbidden(*args, **kwargs):
        pytest.fail("pure CAN-delay admission constructed or executed a runtime owner")
    for owner in (CanBus, Coordinator):
        monkeypatch.setattr(owner, "__init__", forbidden)
    for method in ("send", "reset", "step"):
        monkeypatch.setattr(CanBus, method, forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    original_import = builtins.__import__
    def pure_import(name, globals=None, locals=None, fromlist=(), level=0):
        resolved = (importlib.util.resolve_name("." * level + name, globals["__package__"])
                    if level else name)
        candidates = (resolved, *(resolved + "." + item for item in (fromlist or ()) if item != "*"))
        for candidate in candidates:
            if any(candidate == prefix or candidate.startswith(prefix + ".") for prefix in (
                    "mujoco", "numpy", "sim.physics", "sim.devices", "sim.arena.runtime", "sim.robots.runtime",
                    "sim.robots.shared_world", "sim.lab.runtime", "sim.boards", "tools")):
                pytest.fail(f"CAN-delay admission imported a runtime dependency: {candidate}")
        return original_import(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, "__import__", pure_import)
    spec = importlib.util.spec_from_file_location("scenario_manifest_can_purity", ROOT / "sim/scenario/manifest.py")
    scenario = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, scenario)
    spec.loader.exec_module(scenario)
    original_open, reads, calls = Path.open, [], []
    def open_file(value, *args, **kwargs):
        if value.resolve() == arena_path.resolve() or value.name == "robot.yaml":
            reads.append(value.resolve())
        return original_open(value, *args, **kwargs)
    monkeypatch.setattr(Path, "open", open_file)
    validate = scenario.validate_can_delays
    def admission(value, *, duration_ns):
        calls.append((value, duration_ns, tuple(reads)))
        return validate(value, duration_ns=duration_ns)
    monkeypatch.setattr(scenario, "validate_can_delays", admission)
    result = scenario.load_manifest(path)
    expected = ((0, "west", 0), (0, "east", 1), (500_000, "east", 2_000_000),
                (500_000, "west", 3000), (2_000_000, "east", 3_600_000_000_000))
    assert calls == [(expected, 2_000_000, ())]
    assert reads and result.referees == () and result.arena.referees == ()
    assert (result.path, result.arena.path, result.duration_ns) == (path.resolve(), arena_path.resolve(), 2_000_000)
    assert type(result.can_delays) is tuple
    assert result.can_delays == tuple(scenario.ScenarioCanDelay(*row) for row in expected)
    for record in (result, *result.can_delays):
        with pytest.raises(FrozenInstanceError):
            setattr(record, fields(record)[0].name, None)
    # Public exported records and the old four-position constructor remain valid.
    assert ScenarioCanDelay(0, "west", 0).member == "west"
    assert ScenarioManifest(path, result.arena, 1, ()).can_delays == ()


@pytest.mark.parametrize("invalid", ["outer_mapping", "entry_scalar", "missing", "extra", "action_type",
                                    "unsupported", "target_type", "target_unscoped", "target_three_parts",
                                    "target_non_ascii", "target_nul", "at_type", "at_fraction",
                                    "value_type", "value_negative", "value_limit", "unordered", "horizon", "quota"])
def test_scenario_can_delay_complete_outer_admission_precedes_referenced_reads(tmp_path, monkeypatch, invalid):
    from sim.scenario import load_manifest as load_scenario

    write_arena(tmp_path)
    first = {"at": "0ns", "action": "can_delay", "target": "east.chassis_can", "value": "0ns"}
    last = {"at": "1ms", "action": "can_delay", "target": "west.chassis_can", "value": "7ns"}
    actions = [first, last]
    if invalid == "outer_mapping":
        actions = {"east": last}
    elif invalid == "entry_scalar":
        actions[1] = 7
    elif invalid == "missing":
        del last["value"]
    elif invalid == "extra":
        last["delay_ns"] = 7
    elif invalid in ("action_type", "unsupported"):
        last["action"] = True if invalid == "action_type" else "device_offline"
    elif invalid.startswith("target_"):
        last["target"] = {"target_type": False, "target_unscoped": "chassis_can",
                          "target_three_parts": "east.main.fdcan1", "target_non_ascii": "east.底盘",
                          "target_nul": "west.chassis_can\0"}[invalid]
    elif invalid.startswith("at_"):
        last["at"] = 1 if invalid == "at_type" else "0.5ms"
    elif invalid.startswith("value_"):
        last["value"] = {"value_type": True, "value_negative": "-1ns", "value_limit": "3601s"}[invalid]
    elif invalid == "unordered":
        actions = [last, first]
    elif invalid == "horizon":
        last["at"] = "1000001ns"
    else:
        actions = [dict(first) for _ in range(1025)]
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump({"schema": 1, "arena": "arena.yaml", "duration": "1ms",
                                    "events": actions}, sort_keys=False), encoding="utf-8")
    forbid_child_reads(tmp_path, monkeypatch, include_arena=True)
    with pytest.raises(ManifestError, match="events"):
        load_scenario(path)


@pytest.mark.parametrize("target", ["missing.chassis_can", "east.chassis_can", "east.main", "west.drive_left"])
def test_scenario_can_delay_concrete_target_is_exact_loaded_member_bus(tmp_path, target):
    from sim.scenario import load_manifest as load_scenario

    write_arena(tmp_path, robot_documents=tuple(ROBOT_DOCUMENT.replace("chassis_can", name)
                                               for name in ("drive_left", "drive-right")))
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump({"schema": 1, "arena": "arena.yaml", "duration": "1ms", "events": [
        {"at": "0ns", "action": "can_delay", "target": target, "value": "0ns"}]}), encoding="utf-8")
    with pytest.raises(ManifestError) as caught:
        load_scenario(path)
    assert "events[0].target" in str(caught.value) and target in str(caught.value)


def test_scenario_can_delay_quota_is_independent_of_referee_quota(tmp_path):
    from sim.scenario import load_manifest as load_scenario

    write_arena(tmp_path, referees={"west": referee_configuration(), "east": referee_configuration()})
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump({"schema": 1, "arena": "arena.yaml", "duration": "1ms",
        "referee_events": {"west": [{"at": "1ms", "hit": {"damage_hp": 0}} for _ in range(512)],
                           "east": [{"at": "1ms", "hit": {"damage_hp": 0}} for _ in range(512)]},
        "events": [{"at": "1ms", "action": "can_delay", "target": "west.chassis_can", "value": "0ns"}
                   for _ in range(1024)]}, sort_keys=False), encoding="utf-8")
    result = load_scenario(path)
    assert [(item.member, len(item.events)) for item in result.referees] == [("east", 512), ("west", 512)]
    assert len(result.can_delays) == 1024
    assert all((item.offset_ns, item.member, item.delay_ns) == (1_000_000, "west", 0)
               for item in result.can_delays)


def test_scenario_motor_feedback_mixed_projection_is_pure_actual_named_and_frozen(tmp_path, monkeypatch):
    from sim.scenario import ScenarioCanDelay, ScenarioManifest, ScenarioMotorFeedback

    documents = tuple(ROBOT_DOCUMENT.replace("chassis_can", bus).replace("  fl:", "  front-left:")
                      .replace("  rr:", "  rear_right:") for bus in ("drive_left", "drive-right"))
    arena_path = write_arena(tmp_path, robot_documents=documents)
    path = tmp_path / "feedback.yaml"
    path.write_text(yaml.safe_dump({"schema": 1, "arena": "arena.yaml", "duration": "2ms", "events": [
        {"at": "0ns", "action": "motor_feedback", "target": "west.rear_right", "value": False},
        {"at": "0us", "action": "can_delay", "target": "east.drive_left", "value": "17ns"},
        {"at": "500us", "action": "motor_feedback", "target": "east.front-left", "value": False},
        {"at": "500000ns", "action": "motor_feedback", "target": "east.front-left", "value": True},
        {"at": "500us", "action": "can_delay", "target": "west.drive-right", "value": "0ns"},
        {"at": "2ms", "action": "motor_feedback", "target": "west.rear_right", "value": True},
    ]}, sort_keys=False), encoding="utf-8")

    def forbidden(*args, **kwargs):
        pytest.fail("pure motor feedback producer acquired a runtime resource")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    original_import = builtins.__import__
    def pure_import(name, globals=None, locals=None, fromlist=(), level=0):
        resolved = (importlib.util.resolve_name("." * level + name, globals["__package__"])
                    if level else name)
        candidates = (resolved, *(resolved + "." + item for item in (fromlist or ()) if item != "*"))
        for candidate in candidates:
            if any(candidate == prefix or candidate.startswith(prefix + ".") for prefix in (
                    "mujoco", "numpy", "sim.physics", "sim.devices", "sim.arena.runtime", "sim.robots.runtime",
                    "sim.robots.shared_world", "sim.lab.runtime", "sim.boards", "tools")):
                pytest.fail(f"motor feedback producer imported a runtime dependency: {candidate}")
        return original_import(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, "__import__", pure_import)
    spec = importlib.util.spec_from_file_location("scenario_manifest_feedback_purity", ROOT / "sim/scenario/manifest.py")
    scenario = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, scenario)
    spec.loader.exec_module(scenario)
    result = scenario.load_manifest(path)
    assert (result.path, result.arena.path, result.duration_ns) == (path.resolve(), arena_path.resolve(), 2_000_000)
    assert result.can_delays == (scenario.ScenarioCanDelay(0, "east", 17),
                                 scenario.ScenarioCanDelay(500_000, "west", 0))
    assert result.motor_feedback == (scenario.ScenarioMotorFeedback(0, "west", 4, False),
        scenario.ScenarioMotorFeedback(500_000, "east", 1, False),
        scenario.ScenarioMotorFeedback(500_000, "east", 1, True),
        scenario.ScenarioMotorFeedback(2_000_000, "west", 4, True))
    assert type(result.motor_feedback) is tuple and result.referees == ()
    for record in (result, *result.motor_feedback):
        with pytest.raises(FrozenInstanceError):
            setattr(record, fields(record)[0].name, None)
    assert ScenarioMotorFeedback(0, "east", 1, False).enabled is False
    assert ScenarioManifest(path, result.arena, 1, ()).motor_feedback == ()
    assert ScenarioManifest(path, result.arena, 1, (), (ScenarioCanDelay(0, "east", 0),)).motor_feedback == ()
    path.write_text(yaml.safe_dump({"schema": 1, "arena": "arena.yaml", "duration": "1ns"}), encoding="utf-8")
    assert scenario.load_manifest(path).motor_feedback == ()
    document = yaml.safe_load(path.read_text())
    document["events"] = []
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    assert scenario.load_manifest(path).motor_feedback == ()


@pytest.mark.parametrize("invalid", ["value_int", "value_string", "value_null", "offline", "at_bool",
    "at_fraction", "at_horizon", "target_type", "target_unscoped", "target_three_parts", "target_nul",
    "shape", "extra", "missing", "mixed_order", "feedback_quota", "can_quota", "aggregate_quota"])
def test_scenario_motor_feedback_complete_outer_rejection_precedes_child_reads(tmp_path, monkeypatch, invalid):
    from sim.scenario import load_manifest as load_scenario

    write_arena(tmp_path)
    first = {"at": "0ns", "action": "can_delay", "target": "east.chassis_can", "value": "0ns"}
    last = {"at": "1ms", "action": "motor_feedback", "target": "west.fl", "value": False}
    actions = [first, last]
    if invalid.startswith("value_"):
        last["value"] = {"value_int": 0, "value_string": "false", "value_null": None}[invalid]
    elif invalid == "offline":
        last["action"] = "device_offline"
    elif invalid.startswith("at_"):
        last["at"] = {"at_bool": True, "at_fraction": "0.5ms", "at_horizon": "1000001ns"}[invalid]
    elif invalid.startswith("target_"):
        last["target"] = {"target_type": 1, "target_unscoped": "fl",
                          "target_three_parts": "west.main.fl", "target_nul": "west.fl\0"}[invalid]
    elif invalid == "shape":
        actions[1] = False
    elif invalid == "extra":
        last["enabled"] = False
    elif invalid == "missing":
        del last["value"]
    elif invalid == "mixed_order":
        first["at"], last["at"] = "1ms", "0ns"
    elif invalid == "feedback_quota":
        actions = [dict(last) for _ in range(1025)]
    elif invalid == "can_quota":
        actions = [dict(first) for _ in range(1025)] + [last]
    else:
        actions = [dict(first) for _ in range(1024)] + [dict(last) for _ in range(1025)]
    path = tmp_path / "feedback.yaml"
    path.write_text(yaml.safe_dump({"schema": 1, "arena": "arena.yaml", "duration": "1ms", "events": actions},
                                   sort_keys=False), encoding="utf-8")
    forbid_child_reads(tmp_path, monkeypatch, include_arena=True)
    with pytest.raises(ManifestError, match="events"):
        load_scenario(path)


@pytest.mark.parametrize("action,target", [("motor_feedback", "missing.front-left"),
    ("motor_feedback", "east.fl"), ("motor_feedback", "west.drive-right"),
    ("can_delay", "west.drive_left")])
def test_scenario_motor_feedback_target_rejection_preserves_original_mixed_index(tmp_path, action, target):
    from sim.scenario import load_manifest as load_scenario

    documents = tuple(ROBOT_DOCUMENT.replace("chassis_can", bus).replace("  fl:", "  front-left:")
                      for bus in ("drive_left", "drive-right"))
    write_arena(tmp_path, robot_documents=documents)
    path = tmp_path / "feedback.yaml"
    path.write_text(yaml.safe_dump({"schema": 1, "arena": "arena.yaml", "duration": "1ms", "events": [
        {"at": "0ns", "action": "motor_feedback", "target": "east.front-left", "value": True},
        {"at": "0ns", "action": "can_delay", "target": "west.drive-right", "value": "0ns"},
        {"at": "1ms", "action": action, "target": target,
         "value": False if action == "motor_feedback" else "0ns"}]}, sort_keys=False), encoding="utf-8")
    with pytest.raises(ManifestError) as caught:
        load_scenario(path)
    assert "events[2].target" in str(caught.value) and target in str(caught.value)


def test_scenario_motor_feedback_and_can_quotas_are_independent_at_inclusive_horizon(tmp_path, monkeypatch):
    from sim.arena.manifest import MAX_CAN_DELAY_EVENTS, MAX_MOTOR_FEEDBACK_EVENTS
    from sim.scenario import load_manifest as load_scenario

    write_arena(tmp_path, referees={"east": referee_configuration()})
    actions = [row for index in range(1024) for row in (
        {"at": "1ms", "action": "motor_feedback", "target": "east.fl", "value": bool(index % 2)},
        {"at": "1ms", "action": "can_delay", "target": "west.chassis_can", "value": str(index) + "ns"})]
    assertions = [{"member": "west" if index % 2 else "east", "before": "1ms",
                   "digital_equals": {"endpoint": "gpio.H15", "value": index % 2}}
                  for index in range(1024)]
    path = tmp_path / "feedback.yaml"
    document = {"schema": 1, "arena": "arena.yaml", "duration": "1ms", "events": actions,
        "assertions": assertions, "referee_events": {"east": [{"at": "1ms", "hit": {"damage_hp": 0}}]}}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    result = load_scenario(path)
    assert MAX_CAN_DELAY_EVENTS == MAX_MOTOR_FEEDBACK_EVENTS == 1024
    assert len(result.motor_feedback) == len(result.can_delays) == 1024
    assert [(row.offset_ns, row.member, row.motor_id, row.enabled) for row in result.motor_feedback] == [
        (1_000_000, "east", 1, bool(index % 2)) for index in range(1024)]
    assert [(row.offset_ns, row.member, row.delay_ns) for row in result.can_delays] == [
        (1_000_000, "west", index) for index in range(1024)]
    assert len(result.referees) == len(result.referees[0].events) == 1
    assert [(entry.member, entry.assertion.before_ns, entry.assertion.endpoint, entry.assertion.value)
            for entry in result.assertions] == [
        ("west" if index % 2 else "east", 1_000_000, "gpio.H15", index % 2)
        for index in range(1024)]
    assertions.append({"member": "west", "before": "1ms",
                       "digital_equals": {"endpoint": "gpio.H15", "value": 1}})
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    forbid_child_reads(tmp_path, monkeypatch, include_arena=True)
    with pytest.raises(ManifestError, match="assertions.*1024"):
        load_scenario(path)


def test_public_can_delay_validator_is_pure_neutral_and_accepts_unsigned_horizon(monkeypatch):
    from sim.arena.manifest import MAX_CAN_DELAY_EVENTS, validate_can_delays
    from sim.fabric import CanBus

    def forbidden(*args, **kwargs):
        pytest.fail("pure value validator acquired a bus or runtime resource")
    monkeypatch.setattr(CanBus, "__init__", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    maximum = (1 << 64) - 1
    assert MAX_CAN_DELAY_EVENTS == 1024
    assert validate_can_delays((), duration_ns=0) is None
    assert validate_can_delays(((0, "undeclared", maximum),), duration_ns=0) is None
    assert validate_can_delays(((maximum, "undeclared", 0),), duration_ns=maximum) is None


@pytest.mark.parametrize("actions,horizon", [
    ([(0, "east", 0)], 0), (((0, "east"),), 0),
    (((0, type("Alias", (str,), {})("east"), 0),), 0),
    (((1, "east", 0), (0, "west", 0)), 1), (((1, "east", 0),), 0),
    (((0, "east", 1 << 64),), 0), ((), True), (((0, "east", 0),) * 1025, 0),
])
def test_public_can_delay_validator_refuses_invalid_neutral_input(actions, horizon):
    from sim.arena.manifest import validate_can_delays

    with pytest.raises((TypeError, ValueError)):
        validate_can_delays(actions, duration_ns=horizon)


@pytest.mark.parametrize("location,replacement", [
    (("schema",), True),
    (("arena",), DELETE),
    (("arena",), "arena\0.yaml"),
    (("duration",), "0s"),
    (("duration",), 1),
    (("duration",), "3601s"),
    (("actions",), []),
    (("assertions",), {}),
    (("step",), "1ms"),
    (("referee_events",), []),
    (("referee_events",), {"bad\0name": []}),
    (("referee_events", "east"), [{"hit": {"damage_hp": 1}}]),
    (("referee_events", "east"), [{"at": "1s", "offset_ns": 0, "hit": {"damage_hp": 1}}]),
    (("referee_events", "east"), [{"at": "2s", "hit": {"damage_hp": 1}}]),
    (("referee_events", "east"), [{"at": "1s", "hit": {"damage_hp": 0}},
                                     {"at": "0s", "hit": {"damage_hp": 0}}]),
    (("referee_events", "east"), [{"at": "0s", "hit": {"damage_hp": True}}]),
], ids=["schema-type", "required-arena", "nul-path", "zero-duration", "duration-type", "duration-limit",
        "unsupported-actions", "assertions-mapping", "unsupported-step", "events-mapping", "nul-target",
        "required-at", "unknown-offset", "after-duration", "unordered-events", "wire-range"])
def test_scenario_complete_outer_admission_rejects_before_referenced_arena_read(
        tmp_path, monkeypatch, location, replacement):
    from sim.scenario import load_manifest as load_scenario
    write_arena(tmp_path, referees={"east": referee_configuration()})
    document = {"schema": 1, "arena": "arena.yaml", "duration": "1s", "referee_events": {"east": []}}
    item = document
    for key in location[:-1]:
        item = item[key]
    if replacement is DELETE:
        del item[location[-1]]
    else:
        item[location[-1]] = replacement
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    forbid_child_reads(tmp_path, monkeypatch, include_arena=True)
    with pytest.raises(ManifestError):
        load_scenario(path)


@pytest.mark.parametrize("change", ["duplicate", "anchor"])
def test_scenario_uses_existing_bounded_yaml_loader_before_arena_read(tmp_path, monkeypatch, change):
    from sim.scenario import load_manifest as load_scenario
    write_arena(tmp_path)
    text = "schema: 1\narena: arena.yaml\nduration: 1s\n"
    text += "duration: 2s\n" if change == "duplicate" else "referee_events: {east: &events [], west: *events}\n"
    path = tmp_path / "scenario.yaml"
    path.write_text(text, encoding="utf-8")
    forbid_child_reads(tmp_path, monkeypatch, include_arena=True)
    with pytest.raises(ManifestError):
        load_scenario(path)


@pytest.mark.parametrize("count", [1024, 1025])
def test_scenario_event_capacity_is_aggregate_and_duration_endpoint_is_inclusive(tmp_path, monkeypatch, count):
    from sim.scenario import load_manifest as load_scenario
    write_arena(tmp_path, referees={"east": referee_configuration(), "west": referee_configuration()})
    document = {"schema": 1, "arena": "arena.yaml", "duration": "1s", "referee_events": {
        name: [{"at": "1s", "hit": {"damage_hp": 0}} for _ in range(size)]
        for name, size in (("west", count - 512), ("east", 512))}}
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    if count == 1024:
        result = load_scenario(path)
        assert [(item.member, len(item.events)) for item in result.referees] == [("east", 512), ("west", 512)]
        assert all(event.offset_ns == 1_000_000_000 for item in result.referees for event in item.events)
    else:
        forbid_child_reads(tmp_path, monkeypatch, include_arena=True)
        with pytest.raises(ManifestError, match="1024"):
            load_scenario(path)


@pytest.mark.parametrize("target", ["missing", "west"])
def test_scenario_targets_require_existing_referee_even_for_empty_input(tmp_path, target):
    from sim.scenario import load_manifest as load_scenario
    write_arena(tmp_path, referees={"east": referee_configuration()})
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump({"schema": 1, "arena": "arena.yaml", "duration": "1s",
                                    "referee_events": {target: []}}), encoding="utf-8")
    with pytest.raises(ManifestError, match=target):
        load_scenario(path)


def test_scenario_preserves_context_for_referenced_arena_rejection(tmp_path):
    from sim.scenario import load_manifest as load_scenario
    arena_path = write_arena(tmp_path)
    arena_path.write_text("schema: 0\n", encoding="utf-8")
    path = tmp_path / "scenario.yaml"
    path.write_text("schema: 1\narena: arena.yaml\nduration: 1s\n", encoding="utf-8")
    with pytest.raises(ManifestError) as caught:
        load_scenario(path)
    assert "'arena.yaml'" in str(caught.value) and isinstance(caught.value.__cause__, ManifestError)


def test_scenario_assertions_reuse_pure_lab_parser_and_original_frozen_records(tmp_path, monkeypatch):
    from sim.lab import Assertion, parse_assertion
    from sim.scenario import ScenarioAssertion, ScenarioManifest
    from sim.scenario.manifest import ScenarioAssertion as canonical

    assert ScenarioAssertion is canonical
    write_arena(tmp_path)
    raw = [
        {"member": "west", "before": "2ms", "digital_equals": {"endpoint": "gpio.H15", "value": 1}},
        {"member": "east", "before": "1ms", "uart_contains": {"endpoint": "uart1", "text": "é\0"}},
        {"member": "west", "before": "2ms", "digital_equals": {"endpoint": "gpio.H15", "value": 1}},
    ]
    path = tmp_path / "assertions.yaml"
    path.write_text(yaml.safe_dump({"schema": 1, "arena": "arena.yaml", "duration": "2ms",
                                    "assertions": raw}, sort_keys=False), encoding="utf-8")
    def forbidden(*args, **kwargs):
        pytest.fail("Scenario assertion admission acquired runtime resources")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    original_import = builtins.__import__
    def pure_import(name, globals=None, locals=None, fromlist=(), level=0):
        resolved = (importlib.util.resolve_name("." * level + name, globals["__package__"])
                    if level else name)
        candidates = (resolved, *(resolved + "." + item for item in (fromlist or ()) if item != "*"))
        for candidate in candidates:
            if any(candidate == prefix or candidate.startswith(prefix + ".") for prefix in (
                    "mujoco", "numpy", "sim.physics", "sim.devices", "sim.arena.runtime", "sim.robots.runtime",
                    "sim.robots.shared_world", "sim.lab.runtime", "sim.boards", "sim.transports", "tools")):
                pytest.fail(f"Scenario assertions imported a runtime dependency: {candidate}")
        return original_import(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, "__import__", pure_import)
    spec = importlib.util.spec_from_file_location("scenario_manifest_assertion_purity", ROOT / "sim/scenario/manifest.py")
    scenario = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, scenario)
    spec.loader.exec_module(scenario)
    assert scenario.parse_assertion is parse_assertion
    original_open, reads, calls = Path.open, [], []
    def open_file(value, *args, **kwargs):
        if value.name in ("arena.yaml", "robot.yaml"):
            reads.append(value.resolve())
        return original_open(value, *args, **kwargs)
    def parse(value, index):
        result = parse_assertion(value, index)
        calls.append((index, value, result, tuple(reads)))
        return result
    monkeypatch.setattr(Path, "open", open_file)
    monkeypatch.setattr(scenario, "parse_assertion", parse)
    result = scenario.load_manifest(path)
    assert type(result.assertions) is tuple and reads
    assert [(entry.member, entry.assertion) for entry in result.assertions] == [
        ("west", Assertion(2_000_000, "gpio.H15", "digital_equals", 1)),
        ("east", Assertion(1_000_000, "uart1", "uart_contains", b"\xc3\xa9\0")),
        ("west", Assertion(2_000_000, "gpio.H15", "digital_equals", 1))]
    assert [(index, value, reads) for index, value, _, reads in calls] == [
        (index, {key: value for key, value in raw[index].items() if key != "member"}, ())
        for index in range(3)]
    assert all(entry.assertion is calls[index][2] for index, entry in enumerate(result.assertions))
    for entry in result.assertions:
        for record, attribute in ((entry, "member"), (entry.assertion, "value")):
            with pytest.raises(FrozenInstanceError):
                setattr(record, attribute, None)
    raw[0]["digital_equals"]["value"] = 0
    assert result.assertions[0].assertion.value == 1
    for tail in ((), ((),), ((), ())):
        assert ScenarioManifest(path, result.arena, 1, (), *tail).assertions == ()


@pytest.mark.parametrize("suffix", [
    None,
    {"member": "east", "before": "1ms"},
    {"member": "east", "before": "0ns", "digital_equals": {"endpoint": "key", "value": 0}},
    {"member": "east", "before": "2ms", "digital_equals": {"endpoint": "key", "value": 0}},
    {"member": "east", "before": "1ms", "digital_equals": {"endpoint": "key", "value": True}},
    {"member": "east", "before": "1ms", "can_seen": {"endpoint": "can1", "id": 1}},
    {"member": "east\0", "before": "1ms", "digital_equals": {"endpoint": "key", "value": 0}},
    {"before": "1ms", "digital_equals": {"endpoint": "key", "value": 0}},
], ids=["shape", "kind", "zero-deadline", "after-duration", "bool-value", "unsupported-can",
        "member-nul", "member-required"])
def test_scenario_assertion_bad_suffix_rejects_before_any_referenced_read(tmp_path, monkeypatch, suffix):
    from sim.scenario import load_manifest as load_scenario

    write_arena(tmp_path)
    path = tmp_path / "assertions.yaml"
    prefix = [{"member": member, "before": "1ms", "digital_equals": {"endpoint": "key", "value": 0}}
              for member in ("west", "east")]
    path.write_text(yaml.safe_dump({"schema": 1, "arena": "arena.yaml", "duration": "1ms",
                                    "assertions": [*prefix, suffix]}), encoding="utf-8")
    forbid_child_reads(tmp_path, monkeypatch, include_arena=True)
    with pytest.raises(ManifestError, match=r"assertions\[2\]"):
        load_scenario(path)


@pytest.mark.parametrize("ready,referee,endpoint,member,expected,accepted", [
    (None, False, "uart1", "blue-team", ("uart1",), True),
    ("uart2", False, "uart2", "blue-team", ("uart2",), True),
    ("uart2", False, "uart1", "blue-team", ("uart2",), False),
    ("uart2", True, "uart7", "blue-team", ("uart2", "uart7"), True),
    ("uart7", True, "uart7", "blue-team", ("uart7",), True),
    (None, False, "uart7", "blue-team", ("uart1",), False),
    (None, False, "uart1", "east", ("uart1",), False),
], ids=["default", "ready", "ready-excludes-default", "referee-adds-uart7", "deduplicated",
        "unopened-uart7", "actual-member-name"])
def test_scenario_assertions_associate_named_members_with_shared_uart_profile(
        tmp_path, monkeypatch, ready, referee, endpoint, member, expected, accepted):
    from sim.arena.manifest import member_uarts
    from sim.scenario import manifest as scenario

    document = ROBOT_DOCUMENT
    if ready is not None:
        document += yaml.safe_dump({"ready": {"endpoint": "main." + ready, "text": "READY"}})
    members = {"green_team": {"manifest": "robots/bravo/robot.yaml", "prefix": "bravo/", "physics": "shared-world"},
               "blue-team": {"manifest": "robots/alpha/robot.yaml", "prefix": "alpha/", "physics": "shared-world"}}
    arena_path = write_arena(tmp_path, robot_documents=(document, ROBOT_DOCUMENT), members=members,
                            referees={"blue-team": referee_configuration()} if referee else None)
    arena = load_manifest(arena_path)
    blue = next(item for item in arena.robots if item.name == "blue-team")
    assert member_uarts(blue, referee=referee) == expected
    assert scenario.member_uarts is member_uarts
    calls = []
    def uarts(value, *, referee):
        calls.append((value.name, referee))
        return member_uarts(value, referee=referee)
    monkeypatch.setattr(scenario, "member_uarts", uarts)
    path = tmp_path / "assertions.yaml"
    path.write_text(yaml.safe_dump({"schema": 1, "arena": "arena.yaml", "duration": "1ms", "assertions": [
        {"member": "green_team", "before": "1ms", "digital_equals": {"endpoint": "gpio.H15", "value": 1}},
        {"member": member, "before": "1ms", "uart_contains": {"endpoint": endpoint, "text": "ready"}}]}),
        encoding="utf-8")
    if accepted:
        result = scenario.load_manifest(path)
        assert [(entry.member, entry.assertion.endpoint) for entry in result.assertions] == [
            ("green_team", "gpio.H15"), ("blue-team", endpoint)]
    else:
        with pytest.raises(ManifestError, match=r"assertions\[1\]"):
            scenario.load_manifest(path)
    assert calls == ([] if member == "east" else [("blue-team", referee)])
